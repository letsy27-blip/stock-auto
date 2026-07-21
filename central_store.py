"""Supabase-based shared state for the collector and every dashboard instance."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv()


def _settings(write: bool = False) -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key_name = "SUPABASE_SERVICE_ROLE_KEY" if write else "SUPABASE_ANON_KEY"
    key = os.getenv(key_name, "")
    if not key and not write:
        key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        try:
            import streamlit as st

            url = url or str(st.secrets.get("SUPABASE_URL", "")).rstrip("/")
            key = key or str(st.secrets.get(key_name, ""))
            if not key and not write:
                key = str(st.secrets.get("SUPABASE_PUBLISHABLE_KEY", ""))
        except Exception:
            pass
    return url, key


def _headers(key: str, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    clean = frame.copy()
    for column in clean.columns:
        if pd.api.types.is_datetime64_any_dtype(clean[column]):
            clean[column] = clean[column].astype(str)
    # JSON round-trip converts numpy scalars and replaces NaN/NaT with null.
    return json.loads(clean.to_json(orient="records", date_format="iso", force_ascii=False))


def publish_latest_scores(scored_df: pd.DataFrame, source: str = "collector") -> str:
    """Upsert the authoritative current ranking and append its snapshot."""
    url, key = _settings(write=True)
    if not url or not key:
        raise RuntimeError("Supabase central write credentials are not configured")

    snapshot_at = datetime.now(timezone.utc).isoformat()
    scores = _records(scored_df)
    value = {"snapshot_at": snapshot_at, "source": source, "scores": scores}
    state_response = requests.post(
        f"{url}/rest/v1/hongstock_state?on_conflict=key",
        headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
        json={"key": "latest_scores", "value": value, "updated_at": snapshot_at},
        timeout=30,
    )
    state_response.raise_for_status()

    snapshot_response = requests.post(
        f"{url}/rest/v1/hongstock_snapshots?on_conflict=snapshot_at",
        headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
        json={"snapshot_at": snapshot_at, "scores": scores, "source": source},
        timeout=30,
    )
    snapshot_response.raise_for_status()
    return snapshot_at


def load_latest_scores() -> tuple[pd.DataFrame, pd.Timestamp | None, str | None]:
    """Read the shared current ranking and keep connection failures explicit."""
    url, key = _settings(write=False)
    if not url or not key:
        return pd.DataFrame(), None, "Supabase read credentials are not configured"
    try:
        response = requests.get(
            f"{url}/rest/v1/hongstock_state",
            headers=_headers(key),
            params={"key": "eq.latest_scores", "select": "value", "limit": "1"},
            timeout=12,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return pd.DataFrame(), None, "Supabase latest_scores row is missing"
        value = rows[0].get("value") or {}
        frame = pd.DataFrame(value.get("scores") or [])
        timestamp = pd.to_datetime(value.get("snapshot_at"), errors="coerce", utc=True)
        if pd.isna(timestamp):
            timestamp = None
        return frame, timestamp, None
    except (requests.RequestException, ValueError, TypeError) as exc:
        return pd.DataFrame(), None, f"Supabase latest_scores read failed: {exc}"


DASHBOARD_TABLES = (
    "score",
    "score_current",
    "intraday_snapshot",
    "market_event_history",
    "chart_history",
    "supply_demand",
    "news_history",
    "stock_master",
    "stock_classification",
    "stock_theme_history",
    "auto_strategy_accounts",
    "auto_strategy_positions",
    "auto_strategy_trades",
)


def publish_dashboard_state(
    db_path: str | os.PathLike,
    current_scores: pd.DataFrame,
    source: str = "collector",
) -> str:
    """Publish one authoritative dashboard bundle without changing DB schema."""
    url, key = _settings(write=True)
    if not url or not key:
        raise RuntimeError("Supabase central write credentials are not configured")
    with sqlite3.connect(db_path) as connection:
        tables = {
            table: _records(pd.read_sql_query(f'SELECT * FROM "{table}"', connection))
            for table in DASHBOARD_TABLES
        }
    snapshot_at = datetime.now(timezone.utc).isoformat()
    response = requests.post(
        f"{url}/rest/v1/hongstock_state?on_conflict=key",
        headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
        json={
            "key": "dashboard_state",
            "value": {
                "snapshot_at": snapshot_at,
                "source": source,
                "current_scores": _records(current_scores),
                "tables": tables,
            },
            "updated_at": snapshot_at,
        },
        timeout=90,
    )
    response.raise_for_status()
    return snapshot_at


def load_dashboard_state() -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.Timestamp | None, str | None]:
    """Load the central screen bundle; never substitute local SQLite on failure."""
    url, key = _settings(write=False)
    if not url or not key:
        return {}, pd.DataFrame(), None, "Supabase read credentials are not configured"
    try:
        response = requests.get(
            f"{url}/rest/v1/hongstock_state",
            headers=_headers(key),
            params={"key": "eq.dashboard_state", "select": "value", "limit": "1"},
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return {}, pd.DataFrame(), None, "Supabase dashboard_state row is missing"
        value = rows[0].get("value") or {}
        tables = {
            name: pd.DataFrame(records or [])
            for name, records in (value.get("tables") or {}).items()
        }
        current_scores = pd.DataFrame(value.get("current_scores") or [])
        timestamp = pd.to_datetime(value.get("snapshot_at"), errors="coerce", utc=True)
        if pd.isna(timestamp):
            timestamp = None
        return tables, current_scores, timestamp, None
    except (requests.RequestException, ValueError, TypeError) as exc:
        return {}, pd.DataFrame(), None, f"Supabase dashboard_state read failed: {exc}"


STRATEGY_TABLES = (
    "auto_strategy_accounts",
    "auto_strategy_positions",
    "auto_strategy_trades",
)


def publish_strategy_state(db_path: str | os.PathLike) -> str:
    """자동 가상계좌·보유종목·거래내역 전체를 중앙 상태로 저장한다."""
    url, key = _settings(write=True)
    if not url or not key:
        raise RuntimeError("Supabase central write credentials are not configured")
    with sqlite3.connect(db_path) as connection:
        tables = {
            table: _records(pd.read_sql_query(f'SELECT * FROM "{table}"', connection))
            for table in STRATEGY_TABLES
        }
    snapshot_at = datetime.now(timezone.utc).isoformat()
    response = requests.post(
        f"{url}/rest/v1/hongstock_state?on_conflict=key",
        headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
        json={
            "key": "auto_strategy_state",
            "value": {"snapshot_at": snapshot_at, "tables": tables},
            "updated_at": snapshot_at,
        },
        timeout=30,
    )
    response.raise_for_status()
    return snapshot_at


def restore_strategy_state(db_path: str | os.PathLike) -> bool:
    """중앙 자동매매 상태가 있으면 로컬 실행 DB에 복원한다."""
    url, key = _settings(write=True)
    if not url or not key:
        return False
    response = requests.get(
        f"{url}/rest/v1/hongstock_state",
        headers=_headers(key),
        params={"key": "eq.auto_strategy_state", "select": "value", "limit": "1"},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return False
    tables = (rows[0].get("value") or {}).get("tables") or {}
    with sqlite3.connect(db_path) as connection:
        for table in STRATEGY_TABLES:
            records = tables.get(table)
            if records is None:
                continue
            connection.execute(f'DELETE FROM "{table}"')
            if records:
                pd.DataFrame(records).to_sql(table, connection, if_exists="append", index=False)
    return True
