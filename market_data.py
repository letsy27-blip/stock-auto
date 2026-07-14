from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from config import APP_KEY, APP_SECRET, BASE_URL


REQUEST_TIMEOUT = 15

INDEX_CONFIG = {
    "KOSPI": "0001",
    "KOSDAQ": "1001",
}


def _headers(token: str, tr_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _first_value(data: dict, keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def get_index_quote(
    token: str,
    index_name: str,
) -> dict[str, Any]:
    """
    KIS 국내업종 현재지수 조회.

    KOSPI: 0001
    KOSDAQ: 1001
    """
    code = INDEX_CONFIG[index_name]
    url = (
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/"
        "inquire-index-price"
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": code,
    }

    try:
        response = requests.get(
            url,
            headers=_headers(token, "FHPUP02100000"),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()

        if body.get("rt_cd") not in (None, "0"):
            raise RuntimeError(body.get("msg1", body))

        output = body.get("output", {})
        if isinstance(output, list):
            output = output[0] if output else {}

        current = _safe_float(
            _first_value(
                output,
                [
                    "bstp_nmix_prpr",
                    "bstp_nmix_prpr2",
                    "stck_prpr",
                    "current_price",
                ],
            )
        )
        change = _safe_float(
            _first_value(
                output,
                [
                    "bstp_nmix_prdy_vrss",
                    "prdy_vrss",
                    "change",
                ],
            )
        )
        change_rate = _safe_float(
            _first_value(
                output,
                [
                    "bstp_nmix_prdy_ctrt",
                    "prdy_ctrt",
                    "change_rate",
                ],
            )
        )
        previous = _safe_float(
            _first_value(
                output,
                [
                    "prdy_nmix",
                    "bstp_nmix_prdy_clpr",
                    "previous_close",
                ],
            )
        )

        if previous == 0 and current != 0:
            previous = current - change

        return {
            "name": index_name,
            "current": current,
            "previous": previous,
            "change": change,
            "change_rate": change_rate,
            "source": "KIS",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": "",
        }

    except Exception as exc:
        return {
            "name": index_name,
            "current": 0.0,
            "previous": 0.0,
            "change": 0.0,
            "change_rate": 0.0,
            "source": "KIS",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
        }


def get_usdkrw_quote() -> dict[str, Any]:
    """
    Yahoo Finance 공개 차트 응답으로 USD/KRW 현재값과 전일 종가를 조회한다.
    환율은 제공처 사정에 따라 실시간 또는 지연 데이터일 수 있다.
    """
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        "KRW=X?interval=1m&range=1d"
    )

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        body = response.json()

        result = body["chart"]["result"][0]
        meta = result.get("meta", {})
        current = _safe_float(
            meta.get("regularMarketPrice")
            or meta.get("chartPreviousClose")
        )
        previous = _safe_float(
            meta.get("previousClose")
            or meta.get("chartPreviousClose")
        )
        change = current - previous if current and previous else 0.0
        change_rate = (
            change / previous * 100
            if previous
            else 0.0
        )

        return {
            "name": "USD/KRW",
            "current": current,
            "previous": previous,
            "change": change,
            "change_rate": change_rate,
            "source": "Yahoo Finance",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": "",
        }

    except Exception as exc:
        return {
            "name": "USD/KRW",
            "current": 0.0,
            "previous": 0.0,
            "change": 0.0,
            "change_rate": 0.0,
            "source": "Yahoo Finance",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": str(exc),
        }


def get_market_overview(token: str) -> dict[str, dict[str, Any]]:
    return {
        "KOSPI": get_index_quote(token, "KOSPI"),
        "KOSDAQ": get_index_quote(token, "KOSDAQ"),
        "USD/KRW": get_usdkrw_quote(),
    }
