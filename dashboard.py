import re
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import streamlit as st

from ai.gemini_client import DEFAULT_MODEL, stream_chat

DB_NAME = "stock_data.db"

st.set_page_config(page_title="주식 추천 대시보드", layout="wide")

st.markdown(
    """
    <style>
    div[data-testid="stDialog"] div[role="dialog"] {
        width: 1200px !important;
        max-width: 95vw !important;
    }

    div[data-testid="stDialog"] {
        width: 1200px !important;
        max-width: 95vw !important;
    }

    .metric-card {
        border: 1px solid #E5E7EB;
        border-radius: 10px;
        padding: 14px 16px;
        background-color: #FFFFFF;
        min-height: 95px;
        margin-bottom: 12px;
    }

    .metric-label {
        font-size: 14px;
        color: #374151;
        margin-bottom: 8px;
    }

    .metric-value {
        font-size: 28px;
        color: #111827;
        font-weight: 600;
        white-space: nowrap;
    }

    .stock-grade {
        font-size: 28px;
        color: #111827;
        font-weight: 600;
        white-space: nowrap;
        letter-spacing: 1px;
    }

    .normal-text {
        font-size: 16px;
        color: #111827;
        font-weight: 400;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: keep-all;
        line-height: 1.35;
    }

    .stock-name-text {
        font-size: 16px;
        color: #111827;
        font-weight: 500;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: keep-all;
        line-height: 1.35;
    }

    .reason-text {
        font-size: 14px;
        color: #374151;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: keep-all;
        line-height: 1.35;
    }

    .top-row {
        border-bottom: 1px solid #E5E7EB;
        padding: 8px 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# 공통 유틸
# -----------------------------
def load_table(table_name: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_NAME)
    try:
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


def safe_float(value, default=0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def clean_code(code) -> str:
    return str(code).replace(".0", "").zfill(6)


def normalize_score_df(score_df: pd.DataFrame) -> pd.DataFrame:
    if score_df.empty:
        return score_df

    df = score_df.copy()

    if "저장일자" in df.columns:
        df["저장일자"] = pd.to_datetime(df["저장일자"], errors="coerce").dt.date
    if "저장시간" not in df.columns:
        df["저장시간"] = "00:00:00"
    df["저장시간"] = df["저장시간"].fillna("00:00:00").astype(str)

    # 기존 데이터와 신규 점수 구조가 한 테이블에 섞여 있어도 행별로 보정한다.
    # 컬럼이 없을 때 df.get(..., 0)은 정수 0을 반환하므로, 항상 Series로 만들어야 한다.
    def numeric_series(column_name: str, default: float = 0.0) -> pd.Series:
        if column_name in df.columns:
            return pd.to_numeric(df[column_name], errors="coerce")
        return pd.Series(default, index=df.index, dtype="float64")

    total_raw = numeric_series("총점", 0.0)
    news_raw = numeric_series("뉴스점수", 0.0).fillna(0.0)
    ai_raw = numeric_series("AI점수", 0.0).fillna(0.0)

    if "최종점수" in df.columns:
        final_raw = pd.to_numeric(df["최종점수"], errors="coerce")
        final_score = final_raw.where(final_raw.notna(), total_raw)
    else:
        final_score = total_raw
    final_score = final_score.fillna(0)

    if "시장점수" in df.columns:
        market_raw = pd.to_numeric(df["시장점수"], errors="coerce")
        # 구버전의 총점에는 뉴스점수가 포함되어 있으므로 시장점수는 역산한다.
        inferred_market = final_score - news_raw - ai_raw
        market_score = market_raw.where(market_raw.notna(), inferred_market)
    else:
        market_score = final_score - news_raw - ai_raw

    df["시장점수"] = market_score.clip(lower=0).round(2)
    df["뉴스점수"] = news_raw.round(2)
    df["AI점수"] = ai_raw.round(2)
    df["최종점수"] = final_score.round(2)
    df["총점"] = df["최종점수"]

    if "시장기준일" not in df.columns:
        df["시장기준일"] = ""
    else:
        df["시장기준일"] = df["시장기준일"].fillna("")

    if "최종갱신일자" not in df.columns:
        df["최종갱신일자"] = df["저장일자"].astype(str) if "저장일자" in df.columns else ""
    else:
        fallback_date = df["저장일자"].astype(str) if "저장일자" in df.columns else ""
        df["최종갱신일자"] = df["최종갱신일자"].fillna(fallback_date)

    if "최종갱신시간" not in df.columns:
        df["최종갱신시간"] = df["저장시간"]
    else:
        df["최종갱신시간"] = df["최종갱신시간"].fillna(df["저장시간"])

    if "점수변동사유" not in df.columns:
        df["점수변동사유"] = ""
    else:
        df["점수변동사유"] = df["점수변동사유"].fillna("")

    if "종목코드" in df.columns:
        df["종목코드"] = (
            df["종목코드"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
        )

    return df


def normalize_master_df(master_df: pd.DataFrame) -> pd.DataFrame:
    if master_df.empty:
        return master_df
    df = master_df.copy()
    if "종목코드" in df.columns:
        df["종목코드"] = df["종목코드"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    return df


def make_stock_links(stock_name: str, stock_code: str):
    encoded_name = quote(str(stock_name))
    code = clean_code(stock_code)

    st.subheader("관련 링크")
    col1, col2, col3, col4 = st.columns(4)
    col1.link_button("네이버증권", f"https://finance.naver.com/item/main.naver?code={code}")
    col2.link_button("네이버 뉴스", f"https://search.naver.com/search.naver?where=news&query={encoded_name}")
    col3.link_button("유튜브 검색", f"https://www.youtube.com/results?search_query={encoded_name}+주식")
    col4.link_button("종목토론방", f"https://finance.naver.com/item/board.naver?code={code}")


def metric_card(label: str, value, grade: bool = False):
    css_class = "stock-grade" if grade else "metric-value"
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{label}</div>
            <div class="{css_class}">{value if value is not None else ''}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# -----------------------------
# 점수 설명
# -----------------------------
def show_metric_explainers(latest: pd.Series):
    market_score = safe_float(latest.get("시장점수", latest.get("총점", 0)))
    news_score = safe_float(latest.get("뉴스점수", 0))
    ai_score = safe_float(latest.get("AI점수", 0))
    final_score = safe_float(latest.get("최종점수", latest.get("총점", 0)))
    rsi = safe_float(latest.get("RSI", 0))
    grade = latest.get("등급", "")
    recommend = latest.get("최종추천", "")

    market_cols = [
        "거래량점수", "상승률점수", "거래대금점수",
        "20일수익률점수", "60일수익률점수", "거래량증가점수",
        "정배열점수", "신고가점수", "RSI점수", "MACD점수",
    ]

    with st.expander(f"시장점수 {market_score:g}점인 이유"):
        rows = []
        for col in market_cols:
            if col in latest.index:
                rows.append({"항목": col, "점수": latest.get(col, 0)})
        st.write("시장 순위와 기술지표 점수를 합산한 값입니다.")
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)

    with st.expander(f"뉴스점수 {news_score:+g}점 / AI점수 {ai_score:+g}점"):
        st.write("뉴스점수는 호재·악재 평가값이며, AI점수는 향후 별도 AI 평가에 사용합니다.")
        st.write(f"점수변동사유: {latest.get('점수변동사유', '기록 없음')}")

    with st.expander(f"최종점수 {final_score:g}점, 등급 {grade}인 이유"):
        st.write("최종점수 = 시장점수 + 뉴스점수 + AI점수이며 0~100점 범위로 제한합니다.")
        st.write("85점 이상: ★★★★★")
        st.write("70점 이상: ★★★★☆")
        st.write("55점 이상: ★★★☆☆")
        st.write("40점 이상: ★★☆☆☆")
        st.write("40점 미만: ★☆☆☆☆")

    with st.expander(f"최종추천 '{recommend}'인 이유"):
        if final_score >= 85:
            st.write("85점 이상이라 강력관심입니다.")
        elif final_score >= 70:
            st.write("70점 이상이라 관심입니다.")
        elif final_score >= 55:
            st.write("55점 이상이라 관찰입니다.")
        elif final_score >= 40:
            st.write("40점 이상이라 약세입니다.")
        else:
            st.write("40점 미만이라 제외입니다.")

    with st.expander(f"RSI {rsi:g} 해석"):
        if rsi >= 80:
            st.write("RSI가 80 이상이라 과열 부담이 큽니다.")
        elif rsi >= 70:
            st.write("RSI가 70 이상이라 과열권에 가깝습니다.")
        elif rsi >= 45:
            st.write("RSI가 45~70 구간이라 과열 부담은 크지 않습니다.")
        elif rsi >= 30:
            st.write("RSI가 30~45 구간이라 약세 또는 과매도 근처입니다.")
        else:
            st.write("RSI가 30 미만이라 과매도 구간입니다.")


# -----------------------------
# 차트
# -----------------------------
def get_stock_chart_df(chart_df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if chart_df.empty or "종목코드" not in chart_df.columns:
        return pd.DataFrame()

    df = chart_df.copy()
    df["종목코드"] = df["종목코드"].astype(str).str.replace(".0", "", regex=False).str.zfill(6)
    stock_df = df[df["종목코드"] == clean_code(stock_code)].copy()

    if stock_df.empty or "날짜" not in stock_df.columns:
        return pd.DataFrame()

    stock_df["날짜"] = pd.to_datetime(stock_df["날짜"], errors="coerce")
    stock_df["종가"] = pd.to_numeric(stock_df.get("종가"), errors="coerce")
    stock_df["거래량"] = pd.to_numeric(stock_df.get("거래량"), errors="coerce")
    stock_df = stock_df.dropna(subset=["날짜"]).sort_values("날짜")
    return stock_df


def show_price_volume_charts(chart_df: pd.DataFrame, stock_name: str, stock_code: str):
    stock_chart_df = get_stock_chart_df(chart_df, stock_code)

    if stock_chart_df.empty:
        st.warning("해당 종목의 차트 데이터가 없습니다. 추천 대상에 들어온 적이 없거나 아직 일봉 데이터가 저장되지 않았습니다.")
        return

    if "종가" in stock_chart_df.columns and stock_chart_df["종가"].notna().any():
        st.subheader("주가 차트")
        fig_price = px.line(
            stock_chart_df,
            x="날짜",
            y="종가",
            markers=True,
            title=f"{stock_name} 종가 추이",
        )
        st.plotly_chart(fig_price, use_container_width=True)

    if "거래량" in stock_chart_df.columns and stock_chart_df["거래량"].notna().any():
        st.subheader("거래량 차트")
        fig_volume = px.bar(
            stock_chart_df,
            x="날짜",
            y="거래량",
            title=f"{stock_name} 거래량 추이",
        )
        st.plotly_chart(fig_volume, use_container_width=True)


# -----------------------------
# 종목 상세
# -----------------------------
def show_stock_detail_by_code(score_df: pd.DataFrame, chart_df: pd.DataFrame, stock_name: str, stock_code: str):
    stock_code = clean_code(stock_code)
    stock_score_df = score_df[score_df["종목코드"] == stock_code].copy() if not score_df.empty else pd.DataFrame()

    st.subheader(f"{stock_name} ({stock_code})")

    if stock_score_df.empty:
        st.warning("추천점수 이력이 없습니다. TOP30/추천 대상에 들어온 적이 없는 종목입니다.")
        make_stock_links(stock_name, stock_code)
        show_price_volume_charts(chart_df, stock_name, stock_code)
        return

    stock_score_df = stock_score_df.sort_values(["저장일자", "저장시간"])
    latest = stock_score_df.iloc[-1]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        metric_card("시장점수", latest.get("시장점수", ""))
    with col2:
        metric_card("뉴스점수", f"{safe_float(latest.get('뉴스점수', 0)):+g}")
    with col3:
        metric_card("최종점수", latest.get("최종점수", latest.get("총점", "")))
    with col4:
        metric_card("최종추천", latest.get("최종추천", ""))

    col5, col6, col7, col8 = st.columns(4)
    with col5:
        metric_card("등급", latest.get("등급", ""), grade=True)
    with col6:
        metric_card("RSI", latest.get("RSI", ""))
    with col7:
        metric_card("시장기준일", latest.get("시장기준일", ""))
    with col8:
        updated = f"{latest.get('최종갱신일자', '')} {latest.get('최종갱신시간', '')}".strip()
        metric_card("최종갱신", updated)

    change_reason = make_score_change_text(score_df, latest)
    st.info(f"점수변동: {change_reason}")

    ai_reason = latest.get("AI추천사유", "")
    news = latest.get("뉴스요약", "")
    st.info(ai_reason if ai_reason else "AI 추천사유 없음")

    if news and news not in ["관련 뉴스 없음", "뉴스 조회 실패", "종목명 없음"]:
        st.success(f"뉴스요약: {news}")
    else:
        st.warning("저장된 관련 뉴스가 없습니다. 아래 네이버 뉴스 버튼으로 직접 확인하세요.")

    show_metric_explainers(latest)
    make_stock_links(stock_name, stock_code)

    st.divider()
    st.subheader("점수 추이")
    plot_df = stock_score_df.copy()
    score_columns = [c for c in ["시장점수", "뉴스점수", "최종점수"] if c in plot_df.columns]
    if score_columns:
        long_df = plot_df.melt(
            id_vars=["저장일자"],
            value_vars=score_columns,
            var_name="점수구분",
            value_name="점수",
        )
        fig_score = px.line(
            long_df,
            x="저장일자",
            y="점수",
            color="점수구분",
            markers=True,
            title=f"{stock_name} 시장·뉴스·최종점수 추이",
        )
        st.plotly_chart(fig_score, use_container_width=True)

    show_price_volume_charts(chart_df, stock_name, stock_code)

    st.subheader("상세 이력")
    cols = [
        "저장일자", "저장시간", "시장기준일", "최종갱신일자", "최종갱신시간",
        "점수순위", "시장점수", "뉴스점수", "AI점수", "최종점수", "총점",
        "등급", "최종추천", "점수변동사유", "AI추천사유",
        "20일수익률(%)", "60일수익률(%)", "거래량증가율(%)",
        "정배열", "신고가돌파", "RSI", "MACD", "뉴스요약",
    ]
    cols = [c for c in cols if c in stock_score_df.columns]
    st.dataframe(stock_score_df[cols].sort_values(["저장일자", "저장시간"], ascending=False), use_container_width=True)


# -----------------------------
# 팝업
# -----------------------------
def make_stock_dialog(score_df: pd.DataFrame, chart_df: pd.DataFrame):
    @st.dialog("종목 상세 분석")
    def stock_detail_popup(stock_name: str, stock_code: str):
        show_stock_detail_by_code(score_df, chart_df, stock_name, stock_code)

    return stock_detail_popup


def _format_change(value: float) -> str:
    if abs(value) < 0.005:
        return "변동 없음"
    return f"{value:+.2f}점"


def make_score_change_text(score_df: pd.DataFrame, current_row: pd.Series) -> str:
    """같은 종목의 직전 저장값과 비교해 세부 점수 변화를 만든다."""
    code = clean_code(current_row.get("종목코드", ""))
    current_date = current_row.get("저장일자")
    current_time = str(current_row.get("저장시간", "00:00:00"))

    history = score_df[score_df["종목코드"] == code].copy()
    if history.empty:
        return "최초 저장 데이터"

    history = history.sort_values(["저장일자", "저장시간"])
    previous = history[
        (history["저장일자"] < current_date)
        | ((history["저장일자"] == current_date) & (history["저장시간"].astype(str) < current_time))
    ]

    if previous.empty:
        return "최초 저장 데이터"

    prev = previous.iloc[-1]
    market_diff = safe_float(current_row.get("시장점수")) - safe_float(prev.get("시장점수"))
    news_diff = safe_float(current_row.get("뉴스점수")) - safe_float(prev.get("뉴스점수"))
    ai_diff = safe_float(current_row.get("AI점수")) - safe_float(prev.get("AI점수"))
    final_diff = safe_float(current_row.get("최종점수")) - safe_float(prev.get("최종점수"))

    parts = [
        f"시장 {_format_change(market_diff)}",
        f"뉴스 {_format_change(news_diff)}",
    ]
    if abs(ai_diff) >= 0.005:
        parts.append(f"AI {_format_change(ai_diff)}")
    parts.append(f"최종 {_format_change(final_diff)}")

    # 새 코드가 기록한 원인이 있으면 뒤에 붙인다.
    stored_reason = str(current_row.get("점수변동사유", "")).strip()
    if stored_reason and "기존 데이터" not in stored_reason:
        parts.append(stored_reason)

    return " / ".join(parts)


# -----------------------------
# 오늘 추천 TOP30
# -----------------------------
def show_today_top(score_df: pd.DataFrame, chart_df: pd.DataFrame, selected_date):
    today_df = score_df[score_df["저장일자"] == selected_date].copy()
    today_df = today_df.sort_values(["최종점수", "시장점수"], ascending=False)

    st.subheader(f"{selected_date} 추천점수 TOP30")

    popup = make_stock_dialog(score_df, chart_df)

    widths = [0.7, 1.1, 2.8, 0.9, 0.9, 0.9, 1.1, 4.2, 0.8]
    header_cols = st.columns(widths)
    headers = ["순위", "종목코드", "종목명", "시장", "뉴스", "최종", "추천", "점수변동/추천사유", "상세"]
    for col, text in zip(header_cols, headers):
        col.markdown(f"**{text}**")

    for _, row in today_df.head(30).iterrows():
        code = clean_code(row.get("종목코드", ""))
        name = row.get("종목명", "")
        cols = st.columns(widths)
        cols[0].markdown(f"<div class='normal-text'>{row.get('점수순위', '')}</div>", unsafe_allow_html=True)
        cols[1].markdown(f"<div class='normal-text'>{code}</div>", unsafe_allow_html=True)
        cols[2].markdown(f"<div class='stock-name-text'>{name}</div>", unsafe_allow_html=True)
        cols[3].markdown(f"<div class='normal-text'>{row.get('시장점수', '')}</div>", unsafe_allow_html=True)
        cols[4].markdown(f"<div class='normal-text'>{safe_float(row.get('뉴스점수', 0)):+g}</div>", unsafe_allow_html=True)
        cols[5].markdown(f"<div class='normal-text'>{row.get('최종점수', row.get('총점', ''))}</div>", unsafe_allow_html=True)
        cols[6].markdown(f"<div class='normal-text'>{row.get('최종추천', '')}</div>", unsafe_allow_html=True)

        change_text = make_score_change_text(score_df, row)
        recommendation_reason = str(row.get("AI추천사유", "")).strip()
        display_reason = change_text
        if recommendation_reason:
            display_reason += f"<br><span style='color:#6B7280'>추천: {recommendation_reason}</span>"
        cols[7].markdown(f"<div class='reason-text'>{display_reason}</div>", unsafe_allow_html=True)

        if cols[8].button("상세", key=f"top_detail_{selected_date}_{code}"):
            popup(name, code)


# -----------------------------
# 종목 검색
# -----------------------------
def show_stock_search(score_df: pd.DataFrame, chart_df: pd.DataFrame, master_df: pd.DataFrame):
    st.subheader("종목 검색")

    if master_df.empty:
        st.warning("stock_master가 없습니다. 먼저 python stock_master.py 를 실행하세요.")
        return

    keyword = st.text_input("종목명 또는 종목코드 입력")

    if not keyword:
        return

    search_df = master_df[
        master_df["종목명"].astype(str).str.contains(keyword, case=False, na=False)
        | master_df["종목코드"].astype(str).str.contains(keyword, case=False, na=False)
    ].copy()

    if search_df.empty:
        st.warning("검색 결과가 없습니다.")
        return

    search_df = search_df.sort_values(["시장구분", "종목명"])
    st.write(f"검색 결과: {len(search_df)}개")

    options = [f"{row['종목명']} ({row['종목코드']}) - {row['시장구분']}" for _, row in search_df.iterrows()]
    selected = st.selectbox("종목 선택", options)

    selected_code = selected.split("(")[1].split(")")[0]
    selected_name = selected.split(" (")[0]

    show_stock_detail_by_code(score_df, chart_df, selected_name, selected_code)


# -----------------------------
# 점수 비교 / 반복 추천
# -----------------------------
def show_score_compare(score_df: pd.DataFrame):
    st.subheader("전일 대비 점수 상승 종목")
    dates = sorted(score_df["저장일자"].dropna().unique(), reverse=True)

    if len(dates) < 2:
        st.info("전일 비교를 하려면 최소 2일 이상 데이터가 필요합니다.")
        return

    today = dates[0]
    yesterday = dates[1]

    today_score = score_df[score_df["저장일자"] == today][["종목코드", "종목명", "총점"]].copy()
    yesterday_score = score_df[score_df["저장일자"] == yesterday][["종목코드", "총점"]].copy()

    compare_df = pd.merge(today_score, yesterday_score, on="종목코드", how="inner", suffixes=("_오늘", "_전일"))
    compare_df["점수변화"] = compare_df["총점_오늘"] - compare_df["총점_전일"]
    compare_df = compare_df.sort_values("점수변화", ascending=False)

    st.dataframe(compare_df.head(30), use_container_width=True)


def show_repeat_stocks(score_df: pd.DataFrame):
    st.subheader("최근 반복 추천 종목")

    dates = sorted(score_df["저장일자"].dropna().unique(), reverse=True)
    recent_dates = dates[:5]
    recent_df = score_df[score_df["저장일자"].isin(recent_dates)].copy()

    repeat_df = (
        recent_df.groupby(["종목코드", "종목명"])
        .agg(등장횟수=("저장일자", "nunique"), 평균점수=("총점", "mean"), 최고점수=("총점", "max"))
        .reset_index()
    )

    repeat_df = repeat_df[repeat_df["등장횟수"] >= 3]
    repeat_df = repeat_df.sort_values(["등장횟수", "평균점수"], ascending=False)

    st.dataframe(repeat_df, use_container_width=True)


# -----------------------------
# ai 분석 기능
# -----------------------------
def find_stock_from_question(question: str, master_df: pd.DataFrame):
    if master_df.empty or not question:
        return None

    q = str(question).strip()

    # 종목코드 6자리 우선 탐색
    code_match = re.search(r"\b\d{6}\b", q)
    if code_match:
        code = code_match.group(0)
        hit = master_df[master_df["종목코드"] == code]
        if not hit.empty:
            row = hit.iloc[0]
            return row["종목명"], row["종목코드"]

    # 긴 종목명부터 매칭
    candidates = master_df.copy()
    candidates["name_len"] = candidates["종목명"].astype(str).str.len()
    candidates = candidates.sort_values("name_len", ascending=False)

    for _, row in candidates.iterrows():
        name = str(row["종목명"])
        if name and name in q:
            return name, row["종목코드"]

    # 부분 매칭 fallback
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", q)
    for token in tokens:
        if len(token) < 2:
            continue
        hit = master_df[master_df["종목명"].astype(str).str.contains(token, case=False, na=False)]
        if not hit.empty:
            row = hit.iloc[0]
            return row["종목명"], row["종목코드"]

    return None


def parse_period_from_question(question: str, score_df: pd.DataFrame):
    if score_df.empty or "저장일자" not in score_df.columns:
        return None, None

    min_date = min(score_df["저장일자"].dropna())
    max_date = max(score_df["저장일자"].dropna())
    q = str(question)

    # YYYY-MM-DD 형식 2개
    iso_dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", q)
    if len(iso_dates) >= 2:
        s = pd.to_datetime(iso_dates[0], errors="coerce").date()
        e = pd.to_datetime(iso_dates[1], errors="coerce").date()
        return s, e

    # M월 D일부터 D일까지
    m = re.search(r"(\d{1,2})월\s*(\d{1,2})일.*?(\d{1,2})일", q)
    if m:
        year = max_date.year
        month = int(m.group(1))
        start_day = int(m.group(2))
        end_day = int(m.group(3))
        return date(year, month, start_day), date(year, month, end_day)

    # 최근 N일
    m = re.search(r"최근\s*(\d{1,3})일", q)
    if m:
        days = int(m.group(1))
        return max_date - timedelta(days=days), max_date

    return min_date, max_date


def summarize_stock_from_db(stock_name: str, stock_code: str, question: str, score_df: pd.DataFrame, chart_df: pd.DataFrame):
    stock_code = clean_code(stock_code)
    start_date, end_date = parse_period_from_question(question, score_df)

    lines = []
    lines.append(f"### {stock_name}({stock_code}) ai 분석")

    if start_date and end_date:
        lines.append(f"분석 기간: {start_date} ~ {end_date}")

    # score history 분석
    stock_score = pd.DataFrame()
    if not score_df.empty:
        stock_score = score_df[score_df["종목코드"] == stock_code].copy()
        if start_date and end_date:
            stock_score = stock_score[(stock_score["저장일자"] >= start_date) & (stock_score["저장일자"] <= end_date)]
        stock_score = stock_score.sort_values(["저장일자", "저장시간"])

    if not stock_score.empty:
        first = stock_score.iloc[0]
        last = stock_score.iloc[-1]
        first_score = safe_float(first.get("총점", 0))
        last_score = safe_float(last.get("총점", 0))
        diff = last_score - first_score

        lines.append(f"추천점수는 {first_score:.2f}점에서 {last_score:.2f}점으로 {diff:+.2f}점 변했습니다.")
        lines.append(f"최근 등급은 {last.get('등급', '')}, 최종추천은 {last.get('최종추천', '')}입니다.")

        if "RSI" in stock_score.columns:
            avg_rsi = pd.to_numeric(stock_score["RSI"], errors="coerce").mean()
            if pd.notna(avg_rsi):
                if avg_rsi >= 70:
                    lines.append(f"평균 RSI는 {avg_rsi:.2f}로 과열 부담이 있는 편입니다.")
                elif avg_rsi >= 45:
                    lines.append(f"평균 RSI는 {avg_rsi:.2f}로 비교적 안정적인 구간입니다.")
                elif avg_rsi >= 30:
                    lines.append(f"평균 RSI는 {avg_rsi:.2f}로 약세 또는 과매도 근처입니다.")
                else:
                    lines.append(f"평균 RSI는 {avg_rsi:.2f}로 과매도 구간에 가깝습니다.")

        reason = str(last.get("AI추천사유", ""))
        if reason:
            lines.append(f"최근 추천사유: {reason}")

        news_list = []
        if "뉴스요약" in stock_score.columns:
            news_list = stock_score["뉴스요약"].dropna().astype(str).unique().tolist()
            news_list = [n for n in news_list if n not in ["관련 뉴스 없음", "뉴스 조회 실패", "종목명 없음", ""]]
        if news_list:
            lines.append("저장된 뉴스요약:")
            for n in news_list[:5]:
                lines.append(f"- {n}")
        else:
            lines.append("DB에 저장된 뉴스요약은 없습니다. 최신 뉴스는 아래 링크에서 확인하세요.")
    else:
        lines.append("이 종목은 선택 기간에 추천점수 이력이 없습니다. TOP30/추천 대상에 들어온 적이 없을 가능성이 큽니다.")

    # chart history 분석
    stock_chart = get_stock_chart_df(chart_df, stock_code)
    if not stock_chart.empty:
        if start_date and end_date:
            stock_chart = stock_chart[
                (stock_chart["날짜"].dt.date >= start_date) & (stock_chart["날짜"].dt.date <= end_date)
            ]

        if len(stock_chart) >= 2 and "종가" in stock_chart.columns:
            first_price = safe_float(stock_chart["종가"].iloc[0])
            last_price = safe_float(stock_chart["종가"].iloc[-1])
            if first_price:
                price_rate = ((last_price / first_price) - 1) * 100
                if price_rate > 5:
                    trend = "상승세"
                elif price_rate < -5:
                    trend = "하락세"
                else:
                    trend = "횡보에 가까운 흐름"
                lines.append(f"주가는 {first_price:,.0f}원에서 {last_price:,.0f}원으로 {price_rate:+.2f}% 변해, 기간 중 {trend}로 볼 수 있습니다.")

        if "거래량" in stock_chart.columns and stock_chart["거래량"].notna().any():
            recent_vol = stock_chart["거래량"].tail(5).mean()
            base_vol = stock_chart["거래량"].head(5).mean()
            if base_vol and pd.notna(base_vol):
                vol_rate = ((recent_vol / base_vol) - 1) * 100
                lines.append(f"최근 5일 평균 거래량은 초반 5일 평균 대비 {vol_rate:+.2f}% 변화했습니다.")
    else:
        lines.append("차트 데이터가 아직 저장되어 있지 않습니다. 추천 대상에 들어오거나 별도 일봉 조회 기능을 붙이면 차트 기반 분석이 가능합니다.")

    q = question.lower()
    if "뉴스" in question or "관련뉴스" in question:
        lines.append("관련 뉴스는 네이버 뉴스 검색 버튼을 이용하면 바로 확인할 수 있습니다.")
    if "유튜브" in question or "youtube" in q:
        lines.append("관련 유튜브 영상은 유튜브 검색 버튼을 이용하면 바로 확인할 수 있습니다.")

    return "\n\n".join(lines)


def show_ai_analysis(score_df: pd.DataFrame, chart_df: pd.DataFrame, master_df: pd.DataFrame):
    st.subheader("Gemini AI")
    st.caption("현재 단계는 Gemini 자유 대화 기능입니다. 다음 단계에서 SQLite 조회 도구를 연결합니다.")

    if "gemini_messages" not in st.session_state:
        st.session_state.gemini_messages = [
            {
                "role": "assistant",
                "content": "안녕하세요. 주식과 투자에 관해 무엇이든 질문해 주세요.",
            }
        ]

    top_col1, top_col2 = st.columns([3, 1])
    with top_col1:
        model_name = st.text_input(
            "Gemini 모델",
            value=st.session_state.get("gemini_model", DEFAULT_MODEL),
            help="사용 가능한 모델이 다르면 .env의 GEMINI_MODEL 또는 이 입력값을 변경하세요.",
        )
        st.session_state.gemini_model = model_name.strip() or DEFAULT_MODEL

    with top_col2:
        st.write("")
        st.write("")
        if st.button("대화 초기화", use_container_width=True):
            st.session_state.gemini_messages = [
                {
                    "role": "assistant",
                    "content": "대화를 초기화했습니다. 새 질문을 입력해 주세요.",
                }
            ]
            st.rerun()

    for message in st.session_state.gemini_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("Gemini에게 질문하세요")
    if not prompt:
        return

    st.session_state.gemini_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            history = [
                message
                for message in st.session_state.gemini_messages
                if message.get("content")
            ]
            response_text = st.write_stream(
                stream_chat(history, model=st.session_state.gemini_model)
            )
            if not response_text:
                response_text = "Gemini가 빈 응답을 반환했습니다. 다시 질문해 주세요."
                st.warning(response_text)
        except Exception as exc:
            response_text = f"Gemini 연결 오류: {exc}"
            st.error(response_text)

    st.session_state.gemini_messages.append(
        {"role": "assistant", "content": response_text}
    )


# -----------------------------
# 메인
# -----------------------------
def main():
    st.title("주식 추천 대시보드")

    score_df = normalize_score_df(load_table("score_history"))
    chart_df = load_table("chart_history")
    master_df = normalize_master_df(load_table("stock_master"))

    if score_df.empty:
        st.warning("score_history 데이터가 없습니다. 먼저 main.py를 실행하세요.")
        return

    dates = sorted(score_df["저장일자"].dropna().unique(), reverse=True)
    selected_date = st.sidebar.selectbox("조회 날짜", dates)

    menu = st.sidebar.radio(
        "메뉴",
        ["오늘 추천 TOP30", "종목 검색", "점수 상승 종목", "반복 추천 종목", "Gemini AI"],
    )

    if menu == "오늘 추천 TOP30":
        show_today_top(score_df, chart_df, selected_date)
    elif menu == "종목 검색":
        show_stock_search(score_df, chart_df, master_df)
    elif menu == "점수 상승 종목":
        show_score_compare(score_df)
    elif menu == "반복 추천 종목":
        show_repeat_stocks(score_df)
    elif menu == "Gemini AI":
        show_ai_analysis(score_df, chart_df, master_df)


if __name__ == "__main__":
    main()