"""추천 결과를 실제 주가와 비교해 검증하는 기록 도구."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


DB_PATH = Path(__file__).resolve().with_name("stock_data.db")
TRACKED_RECOMMENDATIONS = {"강력관심", "관심", "관찰"}


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _clean_code(value: Any) -> str:
    text = str(value or "").replace(".0", "").strip()
    return text.zfill(6) if text else ""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def initialize_prediction_tracking() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS prediction_performance (
                prediction_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                final_score REAL NOT NULL,
                entry_price REAL NOT NULL,
                market_score REAL NOT NULL DEFAULT 0,
                supply_score REAL NOT NULL DEFAULT 0,
                news_score REAL NOT NULL DEFAULT 0,
                timing_score REAL NOT NULL DEFAULT 0,
                chase_risk REAL NOT NULL DEFAULT 0,
                reason TEXT,
                return_1d REAL,
                return_5d REAL,
                return_20d REAL,
                evaluated_price_1d REAL,
                evaluated_price_5d REAL,
                evaluated_price_20d REAL,
                failure_reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (prediction_date, stock_code)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_cases (
                case_key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                prediction_date TEXT NOT NULL,
                recommendation TEXT,
                entry_price REAL,
                observed_price REAL,
                return_rate REAL,
                expected_reason TEXT,
                outcome_reason TEXT,
                review_status TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )


def record_learning_case(*, case_key: str, stock_code: str, stock_name: str, prediction_date: str, recommendation: str, entry_price: float, observed_price: float | None, return_rate: float | None, expected_reason: str, outcome_reason: str, review_status: str, evidence: dict | None = None) -> None:
    """예측과 실제 결과의 차이를 향후 규칙 검증용 사례로 남긴다."""
    initialize_prediction_tracking()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO learning_cases (case_key, created_at, stock_code, stock_name, prediction_date, recommendation, entry_price, observed_price, return_rate, expected_reason, outcome_reason, review_status, evidence_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_key) DO UPDATE SET observed_price=excluded.observed_price, return_rate=excluded.return_rate, outcome_reason=excluded.outcome_reason, review_status=excluded.review_status, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at
            """,
            (case_key, now, _clean_code(stock_code), stock_name, prediction_date, recommendation, entry_price, observed_price, return_rate, expected_reason, outcome_reason, review_status, json.dumps(evidence or {}, ensure_ascii=False), now),
        )


def _price_history_map(chart_history_df: pd.DataFrame) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    if chart_history_df is None or chart_history_df.empty:
        return {}
    required = {"종목코드", "날짜", "종가"}
    if not required.issubset(chart_history_df.columns):
        return {}

    prices = chart_history_df.copy()
    prices["종목코드"] = prices["종목코드"].map(_clean_code)
    prices["날짜"] = pd.to_datetime(prices["날짜"], errors="coerce")
    prices["종가"] = pd.to_numeric(prices["종가"], errors="coerce")
    prices = prices.dropna(subset=["종목코드", "날짜", "종가"])
    prices = prices[prices["종가"] > 0].sort_values("날짜")
    result: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for _, row in prices.iterrows():
        result.setdefault(row["종목코드"], []).append((row["날짜"], float(row["종가"])))
    return result


def _price_at_or_before(
    price_history: list[tuple[pd.Timestamp, float]],
    target_date: pd.Timestamp,
) -> tuple[str, float] | None:
    matched = [point for point in price_history if point[0].date() <= target_date.date()]
    if not matched:
        return None
    point_date, close = matched[-1]
    return point_date.strftime("%Y-%m-%d"), close


def record_predictions(scored_df: pd.DataFrame, chart_history_df: pd.DataFrame) -> int:
    """관심·관찰 추천을 종목별 하루 한 번만 예측 표본으로 저장한다."""
    initialize_prediction_tracking()
    if scored_df is None or scored_df.empty:
        return 0

    price_histories = _price_history_map(chart_history_df)
    if not price_histories:
        return 0

    saved = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as connection:
        for _, row in scored_df.iterrows():
            recommendation = str(row.get("최종추천", "")).strip()
            code = _clean_code(row.get("종목코드"))
            if recommendation not in TRACKED_RECOMMENDATIONS or code not in price_histories:
                continue

            recorded_date = pd.to_datetime(
                row.get("최종갱신일자", row.get("저장일자")), errors="coerce"
            )
            if pd.isna(recorded_date):
                recorded_date = price_histories[code][-1][0]
            entry = _price_at_or_before(price_histories[code], recorded_date)
            if entry is None:
                continue
            prediction_date, entry_price = entry
            reason = str(row.get("AI추천사유", "") or row.get("진입판단사유", ""))
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO prediction_performance (
                    prediction_date, stock_code, stock_name, recommendation, final_score,
                    entry_price, market_score, supply_score, news_score, timing_score,
                    chase_risk, reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_date,
                    code,
                    str(row.get("종목명", code)),
                    recommendation,
                    _number(row.get("최종점수")),
                    entry_price,
                    _number(row.get("시장점수")),
                    _number(row.get("수급점수")),
                    _number(row.get("뉴스점수")),
                    _number(row.get("진입타이밍점수")),
                    _number(row.get("추격위험도")),
                    reason,
                    now,
                ),
            )
            saved += cursor.rowcount
    return saved


