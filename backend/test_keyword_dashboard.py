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
    xiyou_weekly_records,
)


class KeywordDashboardTests(unittest.TestCase):
    def test_xiyou_nested_response_is_parsed_with_null_gaps(self):
        records = xiyou_weekly_records({
            "code": "OK",
            "message": "成功",
            "data": [
                {"date": 1793577600000, "rank": 12, "searches": 34500, "label": 202645},
                {"date": 1790294400000, "rank": 9, "searches": 41200, "label": 202639},
            ],
        }, "  power   bank  ")
        self.assertEqual(records, [
            {"keyword": "power bank", "week_start": date(2026, 11, 2), "rank": 12, "volume": 34500},
            {"keyword": "power bank", "week_start": date(2026, 9, 21), "rank": 9, "volume": 41200},
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
            keyword_week_columns(date(2026, 9, 21), date(2026, 11, 2)),
            records,
        )
        self.assertEqual(rows[0]["weekly"][1]["search_rank"], None)
        self.assertEqual(rows[0]["weekly"][1]["search_volume"], None)
        self.assertEqual(rows[0]["rank_change"], 3)

    def test_fetch_xiyou_uses_server_key_and_turns_401_into_business_error(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            401,
            request=httpx.Request("POST", "https://example.test/weekly"),
            json={"code": "APICredentialNotFound", "msg": "invalid key"},
        )
        with patch.dict(os.environ, {"XIYOU_API_KEY": "server-only-test-key"}), patch("app.XIYOU_API_BASE", "https://example.test"):
            with self.assertRaisesRegex(RuntimeError, "无效或未授权"):
                asyncio.run(fetch_xiyou_weekly_records("US", ["power bank"], date(2026, 9, 7), date(2026, 9, 14), client))
        request = client.post.call_args
        self.assertEqual(request.args[0], "https://example.test/v1/aba/research/trends")
        self.assertEqual(request.kwargs["headers"], {"secret-key": "server-only-test-key"})
        self.assertEqual(request.kwargs["json"], {
            "marketplace": "US",
            "keyword": "power bank",
            "timeGranularity": "W",
        })

    def test_rejects_all_site_and_ranges_over_52_weeks(self):
        client = TestClient(app)
        response = client.get("/api/keyword-dashboard", params={"site": "全部站点"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "关键词看板站点无效")

        response = client.get("/api/keyword-dashboard", params={
            "site": "美国",
            "start_week": "2026-01-05",
            "end_week": "2027-01-04",
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
