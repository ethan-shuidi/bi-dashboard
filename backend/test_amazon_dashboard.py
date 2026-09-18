import asyncio
import json
import os
import time
import unittest
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
from sqlalchemy import create_engine, inspect, text
from app import (
    AMAZON_SALES_ALL_SITES,
    AMAZON_SALES_ALL_MODEL,
    AMAZON_PRODUCTS,
    AMAZON_STRATEGY_OPTIONS,
    AMAZON_METRIC_SOURCES,
    AMAZON_SERIES,
    AMAZON_SOURCE_FIELDS,
    AMAZON_SALES_TN20_SMALL_SERIES,
    AMAZON_SALES_TARGET_FIELDS,
    ASIN_MAPPING,
    AMAZON_SITE_CODES,
    AmazonMonthlyTarget,
    AmazonWeeklyTarget,
    Base,
    amazon_empty_row,
    amazon_dashboard_periodic,
    amazon_periods,
    amazon_product,
    product_performance_ad_breakdown,
    product_performance_ad_totals,
    amazon_series,
    amazon_sales_actuals,
    amazon_sales_apply_campaign_ad_cost,
    amazon_sales_campaign_cost_by_series,
    amazon_sales_completion,
    amazon_sales_currency,
    amazon_sales_derived_targets,
    amazon_sales_metric_rows,
    amazon_sales_selected_sites,
    amazon_sales_scope,
    amazon_sales_week_time_progress,
    amazon_sales_actual_rows,
    amazon_sales_target_values,
    amazon_sales_target_number,
    amazon_week_label,
    amazon_ads_chart_rows,
    amazon_ads_charts,
    normalize_week_start,
    migrate_amazon_monthly_targets,
    amazon_sid_accounts,
    amazon_strategy_board_groups,
    finalize_strategy_metrics,
    optional_metric,
    strategy_campaign_name,
    require_business_access,
    _lingxing_mcp_result,
    _lingxing_mcp_metadata_cache,
    _amazon_cache,
    _performance_data_quality,
    _reset_lingxing_mcp_metadata_cache,
    _strategy_campaign_data_quality,
    ad_report_type,
    fetch_mcp_product_performance,
    fetch_product_performance,
    fetch_mcp_campaign_report,
    lingxing_mcp_call,
    product_performance_typed_clicks_present,
    validate_strategy_series,
)


def mcp_response(business: dict):
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test/mcp"),
        json={"result": {"content": [{"type": "text", "text": json.dumps(business)}]}},
    )


def mcp_metadata_responses(tool_id: str, catalog_version: str = "20260915"):
    tool = {
        "toolId": tool_id,
        "schemaVersion": "1.0.1",
        "toolVersionId": 37,
    }
    return [
        mcp_response({"success": True, "data": {"catalogVersion": catalog_version}}),
        mcp_response({"success": True, "data": {"catalogVersion": catalog_version, "tools": [tool]}}),
    ]


def mcp_metadata_responses_without_versions(tool_id: str):
    """Reproduce the 2026-09-17 LingXing catalog response contract."""

    return [
        mcp_response({
            "success": True,
            "data": {
                "usage": "help -> search -> action",
                "total": 1,
                "tools": [{"toolId": tool_id, "displayName": tool_id}],
            },
        }),
        mcp_response({
            "success": True,
            "data": {
                "toolId": tool_id,
                "displayName": tool_id,
                "toolType": "read",
                "inputSchema": {"type": "object", "properties": {}},
            },
        }),
    ]


def reset_mcp_test_state():
    _reset_lingxing_mcp_metadata_cache()
    _amazon_cache.pop(("lingxing-mcp-ad-shops",), None)


