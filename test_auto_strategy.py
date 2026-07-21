import unittest

import pandas as pd

from auto_strategy import _candidate_sets, _user_sell_reason


class AutoStrategyComparisonTest(unittest.TestCase):
    def test_user_strategy_uses_original_score_ranking(self):
        frame = pd.DataFrame(
            [
                {
                    "종목코드": "1234",
                    "종목명": "KODEX 테스트",
                    "최종점수": 90,
                    "최종추천": "강력관심",
                    "진입판단": "위험",
                    "돌파신뢰도": 20,
                    "추격위험도": 90,
                    "RSI": 80,
                    "수급점수": -10,
                },
                {
                    "종목코드": "5678",
                    "종목명": "테스트전자",
                    "최종점수": 80,
                    "최종추천": "관심",
                    "진입판단": "돌파 확인",
                    "돌파신뢰도": 80,
                    "추격위험도": 20,
                    "RSI": 55,
                    "수급점수": 10,
                },
            ]
        )

        candidates = _candidate_sets(frame)

        self.assertEqual(candidates["USER_TOP3"]["종목코드"].tolist(), ["001234", "005678"])
        self.assertEqual(candidates["TOP3"]["종목코드"].tolist(), ["005678"])

    def test_user_sell_signal_depends_only_on_original_recommendation(self):
        self.assertEqual(
            _user_sell_reason({"최종추천": "관심", "진입판단": "위험", "추격위험도": 100}),
            "",
        )
        self.assertEqual(_user_sell_reason({"최종추천": "약세"}), "추천 약세")


if __name__ == "__main__":
    unittest.main()
