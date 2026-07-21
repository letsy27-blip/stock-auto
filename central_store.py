"""Supabase-based shared state for the collector and every dashboard instance."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv()


SHARED_DATABASE_KEY = "shared_database"
_DB_CACHE_LOCK = threading.Lock()
_DB_CACHE_LAST_CHECK = 0.0
_DB_CACHE_INFO: dict[str, object] = {
    "source": "unavailable",
    "updated_at": None,
    "path": None,
    "error": None,
}


def _settings(write: bool = False) -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key_name = "SUPABASE_SERVICE_ROLE_KEY" if write else "SUPABASE_ANON_KEY"
    key = os.getenv(key_name, "")
    if not key and not write:
        key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
        key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        try:
            import streamlit as st

            url = url or str(st.secrets.get("SUPABASE_URL", "")).rstrip("/")
            if write:
                key = key or str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", ""))
            else:
                key = key or str(st.secrets.get("SUPABASE_ANON_KEY", ""))
                key = key or str(st.secrets.get("SUPABASE_PUBLISHABLE_KEY", ""))
                key = key or str(st.secrets.get("SUPABASE_SERVICE_ROLE_KEY", ""))
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


def load_latest_scores() -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Read the shared current ranking. Returns an empty frame when unavailable."""
    url, key = _settings(write=False)
    if not url or not key:
        return pd.DataFrame(), None
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
            return pd.DataFrame(), None
        value = rows[0].get("value") or {}
        frame = pd.DataFrame(value.get("scores") or [])
        timestamp = pd.to_datetime(value.get("snapshot_at"), errors="coerce", utc=True)
        if pd.isna(timestamp):
            timestamp = None
        return frame, timestamp
    except (requests.RequestException, ValueError, TypeError):
        return pd.DataFrame(), None


