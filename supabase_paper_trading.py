"""Supabase에 사용자별 모의투자 데이터를 저장하는 REST 어댑터."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import requests

from supabase_auth import access_token, current_user, is_configured, supabase_anon_key, supabase_url


def is_enabled() -> bool:
    return is_configured()


def is_authenticated() -> bool:
    return current_user() is not None


def _headers(prefer: str | None = None) -> dict[str, str]:
    token = access_token()
    if not token:
        raise PermissionError("모의투자 기록은 로그인 후 사용할 수 있습니다.")
    headers = {
        "apikey": supabase_anon_key(),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _request(method: str, path: str, *, params: dict[str, str] | None = None, body: dict | list | None = None, prefer: str | None = None) -> Any:
    response = requests.request(
        method,
        f"{supabase_url()}{path}",
        headers=_headers(prefer),
        params=params,
        json=body,
        timeout=20,
    )
    if not response.ok:
        try:
            detail = response.json().get("message") or response.json().get("hint")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(detail or "개인 모의투자 DB 요청에 실패했습니다.")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _rpc(name: str, payload: dict[str, Any]) -> Any:
    return _request("POST", f"/rest/v1/rpc/{name}", body=payload)


def initialize_account() -> None:
    _rpc("paper_initialize_account", {})


def get_account() -> dict[str, Any]:
    rows = _request(
        "GET",
        "/rest/v1/paper_accounts",
        params={"select": "cash,initial_cash,updated_at", "limit": "1"},
    )
    if not rows:
        initialize_account()
        rows = _request(
            "GET",
            "/rest/v1/paper_accounts",
            params={"select": "cash,initial_cash,updated_at", "limit": "1"},
        )
    if not rows:
        raise RuntimeError("모의 계좌를 준비하지 못했습니다.")
    return dict(rows[0])


def get_positions() -> pd.DataFrame:
    rows = _request(
        "GET",
        "/rest/v1/paper_positions",
        params={
            "select": "stock_code,stock_name,quantity,average_price,updated_at",
            "quantity": "gt.0",
            "order": "stock_name.asc",
        },
    )
    return pd.DataFrame(rows or [], columns=["stock_code", "stock_name", "quantity", "average_price", "updated_at"])


def get_orders(limit: int = 200) -> pd.DataFrame:
    rows = _request(
        "GET",
        "/rest/v1/paper_orders",
        params={"select": "*", "order": "id.desc", "limit": str(int(limit))},
    )
    return pd.DataFrame(rows or [])


def get_events_since(since_iso: str) -> pd.DataFrame:
    rows = _request(
        "GET",
        "/rest/v1/investor_behavior_events",
        params={"select": "*", "occurred_at": f"gte.{since_iso}", "order": "occurred_at.asc"},
    )
    return pd.DataFrame(rows or [])


def record_behavior_event(event_type: str, stock_code: str = "", stock_name: str = "", metadata: dict | None = None) -> None:
    normalized = str(event_type).upper()
    if normalized not in {"SEARCH", "VIEW", "BUY", "SELL"}:
        raise ValueError("지원하지 않는 행동 기록입니다.")
    _request(
        "POST",
        "/rest/v1/investor_behavior_events",
        body={
            "user_id": str(current_user().get("id")),
            "event_type": normalized.lower() if normalized in {"SEARCH", "VIEW"} else normalized,
            "stock_code": str(stock_code or "").replace(".0", "").zfill(6) if stock_code else "",
            "stock_name": str(stock_name or "").strip(),
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
        },
        prefer="return=minimal",
    )


def place_order(side: str, stock_code: str, stock_name: str, quantity: int, price: float) -> dict[str, Any]:
    result = _rpc(
        "paper_place_order",
        {
            "p_side": str(side).upper(),
            "p_stock_code": str(stock_code).replace(".0", "").zfill(6),
            "p_stock_name": str(stock_name).strip(),
            "p_quantity": int(quantity),
            "p_price": float(price),
        },
    )
    return dict(result or {})


def reset_account() -> None:
    _rpc("paper_reset_account", {})
