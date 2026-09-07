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
    product_performance_ad_breakdown,
    product_performance_ad_totals,
    amazon_series,
    amazon_sid_accounts,
    optional_metric,
    require_business_access,
)


class AmazonDashboardPeriodTests(unittest.TestCase):
    def test_business_access_requires_configured_key(self):
        with patch.dict("os.environ", {"DASHBOARD_API_KEY": "test-dashboard-key", "SYNC_API_KEY": "test-sync-key"}, clear=False):
            self.assertIsNone(require_business_access(None))
            self.assertIsNone(require_business_access("wrong-key"))
            self.assertIsNone(require_business_access("test-dashboard-key"))

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
        self.assertEqual(periods[1][1:], (date(2026, 8, 31), date(2026, 8, 31)))

    def test_week_periods_clip_partial_natural_weeks_to_selected_range(self):
        periods = amazon_periods(date(2026, 8, 1), date(2026, 8, 14), "周")
        self.assertEqual(
            periods,
            [
                ("2026-08-01~2026-08-02", date(2026, 8, 1), date(2026, 8, 2)),
                ("2026-08-03~2026-08-09", date(2026, 8, 3), date(2026, 8, 9)),
                ("2026-08-10~2026-08-14", date(2026, 8, 10), date(2026, 8, 14)),
            ],
        )

    def test_month_periods_cover_cross_month_range(self):
        periods = amazon_periods(date(2026, 8, 1), date(2026, 9, 30), "月")
        self.assertEqual(periods[0][0], "2026-08")
        self.assertEqual(periods[1][0], "2026-09")


    def test_new_ad_order_share_field_is_preserved(self):
        self.assertIn("ad_order_share", amazon_empty_row("series"))
        self.assertIn("ad_order_share", AMAZON_METRIC_SOURCES["calculated"])

    def test_product_performance_uses_confirmed_upstream_fields(self):
        self.assertEqual(
            optional_metric({"net_amount": "12.5", "amount": "99"}, *AMAZON_SOURCE_FIELDS["performance"]["net_sales"]),
            12.5,
        )
        self.assertEqual(
            optional_metric({"spend": "3.25"}, *AMAZON_SOURCE_FIELDS["performance"]["ad_cost"]),
            3.25,
        )
        self.assertEqual(
            optional_metric({"ads_sales_volume_quantity": "4"}, *AMAZON_SOURCE_FIELDS["performance"]["ad_units"]),
            4.0,
        )

    def test_metric_source_mapping_uses_product_performance_for_all_upstream_fields(self):
        self.assertIn("cvr", AMAZON_METRIC_SOURCES["performance"])
        for field in ("impressions", "clicks", "ad_sales", "ad_cost", "ad_units", "ad_orders", "ctr", "cpc", "ad_cvr", "acos"):
            self.assertIn(field, AMAZON_METRIC_SOURCES["performance"])
        self.assertNotIn("ad_report", AMAZON_METRIC_SOURCES)

    def test_dashboard_aggregates_product_performance_metrics(self):
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
            "impressions": 100,
            "clicks": 5,
            "ad_sales_amount": 20,
            "spend": 2,
            "ads_sales_volume_quantity": 3,
            "ad_order_quantity": 2,
        }]
        with patch("app.fetch_product_performance", new=AsyncMock(return_value=performance)):
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
        self.assertEqual(row["asin"], asin)
        self.assertEqual(result["mapping"]["sources"], AMAZON_METRIC_SOURCES)

    def test_product_performance_ad_metrics_sum_sp_sb_sbv_and_sd(self):
        raw = {
            # Deliberately leave the generic fields at SP-only values.  The
            # dashboard must use the typed product-performance dimensions.
            "clicks": 10,
            "spend": 1,
            "ads_sales_volume_quantity": 2,
            "ad_order_quantity": 1,
            "ad_clicks_sp": 10,
            "ad_clicks_sb": 3,
            "ad_clicks_sbv": 2,
            "ad_clicks_sd": 1,
            "ads_sp_cost": 1,
            "shared_ads_sb_cost": 0.3,
            "shared_ads_sbv_cost": 0.2,
            "ads_sd_cost": 0.1,
            "ads_sp_sales_volume_quantity": 2,
            "shared_ads_sb_sales_volume_quantity": 1,
            "shared_ads_sbv_sales_volume_quantity": 1,
            "ads_sd_sales_volume_quantity": 1,
            "ad_order_quantity_sp": 1,
            "shared_ad_order_quantity_sb": 1,
            "shared_ad_order_quantity_sbv": 1,
            "ad_order_quantity_sd": 1,
        }
        self.assertEqual(product_performance_ad_totals(raw)["clicks"], 16)
        self.assertAlmostEqual(product_performance_ad_totals(raw)["ad_cost"], 1.6)
        self.assertEqual(product_performance_ad_totals(raw)["ad_units"], 5)
        self.assertEqual(product_performance_ad_totals(raw)["ad_orders"], 4)
        breakdown = product_performance_ad_breakdown(raw)
        self.assertEqual(breakdown["sb"]["clicks"], 3)
        self.assertEqual(breakdown["sbv"]["ad_units"], 1)
        self.assertEqual(breakdown["sd"]["ad_orders"], 1)

    def test_multi_site_original_currency_keeps_site_rows_separate(self):
        site_names = ["美国", "加拿大"]
        asin = next(iter(ASIN_MAPPING[AMAZON_SITE_CODES[site_names[0]]]))
        product = ASIN_MAPPING[AMAZON_SITE_CODES[site_names[0]]][asin]
        series = amazon_series(product)
        performance = [{"asin": asin, "volume": 1, "net_amount": 10, "sessions_total": 2}]

        async def fake_performance(sid, *args, **kwargs):
            return performance

        with patch("app.fetch_product_performance", new=AsyncMock(side_effect=fake_performance)):
            result = asyncio.run(amazon_dashboard_periodic(
                "日",
                date(2026, 9, 4),
                date(2026, 9, 4),
                site_names,
                {series},
                {product},
                {AMAZON_SITE_CODES[site_names[0]]: {"sid": 1}, AMAZON_SITE_CODES[site_names[1]]: {"sid": 2}},
                [],
                "original",
            ))
        self.assertEqual(result["currency"], "original")
        self.assertEqual(result["selected_sites"], site_names)
        self.assertEqual({row["site"] for row in result["rows"]}, set(site_names))


if __name__ == "__main__":
    unittest.main()
