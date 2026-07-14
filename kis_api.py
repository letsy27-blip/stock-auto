import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from config import APP_KEY, APP_SECRET, BASE_URL


REQUEST_TIMEOUT = 20


def _headers(token: str, tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def _safe_int(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    try:
        res = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()
        return data.get("access_token")
    except Exception as exc:
        print("접근토큰 발급 실패:", exc)
        return None


def get_current_price(token, stock_code):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
    }

    try:
        res = requests.get(
            url,
            headers=_headers(token, "FHKST01010100"),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        return res.json()
    except Exception as exc:
        print(f"{stock_code} 현재가 조회 실패:", exc)
        return {}


def get_daily_price_history(token, stock_code, stock_name="", days=60):
    """종목별 최근 일봉 데이터 조회."""
    url = (
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/"
        "inquire-daily-itemchartprice"
    )

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days * 2)

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "1",
    }

    try:
        res = requests.get(
            url,
            headers=_headers(token, "FHKST03010100"),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()

        if data.get("rt_cd") not in (None, "0"):
            print(
                f"{stock_name}({stock_code}) 일봉 API 오류:",
                data.get("msg1", data),
            )
            return pd.DataFrame()

        rows = []
        for item in data.get("output2", []):
            rows.append(
                {
                    "종목코드": str(stock_code).zfill(6),
                    "종목명": stock_name,
                    "날짜": item.get("stck_bsop_date", ""),
                    "시가": _safe_int(item.get("stck_oprc")),
                    "고가": _safe_int(item.get("stck_hgpr")),
                    "저가": _safe_int(item.get("stck_lwpr")),
                    "종가": _safe_int(item.get("stck_clpr")),
                    "거래량": _safe_int(item.get("acml_vol")),
                    "거래대금": _safe_int(item.get("acml_tr_pbmn")),
                }
            )

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        df["날짜"] = pd.to_datetime(
            df["날짜"],
            format="%Y%m%d",
            errors="coerce",
        )
        df = df.dropna(subset=["날짜"])
        df = df.sort_values("날짜").tail(days)
        df["날짜"] = df["날짜"].dt.strftime("%Y-%m-%d")
        return df

    except Exception as exc:
        print(f"{stock_name}({stock_code}) 일봉 조회 실패:", exc)
        return pd.DataFrame()


def get_investor_trend(
    token: str,
    stock_code: str,
    stock_name: str = "",
    days: int = 10,
) -> pd.DataFrame:
    """
    KIS 종목별 투자자매매동향(일별).

    외국인·기관·개인의 일별 순매수량을 가져온다.
    KIS 응답에서 순매수량은 양수=순매수, 음수=순매도다.
    """
    url = (
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/"
        "inquire-investor"
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": str(stock_code).zfill(6),
    }

    try:
        res = requests.get(
            url,
            headers=_headers(token, "FHKST01010900"),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()

        if data.get("rt_cd") not in (None, "0"):
            print(
                f"{stock_name}({stock_code}) 수급 API 오류:",
                data.get("msg1", data),
            )
            return pd.DataFrame()

        output = data.get("output", [])
        if isinstance(output, dict):
            output = [output]

        rows = []
        for item in output:
            date_value = (
                item.get("stck_bsop_date")
                or item.get("bsop_date")
                or item.get("date")
                or ""
            )
            rows.append(
                {
                    "종목코드": str(stock_code).zfill(6),
                    "종목명": stock_name,
                    "날짜": date_value,
                    "개인순매수량": _safe_int(
                        item.get("prsn_ntby_qty")
                    ),
                    "외국인순매수량": _safe_int(
                        item.get("frgn_ntby_qty")
                    ),
                    "기관순매수량": _safe_int(
                        item.get("orgn_ntby_qty")
                    ),
                    "종가": _safe_int(item.get("stck_clpr")),
                    "거래량": _safe_int(item.get("acml_vol")),
                }
            )

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        df["날짜"] = pd.to_datetime(
            df["날짜"],
            format="%Y%m%d",
            errors="coerce",
        )
        df = df.dropna(subset=["날짜"]).sort_values("날짜")
        df = df.tail(days)
        df["날짜"] = df["날짜"].dt.strftime("%Y-%m-%d")
        return df

    except Exception as exc:
        print(f"{stock_name}({stock_code}) 수급 조회 실패:", exc)
        return pd.DataFrame()


def collect_investor_trends(
    token: str,
    candidate_df: pd.DataFrame,
    days: int = 10,
    delay: float = 0.25,
) -> pd.DataFrame:
    """후보 종목 전체의 수급 데이터를 수집한다."""
    if candidate_df is None or candidate_df.empty:
        return pd.DataFrame()

    collected = []

    for _, row in candidate_df.iterrows():
        code = str(row.get("종목코드", "")).replace(".0", "").zfill(6)
        name = str(row.get("종목명", "")).strip()

        print(f"수급 데이터 조회 중: {name}({code})")
        df = get_investor_trend(
            token=token,
            stock_code=code,
            stock_name=name,
            days=days,
        )
        if not df.empty:
            collected.append(df)

        time.sleep(delay)

    if not collected:
        return pd.DataFrame()

    result = pd.concat(collected, ignore_index=True)
    return result.drop_duplicates(
        subset=["날짜", "종목코드"],
        keep="last",
    )
