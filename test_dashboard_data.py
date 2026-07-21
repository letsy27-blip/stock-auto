from datetime import date
from pathlib import Path
import sqlite3
import unittest

import pandas as pd

from dashboard_data import (
    available_score_dates,
    score_rows_for_date,
    select_morning_briefing,
)


class DashboardDataTest(unittest.TestCase):
    def test_repository_database_has_expected_latest_top30(self):
        database = Path(__file__).resolve().parent / "stock_data.db"
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            scores = pd.read_sql_query('SELECT * FROM "score"', connection)
        dates = available_score_dates(scores)
        self.assertEqual(dates[0], date(2026, 7, 20))
        latest = score_rows_for_date(scores, dates[0])
        self.assertEqual(len(latest), 59)
        self.assertEqual(len(latest.head(30)), 30)

    def test_top30_dates_are_normalized_and_latest_is_first(self):
        scores = pd.DataFrame(
            {
                "저장일자": [
                    "2026-07-19T15:00:00+00:00",
                    pd.Timestamp("2026-07-20"),
                    date(2026, 7, 20),
                ],
                "종목코드": ["1", "2", "3"],
            }
        )
        self.assertEqual(available_score_dates(scores), [date(2026, 7, 20)])
        self.assertEqual(len(score_rows_for_date(scores, date(2026, 7, 20))), 3)

    def test_morning_briefing_uses_central_supabase_snapshot(self):
        supabase = pd.DataFrame({"분석기준일": ["2026-07-20"], "종목코드": ["1"]})
        result = select_morning_briefing(supabase, pd.DataFrame(), date(2026, 7, 21))
        self.assertEqual(result.source, "Supabase 중앙 DB")
        self.assertEqual(result.data_date, date(2026, 7, 20))

    def test_morning_briefing_falls_back_to_previous_close(self):
        history = pd.DataFrame(
            {"저장일자": ["2026-07-20", "2026-07-21"], "종목코드": ["1", "2"]}
        )
        result = select_morning_briefing(
            pd.DataFrame(), history, date(2026, 7, 21)
        )
        self.assertEqual(result.source, "전일 종가 대체 데이터")
        self.assertEqual(result.data_date, date(2026, 7, 20))
        self.assertEqual(result.frame["종목코드"].tolist(), ["1"])


if __name__ == "__main__":
    unittest.main()
