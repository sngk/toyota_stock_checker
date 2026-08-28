from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PRADO_DB", ROOT / "prado_stock.db"))
DEALERS_PATH = Path(os.environ.get("PRADO_DEALERS", ROOT / "dealers.json"))
INTERVAL_SECONDS = int(os.environ.get("PRADO_INTERVAL_SECONDS", 60 * 60))
HTTP_TIMEOUT = int(os.environ.get("PRADO_HTTP_TIMEOUT", 30))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = "PradoStockWatcher/1.0 (personal stock availability checker)"

app = Flask(__name__)
scan_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          dealer_count INTEGER NOT NULL DEFAULT 0, vehicle_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS dealer_checks (
          scan_id INTEGER NOT NULL, dealer TEXT NOT NULL, url TEXT NOT NULL,
          ok INTEGER NOT NULL, vehicle_count INTEGER NOT NULL DEFAULT 0, error TEXT,
          PRIMARY KEY (scan_id, dealer)
        );
        CREATE TABLE IF NOT EXISTS app_meta (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vehicles (
          vin TEXT PRIMARY KEY, dealer TEXT NOT NULL, stock_id TEXT, title TEXT NOT NULL,
          grade TEXT NOT NULL, colour TEXT NOT NULL, price TEXT, image_url TEXT,
          detail_url TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          first_scan_id INTEGER NOT NULL, last_scan_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          newly_added INTEGER NOT NULL DEFAULT 1
        );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vehicles)")}
        if "newly_added" not in columns:
            conn.execute("ALTER TABLE vehicles ADD COLUMN newly_added INTEGER NOT NULL DEFAULT 0")


