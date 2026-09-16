import asyncio
import json
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch
import httpx
from app import (
    AMAZON_PRODUCTS,
    AMAZON_STRATEGY_OPTIONS,
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
    amazon_strategy_board_groups,
    finalize_strategy_metrics,
    optional_metric,
    strategy_campaign_name,
    require_business_access,
    _lingxing_mcp_result,
    fetch_mcp_product_performance,
    fetch_mcp_campaign_report,
    validate_strategy_series,
)


class AmazonDashboardPeriodTests(unittest.TestCase):
    def test_lingxing_mcp_result_decodes_text_business_payload(self):
        payload = {
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({"success": True, "data": [{"clicks": 3239}]})
                }]
            }
        }
        self.assertEqual(_lingxing_mcp_result(payload), [{"clicks": 3239}])

    def test_mcp_product_performance_preserves_ad_type_clicks(self):
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/mcp"),
            json={
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "success": True,
                            "data": {"list": [{
                                "asin": "B0TEST",
                                "clicks": 4226,
                                "ad_clicks_sp": 3239,
                                "ad_clicks_sb": 417,
                                "ad_clicks_sbv": 100,
                                "ad_clicks_sd": 470,
                            }]},
                        }),
                    }]
                }
            },
        )
        client = AsyncMock()
        client.post.return_value = response
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            rows = asyncio.run(fetch_mcp_product_performance(14292, date(2026, 9, 7), date(2026, 9, 13), client))
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sp"]["clicks"], 3239)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sb"]["clicks"], 417)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sbv"]["clicks"], 100)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sd"]["clicks"], 470)

    def test_mcp_campaign_report_resolves_profile_and_keeps_real_name(self):
        shops_response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/mcp"),
            json={"result": {"content": [{"type": "text", "text": json.dumps({
                "success": True,
                "data": [{"sid": 14292, "profile_id": "2815938091388375"}],
            })}]}}
        )
        campaign_response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/mcp"),
            json={"result": {"content": [{"type": "text", "text": json.dumps({
                "success": True,
                "data": {"data": [{"campaign_id": "123", "name": "真实活动名称", "ads_type": "SP"}]},
            })}]}}
        )
        client = AsyncMock()
        client.post.side_effect = [shops_response, campaign_response]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            rows = asyncio.run(fetch_mcp_campaign_report(14292, date(2026, 9, 7), date(2026, 9, 13), client))
        self.assertEqual(rows[0]["name"], "真实活动名称")
        self.assertEqual(rows[0]["campaign_id"], "123")
        self.assertEqual(client.post.call_count, 2)

    def test_campaign_name_reads_nested_campaign_objects(self):
        self.assertEqual(
            strategy_campaign_name({"campaign": {"details": {"campaignTitle": "真实活动名称"}}}),
            "真实活动名称",
        )

    def test_zero_typed_breakdown_does_not_replace_generic_totals(self):
        raw = {"clicks": 4227, "ad_order_quantity": 200}
        totals = product_performance_ad_totals(raw)
        self.assertNotIn("clicks", totals)
        self.assertNotIn("ad_orders", totals)

    def test_zero_filled_typed_breakdown_is_marked_unavailable(self):
        raw = {
            "clicks": 4227,
            "ad_clicks_sp": 0,
            "shared_ad_clicks_sb": 0,
            "shared_ad_clicks_sbv": 0,
            "ad_clicks_sd": 0,
        }
        breakdown = product_performance_ad_breakdown(raw)
        self.assertTrue(all(breakdown[key]["clicks"] is None for key in ("sp", "sb", "sbv", "sd")))
    def test_business_access_does_not_require_key_for_internal_app(self):
        self.assertIsNone(require_business_access(None))
        self.assertIsNone(require_business_access("legacy-key"))

    def test_strategy_and_series_must_be_set_together(self):
        self.assertIn("bundle", AMAZON_STRATEGY_OPTIONS)
        validate_strategy_series("/", "")
        validate_strategy_series("品类词", "TN10系列（主链接）汇总")
        with self.assertRaises(Exception):
            validate_strategy_series("品类词", "")
        with self.assertRaises(Exception):
            validate_strategy_series("/", "TN10系列（主链接）汇总")

    def test_strategy_groups_keep_unassigned_campaigns_visible(self):
        series = AMAZON_SERIES[0]
        week = date(2026, 9, 7)
        metrics = lambda clicks: {"impressions": clicks * 10, "clicks": clicks, "ad_cost": clicks * 0.5, "ad_sales": clicks * 2, "ad_units": clicks, "ad_orders": clicks}
        aggregate = {
            ("US", "100", "assigned"): {"site": "美国", "site_code": "US", "store_sid": "100", "store_name": "Shop", "campaign_id": "assigned", "campaign_name": "Assigned", "ad_type": "SP", "currency": "USD", "metrics": metrics(10)},
            ("US", "100", "legacy-empty-series"): {"site": "美国", "site_code": "US", "store_sid": "100", "store_name": "Shop", "campaign_id": "legacy-empty-series", "campaign_name": "Legacy", "ad_type": "SP", "currency": "USD", "metrics": metrics(8)},
            ("US", "100", "unclassified"): {"site": "美国", "site_code": "US", "store_sid": "100", "store_name": "Shop", "campaign_id": "unclassified", "campaign_name": "Unclassified", "ad_type": "SP", "currency": "USD", "metrics": metrics(5)},
        }
        assignments = {
            ("US", "100", "assigned"): {"strategy": "品类词", "series": series, "product": "", "campaign_name": "Assigned", "store_name": "Shop"},
            ("US", "100", "legacy-empty-series"): {"strategy": "竞品词", "series": "", "product": "", "campaign_name": "Legacy", "store_name": "Shop"},
        }
        notes = {(week, "US", series, "品类词"): "Keep the note"}
        groups = amazon_strategy_board_groups(aggregate, assignments, notes, ["美国"], None, week)

        self.assertEqual([(group["strategy"], group["series"]) for group in groups], [("品类词", series), ("竞品词", ""), ("/", "")])
        self.assertEqual([len(group["campaigns"]) for group in groups], [1, 1, 1])
        self.assertEqual(groups[0]["note"], "Keep the note")

    def test_strategy_order_and_unit_counts_reach_groups_and_campaigns(self):
        series = AMAZON_SERIES[0]
        aggregate = {}
        assignments = {}
        for campaign_id, orders, units in [("one", 2, 5), ("two", 3, 7)]:
            key = ("US", "100", campaign_id)
            aggregate[key] = {
                "site": "美国", "store_name": "Shop", "campaign_name": campaign_id,
                "ad_type": "SP", "currency": "USD",
                "metrics": {"clicks": 10, "ad_cost": 20, "ad_sales": 100,
                            "ad_orders": orders, "ad_units": units},
            }
            assignments[key] = {"strategy": "品类词", "series": series}
        groups = amazon_strategy_board_groups(aggregate, assignments, {}, ["美国"], {series}, date(2026, 9, 7))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["metrics"]["ad_orders"], 5)
        self.assertEqual(groups[0]["metrics"]["ad_units"], 12)
        self.assertEqual(groups[0]["metrics"]["ad_cvr"], 0.25)
        self.assertEqual([(item["ad_orders"], item["ad_units"]) for item in groups[0]["campaigns"]], [(2, 5), (3, 7)])
        empty = finalize_strategy_metrics({})
        self.assertEqual((empty["ad_orders"], empty["ad_units"]), (0, 0))
        self.assertIsNone(empty["ad_cvr"])

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
        self.assertEqual(row["cpo"], 1)
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
