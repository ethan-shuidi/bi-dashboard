import asyncio
import os
import unittest
from datetime import date, datetime
from datetime import timedelta, timezone
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app import (
    KEYWORD_CATEGORIES,
    KeywordDashboardTerm,
    KeywordDashboardWeeklyRecord,
    XiyouInvalidTrendRangeError,
    app,
    clear_keyword_unavailable_weeks,
    fetch_xiyou_weekly_records,
    fetch_xiyou_weekly_records_with_recovery,
    keyword_dashboard_cross_process_lock,
    keyword_fetch_groups,
    keyword_dashboard_rows,
    keyword_history_index,
    keyword_week_columns,
    latest_completed_keyword_week_start,
    normalize_keyword_week_start,
    normalize_week_start,
    keyword_unavailable_weeks,
    remember_keyword_unavailable_weeks,
    xiyou_weekly_records,
    xiyou_keyword_dashboard_scope,
)


class KeywordDashboardTests(unittest.TestCase):
    @staticmethod
    def lock_engine(connection):
        @contextmanager
        def connect():
            yield connection

        database_engine = MagicMock()
        database_engine.connect.side_effect = connect
        return database_engine

    def test_keyword_fetch_lock_is_nonblocking_and_released(self):
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(scalar=MagicMock(return_value=1)),
            MagicMock(),
        ]

        with patch("app.engine", return_value=self.lock_engine(connection)):
            with keyword_dashboard_cross_process_lock() as acquired:
                self.assertTrue(acquired)

        self.assertEqual(connection.execute.call_count, 2)
        self.assertIn("GET_LOCK", str(connection.execute.call_args_list[0].args[0]))
        self.assertIn("RELEASE_LOCK", str(connection.execute.call_args_list[1].args[0]))

    def test_keyword_fetch_lock_returns_cached_when_already_running(self):
        connection = MagicMock()
        connection.execute.return_value = MagicMock(scalar=MagicMock(return_value=0))

        with patch("app.engine", return_value=self.lock_engine(connection)):
            with keyword_dashboard_cross_process_lock() as acquired:
                self.assertFalse(acquired)

        self.assertEqual(connection.execute.call_count, 1)
        self.assertIn("GET_LOCK", str(connection.execute.call_args.args[0]))

    def test_xiyou_official_nested_response_is_parsed_with_null_gaps(self):
        payload = {
            "entities": [
                {
                    "country": "US",
                    "searchTerm": "  power   bank  ",
                    "trends": [
                        {
                            "reportFromDate": "2026-09-13",
                            "reportToDate": "2026-09-19",
                            "searchFrequencyRank": 12,
                            "weeklySearchVolume": 34500,
                        },
                        {
                            "reportFromDate": "2026-09-27",
                            "reportToDate": "2026-10-03",
                            "searchFrequencyRank": 9,
                            "weeklySearchVolume": 41200,
                        },
                    ],
                },
            ],
        }
        records = xiyou_weekly_records(payload)
        self.assertEqual(records, [
            {"keyword": "power bank", "week_start": date(2026, 9, 13), "rank": 12, "volume": 34500},
            {"keyword": "power bank", "week_start": date(2026, 9, 27), "rank": 9, "volume": 41200},
        ])

        term = KeywordDashboardTerm(
            id=7,
            site_code="US",
            category="核心词",
            keyword="Power Bank",
            sort_order=0,
        )
        rows = keyword_dashboard_rows(
            [term],
            keyword_week_columns(date(2026, 9, 13), date(2026, 9, 27)),
            records,
        )
        self.assertEqual([item["label"] for item in rows[0]["weekly"]], ["W38", "W39", "W40"])
        self.assertEqual(rows[0]["weekly"][1]["search_rank"], None)
        self.assertEqual(rows[0]["weekly"][1]["search_volume"], None)
        self.assertEqual(rows[0]["rank_change"], -3)

    def test_week_start_is_sunday_only_for_keyword_dashboard(self):
        self.assertEqual(normalize_keyword_week_start(date(2026, 9, 14)), date(2026, 9, 13))
        self.assertEqual(normalize_keyword_week_start(date(2026, 9, 19)), date(2026, 9, 13))
        self.assertEqual(normalize_keyword_week_start(date(2026, 9, 20)), date(2026, 9, 20))
        self.assertEqual(normalize_week_start(date(2026, 9, 20)), date(2026, 9, 14))

    def test_latest_completed_keyword_week_excludes_in_progress_week(self):
        self.assertEqual(latest_completed_keyword_week_start(date(2026, 9, 18)), date(2026, 9, 6))
        self.assertEqual(latest_completed_keyword_week_start(date(2026, 9, 20)), date(2026, 9, 13))

    def test_keyword_rows_sort_by_fixed_category_then_latest_rank(self):
        terms = [
            KeywordDashboardTerm(id=1, site_code="US", category="Pocket品牌词", keyword="pocket", sort_order=0),
            KeywordDashboardTerm(id=2, site_code="US", category="comu品牌词", keyword="comu", sort_order=1),
            KeywordDashboardTerm(id=3, site_code="US", category="AI核心词", keyword="ai", sort_order=2),
        ]
        records = [
            {"keyword": "pocket", "week_start": date(2026, 9, 20), "rank": 2, "volume": 100},
            {"keyword": "comu", "week_start": date(2026, 9, 20), "rank": 10, "volume": 200},
            {"keyword": "ai", "week_start": date(2026, 9, 20), "rank": 3, "volume": 300},
        ]
        rows = keyword_dashboard_rows(
            terms,
            keyword_week_columns(date(2026, 9, 20), date(2026, 9, 20)),
            records,
        )
        self.assertEqual([row["keyword"] for row in rows], ["comu", "ai", "pocket"])
        self.assertEqual(KEYWORD_CATEGORIES[0], "comu品牌词")

    def test_keyword_rows_use_latest_present_rank_when_tail_week_is_empty(self):
        term = KeywordDashboardTerm(id=1, site_code="US", category="AI核心词", keyword="power bank", sort_order=0)
        rows = keyword_dashboard_rows(
            [term],
            keyword_week_columns(date(2026, 9, 13), date(2026, 9, 27)),
            [{"keyword": "power bank", "week_start": date(2026, 9, 20), "rank": 7, "volume": 100}],
        )
        self.assertEqual(rows[0]["latest_search_rank"], 7)
        self.assertEqual(rows[0]["rank_change"], None)

    def test_fetch_xiyou_uses_official_batched_contract_and_server_key(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"entities": []},
        )
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"), xiyou_keyword_dashboard_scope():
            result = asyncio.run(fetch_xiyou_weekly_records(
                "US",
                ["power bank", "usb c cable"],
                date(2026, 9, 13),
                date(2026, 10, 4),
                client,
            ))
        self.assertEqual(result, [])
        request = client.post.call_args
        self.assertEqual(request.args[0], "https://example.test/v1/searchTerms/abaReport/trends/weekly")
        self.assertEqual(request.kwargs["headers"], {
            "X-Auth-Version": "2.0",
            "X-Api-Key": "server-only-test-key",
        })
        self.assertEqual(request.kwargs["json"], {
            "country": "US",
            "searchTerms": ["power bank", "usb c cable"],
            "startWeek": {"startDate": "2026-09-13", "endDate": "2026-09-19"},
            "endWeek": {"startDate": "2026-10-04", "endDate": "2026-10-10"},
        })

    def test_fetch_xiyou_turns_401_into_business_error(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            401,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"code": "APICredentialNotFound", "msg": "invalid key"},
        )
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"), xiyou_keyword_dashboard_scope():
            with self.assertRaisesRegex(RuntimeError, "无效或未授权"):
                asyncio.run(fetch_xiyou_weekly_records(
                    "US",
                    ["power bank"],
                    date(2026, 9, 13),
                    date(2026, 9, 19),
                    client,
                ))

    def test_fetch_xiyou_translates_invalid_range_error(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            400,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"code": "InvalidTrendsRange", "msg": "Internal Server Error"},
        )
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"), xiyou_keyword_dashboard_scope():
            with self.assertRaises(XiyouInvalidTrendRangeError):
                asyncio.run(fetch_xiyou_weekly_records(
                    "US",
                    ["power bank"],
                    date(2026, 9, 13),
                    date(2026, 9, 20),
                    client,
                ))

    def test_invalid_multi_week_range_falls_back_to_single_weeks(self):
        client = AsyncMock()
        invalid = httpx.Response(
            400,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"code": "InvalidTrendsRange", "msg": "Internal Server Error"},
        )

        def success(week: date, rank: int) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", "https://example.test/weekly"),
                json={"entities": [{
                    "searchTerm": "power bank",
                    "trends": [{
                        "reportFromDate": week.isoformat(),
                        "searchFrequencyRank": rank,
                        "weeklySearchVolume": rank * 100,
                    }],
                }]},
            )
        client.post.side_effect = [
            invalid,
            success(date(2026, 9, 13), 20),
            invalid,
            success(date(2026, 9, 27), 10),
        ]
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"), xiyou_keyword_dashboard_scope():
            outcome = asyncio.run(fetch_xiyou_weekly_records_with_recovery(
                "US",
                ["power bank"],
                date(2026, 9, 13),
                date(2026, 9, 27),
                client,
            ))
        self.assertEqual(outcome.successful_weeks, (date(2026, 9, 13), date(2026, 9, 27)))
        self.assertEqual(outcome.unavailable_weeks, (date(2026, 9, 20),))
        self.assertEqual(outcome.request_count, 4)
        self.assertEqual([(item["week_start"], item["rank"]) for item in outcome.records], [
            (date(2026, 9, 13), 20),
            (date(2026, 9, 27), 10),
        ])

    def test_single_week_fallback_keeps_successful_weeks_when_one_week_fails(self):
        client = AsyncMock()
        responses = [
            httpx.Response(
                400,
                request=httpx.Request("POST", "https://example.test/weekly"),
                json={"code": "InvalidTrendsRange", "msg": "Internal Server Error"},
            ),
            httpx.Response(
                200,
                request=httpx.Request("POST", "https://example.test/weekly"),
                json={"entities": []},
            ),
            httpx.Response(
                500,
                request=httpx.Request("POST", "https://example.test/weekly"),
                json={"msg": "upstream timeout"},
            ),
            httpx.Response(
                200,
                request=httpx.Request("POST", "https://example.test/weekly"),
                json={"entities": []},
            ),
        ]
        client.post.side_effect = responses
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"), xiyou_keyword_dashboard_scope():
            outcome = asyncio.run(fetch_xiyou_weekly_records_with_recovery(
                "US",
                ["power bank"],
                date(2026, 9, 13),
                date(2026, 9, 27),
                client,
            ))
        self.assertEqual(outcome.successful_weeks, (date(2026, 9, 13), date(2026, 9, 27)))
        self.assertEqual(outcome.unavailable_weeks, ())
        self.assertIn("西柚接口请求失败：HTTP 500 upstream timeout", outcome.errors)

    def test_fetch_xiyou_is_rejected_outside_keyword_dashboard_scope(self):
        client = AsyncMock()
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"):
            with self.assertRaisesRegex(RuntimeError, "仅允许搜索词看板"):
                asyncio.run(fetch_xiyou_weekly_records(
                    "US",
                    ["power bank"],
                    date(2026, 9, 13),
                    date(2026, 9, 19),
                    client,
                ))
        client.post.assert_not_called()

    def test_fetch_groups_reuse_history_and_only_fetch_missing_runs(self):
        weeks = keyword_week_columns(date(2026, 9, 13), date(2026, 9, 27))
        terms = [
            KeywordDashboardTerm(id=1, site_code="US", category="AI核心词", keyword="A", sort_order=0),
            KeywordDashboardTerm(id=2, site_code="US", category="AI核心词", keyword="B", sort_order=1),
        ]
        history = keyword_history_index([
            KeywordDashboardWeeklyRecord(
                site_code="US",
                keyword="A",
                week_start=date(2026, 9, 13),
                fetched_at=datetime(2026, 9, 20),
            ),
            KeywordDashboardWeeklyRecord(
                site_code="US",
                keyword="B",
                week_start=date(2026, 9, 20),
                fetched_at=datetime(2026, 9, 27),
            ),
        ])
        groups = keyword_fetch_groups(terms, weeks, history, refresh=False)
        self.assertEqual(groups, [
            (date(2026, 9, 13), date(2026, 9, 13), ["B"]),
            (date(2026, 9, 20), date(2026, 9, 27), ["A"]),
            (date(2026, 9, 27), date(2026, 9, 27), ["B"]),
        ])

        refreshed = keyword_fetch_groups(terms, weeks, history, refresh=True)
        self.assertEqual(refreshed, [(date(2026, 9, 13), date(2026, 9, 27), ["A", "B"])])

        unavailable = keyword_fetch_groups(
            terms,
            weeks,
            history,
            refresh=False,
            unavailable_weeks={date(2026, 9, 20)},
        )
        self.assertEqual(unavailable, [
            (date(2026, 9, 13), date(2026, 9, 13), ["B"]),
            (date(2026, 9, 27), date(2026, 9, 27), ["A", "B"]),
        ])
        forced_refresh = keyword_fetch_groups(
            terms,
            weeks,
            history,
            refresh=True,
            unavailable_weeks={date(2026, 9, 20)},
        )
        self.assertEqual(forced_refresh, [(date(2026, 9, 13), date(2026, 9, 27), ["A", "B"])])

    def test_unavailable_week_memory_expires_and_refresh_clears_it(self):
        weeks = keyword_week_columns(date(2026, 9, 13), date(2026, 9, 20))
        failed_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        try:
            remember_keyword_unavailable_weeks("US", [date(2026, 9, 20)], now=failed_at)
            self.assertEqual(
                keyword_unavailable_weeks("US", weeks, now=failed_at + timedelta(hours=1)),
                {date(2026, 9, 20)},
            )
            self.assertEqual(
                keyword_unavailable_weeks("US", weeks, now=failed_at + timedelta(hours=6, seconds=1)),
                set(),
            )
            remember_keyword_unavailable_weeks("US", [date(2026, 9, 20)], now=failed_at)
            clear_keyword_unavailable_weeks("US")
            self.assertEqual(keyword_unavailable_weeks("US", weeks, now=failed_at), set())
        finally:
            clear_keyword_unavailable_weeks("US")

    def test_rejects_all_site_and_ranges_over_52_weeks(self):
        client = TestClient(app)
        response = client.get("/api/keyword-dashboard", params={"site": "全部站点"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "关键词看板站点无效")

        response = client.get("/api/keyword-dashboard", params={
            "site": "美国",
            "start_week": "2026-01-05",
            "end_week": "2027-01-11",
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "关键词看板最多支持 52 周")

    def test_rejects_duplicate_keywords_before_database_write(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"SYNC_API_KEY": "test-key"}):
            response = client.post("/api/keyword-dashboard/terms", json={
                "site": "美国",
                "terms": [
                    {"category": "AI核心词", "keyword": "Power Bank"},
                    {"category": "AI核心词", "keyword": "power   bank"},
                ],
            }, headers={"X-Sync-Key": "test-key", "X-Dashboard-Editor": "pytest"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("关键词重复", response.json()["detail"])

    def test_rejects_keyword_category_outside_fixed_options(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"SYNC_API_KEY": "test-key"}):
            response = client.post("/api/keyword-dashboard/terms", json={
                "site": "美国",
                "terms": [{"category": "核心词", "keyword": "Power Bank"}],
            }, headers={"X-Sync-Key": "test-key", "X-Dashboard-Editor": "pytest"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("关键词分类无效", response.json()["detail"])

    def test_write_without_dashboard_key_is_rejected(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"SYNC_API_KEY": "test-key"}):
            response = client.post("/api/keyword-dashboard/terms", json={})
        self.assertEqual(response.status_code, 401)
        self.assertIn("X-Sync-Key", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
