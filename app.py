from __future__ import annotations

import json
import getpass
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PRADO_DB", ROOT / "prado_stock.db"))
DEALERS_PATH = Path(os.environ.get("PRADO_DEALERS", ROOT / "dealers.json"))
NSW_DEALERS_PATH = Path(os.environ.get("PRADO_NSW_DEALERS", ROOT / "dealers_nsw.json"))
SA_DEALERS_PATH = Path(os.environ.get("PRADO_SA_DEALERS", ROOT / "dealers_sa.json"))
VIC_DEALERS_PATH = Path(os.environ.get("PRADO_VIC_DEALERS", ROOT / "dealers_vic.json"))
QLD_DEALERS_PATH = Path(os.environ.get("PRADO_QLD_DEALERS", ROOT / "dealers_qld.json"))
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("PRADO_INTERVAL_SECONDS", 60 * 60))
HTTP_TIMEOUT = int(os.environ.get("PRADO_HTTP_TIMEOUT", 30))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = "PradoStockWatcher/1.0 (personal stock availability checker)"

app = Flask(__name__)
scan_lock = threading.Lock()
scheduler_wakeup = threading.Event()
REGIONS = {
    "wa": DEALERS_PATH, "nsw": NSW_DEALERS_PATH, "sa": SA_DEALERS_PATH,
    "vic": VIC_DEALERS_PATH, "qld": QLD_DEALERS_PATH,
}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          dealer_count INTEGER NOT NULL DEFAULT 0, vehicle_count INTEGER NOT NULL DEFAULT 0,
          region TEXT NOT NULL DEFAULT 'wa'
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
          grade TEXT NOT NULL, colour TEXT NOT NULL, condition TEXT NOT NULL DEFAULT 'New', price TEXT, image_url TEXT,
          detail_url TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          first_scan_id INTEGER NOT NULL, last_scan_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          newly_added INTEGER NOT NULL DEFAULT 1, discord_message_id TEXT
        );
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vehicles)")}
        if "newly_added" not in columns:
            conn.execute("ALTER TABLE vehicles ADD COLUMN newly_added INTEGER NOT NULL DEFAULT 0")
        if "condition" not in columns:
            conn.execute("ALTER TABLE vehicles ADD COLUMN condition TEXT NOT NULL DEFAULT 'New'")
        scan_columns = {row[1] for row in conn.execute("PRAGMA table_info(scans)")}
        if "region" not in scan_columns:
            conn.execute("ALTER TABLE scans ADD COLUMN region TEXT NOT NULL DEFAULT 'wa'")
        # Older installs keyed vehicles by VIN alone. Rebuild once so the same VIN can
        # legitimately appear in both state views without overwriting either record.
        vehicle_pk = [row[1] for row in conn.execute("PRAGMA table_info(vehicles)") if row[5]]
        if vehicle_pk == ["vin"]:
            conn.executescript("""
            ALTER TABLE vehicles RENAME TO vehicles_wa_legacy;
            CREATE TABLE vehicles (
              vin TEXT NOT NULL, region TEXT NOT NULL DEFAULT 'wa', dealer TEXT NOT NULL,
              stock_id TEXT, title TEXT NOT NULL, grade TEXT NOT NULL, colour TEXT NOT NULL,
              condition TEXT NOT NULL DEFAULT 'New', price TEXT, image_url TEXT, detail_url TEXT,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, first_scan_id INTEGER NOT NULL,
              last_scan_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              newly_added INTEGER NOT NULL DEFAULT 1, discord_message_id TEXT,
              PRIMARY KEY (region, vin)
            );
            INSERT INTO vehicles(vin,region,dealer,stock_id,title,grade,colour,condition,price,image_url,
              detail_url,first_seen,last_seen,first_scan_id,last_scan_id,active,newly_added)
              SELECT vin,'wa',dealer,stock_id,title,grade,colour,condition,price,image_url,detail_url,
              first_seen,last_seen,first_scan_id,last_scan_id,active,newly_added FROM vehicles_wa_legacy;
            DROP TABLE vehicles_wa_legacy;
            """)
        vehicle_columns = {row[1] for row in conn.execute("PRAGMA table_info(vehicles)")}
        if "discord_message_id" not in vehicle_columns:
            conn.execute("ALTER TABLE vehicles ADD COLUMN discord_message_id TEXT")


