import unittest
from pathlib import Path
from app import parse_stock


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


if __name__ == "__main__":
    unittest.main()

