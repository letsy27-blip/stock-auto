import sqlite3
import pandas as pd
from datetime import datetime, timedelta

DB_NAME = "stock_data.db"


def load_table(table_name: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_NAME)
    try:
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


def clean_code(code) -> str:
    try:
        return str(code).zfill(6)
    except Exception:
        return ""


def find_stock(keyword: str) -> str:
    master_df = load_table("stock_master")

    if master_df.empty:
        return "stock_master 데이터가 없습니다. 먼저 python stock_master.py를 실행하세요."

    master_df["종목코드"] = master_df["종목코드"].astype(str).str.zfill(6)

    result = master_df[
        master_df["종목명"].astype(str).str.contains(keyword, case=False, na=False)
        | master_df["종목코드"].astype(str).str.contains(keyword, case=False, na=False)
    ].copy()

    if result.empty:
        return f"'{keyword}'에 해당하는 종목을 찾지 못했습니다."

    result = result.head(20)

    lines = ["검색 결과:"]
    for _, row in result.iterrows():
        lines.append(
            f"- {row['종목명']} ({row['종목코드']}) / {row.get('시장구분', '')}"
        )

    return "\n".join(lines)


def get_stock_code(stock_name_or_code: str) -> tuple[str, str]:
    master_df = load_table("stock_master")

    if master_df.empty:
        return "", ""

    master_df["종목코드"] = master_df["종목코드"].astype(str).str.zfill(6)

    keyword = str(stock_name_or_code).strip()

    exact_code = master_df[master_df["종목코드"] == keyword.zfill(6)]
    if not exact_code.empty:
        row = exact_code.iloc[0]
        return row["종목코드"], row["종목명"]

    exact_name = master_df[master_df["종목명"] == keyword]
    if not exact_name.empty:
        row = exact_name.iloc[0]
        return row["종목코드"], row["종목명"]

    contains_name = master_df[
        master_df["종목명"].astype(str).str.contains(keyword, case=False, na=False)
    ]

    if not contains_name.empty:
        row = contains_name.iloc[0]
        return row["종목코드"], row["종목명"]

    return "", ""


def get_stock_score(stock_name_or_code: str) -> str:
    code, name = get_stock_code(stock_name_or_code)

    if not code:
        return f"'{stock_name_or_code}' 종목을 찾지 못했습니다."

    score_df = load_table("score_history")

    if score_df.empty:
        return "score_history 데이터가 없습니다."

    score_df["종목코드"] = score_df["종목코드"].astype(str).str.zfill(6)
    stock_df = score_df[score_df["종목코드"] == code].copy()

    if stock_df.empty:
        return f"{name}({code})는 추천점수 이력이 없습니다."

    stock_df["저장일자"] = pd.to_datetime(stock_df["저장일자"])
    stock_df = stock_df.sort_values(["저장일자", "저장시간"])

    latest = stock_df.iloc[-1]

    return f"""
{name}({code}) 최근 추천점수 정보

- 저장일자: {latest.get("저장일자")}
- 저장시간: {latest.get("저장시간")}
- 총점: {latest.get("총점")}
- 등급: {latest.get("등급")}
- 최종추천: {latest.get("최종추천")}
- RSI: {latest.get("RSI")}
- MACD: {latest.get("MACD")}
- AI추천사유: {latest.get("AI추천사유", "")}
"""


def get_stock_price(stock_name_or_code: str, target: str = "latest") -> str:
    code, name = get_stock_code(stock_name_or_code)

    if not code:
        return f"'{stock_name_or_code}' 종목을 찾지 못했습니다."

    chart_df = load_table("chart_history")

    if chart_df.empty:
        return "chart_history 데이터가 없습니다."

    chart_df["종목코드"] = chart_df["종목코드"].astype(str).str.zfill(6)
    chart_df = chart_df[chart_df["종목코드"] == code].copy()

    if chart_df.empty:
        return f"{name}({code})의 차트 데이터가 없습니다."

    chart_df["날짜"] = pd.to_datetime(chart_df["날짜"])
    chart_df["종가"] = pd.to_numeric(chart_df["종가"], errors="coerce")
    chart_df["거래량"] = pd.to_numeric(chart_df.get("거래량", 0), errors="coerce")
    chart_df = chart_df.dropna(subset=["종가"]).sort_values("날짜")

    if chart_df.empty:
        return f"{name}({code})의 유효한 가격 데이터가 없습니다."

    if target in ["yesterday", "어제"]:
        latest_date = chart_df["날짜"].max()
        target_df = chart_df[chart_df["날짜"] < latest_date]

        if target_df.empty:
            row = chart_df.iloc[-1]
        else:
            row = target_df.iloc[-1]
    else:
        row = chart_df.iloc[-1]

    return f"""
{name}({code}) 가격 정보

- 기준일: {row["날짜"].strftime("%Y-%m-%d")}
- 종가: {int(row["종가"]):,}원
- 거래량: {int(row["거래량"]):,}주
"""


