import unittest
from datetime import date

from app import amazon_periods, amazon_product, amazon_series, amazon_sid_accounts


class AmazonDashboardPeriodTests(unittest.TestCase):
    def test_japan_uses_both_named_shops(self):
        accounts = amazon_sid_accounts("日本", {"JP": {"sid": 100}}, [
            {"sid": 100, "country": "日本", "name": "Comu-JP"},
            {"sid": 200, "country": "日本", "name": "Comulytic-JP"},
        ])
        self.assertEqual([item["sid"] for item in accounts], [100, 200])

    def test_product_performance_asins_match_any_array_entry(self):
        self.assertEqual(
            amazon_product("US", [
                {"asin": "UNMAPPED"},
                {"asin": "B0G1XQ3H4H", "sid": 123},
            ]),
            "TN10-主链接-黑色",
        )

    def test_product_performance_nested_asins_are_supported(self):
        self.assertEqual(
            amazon_product("US", {"asins": [{"ASIN": "B0G1YMLFSZ"}]}),
            "TN10-小链接-银色",
        )

    def test_us_asin_product_mapping_matches_latest_assignment(self):
        expected = {
            "B0GZNNL72W": "TN10-主链接-橙色",
            "B0GMGP9B1D": "TN10-小链接-橙色",
            "B0GR9CDQYG": "TN10-主链接-银色",
            "B0G1YMLFSZ": "TN10-小链接-银色",
        }
        expected_series = {
            "TN10-主链接-橙色": "TN10系列（主链接）汇总",
            "TN10-小链接-橙色": "TN10系列（小链接）汇总",
            "TN10-主链接-银色": "TN10系列（主链接）汇总",
            "TN10-小链接-银色": "TN10系列（小链接）汇总",
        }
        for asin, product in expected.items():
            with self.subTest(asin=asin):
                self.assertEqual(amazon_product("US", asin), product)
                self.assertEqual(amazon_series(product), expected_series[product])

    def test_daily_periods_cover_every_day(self):
        periods = amazon_periods(date(2026, 8, 24), date(2026, 8, 27), "日")
        self.assertEqual([item[0] for item in periods], [
            "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"
        ])

    def test_week_periods_are_natural_monday_to_sunday_windows(self):
        periods = amazon_periods(date(2026, 8, 24), date(2026, 8, 31), "周")
        self.assertEqual(periods[0][1:], (date(2026, 8, 24), date(2026, 8, 30)))
        self.assertEqual(periods[1][1:], (date(2026, 8, 31), date(2026, 9, 6)))

    def test_month_periods_cover_cross_month_range(self):
        periods = amazon_periods(date(2026, 8, 1), date(2026, 9, 30), "月")
        self.assertEqual(periods[0][0], "2026-08")
        self.assertEqual(periods[1][0], "2026-09")


if __name__ == "__main__":
    unittest.main()
