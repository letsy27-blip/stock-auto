import unittest
from unittest.mock import patch

import pandas as pd

import paper_trading


class InvestorProfileTest(unittest.TestCase):
    @patch.object(paper_trading, "is_paper_user_authenticated", return_value=True)
    @patch.object(paper_trading, "is_remote_storage_enabled", return_value=True)
    @patch.object(paper_trading.cloud_paper, "get_events_since")
    def test_supabase_json_object_metadata_is_supported(
        self,
        get_events_since,
        _remote_enabled,
        _authenticated,
    ):
        get_events_since.return_value = pd.DataFrame(
            [
                {
                    "occurred_at": "2026-07-21T10:00:00+09:00",
                    "event_type": "view",
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "metadata_json": {"chase_risk": 50},
                },
                {
                    "occurred_at": "2026-07-21T10:05:00+09:00",
                    "event_type": "BUY",
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "metadata_json": '{"quantity": 1, "price": 1000}',
                },
            ]
        )

        profile = paper_trading.get_investor_profile(days=30)

        self.assertEqual(profile["total_actions"], 2)
        self.assertEqual(profile["views"], 1)
        self.assertEqual(profile["buys"], 1)
        self.assertEqual(profile["high_risk_ratio"], 100.0)

    def test_invalid_or_non_object_metadata_becomes_empty_object(self):
        self.assertEqual(paper_trading._parse_event_metadata(None), {})
        self.assertEqual(paper_trading._parse_event_metadata("not-json"), {})
        self.assertEqual(paper_trading._parse_event_metadata("[]"), {})

    @patch.object(paper_trading, "is_paper_user_authenticated", return_value=True)
    @patch.object(paper_trading, "is_remote_storage_enabled", return_value=True)
    @patch.object(paper_trading.cloud_paper, "get_events_since")
    def test_malformed_event_values_do_not_break_profile(
        self,
        get_events_since,
        _remote_enabled,
        _authenticated,
    ):
        get_events_since.return_value = pd.DataFrame(
            [
                {
                    "occurred_at": "invalid-date",
                    "event_type": "view",
                    "stock_code": None,
                    "stock_name": None,
                    "metadata_json": {"chase_risk": {"unexpected": 50}},
                },
                {
                    "occurred_at": "2026-07-21T10:05:00+09:00",
                    "event_type": "BUY",
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "metadata_json": {"chase_risk": [50]},
                },
            ]
        )

        profile = paper_trading.get_investor_profile(days=30)

        self.assertEqual(profile["total_actions"], 2)
        self.assertEqual(profile["high_risk_ratio"], 0.0)
        self.assertEqual(profile["rapid_entry_count"], 0)


if __name__ == "__main__":
    unittest.main()
