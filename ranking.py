import time
import requests
import pandas as pd
from datetime import datetime
from config import APP_KEY, APP_SECRET, BASE_URL
from news import get_news_summary


def get_volume_rank(token):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST01710000"
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "000000",
        "FID_INPUT_PRICE_1": "0",
        "FID_INPUT_PRICE_2": "0",
        "FID_VOL_CNT": "0",
        "FID_INPUT_DATE_1": ""
    }

    res = requests.get(url, headers=headers, params=params)
    data = res.json()

    rows = []

    for item in data.get("output", []):
        stock_name = item.get("hts_kor_isnm", "")

        rows.append({
            "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "순위": item.get("data_rank", ""),
            "종목코드": item.get("mksc_shrn_iscd", ""),
            "종목명": stock_name,
            "현재가": item.get("stck_prpr", ""),
            "등락률(%)": item.get("prdy_ctrt", ""),
            "거래량": item.get("acml_vol", ""),
            "거래대금": item.get("acml_tr_pbmn", ""),
            "뉴스요약": get_news_summary(stock_name)
        })

        time.sleep(0.3)

    return pd.DataFrame(rows)


def get_rise_rank(token):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/ranking/fluctuation"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHPST01700000"
    }

    params = {
        "fid_rsfl_rate2": "",
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20170",
        "fid_input_iscd": "0000",
        "fid_rank_sort_cls_code": "0",
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "0",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": ""
    }

    res = requests.get(url, headers=headers, params=params)
    data = res.json()

    rows = []

    for item in data.get("output", []):
        stock_name = item.get("hts_kor_isnm", "")

        rows.append({
            "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "순위": item.get("data_rank", ""),
            "종목코드": item.get("stck_shrn_iscd", ""),
            "종목명": stock_name,
            "현재가": item.get("stck_prpr", ""),
            "전일대비": item.get("prdy_vrss", ""),
            "등락률(%)": item.get("prdy_ctrt", ""),
            "거래량": item.get("acml_vol", ""),
            "뉴스요약": get_news_summary(stock_name)
        })

        time.sleep(0.3)

        def get_trade_value_rank(token):
            """
            거래대금 TOP30
            1차: KIS 거래대금 순위 API 시도
            2차: 실패하면 거래량 TOP30 데이터를 거래대금 기준으로 정렬
            """
            url = f"{BASE_URL}/uapi/domestic-stock/v1/ranking/trade-vol"

            headers = {
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": APP_KEY,
                "appsecret": APP_SECRET,
                "tr_id": "FHPST01720000"
            }

            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20172",
                "FID_INPUT_ISCD": "0000",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": "0",
                "FID_INPUT_PRICE_2": "0",
                "FID_VOL_CNT": "0",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0"
            }

            rows = []

            try:
                res = requests.get(url, headers=headers, params=params)
                data = res.json()

                for item in data.get("output", []):
                    stock_name = item.get("hts_kor_isnm", "")

                    rows.append({
                        "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "순위": item.get("data_rank", ""),
                        "종목코드": item.get("mksc_shrn_iscd", item.get("stck_shrn_iscd", "")),
                        "종목명": stock_name,
                        "현재가": item.get("stck_prpr", ""),
                        "등락률(%)": item.get("prdy_ctrt", ""),
                        "거래량": item.get("acml_vol", ""),
                        "거래대금": item.get("acml_tr_pbmn", ""),
                        "뉴스요약": get_news_summary(stock_name)
                    })

                    time.sleep(0.3)

            except Exception as e:
                print("거래대금 TOP30 API 조회 실패:", e)

            columns = [
                "조회시간", "순위", "종목코드", "종목명", "현재가",
                "등락률(%)", "거래량", "거래대금", "뉴스요약"
            ]

            return pd.DataFrame(rows, columns=columns)

    return pd.DataFrame(rows)
def get_trade_value_rank(token):
    """
    거래대금 TOP30
    일단 거래량 TOP30 데이터를 가져온 뒤 거래대금 기준으로 재정렬
    """
    df = get_volume_rank(token)

    if df.empty or "거래대금" not in df.columns:
        return pd.DataFrame(columns=[
            "조회시간", "순위", "종목코드", "종목명", "현재가",
            "등락률(%)", "거래량", "거래대금", "뉴스요약"
        ])

    df["거래대금_숫자"] = pd.to_numeric(df["거래대금"], errors="coerce").fillna(0)
    df = df.sort_values(by="거래대금_숫자", ascending=False).head(30).copy()
    df["순위"] = range(1, len(df) + 1)
    df = df.drop(columns=["거래대금_숫자"])

    return df