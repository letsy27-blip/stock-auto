"""Pure dataframe selection helpers used by the Streamlit dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


def normalize_kst_date(value) -> date | None:
    """Normalize strings, dates and timestamps to a KST calendar date."""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Seoul")
    else:
        timestamp = timestamp.tz_convert("Asia/Seoul")
    return timestamp.date()


def normalize_kst_date_series(series: pd.Series) -> pd.Series:
    return series.map(normalize_kst_date)


def normalize_kst_datetime(value) -> pd.Timestamp | pd.NaT:
    """Normalize naive SQLite and timezone-aware Supabase timestamps to KST."""
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return pd.NaT
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("Asia/Seoul")
    return timestamp.tz_convert("Asia/Seoul")


def normalize_kst_datetime_series(series: pd.Series) -> pd.Series:
    return series.map(normalize_kst_datetime)


def available_score_dates(score_df: pd.DataFrame) -> list[date]:
    if score_df is None or score_df.empty or "저장일자" not in score_df.columns:
        return []
    dates = normalize_kst_date_series(score_df["저장일자"]).dropna().unique()
    return sorted(dates, reverse=True)


def score_rows_for_date(score_df: pd.DataFrame, selected_date) -> pd.DataFrame:
    if score_df is None or score_df.empty or "저장일자" not in score_df.columns:
        return pd.DataFrame()
    normalized_date = normalize_kst_date(selected_date)
    normalized = score_df.copy()
    normalized["저장일자"] = normalize_kst_date_series(normalized["저장일자"])
    return normalized[normalized["저장일자"] == normalized_date].copy()


@dataclass(frozen=True)
class MorningBriefingSelection:
    frame: pd.DataFrame
    source: str
    data_date: date | None
    is_premarket: bool


def _latest_rows_on_or_before(
    frame: pd.DataFrame,
    date_column: str,
    today: date,
) -> tuple[pd.DataFrame, date | None]:
    if frame is None or frame.empty or date_column not in frame.columns:
        return pd.DataFrame(), None
    candidate = frame.copy()
    candidate[date_column] = normalize_kst_date_series(candidate[date_column])
    dates = candidate.loc[
        candidate[date_column].notna() & (candidate[date_column] <= today),
        date_column,
    ]
    if dates.empty:
        return pd.DataFrame(), None
    latest_date = max(dates)
    return candidate[candidate[date_column] == latest_date].copy(), latest_date


def select_morning_briefing(
    central_premarket: pd.DataFrame,
    score_history: pd.DataFrame,
    today: date,
) -> MorningBriefingSelection:
    """Select the central Supabase snapshot, then its previous-close history."""
    selected, selected_date = _latest_rows_on_or_before(
        central_premarket, "분석기준일", today
    )
    if not selected.empty:
        return MorningBriefingSelection(
            selected, "Supabase 중앙 DB", selected_date, True
        )

    if score_history is not None and not score_history.empty and "저장일자" in score_history.columns:
        history = score_history.copy()
        history["저장일자"] = normalize_kst_date_series(history["저장일자"])
        previous_dates = history.loc[
            history["저장일자"].notna() & (history["저장일자"] < today),
            "저장일자",
        ]
        if not previous_dates.empty:
            previous_date = max(previous_dates)
            selected = history[history["저장일자"] == previous_date].copy()
            return MorningBriefingSelection(
                selected, "전일 종가 대체 데이터", previous_date, False
            )

    return MorningBriefingSelection(pd.DataFrame(), "데이터 없음", None, False)