class AmazonDashboardPeriodTests(unittest.TestCase):
    def setUp(self):
        reset_mcp_test_state()

    def tearDown(self):
        reset_mcp_test_state()

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
        action_response = mcp_response({
            "success": True,
            "data": {"list": [{
                "asin": "B0TEST",
                "clicks": 4226,
                "ad_clicks_sp": 3239,
                "ad_clicks_sb": 417,
                "ad_clicks_sbv": 100,
                "ad_clicks_sd": 470,
            }]},
        })
        client = AsyncMock()
        client.post.side_effect = [*mcp_metadata_responses("query_product_performance_asin_lists"), action_response]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            reset_mcp_test_state()
            rows = asyncio.run(fetch_mcp_product_performance(14292, date(2026, 9, 7), date(2026, 9, 13), client))
            reset_mcp_test_state()
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sp"]["clicks"], 3239)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sb"]["clicks"], 417)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sbv"]["clicks"], 100)
        self.assertEqual(product_performance_ad_breakdown(rows[0])["sd"]["clicks"], 470)
        self.assertTrue(product_performance_typed_clicks_present(rows[0]))
        self.assertEqual(client.post.call_count, 3)

    def test_mcp_campaign_report_resolves_profile_and_keeps_real_name(self):
        shops_response = mcp_response({
            "success": True,
            "data": [{"sid": 14292, "profile_id": "2815938091388375"}],
        })
        campaign_response = mcp_response({
            "success": True,
            "data": {
                "recordsFiltered": 1,
                "data": [{"campaign_id": "123", "name": "真实活动名称", "ads_type": "SP"}],
            },
        })
        client = AsyncMock()
        client.post.side_effect = [
            *mcp_metadata_responses("ad_auth_shops"),
            shops_response,
            *mcp_metadata_responses("ad_campaign_report"),
            campaign_response,
        ]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            reset_mcp_test_state()
            result = asyncio.run(fetch_mcp_campaign_report(14292, date(2026, 9, 7), date(2026, 9, 13), client))
            reset_mcp_test_state()
        self.assertEqual(result.rows[0]["name"], "真实活动名称")
        self.assertEqual(result.rows[0]["campaign_id"], "123")
        self.assertEqual(result.expected, 1)
        self.assertEqual(result.pages, 1)
        self.assertTrue(result.complete)
        self.assertEqual(client.post.call_count, 6)

    def test_mcp_catalog_update_refreshes_metadata_and_retries_once(self):
        error_response = mcp_response({"success": False, "msg": "Catalog已更新，请重新查询工具版本"})
        success_response = mcp_response({"success": True, "data": {"value": 42}})
        client = AsyncMock()
        client.post.side_effect = [
            *mcp_metadata_responses("ad_campaign_report", "20260914"),
            error_response,
            *mcp_metadata_responses("ad_campaign_report", "20260915"),
            success_response,
        ]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            reset_mcp_test_state()
            result = asyncio.run(lingxing_mcp_call("ad_campaign_report", {"page": 1}, client))
            reset_mcp_test_state()
        self.assertEqual(result, {"value": 42})
        self.assertEqual(client.post.call_count, 6)
        retry_arguments = client.post.call_args_list[-1].kwargs["json"]["params"]["arguments"]
        self.assertEqual(retry_arguments["catalogVersion"], "20260915")

    def test_mcp_version_fallback_survives_catalog_without_version_fields(self):
        action_response = mcp_response({"success": True, "data": {"value": 42}})
        client = AsyncMock()
        client.post.side_effect = [
            *mcp_metadata_responses_without_versions("ad_auth_shops"),
            action_response,
        ]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            reset_mcp_test_state()
            result = asyncio.run(lingxing_mcp_call("ad_auth_shops", {}, client))
            reset_mcp_test_state()
        self.assertEqual(result, {"value": 42})
        self.assertEqual(client.post.call_count, 3)
        action_arguments = client.post.call_args_list[-1].kwargs["json"]["params"]["arguments"]
        self.assertEqual(action_arguments["catalogVersion"], "lingxing-mcp-20260915-v1")
        self.assertEqual(action_arguments["schemaVersion"], "ad_auth_shops-v1-c500-20260907")
        self.assertEqual(action_arguments["toolVersionId"], 199)

    def test_mcp_campaign_report_paginates_to_records_filtered(self):
        shops_response = mcp_response({"success": True, "data": [{"sid": 14292, "profile_id": "profile-1"}]})
        batches = []
        for page in range(3):
            start = page * 100 + 1
            count = 100 if page < 2 else 8
            rows = [{"campaign_id": str(index), "campaign_name": f"Campaign {index}", "sponsored_type": "SP"} for index in range(start, start + count)]
            if page == 0:
                rows.insert(0, {"clicks": 999999})
            batches.append(mcp_response({"success": True, "data": {"recordsFiltered": 208, "data": rows}}))
        client = AsyncMock()
        client.post.side_effect = [
            *mcp_metadata_responses("ad_auth_shops"),
            shops_response,
            *mcp_metadata_responses("ad_campaign_report"),
            *batches,
        ]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            _reset_lingxing_mcp_metadata_cache()
            result = asyncio.run(fetch_mcp_campaign_report(14292, date(2026, 9, 7), date(2026, 9, 13), client))
            _reset_lingxing_mcp_metadata_cache()
        self.assertEqual(len(result.rows), 208)
        self.assertEqual(result.expected, 208)
        self.assertEqual(result.pages, 3)
        self.assertTrue(result.complete)

    def test_mcp_campaign_short_final_page_is_complete_without_total_count(self):
        shops_response = mcp_response({"success": True, "data": [{"sid": 14292, "profile_id": "profile-1"}]})
        first_rows = [{"campaign_id": str(index), "campaign_name": f"Campaign {index}", "sponsored_type": "SP"} for index in range(1, 101)]
        first_rows.insert(0, {"clicks": 999999})
        second_rows = [{"campaign_id": str(index), "campaign_name": f"Campaign {index}", "sponsored_type": "SP"} for index in range(101, 108)]
        batches = [
            mcp_response({"success": True, "data": {"data": first_rows}}),
            mcp_response({"success": True, "data": {"data": second_rows}}),
        ]
        client = AsyncMock()
        client.post.side_effect = [
            *mcp_metadata_responses("ad_auth_shops"),
            shops_response,
            *mcp_metadata_responses("ad_campaign_report"),
            *batches,
        ]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            _reset_lingxing_mcp_metadata_cache()
            result = asyncio.run(fetch_mcp_campaign_report(14292, date(2026, 9, 7), date(2026, 9, 13), client))
            _reset_lingxing_mcp_metadata_cache()
        self.assertEqual(len(result.rows), 107)
        self.assertIsNone(result.expected)
        self.assertEqual(result.pages, 2)
        self.assertTrue(result.complete)
        self.assertEqual(result.error, "")

    def test_mcp_campaign_profile_missing_is_explicitly_incomplete(self):
        shops_response = mcp_response({"success": True, "data": [{"sid": 99999, "profile_id": "other"}]})
        client = AsyncMock()
        client.post.side_effect = [*mcp_metadata_responses("ad_auth_shops"), shops_response]
        with patch.dict(os.environ, {"LINGXING_MCP_KEY": "test-key"}):
            _reset_lingxing_mcp_metadata_cache()
            result = asyncio.run(fetch_mcp_campaign_report(14292, date(2026, 9, 7), date(2026, 9, 13), client))
            _reset_lingxing_mcp_metadata_cache()
        self.assertFalse(result.profile_found)
        self.assertFalse(result.complete)
        self.assertIn("profile_id", result.error)

    def test_sb_video_is_classified_before_generic_sb(self):
        self.assertEqual(ad_report_type({"sponsored_type": "SB2", "creative_type": "VIDEO"}), "SBV")
        self.assertEqual(ad_report_type({"sponsored_type": "HSA", "creative_type": "video"}), "SBV")
        self.assertEqual(ad_report_type({"sponsored_type": "SB2", "creative_type": "PRODUCT_COLLECTION"}), "SB")
        self.assertEqual(ad_report_type({"sponsored_type": "SB2", "campaign_name": "US-TN10-C-SBV-MK-E"}), "SBV")
        self.assertEqual(ad_report_type({"sponsored_type": "SB2", "campaign_name": "US-TN10-C-SB-MK-E"}), "SB")

    def test_strategy_data_quality_reports_counts_and_type_presence(self):
        raw_rows = [
            {"sponsored_type": "SP"},
            {"sponsored_type": "SB2", "creative_type": "VIDEO"},
            {"sponsored_type": "SB2", "creative_type": "PRODUCT"},
            {"sponsored_type": "SD"},
        ]
        fetched = [(
            "美国", "US", raw_rows,
            [{
                "source": "mcp", "mcp_ok": True, "campaign_pages": 3,
                "campaign_rows": 4, "campaign_expected": 4,
                "profile_found": True, "complete": True, "errors": [],
            }],
        )]
        quality = _strategy_campaign_data_quality(fetched)
        self.assertTrue(quality["complete"])
        self.assertTrue(quality["types_present"])
        self.assertEqual(quality["type_counts"], {"SP": 1, "SB": 1, "SBV": 1, "SD": 1})
        self.assertEqual(quality["campaign_rows"], 4)
        self.assertEqual(quality["campaign_expected"], 4)

    def test_strategy_data_quality_rejects_and_does_not_cache_untyped_campaigns(self):
        fetched = [(
            "美国", "US",
            [{"sponsored_type": "SP"}, {"campaign_id": "123", "name": "SB campaign"}],
            [{
                "source": "mcp", "mcp_ok": True, "campaign_pages": 1,
                "campaign_rows": 2, "campaign_expected": 2,
                "profile_found": True, "complete": True, "errors": [],
            }],
        )]
        quality = _strategy_campaign_data_quality(fetched)
        self.assertFalse(quality["complete"])
        self.assertFalse(quality["campaign_inventory_complete"])
        self.assertFalse(quality["cacheable"])
        self.assertIn("no recognizable ad type", quality["errors"][0])

    def test_openapi_product_fallback_is_never_marked_complete(self):
        quality = {
            "sources": {"openapi_fallback"}, "mcp_ok": False,
            "raw_rows": 12, "typed_rows": 12, "errors": ["MCP unavailable"],
        }
        result = _performance_data_quality(quality)
        self.assertEqual(result["source"], "openapi_fallback")
        self.assertFalse(result["mcp_ok"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["typed_clicks_present"])

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

    def test_sales_scope_merges_current_and_future_tn20_variants(self):
        products, series = amazon_sales_scope("TN20")
        self.assertIn("TN20-主链接-黑色", products)
        self.assertIn("TN20-小链接-樱桃红", products)
        self.assertIn("TN20系列（主链接）汇总", series)
        self.assertIn("TN20系列（小链接）汇总", series)

    def test_sales_completion_uses_requested_business_rules(self):
        self.assertEqual(amazon_sales_completion("units", 100, 90)["status"], "green")
        self.assertEqual(amazon_sales_completion("units", 100, 110)["status"], "red")
        self.assertEqual(amazon_sales_completion("units", 100, 100)["status"], "gray")
        self.assertEqual(amazon_sales_completion("cpc", 1, 0.8)["status"], "red")
        self.assertEqual(amazon_sales_completion("cpc", 1, 1.2)["status"], "green")
        self.assertEqual(amazon_sales_completion("cpc", 1, 1)["status"], "gray")
        self.assertEqual(amazon_sales_completion("ad_units", 100, 110)["status"], "red")
        self.assertEqual(amazon_sales_completion("ad_units", 100, 90)["status"], "green")
        self.assertEqual(amazon_sales_completion("ad_units", 100, 100)["status"], "gray")
        self.assertEqual(amazon_sales_completion("units", None, 90), {"value": None, "status": ""})
        self.assertEqual(amazon_sales_completion("units", 100, None), {"value": None, "status": ""})

    def test_sales_target_number_validates_nonnegative_finite_values(self):
        self.assertIsNone(amazon_sales_target_number(None))
        self.assertIsNone(amazon_sales_target_number(""))
        self.assertEqual(amazon_sales_target_number("12.5"), 12.5)

    def test_sales_targets_only_accept_the_five_manual_inputs(self):
        self.assertEqual(AMAZON_SALES_TARGET_FIELDS, ("units", "aov", "cpc", "ad_sales_share", "ad_cvr"))
        for invalid in (-1, "abc", float("inf")):
            with self.assertRaises(ValueError):
                amazon_sales_target_number(invalid)

    def test_sales_target_number_accepts_percent_strings(self):
        self.assertEqual(amazon_sales_target_number("10%"), 0.1)
        self.assertEqual(amazon_sales_target_number("20 %"), 0.2)
        self.assertEqual(amazon_sales_target_number("12.5"), 12.5)

    def test_sales_targets_are_numeric_after_database_round_trip(self):
        item = SimpleNamespace(
            **{f"target_{key}": Decimal("10") if key == "units" else None
               for key in AMAZON_SALES_TARGET_FIELDS}
        )
        values = amazon_sales_target_values(item)
        self.assertIsInstance(values["units"], float)
        self.assertEqual(values["units"], 10.0)

    def test_sales_dashboard_site_scope_is_validated(self):
        self.assertEqual(amazon_sales_selected_sites("美国"), ["美国"])
        self.assertEqual(amazon_sales_selected_sites(AMAZON_SALES_ALL_SITES), list(AMAZON_SITE_CODES))
        with self.assertRaisesRegex(ValueError, "站点无效"):
            amazon_sales_selected_sites("火星")

    def test_sales_dashboard_all_model_scope_includes_every_product(self):
        products, series = amazon_sales_scope(AMAZON_SALES_ALL_MODEL)
        self.assertEqual(products, set(AMAZON_PRODUCTS))
        self.assertIn(AMAZON_SERIES[0], series)
        self.assertIn(AMAZON_SALES_TN20_SMALL_SERIES, series)

    def test_sales_dashboard_currency_follows_site_scope(self):
        self.assertEqual(amazon_sales_currency(amazon_sales_selected_sites("全部站点")), "USD")
        self.assertEqual(amazon_sales_currency(amazon_sales_selected_sites("美国")), "USD")
        self.assertEqual(amazon_sales_currency(amazon_sales_selected_sites("德国")), "EUR")
        self.assertEqual(amazon_sales_currency(amazon_sales_selected_sites("日本")), "JPY")

    def test_sales_actuals_use_campaign_report_money(self):
        series = AMAZON_SERIES[0]
        rows = [
            {"site": "美国", "series": series, "product": "TN10-主链接-黑色", "ad_cost": 300000.0, "clicks": 9000},
            {"site": "美国", "series": series, "product": "TN10-主链接-银色", "ad_cost": 87034.42, "clicks": 3000},
        ]
        result = amazon_sales_apply_campaign_ad_cost(rows, {("美国", series): 54982.14})
        self.assertAlmostEqual(amazon_sales_actuals(result)["ad_cost"], 54982.14)
        self.assertAlmostEqual(amazon_sales_actuals(result)["cpc"], 54982.14 / 12000)
        self.assertTrue(all(row["ad_cost_source"] == "campaign_report" for row in result))

    def test_sales_campaign_money_converts_all_site_native_currency(self):
        async def strategy_payload(*args, **kwargs):
            return {
                "data_quality": {"complete": True, "errors": []},
                "strategies": [
                    {"site": "美国", "series": AMAZON_SERIES[0], "currency": "USD", "metrics": {"ad_cost": 100}},
                    {"site": "德国", "series": AMAZON_SERIES[0], "currency": "EUR", "metrics": {"ad_cost": 100}},
                ],
            }

        async def exchange_rates(*args, **kwargs):
            return {"USD": 1.0, "EUR": 1.15}

        with patch("app.amazon_strategy_board_payload", new=AsyncMock(side_effect=strategy_payload)), \
             patch("app.amazon_usd_exchange_rates", new=AsyncMock(side_effect=exchange_rates)):
            totals, quality = asyncio.run(amazon_sales_campaign_cost_by_series(
                date(2026, 9, 1),
                date(2026, 9, 17),
                ["美国", "德国"],
                {AMAZON_SERIES[0]},
                {},
                [],
                "USD",
                False,
            ))
        self.assertAlmostEqual(totals[("美国", AMAZON_SERIES[0])], 100)
        self.assertAlmostEqual(totals[("德国", AMAZON_SERIES[0])], 115)
        self.assertTrue(quality["complete"])

    def test_sales_actual_rows_replace_product_performance_money(self):
        series = AMAZON_SERIES[0]
        periodic = {
            "rows": [{
                "site": "美国", "series": series, "product": "TN10-主链接-黑色",
                "units": 100, "net_sales": 20000, "clicks": 12000,
                "ad_cost": 387034.42, "ad_units": 300, "ad_orders": 240,
            }],
            "data_quality": {"source": "mcp", "complete": True, "errors": []},
        }

        async def strategy_payload(*args, **kwargs):
            return {
                "data_quality": {"complete": True, "errors": []},
                "strategies": [{
                    "site": "美国", "series": series, "currency": "USD",
                    "metrics": {"ad_cost": 54982.14},
                }],
            }

        env = {
            "LINGXING_APP_ID": "test-id",
            "LINGXING_APP_SECRET": "test-secret",
            "LINGXING_SIDS_JSON": json.dumps({"US": {"sid": 101}}),
        }
        with patch.dict(os.environ, env), \
             patch("app.lingxing_store_rows", new=AsyncMock(return_value=[])), \
             patch("app.amazon_dashboard_periodic", new=AsyncMock(return_value=periodic)), \
             patch("app.amazon_strategy_board_payload", new=AsyncMock(side_effect=strategy_payload)):
            rows, quality = asyncio.run(amazon_sales_actual_rows(
                date(2026, 9, 1), date(2026, 9, 17), "月", ["美国"],
                {series}, {"TN10-主链接-黑色"}, False,
            ))
        actuals = amazon_sales_actuals(rows)
        self.assertAlmostEqual(actuals["ad_cost"], 54982.14)
        self.assertAlmostEqual(actuals["cpc"], 54982.14 / 12000)
        self.assertEqual(actuals["units"], 100)
        self.assertEqual(actuals["clicks"], 12000)
        self.assertEqual(quality["ad_money_source"], "campaign_report")

    def test_sales_campaign_money_fails_closed_when_inventory_incomplete(self):
        async def strategy_payload(*args, **kwargs):
            return {"data_quality": {"complete": False, "errors": ["missing SB"]}, "strategies": []}

        with patch("app.amazon_strategy_board_payload", new=AsyncMock(side_effect=strategy_payload)):
            with self.assertRaisesRegex(RuntimeError, "广告活动金额来源不完整"):
                asyncio.run(amazon_sales_campaign_cost_by_series(
                    date(2026, 9, 1), date(2026, 9, 17), ["美国"],
                    {AMAZON_SERIES[0]}, {}, [], "original", False,
                ))

    def test_monthly_target_migration_adds_site_without_losing_legacy_rows(self):
        database = create_engine("sqlite:///:memory:")
        with database.begin() as connection:
            connection.execute(text("""
                CREATE TABLE amazon_monthly_targets (
                    id INTEGER PRIMARY KEY,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    model VARCHAR(16) NOT NULL,
                    target_units NUMERIC(18, 4),
                    UNIQUE (year, month, model)
                )
            """))
            connection.execute(text(
                "INSERT INTO amazon_monthly_targets (year, month, model, target_units) VALUES (2026, 9, 'TN10', 100)"
            ))

        migrate_amazon_monthly_targets(database)

        inspector = inspect(database)
        columns = {column["name"] for column in inspector.get_columns(AmazonMonthlyTarget.__tablename__)}
        indexes = inspector.get_indexes(AmazonMonthlyTarget.__tablename__)
        with database.connect() as connection:
            row = connection.execute(text(
                "SELECT year, month, model, site, target_units FROM amazon_monthly_targets"
            )).one()
        self.assertIn("site", columns)
        self.assertIn("target_aov", columns)
        self.assertEqual(row.site, AMAZON_SALES_ALL_SITES)
        self.assertEqual(row.target_units, 100)
        self.assertTrue(any(index["name"] == "uq_amazon_monthly_target_scope_site" and index["unique"] for index in indexes))
        self.assertFalse(any(index["unique"] and set(index["column_names"] or []) == {"year", "month", "model"} for index in indexes))

    def test_monthly_targets_can_store_the_same_scope_for_different_sites(self):
        database = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(database, tables=[AmazonMonthlyTarget.__table__])
        migrate_amazon_monthly_targets(database)
        from sqlalchemy.orm import Session
        with Session(database) as db:
            timestamp = datetime(2026, 9, 17, tzinfo=timezone.utc)
            db.add_all([
                AmazonMonthlyTarget(year=2026, month=9, model="TN10", site=AMAZON_SALES_ALL_SITES, target_units=100, updated_at=timestamp),
                AmazonMonthlyTarget(year=2026, month=9, model="TN10", site="美国", target_units=30, updated_at=timestamp),
            ])
            db.commit()
            count = db.execute(text("SELECT COUNT(*) FROM amazon_monthly_targets")).scalar_one()
        self.assertEqual(count, 2)

    def test_weekly_targets_can_store_the_same_week_for_different_sites(self):
        database = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(database, tables=[AmazonWeeklyTarget.__table__])
        from sqlalchemy.orm import Session
        with Session(database) as db:
            timestamp = datetime(2026, 9, 17, tzinfo=timezone.utc)
            week = date(2026, 9, 14)
            db.add_all([
                AmazonWeeklyTarget(week_start=week, model="TN10", site=AMAZON_SALES_ALL_SITES, target_units=100, updated_at=timestamp),
                AmazonWeeklyTarget(week_start=week, model="TN10", site="美国", target_units=30, updated_at=timestamp),
                AmazonWeeklyTarget(week_start=week + timedelta(days=7), model="TN10", site="美国", target_units=35, updated_at=timestamp),
            ])
            db.commit()
            count = db.execute(text("SELECT COUNT(*) FROM amazon_weekly_targets")).scalar_one()
        self.assertEqual(count, 3)

    def test_weekly_target_values_read_only_manual_inputs(self):
        item = SimpleNamespace(
            week_start=date(2026, 9, 14),
            **{f"target_{key}": Decimal("10") if key == "units" else None
               for key in AMAZON_SALES_TARGET_FIELDS},
        )
        values = amazon_sales_target_values(item)
        self.assertEqual(set(values), set(AMAZON_SALES_TARGET_FIELDS))
        self.assertEqual(values["units"], 10.0)

    def test_sales_week_time_progress_uses_elapsed_week_days(self):
        week = date(2026, 9, 14)
        week_end = date(2026, 9, 20)
        self.assertEqual(amazon_sales_week_time_progress(week, week_end, date(2026, 9, 13)), 0)
        self.assertAlmostEqual(amazon_sales_week_time_progress(week, week_end, date(2026, 9, 14)), 1 / 7)
        self.assertAlmostEqual(amazon_sales_week_time_progress(week, week_end, date(2026, 9, 17)), 4 / 7)
        self.assertEqual(amazon_sales_week_time_progress(week, week_end, date(2026, 9, 21)), 1)

    def test_week_start_is_normalized_to_monday(self):
        self.assertEqual(normalize_week_start(date(2026, 9, 17)), date(2026, 9, 14))
        self.assertEqual(normalize_week_start(date(2026, 9, 20)), date(2026, 9, 14))

    def test_sales_actuals_recalculate_ratios_from_totals(self):
        rows = [
            {"units": 10, "net_sales": 200, "clicks": 20, "ad_cost": 10, "ad_units": 4, "ad_orders": 3},
            {"units": 15, "net_sales": 300, "clicks": 30, "ad_cost": 30, "ad_units": 6, "ad_orders": 7},
        ]
        actuals = amazon_sales_actuals(rows)
        self.assertEqual(actuals["units"], 25)
        self.assertAlmostEqual(actuals["aov"], 500 / 25)
        self.assertEqual(actuals["net_sales"], 500)
        self.assertEqual(actuals["clicks"], 50)
        self.assertAlmostEqual(actuals["cpc"], 40 / 50)
        self.assertAlmostEqual(actuals["ad_sales_share"], 10 / 25)
        self.assertAlmostEqual(actuals["acoas"], 40 / 500)
        self.assertAlmostEqual(actuals["ad_cvr"], 10 / 50)

    def test_sales_derived_targets_follow_requested_formulas(self):
        targets = {
            "units": 100,
            "aov": 80,
            "cpc": 0.5,
            "ad_sales_share": 0.25,
            "ad_cvr": 0.05,
        }
        derived = amazon_sales_derived_targets(targets)
        self.assertEqual(derived["net_sales"], 8000)
        self.assertEqual(derived["clicks"], 500)
        self.assertEqual(derived["ad_units"], 25)
        self.assertEqual(derived["ad_cost"], 250)
        self.assertAlmostEqual(derived["acoas"], 250 / 8000)

    def test_sales_metric_rows_use_manual_and_derived_targets(self):
        rows = amazon_sales_metric_rows(
            {"units": 100, "aov": 80, "cpc": 0.5, "ad_sales_share": 0.25, "ad_cvr": 0.05},
            {"units": 90, "aov": 70, "net_sales": 6300, "cpc": 0.4, "clicks": 400},
        )
        by_key = {row["key"]: row for row in rows}
        self.assertEqual(by_key["aov"]["target"], 80)
        self.assertEqual(by_key["net_sales"]["target"], 8000)
        self.assertEqual(by_key["clicks"]["label"], "广告点击")
        self.assertEqual(by_key["clicks"]["target"], 500)
        self.assertEqual(by_key["ad_cvr"]["label"], "广告CVR")
        self.assertEqual(by_key["ad_units"]["target"], 25)
        self.assertEqual(by_key["ad_cost"]["target"], 250)
        self.assertAlmostEqual(by_key["acoas"]["target"], 250 / 8000)

    def test_sales_metric_rows_include_target_actual_and_completion(self):
        rows = amazon_sales_metric_rows(
            {"units": 100, "cpc": 1},
            {"units": 90, "cpc": 0.8},
        )
        units = next(row for row in rows if row["key"] == "units")
        cpc = next(row for row in rows if row["key"] == "cpc")
        self.assertEqual((units["target"], units["actual"], units["completion"]["value"]), (100, 90, 0.9))
        self.assertEqual((cpc["target"], cpc["actual"]), (1, 0.8))
        self.assertAlmostEqual(cpc["completion"]["value"], -0.2)

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
        self.assertEqual(
            optional_metric({"pageViewsTotal": "18"}, *AMAZON_SOURCE_FIELDS["performance"]["page_views"]),
            18.0,
        )

    def test_product_performance_cache_hit_does_not_reference_rate_limit_state(self):
        start = date(2026, 9, 1)
        end = date(2026, 9, 7)
        cache_key = (
            "product-performance-v3", 101, start.isoformat(), end.isoformat(), ("B0TEST",), "USD"
        )
        cached_rows = [{"asin": "B0TEST", "volume": 1}]
        _amazon_cache[cache_key] = (time.monotonic(), cached_rows)
        quality = {}

        async def call_cached_product_performance():
            async with httpx.AsyncClient() as client:
                return await fetch_product_performance(
                    101,
                    start,
                    end,
                    "周",
                    client,
                    asyncio.Semaphore(1),
                    ["B0TEST"],
                    "USD",
                    quality,
                )

        try:
            with patch("app.lingxing_mcp_key", return_value=""):
                rows = asyncio.run(call_cached_product_performance())
        finally:
            _amazon_cache.pop(cache_key, None)

        self.assertEqual(rows, cached_rows)
        self.assertEqual(quality["raw_rows"], 1)
        self.assertEqual(quality["errors"], [])

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
            "pageViewsTotal": 120,
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
        self.assertEqual(row["sessions"], 50)
        self.assertEqual(row["page_views"], 120)
        self.assertEqual(row["asin"], asin)
        self.assertEqual(result["mapping"]["sources"], AMAZON_METRIC_SOURCES)

    def test_ads_chart_rows_recalculate_ratios_from_period_totals(self):
        periodic = {
            "rows": [
                {
                    "period": "2026-09-07~2026-09-13",
                    "period_start": "2026-09-07",
                    "period_end": "2026-09-13",
                    "net_sales": 100,
                    "ad_sales": 30,
                    "ad_cost": 10,
                    "clicks": 50,
                    "ad_orders": 2,
                    "sessions": 80,
                    "page_views": 200,
                },
                {
                    "period": "2026-09-07~2026-09-13",
                    "period_start": "2026-09-07",
                    "period_end": "2026-09-13",
                    "net_sales": 100,
                    "ad_sales": 20,
                    "ad_cost": 10,
                    "clicks": 50,
                    "ad_orders": 3,
                    "sessions": 70,
                    "page_views": 220,
                },
            ]
        }
        row = amazon_ads_chart_rows(periodic)[0]
        self.assertEqual(row["net_sales"], 200)
        self.assertEqual(row["ad_sales"], 50)
        self.assertEqual(row["ad_cost"], 20)
        self.assertAlmostEqual(row["fee_ratio"], 0.1)
        self.assertEqual(row["clicks"], 100)
        self.assertEqual(row["ad_orders"], 5)
        self.assertAlmostEqual(row["ad_cvr"], 0.05)
        self.assertEqual(row["sessions"], 150)
        self.assertEqual(row["page_views"], 420)

    def test_ads_chart_rows_use_backend_iso_week_labels(self):
        rows = amazon_ads_chart_rows({
            "rows": [
                {
                    "period": "2025-12-29~2026-01-04",
                    "period_start": "2025-12-29",
                    "period_end": "2026-01-04",
                    "net_sales": 100,
                },
                {
                    "period": "2026-09-07~2026-09-13",
                    "period_start": "2026-09-07",
                    "period_end": "2026-09-13",
                    "net_sales": 200,
                },
            ]
        })
        self.assertEqual([row["week_label"] for row in rows], ["W01", "W37"])
        self.assertEqual(amazon_week_label("2026-09-14"), "W38")

    def test_ads_chart_keeps_missing_pv_explicitly_unavailable(self):
        row = amazon_ads_chart_rows({
            "rows": [{
                "period": "2026-09-07~2026-09-13",
                "period_start": "2026-09-07",
                "period_end": "2026-09-13",
                "sessions": 20,
            }]
        })[0]
        self.assertEqual(row["sessions"], 20)
        self.assertIsNone(row["page_views"])

    def test_ads_charts_endpoint_reports_explicit_field_availability(self):
        periodic = {
            "data_quality": {"source": "mcp", "complete": True, "errors": []},
            "rows": [{
                "period": "2026-09-07~2026-09-13",
                "period_start": "2026-09-07",
                "period_end": "2026-09-13",
                "net_sales": 100,
                "ad_sales": 25,
                "ad_cost": 5,
                "clicks": 20,
                "ad_orders": 1,
                "sessions": 30,
                "page_views": None,
            }],
        }

        env = {
            "LINGXING_APP_ID": "test-id",
            "LINGXING_APP_SECRET": "test-secret",
            "LINGXING_SIDS_JSON": json.dumps({"US": {"sid": 101}}),
        }
        periodic_mock = AsyncMock(return_value=periodic)
        with patch.dict(os.environ, env), patch("app.lingxing_store_rows", new=AsyncMock(return_value=[])), patch("app.amazon_dashboard_periodic", new=periodic_mock):
            result = asyncio.run(amazon_ads_charts(
                date(2026, 9, 7),
                date(2026, 9, 7),
                "全部站点",
                AMAZON_SALES_ALL_MODEL,
                False,
            ))
        self.assertEqual(result["model"], AMAZON_SALES_ALL_MODEL)
        self.assertEqual(periodic_mock.call_args.args[5], set(AMAZON_PRODUCTS))
        self.assertEqual(periodic_mock.call_args.args[4], set(AMAZON_SERIES) | {AMAZON_SALES_TN20_SMALL_SERIES})
        self.assertEqual(result["currency"], "USD")
        self.assertTrue(result["field_availability"]["sessions_present"])
        self.assertFalse(result["field_availability"]["page_views_present"])
        self.assertAlmostEqual(result["rows"][0]["fee_ratio"], 0.05)

    def test_product_performance_ad_metrics_sum_sp_sb_sbv_and_sd(self):
        raw = {
            # Deliberately leave the generic count fields at SP-only values.
            # Counts use the typed product-performance dimensions, while money
            # stays on the requested-currency generic fields.
            "clicks": 10,
            "spend": 10,
            "ad_sales_amount": 20,
            "ads_sales_volume_quantity": 2,
            "ad_order_quantity": 1,
            "ad_clicks_sp": 10,
            "ad_clicks_sb": 3,
            "ad_clicks_sbv": 2,
            "ad_clicks_sd": 1,
            "ads_sp_cost": 10,
            "shared_ads_sb_cost": 3,
            "shared_ads_sbv_cost": 2,
            "ads_sd_cost": 1,
            "ads_sp_sales": 20,
            "shared_ads_sb_sales": 6,
            "shared_ads_sbv_sales": 4,
            "ads_sd_sales": 2,
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
        self.assertNotIn("ad_cost", product_performance_ad_totals(raw))
        self.assertNotIn("ad_sales", product_performance_ad_totals(raw))
        self.assertEqual(product_performance_ad_totals(raw)["ad_units"], 5)
        self.assertEqual(product_performance_ad_totals(raw)["ad_orders"], 4)
        breakdown = product_performance_ad_breakdown(raw)
        self.assertEqual(breakdown["sb"]["clicks"], 3)
        self.assertEqual(breakdown["sbv"]["ad_units"], 1)
        self.assertEqual(breakdown["sd"]["ad_orders"], 1)
        self.assertIsNone(breakdown["sp"]["ad_cost"])
        self.assertIsNone(breakdown["sb"]["ad_sales"])

    def test_dashboard_uses_generic_money_but_typed_ad_counts(self):
        site_name = next(iter(AMAZON_SITE_CODES))
        asin = next(iter(ASIN_MAPPING["US"]))
        product = ASIN_MAPPING["US"][asin]
        series = amazon_series(product)
        performance = [{
            "asin": asin,
            "clicks": 10,
            "spend": 10,
            "ad_sales_amount": 20,
            "ad_clicks_sp": 10,
            "ad_clicks_sb": 3,
            "ad_clicks_sbv": 2,
            "ad_clicks_sd": 1,
            "ads_sp_cost": 10,
            "shared_ads_sb_cost": 3,
            "shared_ads_sbv_cost": 2,
            "ads_sd_cost": 1,
            "ads_sp_sales": 20,
            "shared_ads_sb_sales": 6,
            "shared_ads_sbv_sales": 4,
            "ads_sd_sales": 2,
        }]

        with patch("app.fetch_product_performance", new=AsyncMock(return_value=performance)):
            result = asyncio.run(amazon_dashboard_periodic(
                "日",
                date(2026, 9, 4),
                date(2026, 9, 4),
                site_name,
                {series},
                {product},
                {"US": {"sid": 1}},
            ))

        row = result["rows"][0]
        self.assertEqual(row["clicks"], 16)
        self.assertEqual(row["ad_cost"], 10)
        self.assertEqual(row["ad_sales"], 20)
        self.assertIsNone(row["ad_breakdown"]["sp"]["ad_cost"])
        self.assertIsNone(row["ad_breakdown"]["sb"]["ad_sales"])

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