def _failure_reason(row: sqlite3.Row, return_rate: float) -> str:
    if return_rate > 0:
        return "예측 방향 적중"
    if float(row["chase_risk"] or 0) >= 45:
        return "급등·추격 위험이 실제 하락으로 이어짐"
    if float(row["supply_score"] or 0) <= 0:
        return "수급 매수 우위가 부족했음"
    if float(row["news_score"] or 0) <= 0:
        return "뉴스 모멘텀이 약했음"
    if float(row["timing_score"] or 0) <= 0:
        return "진입 타이밍 신호가 약했음"
    return "시장 변동 또는 추가 요인 확인 필요"


def evaluate_predictions() -> int:
    """저장된 일봉으로 1·5·20 거래일 뒤 성과를 채운다."""
    initialize_prediction_tracking()
    with _connect() as connection:
        predictions = connection.execute("SELECT * FROM prediction_performance").fetchall()
        chart_rows = connection.execute(
            'SELECT "종목코드", "날짜", "종가" FROM chart_history'
        ).fetchall()

        prices_by_code: dict[str, list[tuple[pd.Timestamp, float]]] = {}
        for item in chart_rows:
            code = _clean_code(item["종목코드"])
            point_date = pd.to_datetime(item["날짜"], errors="coerce")
            close = _number(item["종가"])
            if code and pd.notna(point_date) and close > 0:
                prices_by_code.setdefault(code, []).append((point_date, close))
        for points in prices_by_code.values():
            points.sort(key=lambda item: item[0])

        changed = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for prediction in predictions:
            points = prices_by_code.get(prediction["stock_code"], [])
            prediction_date = pd.to_datetime(prediction["prediction_date"], errors="coerce")
            entry_price = _number(prediction["entry_price"])
            if pd.isna(prediction_date) or entry_price <= 0:
                continue
            future = [point for point in points if point[0].date() > prediction_date.date()]
            updates: dict[str, Any] = {}
            for days in (1, 5, 20):
                if len(future) >= days:
                    close = future[days - 1][1]
                    updates[f"return_{days}d"] = round((close / entry_price - 1) * 100, 2)
                    updates[f"evaluated_price_{days}d"] = close
            if "return_5d" in updates:
                updates["failure_reason"] = _failure_reason(prediction, updates["return_5d"])
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = list(updates.values()) + [now, prediction["prediction_date"], prediction["stock_code"]]
            cursor = connection.execute(
                f"UPDATE prediction_performance SET {assignments}, updated_at = ? "
                "WHERE prediction_date = ? AND stock_code = ?",
                values,
            )
            changed += cursor.rowcount
    return changed


def update_prediction_tracking(scored_df: pd.DataFrame, chart_history_df: pd.DataFrame) -> tuple[int, int]:
    saved = record_predictions(scored_df, chart_history_df)
    evaluated = evaluate_predictions()
    return saved, evaluated


def get_prediction_summary() -> dict[str, Any]:
    initialize_prediction_tracking()
    with _connect() as connection:
        total = connection.execute("SELECT COUNT(*) FROM prediction_performance").fetchone()[0]
        completed = connection.execute(
            "SELECT COUNT(*) FROM prediction_performance WHERE return_5d IS NOT NULL"
        ).fetchone()[0]
        positive = connection.execute(
            "SELECT COUNT(*) FROM prediction_performance WHERE return_5d > 0"
        ).fetchone()[0]
        average_return = connection.execute(
            "SELECT AVG(return_5d) FROM prediction_performance WHERE return_5d IS NOT NULL"
        ).fetchone()[0]
    return {
        "total": int(total),
        "completed": int(completed),
        "waiting": int(total - completed),
        "success_rate": (positive / completed * 100) if completed else None,
        "average_return": float(average_return) if average_return is not None else None,
    }
