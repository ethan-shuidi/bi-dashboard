import asyncio
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app import (
    KeywordDashboardTerm,
    app,
    fetch_xiyou_weekly_records,
    keyword_dashboard_rows,
    keyword_week_columns,
    normalize_keyword_week_start,
    normalize_week_start,
    xiyou_weekly_records,
)


class KeywordDashboardTests(unittest.TestCase):
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

    def test_fetch_xiyou_uses_official_batched_contract_and_server_key(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"entities": []},
        )
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"):
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
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"):
            with self.assertRaisesRegex(RuntimeError, "无效或未授权"):
                asyncio.run(fetch_xiyou_weekly_records(
                    "US",
                    ["power bank"],
                    date(2026, 9, 13),
                    date(2026, 9, 19),
                    client,
                ))

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
        response = client.post("/api/keyword-dashboard/terms", json={
            "site": "美国",
            "terms": [
                {"category": "核心词", "keyword": "Power Bank"},
                {"category": "核心词", "keyword": "power   bank"},
            ],
        })
        self.assertEqual(response.status_code, 422)
        self.assertIn("关键词重复", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