def _validate_sqlite(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise ValueError("Downloaded shared database failed SQLite integrity check")


def publish_database_snapshot(
    db_path: str | os.PathLike,
    source: str = "collector",
) -> str:
    """Publish one compressed, authoritative SQLite snapshot for every dashboard."""
    url, key = _settings(write=True)
    if not url or not key:
        raise RuntimeError("Supabase central write credentials are not configured")

    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    _validate_sqlite(path)

    raw = path.read_bytes()
    compressed = gzip.compress(raw, compresslevel=6)
    snapshot_at = datetime.now(timezone.utc).isoformat()
    value = {
        "snapshot_at": snapshot_at,
        "source": source,
        "encoding": "gzip+base64",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "payload": base64.b64encode(compressed).decode("ascii"),
    }
    response = requests.post(
        f"{url}/rest/v1/hongstock_state?on_conflict=key",
        headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
        json={"key": SHARED_DATABASE_KEY, "value": value, "updated_at": snapshot_at},
        timeout=60,
    )
    response.raise_for_status()
    return snapshot_at


def _shared_cache_paths() -> tuple[Path, Path]:
    configured = os.getenv("HONGSTOCK_CACHE_DIR", "").strip()
    cache_dir = Path(configured) if configured else Path(tempfile.gettempdir()) / "hongstock"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "shared_stock_data.db", cache_dir / "shared_stock_data.json"


def _read_cache_metadata(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def get_shared_database_path(min_check_seconds: int = 30) -> Path:
    """Return only a validated central snapshot or its last known-good cache."""
    global _DB_CACHE_LAST_CHECK, _DB_CACHE_INFO

    cache_path, metadata_path = _shared_cache_paths()
    url, key = _settings(write=False)
    if not url or not key:
        if cache_path.is_file():
            try:
                _validate_sqlite(cache_path)
                cached_metadata = _read_cache_metadata(metadata_path)
                _DB_CACHE_INFO = {
                    "source": "supabase-cache",
                    "updated_at": cached_metadata.get("updated_at"),
                    "path": str(cache_path),
                    "error": "Supabase read credentials are not configured",
                }
                return cache_path
            except (OSError, ValueError, sqlite3.DatabaseError):
                pass
        _DB_CACHE_INFO = {
            "source": "unavailable",
            "updated_at": None,
            "path": None,
            "error": "Supabase read credentials are not configured",
        }
        raise RuntimeError(
            "Supabase read credentials are required; local SQLite fallback is disabled"
        )

    with _DB_CACHE_LOCK:
        now = time.monotonic()
        if (
            cache_path.is_file()
            and now - _DB_CACHE_LAST_CHECK < max(1, int(min_check_seconds))
        ):
            return cache_path
        _DB_CACHE_LAST_CHECK = now

        try:
            metadata_response = requests.get(
                f"{url}/rest/v1/hongstock_state",
                headers=_headers(key),
                params={
                    "key": f"eq.{SHARED_DATABASE_KEY}",
                    "select": "updated_at",
                    "limit": "1",
                },
                timeout=12,
            )
            metadata_response.raise_for_status()
            metadata_rows = metadata_response.json()
            if not metadata_rows:
                raise ValueError("No shared database snapshot exists in Supabase")

            remote_updated_at = metadata_rows[0].get("updated_at")
            cached_metadata = _read_cache_metadata(metadata_path)
            if (
                cache_path.is_file()
                and cached_metadata.get("updated_at") == remote_updated_at
            ):
                _validate_sqlite(cache_path)
                _DB_CACHE_INFO = {
                    "source": "supabase",
                    "updated_at": remote_updated_at,
                    "path": str(cache_path),
                    "error": None,
                }
                return cache_path

            payload_response = requests.get(
                f"{url}/rest/v1/hongstock_state",
                headers=_headers(key),
                params={
                    "key": f"eq.{SHARED_DATABASE_KEY}",
                    "select": "value",
                    "limit": "1",
                },
                timeout=60,
            )
            payload_response.raise_for_status()
            payload_rows = payload_response.json()
            if not payload_rows:
                raise ValueError("Shared database payload is empty")
            value = payload_rows[0].get("value") or {}
            if value.get("encoding") != "gzip+base64":
                raise ValueError("Unsupported shared database encoding")

            raw = gzip.decompress(base64.b64decode(value.get("payload") or ""))
            expected_hash = str(value.get("sha256") or "")
            if not expected_hash or hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError("Shared database checksum mismatch")

            temporary_path = cache_path.with_suffix(".tmp")
            temporary_path.write_bytes(raw)
            _validate_sqlite(temporary_path)
            os.replace(temporary_path, cache_path)
            metadata_path.write_text(
                json.dumps(
                    {
                        "updated_at": remote_updated_at,
                        "snapshot_at": value.get("snapshot_at"),
                        "sha256": expected_hash,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _DB_CACHE_INFO = {
                "source": "supabase",
                "updated_at": remote_updated_at or value.get("snapshot_at"),
                "path": str(cache_path),
                "error": None,
            }
            return cache_path
        except (OSError, ValueError, TypeError, requests.RequestException) as exc:
            if cache_path.is_file():
                try:
                    _validate_sqlite(cache_path)
                    cached_metadata = _read_cache_metadata(metadata_path)
                    _DB_CACHE_INFO = {
                        "source": "supabase-cache",
                        "updated_at": cached_metadata.get("updated_at"),
                        "path": str(cache_path),
                        "error": str(exc),
                    }
                    return cache_path
                except (OSError, ValueError, sqlite3.DatabaseError):
                    pass
            _DB_CACHE_INFO = {
                "source": "unavailable",
                "updated_at": None,
                "path": None,
                "error": str(exc),
            }
            raise RuntimeError(
                "Supabase central database is unavailable and no valid central cache exists"
            ) from exc


def restore_database_snapshot(target_path: str | os.PathLike) -> Path:
    """Restore the authoritative Supabase snapshot into a worker database."""
    source_path = get_shared_database_path(min_check_seconds=0)
    target = Path(target_path).resolve()
    if source_path.resolve() == target:
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".central.tmp")
    shutil.copy2(source_path, temporary)
    try:
        _validate_sqlite(temporary)
        try:
            os.replace(temporary, target)
        except PermissionError:
            # Windows can reject replacing an existing SQLite file even after
            # its handles are closed. This only overwrites the worker copy.
            shutil.copy2(temporary, target)
        _validate_sqlite(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def get_shared_database_info() -> dict[str, object]:
    return dict(_DB_CACHE_INFO)


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
