import requests
import pandas as pd
from datetime import datetime

from config import APP_KEY, APP_SECRET, BASE_URL, ACCOUNT_NO, ACCOUNT_CODE


def get_account_balance(token):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTC8434R"
    }

    params = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_CODE,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": ""
    }

    res = requests.get(url, headers=headers, params=params)
    data = res.json()

    rows = []

    for item in data.get("output1", []):
        qty = int(float(item.get("hldg_qty", 0)))

        if qty <= 0:
            continue

        rows.append({
            "날짜": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "종목명": item.get("prdt_name", ""),
            "종목코드": item.get("pdno", ""),
            "현재가": int(float(item.get("prpr", 0))),
            "평균단가": int(float(item.get("pchs_avg_pric", 0))),
            "보유수량": qty,
            "매수금액": int(float(item.get("pchs_amt", 0))),
            "평가금액": int(float(item.get("evlu_amt", 0))),
            "손익": int(float(item.get("evlu_pfls_amt", 0))),
            "수익률(%)": float(item.get("evlu_pfls_rt", 0))
        })

    return pd.DataFrame(rows)