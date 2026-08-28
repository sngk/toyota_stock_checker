import unittest
from pathlib import Path
from app import discord_payload, parse_stock


class ParserTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