def load_dealers() -> list[dict]:
    data = json.loads(DEALERS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("dealers.json must contain a non-empty list")
    return data


def clean(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_stock(html: str, dealer: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for card in soup.select('li.tb-list-item[data-id]'):
        category = clean(card.get("data-item-category2"))
        if "prado" not in category.lower() and "prado" not in clean(card.get("data-vehicleline")).lower():
            continue
        heading = clean(card.select_one("h3").get_text(" ") if card.select_one("h3") else card.get("data-name"))
        vin_match = re.search(r"\bVIN:\s*([A-Z0-9]{12,20})", card.get_text(" ", strip=True), re.I)
        if not vin_match:
            continue
        colour_match = re.search(r"\(([^()]+)\)(?:\s+with\b.*)?$", heading)
        colour = clean(colour_match.group(1)) if colour_match else "Unknown"
        grade = clean(card.get("data-item-category3"))
        if not grade:
            model_match = re.search(r"Prado\s+(GX|GXL|VX|Altitude|Kakadu)\b", heading, re.I)
            grade = model_match.group(1) if model_match else "Unknown"
        price_node = card.select_one(".tb-list-item-price")
        price_match = re.search(r"\$[\d,]+", clean(price_node.get_text(" ") if price_node else ""))
        image = card.select_one("img[src]")
        link = card.select_one('a[href*="/inventory/"]')
        found.append({
            "vin": vin_match.group(1).upper(), "stock_id": card.get("data-id"),
            "title": heading, "grade": grade, "colour": colour,
            "price": price_match.group(0) if price_match else None,
            "image_url": urljoin(dealer["url"], image.get("src")) if image else None,
            "detail_url": urljoin(dealer["url"], link.get("href")) if link else dealer["url"],
        })
    return found


def discord_payload(vehicle: dict) -> dict:
    grade = vehicle["grade"].upper()
    wanted_colour = vehicle["colour"].lower() in {"onyx black", "onyx night", "dusty bronze"}
    priority = grade == "GX" and wanted_colour
    if priority:
        heading, embed_colour = "🚨 PRIORITY PRADO FOUND 🚨", 0xE50000
    elif grade == "GX":
        heading, embed_colour = "⭐ New GX Prado found", 0xFFCA00
    elif wanted_colour:
        heading, embed_colour = "🎨 New preferred-colour Prado found", 0x8A6138
    else:
        heading, embed_colour = "New Prado found", 0x2774AE
    fields = [
        {"name": "Dealer", "value": vehicle["dealer"], "inline": True},
        {"name": "Grade", "value": vehicle["grade"], "inline": True},
        {"name": "Colour", "value": vehicle["colour"], "inline": True},
        {"name": "Price", "value": vehicle.get("price") or "Ask dealer", "inline": True},
        {"name": "VIN", "value": vehicle["vin"], "inline": False},
    ]
    embed = {
        "title": heading, "description": vehicle["title"], "url": vehicle["detail_url"],
        "color": embed_colour, "fields": fields,
        "footer": {"text": "WA Prado Watch"},
    }
    if vehicle.get("image_url"):
        embed["thumbnail"] = {"url": vehicle["image_url"]}
    return {"username": "WA Prado Watch", "embeds": [embed], "allowed_mentions": {"parse": []}}


def notify_discord(vehicles: list[dict]) -> tuple[int, list[str]]:
    if not DISCORD_WEBHOOK_URL or not vehicles:
        return 0, []
    sent, errors = 0, []
    for vehicle in vehicles:
        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL, params={"wait": "true"}, json=discord_payload(vehicle),
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
            sent += 1
        except Exception as exc:
            errors.append(f'{vehicle["vin"]}: {exc}')
    return sent, errors


def scan_all() -> dict:
    if not scan_lock.acquire(blocking=False):
        return {"started": False, "message": "A scan is already running"}
    try:
        dealers = load_dealers()
        now = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            scan_id = conn.execute("INSERT INTO scans(started_at, dealer_count) VALUES (?, ?)", (now, len(dealers))).lastrowid
        seen = set()
        newly_found = []
        total = 0
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
        for dealer in dealers:
            try:
                response = requests.get(dealer["url"], headers=headers, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                vehicles = parse_stock(response.text, dealer)
                # A valid stock page always has this marker, including when there are zero results.
                if "Dealer Stock" not in response.text and "LandCruiser Prado" not in response.text:
                    raise ValueError("Page did not look like a Toyota stock page")
                with db() as conn:
                    conn.execute("UPDATE vehicles SET newly_added=0 WHERE dealer=?", (dealer["name"],))
                    for vehicle in vehicles:
                        seen.add(vehicle["vin"])
                        previous = conn.execute("SELECT vin, active FROM vehicles WHERE vin=?", (vehicle["vin"],)).fetchone()
                        values = (dealer["name"], vehicle["stock_id"], vehicle["title"], vehicle["grade"],
                                  vehicle["colour"], vehicle["price"], vehicle["image_url"], vehicle["detail_url"], now, scan_id, vehicle["vin"])
                        if previous:
                            if not previous["active"]:
                                newly_found.append({**vehicle, "dealer": dealer["name"]})
                            conn.execute("""UPDATE vehicles SET dealer=?,stock_id=?,title=?,grade=?,colour=?,price=?,image_url=?,detail_url=?,
                              last_seen=?,last_scan_id=?,active=1,newly_added=? WHERE vin=?""", values[:-1] + (0 if previous["active"] else 1, values[-1]))
                        else:
                            # On the first run this intentionally includes every currently stocked car.
                            newly_found.append({**vehicle, "dealer": dealer["name"]})
                            conn.execute("""INSERT INTO vehicles(dealer,stock_id,title,grade,colour,price,image_url,detail_url,
                              first_seen,last_seen,first_scan_id,last_scan_id,active,newly_added,vin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,1,?)""",
                              values[:-2] + (now, scan_id, scan_id, vehicle["vin"]))
                    conn.execute("INSERT INTO dealer_checks VALUES(?,?,?,?,?,NULL)", (scan_id, dealer["name"], dealer["url"], 1, len(vehicles)))
                total += len(vehicles)
            except Exception as exc:
                with db() as conn:
                    conn.execute("INSERT INTO dealer_checks VALUES(?,?,?,?,?,?)", (scan_id, dealer.get("name", "Unknown"), dealer.get("url", ""), 0, 0, str(exc)[:500]))
        # Only deactivate cars from dealers that completed successfully.
        with db() as conn:
            successful = [r[0] for r in conn.execute("SELECT dealer FROM dealer_checks WHERE scan_id=? AND ok=1", (scan_id,))]
            if successful:
                marks = ",".join("?" for _ in successful)
                conn.execute(f"UPDATE vehicles SET active=0 WHERE dealer IN ({marks}) AND last_scan_id < ?", (*successful, scan_id))
            conn.execute("UPDATE scans SET finished_at=?, vehicle_count=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), total, scan_id))
        notification_batch = newly_found
        discord_was_initialized = False
        if DISCORD_WEBHOOK_URL:
            with db() as conn:
                discord_was_initialized = conn.execute(
                    "SELECT value FROM app_meta WHERE key='discord_initialized'"
                ).fetchone() is not None
                if not discord_was_initialized:
                    notification_batch = [dict(row) for row in conn.execute(
                        "SELECT * FROM vehicles WHERE active=1 ORDER BY dealer, title"
                    )]
        notified, notification_errors = notify_discord(notification_batch)
        if DISCORD_WEBHOOK_URL and not notification_errors and not discord_was_initialized:
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta(key,value) VALUES('discord_initialized',?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
        for error in notification_errors:
            print(f"Discord notification failed: {error}", flush=True)
        return {"started": True, "scan_id": scan_id, "vehicle_count": total,
                "new_vehicle_count": len(newly_found), "notified_count": notified,
                "notification_errors": notification_errors}
    finally:
        scan_lock.release()


def scheduler() -> None:
    # Scan immediately on startup only if there has never been a completed scan.
    with db() as conn:
        last = conn.execute("SELECT finished_at FROM scans WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if not last:
        scan_all()
    while True:
        time.sleep(INTERVAL_SECONDS)
        scan_all()


@app.get("/")
def index():
    return render_template("index.html", interval_hours=INTERVAL_SECONDS / 3600)


@app.get("/api/status")
def status():
    with db() as conn:
        last = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        checks = conn.execute("SELECT * FROM dealer_checks WHERE scan_id=? ORDER BY dealer", (last["id"],)).fetchall() if last else []
    return jsonify({"scanning": scan_lock.locked(), "last_scan": dict(last) if last else None, "dealers": [dict(x) for x in checks]})


@app.get("/api/vehicles")
def vehicles():
    include_gone = request.args.get("include_gone") == "1"
    with db() as conn:
        rows = conn.execute("""SELECT *, newly_added AS is_new FROM vehicles
          WHERE active=1 OR ? ORDER BY CASE
            WHEN upper(grade)='GX' AND lower(colour) IN ('onyx black','onyx night','dusty bronze') THEN 0
            WHEN upper(grade)='GX' THEN 1
            WHEN lower(colour) IN ('onyx black','onyx night','dusty bronze') THEN 2
            ELSE 3 END,
          is_new DESC, dealer, title""", (include_gone,)).fetchall()
    return jsonify([dict(x) for x in rows])


@app.post("/api/scan")
def scan():
    thread = threading.Thread(target=scan_all, daemon=True)
    thread.start()
    return jsonify({"started": True}), 202


if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler, daemon=True, name="prado-scheduler").start()
    app.run(host=os.environ.get("PRADO_HOST", "127.0.0.1"), port=int(os.environ.get("PRADO_PORT", "8080")), debug=False)