def load_dealers(region: str = "wa") -> list[dict]:
    if region not in REGIONS:
        raise ValueError("Unknown region")
    data = json.loads(REGIONS[region].read_text(encoding="utf-8"))
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
        link = card.select_one('a[href*="/inventory/"], a[href*="/demo/"]')
        stock_list = card.find_parent("ul", attrs={"data-list-type": True})
        list_type = clean(stock_list.get("data-list-type") if stock_list else "").lower()
        category4 = clean(card.get("data-item-category4")).lower()
        condition = "Demo" if "demo" in list_type or "demo" in category4 else "New"
        found.append({
            "vin": vin_match.group(1).upper(), "stock_id": card.get("data-id"),
            "title": heading, "grade": grade, "colour": colour, "condition": condition,
            "price": price_match.group(0) if price_match else None,
            "image_url": urljoin(dealer["url"], image.get("src")) if image else None,
            "detail_url": urljoin(dealer["url"], link.get("href")) if link else dealer["url"],
        })
    return found


def discord_payload(vehicle: dict) -> dict:
    grade = vehicle["grade"].upper()
    wanted_colour = vehicle["colour"].lower() in {"onyx black", "onyx night", "dusty bronze"}
    priority = grade == "GX" and wanted_colour
    is_demo = vehicle.get("condition", "New").lower() == "demo"
    if priority:
        heading, embed_colour = "🚨 PRIORITY PRADO FOUND 🚨", 0xE50000
    elif grade == "GX":
        heading, embed_colour = "⭐ New GX Prado found", 0xFFCA00
    elif wanted_colour:
        heading, embed_colour = "🎨 New preferred-colour Prado found", 0x8A6138
    else:
        heading, embed_colour = "New Prado found", 0x2774AE
    if is_demo:
        heading = f"🧪 DEMO VEHICLE — {heading}"
    fields = [
        {"name": "Dealer", "value": vehicle["dealer"], "inline": True},
        {"name": "Grade", "value": vehicle["grade"], "inline": True},
        {"name": "Colour", "value": vehicle["colour"], "inline": True},
        {"name": "Condition", "value": "DEMONSTRATOR" if is_demo else "Brand new", "inline": True},
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


def unavailable_discord_payload(vehicle: dict) -> dict:
    payload = discord_payload(vehicle)
    embed = payload["embeds"][0]
    embed["title"] = "❌ NO LONGER AVAILABLE"
    embed["description"] = f'~~{vehicle["title"]}~~\n\n**This vehicle is no longer listed by the dealer.**'
    embed["color"] = 0x6B7280
    embed["fields"].insert(0, {"name": "Status", "value": "No longer available", "inline": False})
    return {"embeds": payload["embeds"], "allowed_mentions": {"parse": []}}


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
            message_id = str(response.json().get("id", "")).strip()
            if not message_id:
                raise ValueError("Discord did not return a message ID")
            with db() as conn:
                conn.execute(
                    "UPDATE vehicles SET discord_message_id=? WHERE region='wa' AND vin=?",
                    (message_id, vehicle["vin"]),
                )
            sent += 1
        except Exception as exc:
            errors.append(f'{vehicle["vin"]}: {exc}')
    return sent, errors


def mark_discord_unavailable(vehicles: list[dict]) -> list[str]:
    if not DISCORD_WEBHOOK_URL:
        return []
    errors = []
    for vehicle in vehicles:
        try:
            response = requests.patch(
                f'{DISCORD_WEBHOOK_URL}/messages/{vehicle["discord_message_id"]}',
                json=unavailable_discord_payload(vehicle),
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
        except Exception as exc:
            errors.append(f'{vehicle["vin"]}: {exc}')
    return errors


def configure_discord() -> None:
    global DISCORD_WEBHOOK_URL
    if DISCORD_WEBHOOK_URL or not os.isatty(0):
        return
    print("Discord alerts are not configured.")
    entered = getpass.getpass("Paste Discord webhook URL (hidden), or press Enter to skip: ").strip()
    if not entered:
        print("Starting without Discord alerts.")
        return
    if not re.fullmatch(r"https://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)/api/webhooks/\d+/[^\s]+", entered):
        raise SystemExit("That does not look like a valid Discord webhook URL.")
    DISCORD_WEBHOOK_URL = entered
    print("Discord alerts enabled for this run.")


def scan_all(region: str = "wa") -> dict:
    if region not in REGIONS:
        return {"started": False, "message": "Unknown region"}
    if not scan_lock.acquire(blocking=False):
        return {"started": False, "message": "A scan is already running"}
    try:
        dealers = load_dealers(region)
        now = datetime.now(timezone.utc).isoformat()
        with db() as conn:
            scan_id = conn.execute("INSERT INTO scans(started_at, dealer_count, region) VALUES (?, ?, ?)", (now, len(dealers), region)).lastrowid
        seen = set()
        newly_found = []
        total = 0
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
        for dealer in dealers:
            try:
                new_url = dealer["url"]
                demo_url = dealer.get("demo_url") or urljoin(new_url, "/demonstrators/prado")
                vehicles_by_vin = {}
                for source_url in (new_url, demo_url):
                    response = requests.get(source_url, headers=headers, timeout=HTTP_TIMEOUT)
                    response.raise_for_status()
                    if not re.search(r"\b(?:LandCruiser\s+)?Prado\b", response.text, re.I):
                        raise ValueError(f"Page did not look like a Prado stock page: {source_url}")
                    source_dealer = {**dealer, "url": source_url}
                    for vehicle in parse_stock(response.text, source_dealer):
                        vehicles_by_vin[vehicle["vin"]] = vehicle
                vehicles = list(vehicles_by_vin.values())
                with db() as conn:
                    conn.execute("UPDATE vehicles SET newly_added=0 WHERE region=? AND dealer=?", (region, dealer["name"]))
                    for vehicle in vehicles:
                        seen.add(vehicle["vin"])
                        previous = conn.execute("SELECT vin, active FROM vehicles WHERE region=? AND vin=?", (region, vehicle["vin"])).fetchone()
                        values = (dealer["name"], vehicle["stock_id"], vehicle["title"], vehicle["grade"],
                                  vehicle["colour"], vehicle["condition"], vehicle["price"], vehicle["image_url"],
                                  vehicle["detail_url"], now, scan_id, vehicle["vin"])
                        if previous:
                            if not previous["active"]:
                                newly_found.append({**vehicle, "dealer": dealer["name"]})
                            conn.execute("""UPDATE vehicles SET dealer=?,stock_id=?,title=?,grade=?,colour=?,condition=?,price=?,image_url=?,detail_url=?,
                              last_seen=?,last_scan_id=?,active=1,newly_added=? WHERE region=? AND vin=?""", values[:-1] + (0 if previous["active"] else 1, region, values[-1]))
                        else:
                            # On the first run this intentionally includes every currently stocked car.
                            newly_found.append({**vehicle, "dealer": dealer["name"]})
                            conn.execute("""INSERT INTO vehicles(dealer,stock_id,title,grade,colour,condition,price,image_url,detail_url,
                              first_seen,last_seen,first_scan_id,last_scan_id,active,newly_added,region,vin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?)""",
                              values[:-2] + (now, scan_id, scan_id, region, vehicle["vin"]))
                    checked_urls = f"{new_url} | {demo_url}"
                    conn.execute("INSERT INTO dealer_checks VALUES(?,?,?,?,?,NULL)", (scan_id, dealer["name"], checked_urls, 1, len(vehicles)))
                total += len(vehicles)
            except Exception as exc:
                with db() as conn:
                    conn.execute("INSERT INTO dealer_checks VALUES(?,?,?,?,?,?)", (scan_id, dealer.get("name", "Unknown"), dealer.get("url", ""), 0, 0, str(exc)[:500]))
        # Only deactivate cars from dealers that completed successfully.
        disappeared = []
        with db() as conn:
            successful = [r[0] for r in conn.execute("SELECT dealer FROM dealer_checks WHERE scan_id=? AND ok=1", (scan_id,))]
            if successful:
                marks = ",".join("?" for _ in successful)
                if region == "wa":
                    disappeared = [dict(row) for row in conn.execute(
                        f"""SELECT * FROM vehicles WHERE region=? AND active=1
                          AND discord_message_id IS NOT NULL AND dealer IN ({marks}) AND last_scan_id < ?""",
                        (region, *successful, scan_id),
                    )]
                conn.execute(f"UPDATE vehicles SET active=0 WHERE region=? AND dealer IN ({marks}) AND last_scan_id < ?", (region, *successful, scan_id))
            conn.execute("UPDATE scans SET finished_at=?, vehicle_count=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), total, scan_id))
        unavailable_errors = mark_discord_unavailable(disappeared)
        notification_batch = newly_found if region == "wa" else []
        discord_was_initialized = False
        if DISCORD_WEBHOOK_URL and region == "wa":
            with db() as conn:
                discord_was_initialized = conn.execute(
                    "SELECT value FROM app_meta WHERE key='discord_initialized'"
                ).fetchone() is not None
                if not discord_was_initialized:
                    notification_batch = [dict(row) for row in conn.execute(
                        "SELECT * FROM vehicles WHERE region='wa' AND active=1 ORDER BY dealer, title"
                    )]
        notified, notification_errors = notify_discord(notification_batch)
        if DISCORD_WEBHOOK_URL and region == "wa" and not notification_errors and not discord_was_initialized:
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO app_meta(key,value) VALUES('discord_initialized',?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
        for error in notification_errors:
            print(f"Discord notification failed: {error}", flush=True)
        for error in unavailable_errors:
            print(f"Discord unavailable update failed: {error}", flush=True)
        return {"started": True, "scan_id": scan_id, "vehicle_count": total,
                "new_vehicle_count": len(newly_found), "notified_count": notified,
                "notification_errors": notification_errors,
                "unavailable_updated_count": len(disappeared) - len(unavailable_errors),
                "unavailable_errors": unavailable_errors}
    finally:
        scan_lock.release()


def current_interval() -> int:
    with db() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key='interval_seconds'").fetchone()
    return max(60, int(row[0])) if row else max(60, DEFAULT_INTERVAL_SECONDS)


def scheduler() -> None:
    # Check every state on startup, then use the saved interval (which can change live).
    for region in REGIONS:
        scan_all(region)
    while True:
        if scheduler_wakeup.wait(current_interval()):
            scheduler_wakeup.clear()
            continue
        for region in REGIONS:
            scan_all(region)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    region = request.args.get("region", "wa").lower()
    if region not in REGIONS:
        return jsonify({"error": "Unknown region"}), 400
    with db() as conn:
        last = conn.execute("SELECT * FROM scans WHERE region=? ORDER BY id DESC LIMIT 1", (region,)).fetchone()
        checks = conn.execute("SELECT * FROM dealer_checks WHERE scan_id=? ORDER BY dealer", (last["id"],)).fetchall() if last else []
    return jsonify({"scanning": scan_lock.locked(), "last_scan": dict(last) if last else None, "dealers": [dict(x) for x in checks], "interval_seconds": current_interval()})


@app.get("/api/vehicles")
def vehicles():
    region = request.args.get("region", "wa").lower()
    if region not in REGIONS:
        return jsonify({"error": "Unknown region"}), 400
    include_gone = request.args.get("include_gone") == "1"
    with db() as conn:
        rows = conn.execute("""SELECT *, newly_added AS is_new FROM vehicles
          WHERE region=? AND (active=1 OR ?) ORDER BY CASE
            WHEN upper(grade)='GX' AND lower(colour) IN ('onyx black','onyx night','dusty bronze') THEN 0
            WHEN upper(grade)='GX' THEN 1
            WHEN lower(colour) IN ('onyx black','onyx night','dusty bronze') THEN 2
            ELSE 3 END,
          is_new DESC, dealer, title""", (region, include_gone)).fetchall()
    return jsonify([dict(x) for x in rows])


@app.post("/api/scan")
def scan():
    region = request.args.get("region", "wa").lower()
    if region not in REGIONS:
        return jsonify({"error": "Unknown region"}), 400
    thread = threading.Thread(target=scan_all, args=(region,), daemon=True)
    thread.start()
    return jsonify({"started": True}), 202


@app.post("/api/settings")
def settings():
    try:
        seconds = int(request.get_json(silent=True)["interval_seconds"])
    except (TypeError, ValueError, KeyError):
        return jsonify({"error": "interval_seconds must be a whole number"}), 400
    if not 60 <= seconds <= 30 * 24 * 3600:
        return jsonify({"error": "Interval must be between 1 minute and 30 days"}), 400
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('interval_seconds',?)", (str(seconds),))
    scheduler_wakeup.set()
    return jsonify({"interval_seconds": seconds})


if __name__ == "__main__":
    configure_discord()
    init_db()
    threading.Thread(target=scheduler, daemon=True, name="prado-scheduler").start()
    app.run(host=os.environ.get("PRADO_HOST", "127.0.0.1"), port=int(os.environ.get("PRADO_PORT", "8080")), debug=False)
