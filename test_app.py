import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as stock_app
from app import discord_payload, parse_stock, should_notify, unavailable_discord_payload


class ParserTest(unittest.TestCase):
    def test_notification_filter_keeps_all_wa_and_only_priority_matches_elsewhere(self):
        ordinary = {"grade": "Kakadu", "colour": "Glacier White"}
        self.assertTrue(should_notify("wa", ordinary))
        self.assertFalse(should_notify("vic", ordinary))
        self.assertTrue(should_notify("vic", {"grade": "GX", "colour": "Onyx Night"}))
        self.assertTrue(should_notify("qld", {"grade": "gx", "colour": "Dusty Bronze"}))
        self.assertFalse(should_notify("nsw", {"grade": "GXL", "colour": "Onyx Night"}))
        self.assertFalse(should_notify("sa", {"grade": "GX", "colour": "Glacier White"}))

    def test_bunbury_fixture_when_present(self):
        fixture = Path("bunbury.tmp.html")
        if not fixture.exists():
            self.skipTest("downloaded fixture not present")
        cars = parse_stock(fixture.read_text(encoding="utf-8"), {"name": "Bunbury Toyota", "url": "https://bunburytoyota.dealer.toyota.com.au/new-vehicles/dealer-stock/prado"})
        self.assertEqual(1, len(cars))
        self.assertEqual("JTEACDBJ10K044582", cars[0]["vin"])
        self.assertEqual("Altitude", cars[0]["grade"])
        self.assertEqual("Dusty Bronze", cars[0]["colour"])

    def test_priority_discord_payload(self):
        payload = discord_payload({
            "vin": "TESTVIN123456789", "dealer": "Test Toyota",
            "title": "2026 Toyota LandCruiser Prado GX (Dusty Bronze)",
            "grade": "GX", "colour": "Dusty Bronze", "price": "$80,000",
            "image_url": "https://example.com/car.png", "detail_url": "https://example.com/car",
        })
        self.assertIn("PRIORITY", payload["embeds"][0]["title"])
        self.assertEqual(0xE50000, payload["embeds"][0]["color"])
        self.assertEqual([], payload["allowed_mentions"]["parse"])

    def test_unavailable_discord_payload_is_struck_through(self):
        payload = unavailable_discord_payload({
            "vin": "TESTVIN123456789", "dealer": "Test Toyota",
            "title": "2026 Toyota LandCruiser Prado GX (Dusty Bronze)",
            "grade": "GX", "colour": "Dusty Bronze", "condition": "New",
            "price": "$80,000", "image_url": None, "detail_url": "https://example.com/car",
        })
        embed = payload["embeds"][0]
        self.assertEqual("❌ NO LONGER AVAILABLE", embed["title"])
        self.assertIn("~~2026 Toyota LandCruiser Prado", embed["description"])
        self.assertEqual(0x6B7280, embed["color"])

    def test_demo_is_detected_and_labelled(self):
        html = '''<ul data-list-type="demonstrator"><li class="tb-list-item" data-id="demo1"
          data-vehicleline="LandCruiser Prado" data-item-category2="LandCruiser Prado" data-item-category3="GX">
          <h3>2026 Toyota LandCruiser Prado GX (Onyx Night)</h3><small>VIN: DEMOVIN123456789</small>
          <a href="/inventory/demo">View Vehicle Details</a></li></ul>'''
        car = parse_stock(html, {"name": "Test Toyota", "url": "https://example.com/prado"})[0]
        self.assertEqual("Demo", car["condition"])
        self.assertEqual("https://example.com/inventory/demo", car["detail_url"])
        car["dealer"] = "Test Toyota"
        self.assertIn("DEMO VEHICLE", discord_payload(car)["embeds"][0]["title"])

    def test_rockingham_demo_fixture_when_present(self):
        fixture = Path("rockingham-demo.tmp.html")
        if not fixture.exists():
            self.skipTest("downloaded demo fixture not present")
        cars = parse_stock(fixture.read_text(encoding="utf-8"), {
            "name": "Rockingham Toyota",
            "url": "https://rockinghamtoyota.dealer.toyota.com.au/demonstrators/prado",
        })
        self.assertEqual(1, len(cars))
        self.assertEqual("JTEACDBJ50K034038", cars[0]["vin"])
        self.assertEqual("Demo", cars[0]["condition"])
        self.assertIn("/demo/prado/", cars[0]["detail_url"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db_patch = patch.object(stock_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        stock_app.init_db()
        self.client = stock_app.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_region_filters_are_isolated(self):
        with stock_app.db() as conn:
            for region in ("wa", "nsw", "sa", "vic", "qld"):
                conn.execute("""INSERT INTO vehicles(vin,region,dealer,title,grade,colour,condition,
                  first_seen,last_seen,first_scan_id,last_scan_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  ("SAMEVIN123456789", region, region.upper(), "Prado GX", "GX", "Onyx Night",
                   "New", "now", "now", 1, 1))
        wa = self.client.get("/api/vehicles?region=wa").get_json()
        nsw = self.client.get("/api/vehicles?region=nsw").get_json()
        sa = self.client.get("/api/vehicles?region=sa").get_json()
        vic = self.client.get("/api/vehicles?region=vic").get_json()
        qld = self.client.get("/api/vehicles?region=qld").get_json()
        self.assertEqual("WA", wa[0]["dealer"])
        self.assertEqual("NSW", nsw[0]["dealer"])
        self.assertEqual("SA", sa[0]["dealer"])
        self.assertEqual("VIC", vic[0]["dealer"])
        self.assertEqual("QLD", qld[0]["dealer"])

    def test_interval_setting_is_persisted_and_validated(self):
        response = self.client.post("/api/settings", json={"interval_seconds": 1800})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1800, self.client.get("/api/status?region=wa").get_json()["interval_seconds"])
        self.assertEqual(400, self.client.post("/api/settings", json={"interval_seconds": 30}).status_code)

    def test_nsw_scan_does_not_notify_discord(self):
        dealer_file = Path(self.temp_dir.name) / "nsw.json"
        dealer_file.write_text(json.dumps([{"name": "NSW Toyota", "url": "https://example.com/prado"}]))
        response = type("Response", (), {"text": "LandCruiser Prado", "raise_for_status": lambda self: None})()
        with patch.dict(stock_app.REGIONS, {"nsw": dealer_file}), \
             patch.object(stock_app.requests, "get", return_value=response), \
             patch.object(stock_app, "parse_stock", return_value=[]), \
             patch.object(stock_app, "notify_discord", return_value=(0, [])) as notify:
            stock_app.scan_all("nsw")
        notify.assert_called_once_with([])

    def test_sa_scan_does_not_notify_discord(self):
        dealer_file = Path(self.temp_dir.name) / "sa.json"
        dealer_file.write_text(json.dumps([{"name": "SA Toyota", "url": "https://example.com/prado"}]))
        response = type("Response", (), {"text": "LandCruiser Prado", "raise_for_status": lambda self: None})()
        with patch.dict(stock_app.REGIONS, {"sa": dealer_file}), \
             patch.object(stock_app.requests, "get", return_value=response), \
             patch.object(stock_app, "parse_stock", return_value=[]), \
             patch.object(stock_app, "notify_discord", return_value=(0, [])) as notify:
            stock_app.scan_all("sa")
        notify.assert_called_once_with([])

    def test_vic_and_qld_scans_do_not_notify_discord(self):
        for region in ("vic", "qld"):
            dealer_file = Path(self.temp_dir.name) / f"{region}.json"
            dealer_file.write_text(json.dumps([{"name": f"{region.upper()} Toyota", "url": "https://example.com/prado"}]))
            response = type("Response", (), {"text": "LandCruiser Prado", "raise_for_status": lambda self: None})()
            with patch.dict(stock_app.REGIONS, {region: dealer_file}), \
                 patch.object(stock_app.requests, "get", return_value=response), \
                 patch.object(stock_app, "parse_stock", return_value=[]), \
                 patch.object(stock_app, "notify_discord", return_value=(0, [])) as notify:
                stock_app.scan_all(region)
            notify.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
