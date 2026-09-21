from __future__ import annotations

import ast
import unittest
from pathlib import Path


APP_SOURCE = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
APP_TREE = ast.parse(APP_SOURCE)


def functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in ast.walk(APP_TREE)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def identifiers(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }


class DataSourceIsolationTests(unittest.TestCase):
    def test_xiyou_fetch_has_runtime_keyword_dashboard_guard(self):
        node = functions()["fetch_xiyou_weekly_records"]
        source = ast.unparse(node)
        self.assertIn("_xiyou_keyword_dashboard_scope.get()", source)
        self.assertIn("西柚接口仅允许搜索词看板调用", source)

    def test_http_middleware_enables_xiyou_only_for_keyword_dashboard(self):
        node = functions()["scope_dashboard_request"]
        source = ast.unparse(node)
        self.assertIn("/api/keyword-dashboard", source)
        self.assertIn("xiyou_keyword_dashboard_scope()", source)
        self.assertIn("clear_current_namespace", APP_SOURCE)
        self.assertIn("X-Sync-Key", source)
        self.assertIn("X-Dashboard-Editor", APP_SOURCE)

    def test_amazon_and_lingxing_code_do_not_reference_xiyou(self):
        for name, node in functions().items():
            if not (name.startswith("amazon_") or name.startswith("lingxing_")):
                continue
            self.assertFalse(
                any("xiyou" in identifier.lower() for identifier in identifiers(node)),
                f"{name} 不允许引用西柚数据链路",
            )

    def test_keyword_dashboard_does_not_reference_lingxing(self):
        node = functions()["keyword_dashboard"]
        values = identifiers(node)
        self.assertIn("fetch_xiyou_weekly_records", values)
        self.assertFalse(
            any("lingxing" in identifier.lower() for identifier in values),
            "搜索词看板不允许引用领星数据链路",
        )

    def test_cache_isolates_module_namespaces(self):
        from app import ModuleScopedCache, _amazon_cache_scope

        cache = ModuleScopedCache()
        shared_token = _amazon_cache_scope.set("shared")
        cache[("amazon-usd-fx", "US")] = ("meta", 1)
        _amazon_cache_scope.reset(shared_token)

        first_token = _amazon_cache_scope.set("first")
        cache[("dashboard", "a")] = ("meta", "first")
        cache.clear_current_namespace()
        self.assertIsNone(cache.get(("dashboard", "a")))
        self.assertEqual(cache[("amazon-usd-fx", "US")], ("meta", 1))
        _amazon_cache_scope.reset(first_token)

        second_token = _amazon_cache_scope.set("second")
        cache[("dashboard", "a")] = ("meta", "second")
        cache.clear_namespaces("first")
        self.assertEqual(cache.get(("dashboard", "a")), ("meta", "second"))
        _amazon_cache_scope.reset(second_token)


if __name__ == "__main__":
    unittest.main()
