import unittest

from app import amazon_sales_validate_money_reconciliation


class SalesWeeklyZeroPurchaseTest(unittest.TestCase):
    def test_zero_product_spend_degrades_to_warning(self):
        rows = [
            {
                "site": "澳洲",
                "series": "TN20系列（主链接）汇总",
                "currency": "AUD",
                "ad_cost": 0,
                "clicks": 0,
                "ad_orders": 0,
            }
        ]
        result = amazon_sales_validate_money_reconciliation(
            rows,
            {("澳洲", "TN20系列（主链接）汇总"): 387.12},
        )
        check = result["checks"][0]
        self.assertTrue(check["passed"])
        self.assertEqual(check["status"], "warning")
        self.assertTrue(check["purchase_side_missing"])
        self.assertEqual(len(result["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
