import requests
import pandas as pd
from datetime import datetime, timedelta

from config import APP_KEY, APP_SECRET, BASE_URL


def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"

    headers = {"content-type": "application/json"}

    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }

    res = requests.post(url, headers=headers, json=body)
    data = res.json()

    return data.get("access_token")


def get_current_price(token, stock_code):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100"
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code
    }

    res = requests.get(url, headers=headers, params=params)
    return res.json()


def get_daily_price_history(token, stock_code, stock_name="", days=60):
    """
    종목별 최근 일봉 데이터 조회
    엑셀 차트용 데이터 생성
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100"
    }

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days * 2)

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "1"
    }

    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        rows = []

        for item in data.get("output2", []):
            rows.append({
                "종목코드": stock_code,
                "종목명": stock_name,
                "날짜": item.get("stck_bsop_date", ""),
                "시가": int(float(item.get("stck_oprc", 0))),
                "고가": int(float(item.get("stck_hgpr", 0))),
                "저가": int(float(item.get("stck_lwpr", 0))),
                "종가": int(float(item.get("stck_clpr", 0))),
                "거래량": int(float(item.get("acml_vol", 0)))
            })

        df = pd.DataFrame(rows)

        if df.empty:
            return pd.DataFrame()

        df["날짜"] = pd.to_datetime(df["날짜"], format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["날짜"])
        df = df.sort_values("날짜").tail(days)
        df["날짜"] = df["날짜"].dt.strftime("%Y-%m-%d")

        return df

    except Exception as e:
        print(f"{stock_name}({stock_code}) 일봉 조회 실패:", e)
        return pd.DataFrame()