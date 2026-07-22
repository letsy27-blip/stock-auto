import unittest

from strategy_backtest import run_long_horizon_validation


class LongHorizonValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary, cls.regimes, cls.trades, cls.metadata = run_long_horizon_validation(
            "stock_data.db"
        )

    def test_precomputed_validation_has_five_year_scale_coverage(self):
        self.assertGreaterEqual(int(self.metadata["trading_days"]), 1_200)
        self.assertGreaterEqual(int(self.metadata["stock_count"]), 250)
        self.assertLess(self.metadata["development_end"], self.metadata["holdout_start"])

    def test_validation_keeps_development_holdout_and_full_period_separate(self):
        self.assertEqual(set(self.summary["전략"]), {"TOP3", "TOP30"})
        self.assertEqual(
            set(self.summary["검증구간"]),
            {"개발 참고 구간", "보지 않은 기간(OOS)", "전체 기간"},
        )
        self.assertEqual(len(self.summary), 6)

    def test_market_regime_report_is_explicit(self):
        expected = {"상승장", "횡보장", "하락장", "급락장"}
        for strategy in ["TOP3", "TOP30"]:
            rows = self.regimes[self.regimes["전략"] == strategy]
            self.assertEqual(set(rows["시장국면"]), expected)


if __name__ == "__main__":
    unittest.main()
