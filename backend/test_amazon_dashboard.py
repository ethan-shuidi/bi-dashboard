import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app import (
    AMAZON_PRODUCTS,
    AMAZON_METRIC_SOURCES,
    AMAZON_SERIES,
    AMAZON_SOURCE_FIELDS,
    ASIN_MAPPING,
    AMAZON_SITE_CODES,
    amazon_empty_row,
    amazon_dashboard_periodic,
    amazon_periods,
    amazon_product,
    amazon_series,
    amazon_sid_accounts,
    optional_metric,
    require_business_access,
)


class AmazonDashboardPeriodTests(unittest.TestCase):
    def test_business_access_never_trusts_origin_header(self):
        with patch.dict("os.environ", {"DASHBOARD_API_KEY": "dashboard-secret", "SYNC_API_KEY": "sync-secret"}, clear=False):
            with self.assertRaises(Exception):
                require_business_access(None)
            with self.assertRaises(Exception):
                require_business_access("wrong-key")
            self.assertIsNone(require_business_access("dashboard-secret"))

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

    def test_tn20_main_red_product_name_is_used_by_api_mapping(self):
        self.assertIn("TN20-主链接-红", AMAZON_PRODUCTS)
        self.assertNotIn("TN20-主链接-樱桃红", AMAZON_PRODUCTS)
        self.assertEqual(amazon_product("US", "B0H8SZZN8X"), "TN20-主链接-红")
        self.assertEqual(amazon_series("TN20-主链接-红"), "TN20系列（主链接）汇总")

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


    def test_new_ad_order_share_field_is_preserved(self):
        self.assertIn("ad_order_share", amazon_empty_row("series"))
        self.assertIn("ad_order_share", AMAZON_METRIC_SOURCES["calculated"])

    def test_net_sales_and_ad_report_use_confirmed_upstream_fields(self):
        self.assertEqual(
            optional_metric({"net_amount": "12.5", "amount": "99"}, *AMAZON_SOURCE_FIELDS["performance"]["net_sales"]),
            12.5,
        )
        self.assertEqual(
            optional_metric({"spends": "3.25"}, *AMAZON_SOURCE_FIELDS["ad_report"]["ad_cost"]),
            3.25,
        )
        self.assertEqual(
            optional_metric({"ad_units": "4"}, *AMAZON_SOURCE_FIELDS["ad_report"]["ad_units"]),
            4.0,
        )

    def test_metric_source_mapping_separates_performance_and_ad_report(self):
        self.assertIn("cvr", AMAZON_METRIC_SOURCES["performance"])
        self.assertIn("ctr", AMAZON_METRIC_SOURCES["ad_report"])
        self.assertNotIn("ctr", AMAZON_METRIC_SOURCES["performance"])

    def test_dashboard_aggregates_performance_and_ad_report_separately(self):
        site_name = next(iter(AMAZON_SITE_CODES))
        product = next(iter(ASIN_MAPPING["US"].values()))
        asin = next(iter(ASIN_MAPPING["US"]))
        series = next(iter(AMAZON_SERIES))
        performance = [{
            "asin": asin,
            "volume": 10,
            "net_amount": 80,
            "amount": 99,
            "order_items": 5,
            "b2b_volume": 1,
            "b2b_order_items": 1,
            "sessions_total": 50,
            "cvr": 0.1,
        }]
        ad_report = [{
            "asin": asin,
            "impressions": 100,
            "clicks": 5,
            "sales": 20,
            "spends": 2,
            "ad_units": 3,
            "orders": 2,
            "_source": "ad_report",
            "_dashboard_date": "2026-09-04",
        }]
        with patch("app.fetch_product_performance", new=AsyncMock(return_value=performance)), patch(
            "app.fetch_ad_reports_range", new=AsyncMock(return_value=ad_report)
        ):
            result = asyncio.run(amazon_dashboard_periodic(
                "日",
                date(2026, 9, 4),
                date(2026, 9, 4),
                site_name,
                set(AMAZON_SERIES),
                set(AMAZON_PRODUCTS),
                {"US": {"sid": 1}},
            ))
        row = next(item for item in result["rows"] if item["product"] == product)
        self.assertEqual(row["net_sales"], 80)
        self.assertEqual(row["ad_cost"], 2)
        self.assertEqual(row["ad_units"], 3)
        self.assertEqual(row["ad_orders"], 2)
        self.assertEqual(row["ad_order_share"], 0.4)
        self.assertEqual(result["mapping"]["sources"], AMAZON_METRIC_SOURCES)


if __name__ == "__main__":
    unittest.main()