def get_stock_history(stock_name_or_code: str, days: int = 30) -> str:
    code, name = get_stock_code(stock_name_or_code)

    if not code:
        return f"'{stock_name_or_code}' 종목을 찾지 못했습니다."

    score_df = load_table("score_history")

    if score_df.empty:
        return "score_history 데이터가 없습니다."

    score_df["종목코드"] = score_df["종목코드"].astype(str).str.zfill(6)
    stock_df = score_df[score_df["종목코드"] == code].copy()

    if stock_df.empty:
        return f"{name}({code})는 추천점수 이력이 없습니다."

    stock_df["저장일자"] = pd.to_datetime(stock_df["저장일자"])
    stock_df["총점"] = pd.to_numeric(stock_df["총점"], errors="coerce")
    stock_df = stock_df.sort_values("저장일자").tail(days)

    first = stock_df.iloc[0]
    last = stock_df.iloc[-1]

    diff = float(last["총점"]) - float(first["총점"])

    return f"""
{name}({code}) 최근 {days}개 기록 기준 추천점수 이력

- 시작일: {first["저장일자"].strftime("%Y-%m-%d")}
- 시작 총점: {first["총점"]}
- 마지막일: {last["저장일자"].strftime("%Y-%m-%d")}
- 마지막 총점: {last["총점"]}
- 점수 변화: {diff:.2f}
- 최근 등급: {last.get("등급", "")}
- 최근 최종추천: {last.get("최종추천", "")}
- 최근 AI추천사유: {last.get("AI추천사유", "")}
"""


def get_today_top30() -> str:
    score_df = load_table("score_history")

    if score_df.empty:
        return "score_history 데이터가 없습니다."

    score_df["저장일자"] = pd.to_datetime(score_df["저장일자"])
    score_df["총점"] = pd.to_numeric(score_df["총점"], errors="coerce")

    latest_date = score_df["저장일자"].max()
    today_df = score_df[score_df["저장일자"] == latest_date].copy()
    today_df = today_df.sort_values("총점", ascending=False).head(30)

    lines = [f"{latest_date.strftime('%Y-%m-%d')} 추천 TOP30"]

    for _, row in today_df.iterrows():
        lines.append(
            f"- {row.get('점수순위', '')}위 {row.get('종목명', '')}({clean_code(row.get('종목코드', ''))}) "
            f"/ 총점 {row.get('총점', '')} / {row.get('등급', '')} / {row.get('최종추천', '')}"
        )

    return "\n".join(lines)


def get_news_summary(stock_name_or_code: str) -> str:
    code, name = get_stock_code(stock_name_or_code)

    if not code:
        return f"'{stock_name_or_code}' 종목을 찾지 못했습니다."

    score_df = load_table("score_history")

    if score_df.empty:
        return "score_history 데이터가 없습니다."

    score_df["종목코드"] = score_df["종목코드"].astype(str).str.zfill(6)
    stock_df = score_df[score_df["종목코드"] == code].copy()

    if stock_df.empty:
        return f"{name}({code})는 저장된 뉴스요약 이력이 없습니다."

    if "뉴스요약" not in stock_df.columns:
        return f"{name}({code}) 뉴스요약 컬럼이 없습니다."

    news_list = stock_df["뉴스요약"].dropna().unique().tolist()
    news_list = [
        n for n in news_list
        if n not in ["관련 뉴스 없음", "뉴스 조회 실패", "종목명 없음", ""]
    ]

    if not news_list:
        return f"{name}({code})의 저장된 관련 뉴스요약은 없습니다."

    lines = [f"{name}({code}) 저장 뉴스요약"]
    for news in news_list[:5]:
        lines.append(f"- {news}")

    return "\n".join(lines)


def get_stock_context(stock_name_or_code: str) -> str:
    return "\n\n".join([
        find_stock(stock_name_or_code),
        get_stock_score(stock_name_or_code),
        get_stock_price(stock_name_or_code),
        get_stock_history(stock_name_or_code),
        get_news_summary(stock_name_or_code),
    ])