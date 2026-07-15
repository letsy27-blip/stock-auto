import os
import re
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ai.gemini_client import DEFAULT_MODEL, stream_chat
from kis_api import get_access_token, get_current_price
from market_data import get_market_overview
from realtime_quotes import get_realtime_quote_hub
from sector_theme_strength import (
    make_industry_strength,
    make_theme_strength,
)

DB_PATH = Path(__file__).resolve().with_name("stock_data.db")
DB_NAME = str(DB_PATH)
REMOTE_DB_URL = (
    "https://raw.githubusercontent.com/letsy27-blip/stock-auto/"
    "main/stock_data.db"
)
DB_SYNC_INTERVAL_SECONDS = 60


def _is_valid_database(path: Path) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(path)
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        has_score = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'score'"
        ).fetchone()
        return quick_check == "ok" and has_score is not None
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def sync_database_from_github() -> None:
    """GitHub Actions가 갱신한 DB를 로컬/배포 대시보드에 반영한다."""
    now = datetime.now().timestamp()
    last_check = st.session_state.get("db_sync_last_check", 0.0)
    if now - last_check < DB_SYNC_INTERVAL_SECONDS:
        return
    st.session_state["db_sync_last_check"] = now

    try:
        head = requests.head(
            REMOTE_DB_URL,
            headers={"Cache-Control": "no-cache"},
            timeout=10,
            allow_redirects=True,
        )
        head.raise_for_status()
        remote_etag = head.headers.get("ETag", "")

        if remote_etag and remote_etag == st.session_state.get("db_sync_etag"):
            return

        response = requests.get(
            REMOTE_DB_URL,
            headers={"Cache-Control": "no-cache"},
            timeout=30,
        )
        response.raise_for_status()

        with NamedTemporaryFile(
            mode="wb",
            suffix=".db",
            prefix="stock_data_",
            dir=DB_PATH.parent,
            delete=False,
        ) as temporary_file:
            temporary_file.write(response.content)
            temporary_path = Path(temporary_file.name)

        try:
            if not _is_valid_database(temporary_path):
                raise RuntimeError("GitHub에서 받은 DB 파일 검증에 실패했습니다.")
            os.replace(temporary_path, DB_PATH)
            st.session_state["db_sync_etag"] = remote_etag
            st.session_state["db_sync_message"] = "GitHub 최신 DB 동기화 완료"
        finally:
            temporary_path.unlink(missing_ok=True)
    except (requests.RequestException, OSError, RuntimeError) as exc:
        # 네트워크 문제여도 기존 DB로 대시보드는 계속 표시한다.
        st.session_state["db_sync_message"] = f"DB 동기화 보류: {exc}"

st.set_page_config(page_title="주식 추천 대시보드", layout="wide")

st.markdown(
    """
    <style>
    div[data-testid="stDialog"] div[role="dialog"],
    div[data-testid="stDialog"] > div,
    div[role="dialog"] {
        width: min(1400px, 96vw) !important;
        max-width: 96vw !important;
    }

    div[data-testid="stDialog"] div[role="dialog"] > div {
        max-width: none !important;
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
        font-size: clamp(20px, 2vw, 28px);
        color: #111827;
        font-weight: 600;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: keep-all;
        line-height: 1.2;
    }

    .stock-grade {
        font-size: clamp(20px, 2vw, 28px);
        color: #111827;
        font-weight: 600;
        white-space: normal;
        overflow-wrap: anywhere;
        word-break: break-all;
        line-height: 1.2;
        letter-spacing: 0;
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

    /* TOP30 클릭 텍스트: 버튼 기능은 유지하고 링크처럼 표시 */
    [class*="st-key-stock_link_"] button,
    [class*="st-key-trading_link_"] button,
    [class*="st-key-news_good_"] button,
    [class*="st-key-news_bad_"] button,
    [class*="st-key-news_neutral_"] button,
    [class*="st-key-news_none_"] button,
    [class*="st-key-news_unanalyzed_"] button {
        background: transparent !important;
        border: 0 !important;
        box-shadow: none !important;
        padding: 0 !important;
        min-height: 0 !important;
        height: auto !important;
        width: auto !important;
        justify-content: flex-start !important;
        text-align: left !important;
        font-size: 16px !important;
        font-weight: 500 !important;
        line-height: 1.35 !important;
        white-space: normal !important;
        word-break: keep-all !important;
        color: #111827 !important;
    }

    [class*="st-key-stock_link_"] button:hover,
    [class*="st-key-trading_link_"] button:hover,
    [class*="st-key-news_good_"] button:hover,
    [class*="st-key-news_bad_"] button:hover,
    [class*="st-key-news_neutral_"] button:hover,
    [class*="st-key-news_none_"] button:hover,
    [class*="st-key-news_unanalyzed_"] button:hover {
        color: #2563EB !important;
        text-decoration: underline !important;
        background: transparent !important;
        border: 0 !important;
    }

    [class*="st-key-stock_link_"] button:focus,
    [class*="st-key-trading_link_"] button:focus,
    [class*="st-key-news_good_"] button:focus,
    [class*="st-key-news_bad_"] button:focus,
    [class*="st-key-news_neutral_"] button:focus,
    [class*="st-key-news_none_"] button:focus,
    [class*="st-key-news_unanalyzed_"] button:focus {
        box-shadow: none !important;
        outline: none !important;
    }

    [class*="st-key-news_good_"] button {
        color: #15803D !important;
    }

    [class*="st-key-news_bad_"] button {
        color: #DC2626 !important;
    }

    [class*="st-key-news_neutral_"] button,
    [class*="st-key-news_none_"] button,
    [class*="st-key-news_unanalyzed_"] button {
        color: #6B7280 !important;
    }

    [class*="st-key-trading_link_"] button {
        color: #374151 !important;
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
        df = pd.read_sql(f'SELECT * FROM "{table_name}"', conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df
# -----------------------------
# DB 상태 검사
# -----------------------------
def get_db_status() -> dict:
    """SQLite DB의 기본 상태를 검사해서 딕셔너리로 반환한다."""

    result = {
        "exists": False,
        "size_mb": 0.0,
        "modified_at": None,
        "tables": [],
        "table_counts": {},
        "latest_dates": {},
        "duplicate_counts": {},
        "errors": [],
    }

    if not os.path.exists(DB_NAME):
        result["errors"].append(f"DB 파일을 찾을 수 없습니다: {DB_NAME}")
        return result

    result["exists"] = True
    result["size_mb"] = os.path.getsize(DB_NAME) / (1024 * 1024)
    result["modified_at"] = datetime.fromtimestamp(
        os.path.getmtime(DB_NAME)
    )

    conn = None

    try:
        conn = sqlite3.connect(DB_NAME)

        table_df = pd.read_sql_query(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """,
            conn,
        )

        tables = table_df["name"].tolist()
        result["tables"] = tables

        for table_name in tables:
            try:
                count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).fetchone()[0]

                result["table_counts"][table_name] = int(count)

                column_df = pd.read_sql_query(
                    f'PRAGMA table_info("{table_name}")',
                    conn,
                )

                columns = column_df["name"].tolist()

                # 저장일자 또는 날짜가 있으면 최신 날짜 확인
                date_column = None

                for candidate in [
                    "저장일자",
                    "날짜",
                    "기준일자",
                    "거래일자",
                ]:
                    if candidate in columns:
                        date_column = candidate
                        break

                if date_column:
                    latest_value = conn.execute(
                        f'''
                        SELECT MAX("{date_column}")
                        FROM "{table_name}"
                        '''
                    ).fetchone()[0]

                    result["latest_dates"][table_name] = latest_value

            except Exception as table_error:
                result["errors"].append(
                    f"{table_name} 검사 실패: {table_error}"
                )

        # 주요 테이블 중복 검사
        duplicate_rules = {
            "stock_master": ["종목코드"],
            "score_history": ["저장일자", "종목코드"],
            "chart_history": ["날짜", "종목코드"],
        }

        for table_name, keys in duplicate_rules.items():
            if table_name not in tables:
                continue

            column_df = pd.read_sql_query(
                f'PRAGMA table_info("{table_name}")',
                conn,
            )
            columns = column_df["name"].tolist()

            existing_keys = [
                key for key in keys if key in columns
            ]

            if len(existing_keys) != len(keys):
                continue

            key_sql = ", ".join(
                f'"{key}"' for key in existing_keys
            )

            duplicate_query = f"""
                SELECT COALESCE(SUM(duplicate_count - 1), 0)
                FROM (
                    SELECT COUNT(*) AS duplicate_count
                    FROM "{table_name}"
                    GROUP BY {key_sql}
                    HAVING COUNT(*) > 1
                )
            """

            duplicate_count = conn.execute(
                duplicate_query
            ).fetchone()[0]

            result["duplicate_counts"][table_name] = int(
                duplicate_count or 0
            )

    except sqlite3.DatabaseError as db_error:
        result["errors"].append(f"DB 오류: {db_error}")

    except Exception as error:
        result["errors"].append(f"검사 오류: {error}")

    finally:
        if conn is not None:
            conn.close()

    return result


def calculate_db_health(status: dict) -> tuple[int, str]:
    """DB 상태를 100점 만점으로 계산한다."""

    score = 100

    if not status["exists"]:
        return 0, "DB 없음"

    if status["errors"]:
        score -= min(len(status["errors"]) * 15, 60)

    if not status["tables"]:
        score -= 40

    empty_tables = sum(
        1
        for count in status["table_counts"].values()
        if count == 0
    )
    score -= min(empty_tables * 5, 20)

    total_duplicates = sum(
        status["duplicate_counts"].values()
    )
    if total_duplicates > 0:
        score -= min(10 + total_duplicates, 30)

    score = max(0, score)

    if score >= 90:
        grade = "매우 양호"
    elif score >= 75:
        grade = "양호"
    elif score >= 50:
        grade = "주의"
    else:
        grade = "점검 필요"

    return score, grade


def show_db_status():
    """Streamlit DB 상태 화면을 표시한다."""

    table_name_map = {
        "chart_history": "차트 이력",
        "portfolio": "보유 종목",
        "portfolio_history": "포트폴리오 이력",
        "rise_rank": "상승률 순위",
        "rise_rank_history": "상승률 순위 이력",
        "score_history": "추천 점수 이력",
        "signal": "매매 신호",
        "signal_history": "매매 신호 이력",
        "stock_master": "종목 마스터",
        "trade_value_rank": "거래대금 순위",
        "trade_value_rank_history": "거래대금 순위 이력",
        "volume_rank": "거래량 순위",
        "volume_rank_history": "거래량 순위 이력",
        "supply_demand_history": "투자자 수급 이력",
        "news_history": "뉴스 원본 이력",
        "news_ai_summary": "AI 뉴스 분석 요약",
        "score_current": "현재 추천 점수",
        "stock_classification": "종목 업종·테마 분류",
        "stock_theme_history": "종목 테마 변경 이력",
        "intraday_snapshot": "장중 30분 스냅샷",
        "market_event_history": "장중 주요 이벤트",
    }

    st.header("DB 상태")

    status = get_db_status()
    health_score, health_grade = calculate_db_health(status)

    if not status["exists"]:
        st.error(f"`{DB_NAME}` 파일을 찾을 수 없습니다.")
        return

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "DB 파일",
        "정상",
    )

    col2.metric(
        "DB 크기",
        f'{status["size_mb"]:.2f} MB',
    )

    modified_text = (
        status["modified_at"].strftime("%Y-%m-%d %H:%M:%S")
        if status["modified_at"]
        else "-"
    )

    col3.metric(
        "마지막 파일 변경",
        modified_text,
    )

    col4.metric(
        "DB 건강도",
        f"{health_score}점",
        health_grade,
    )

    st.subheader("테이블 현황")
    st.caption(
        "중복 데이터는 같은 기준키가 두 번 이상 저장된 행입니다. "
        "검사 대상이 아닌 테이블은 '검사 대상 아님'으로 표시됩니다."
    )

    table_rows = []

    for table_name in status["tables"]:
        row_count = status["table_counts"].get(
            table_name, 0
        )

        latest_date = status["latest_dates"].get(
            table_name, "-"
        )

        duplicate_count = status["duplicate_counts"].get(
            table_name, "-"
        )

        duplicate_text = (
            f"{duplicate_count:,}건"
            if isinstance(duplicate_count, int)
            else "검사 대상 아님"
        )

        table_rows.append(
            {
                "테이블": table_name_map.get(table_name, table_name),
                "DB 테이블명": table_name,
                "데이터 건수": row_count,
                "최신 날짜": latest_date,
                "중복 데이터": duplicate_text,
            }
        )

    if table_rows:
        table_status_df = pd.DataFrame(table_rows)

        st.dataframe(
            table_status_df,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.warning("DB에 테이블이 없습니다.")

    total_rows = sum(status["table_counts"].values())
    total_duplicates = sum(
        status["duplicate_counts"].values()
    )

    summary_col1, summary_col2, summary_col3 = st.columns(3)

    summary_col1.metric(
        "전체 테이블",
        f'{len(status["tables"])}개',
    )

    summary_col2.metric(
        "전체 데이터",
        f"{total_rows:,}건",
    )

    summary_col3.metric(
        "확인된 중복 데이터",
        f"{total_duplicates:,}건",
    )

    if status["errors"]:
        st.subheader("검사 중 발견된 문제")

        for error in status["errors"]:
            st.error(error)

    elif total_duplicates > 0:
        st.warning(
            "일부 테이블에서 중복 데이터가 발견됐습니다. "
            "아직 자동 삭제하지는 않습니다."
        )

    else:
        st.success("기본 DB 검사 결과 이상이 없습니다.")

    if st.button("DB 다시 검사", use_container_width=True):
        st.rerun()


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


def normalize_classification_df(
    classification_df: pd.DataFrame,
) -> pd.DataFrame:
    if classification_df is None or classification_df.empty:
        return pd.DataFrame()

    df = classification_df.copy()

    if "종목코드" in df.columns:
        df["종목코드"] = (
            df["종목코드"]
            .astype(str)
            .str.replace(".0", "", regex=False)
            .str.zfill(6)
        )

    return df


def normalize_theme_history_df(
    theme_history_df: pd.DataFrame,
) -> pd.DataFrame:
    if theme_history_df is None or theme_history_df.empty:
        return pd.DataFrame()

    df = theme_history_df.copy()

    if "종목코드" in df.columns:
        df["종목코드"] = (
            df["종목코드"]
            .astype(str)
            .str.replace(".0", "", regex=False)
            .str.zfill(6)
        )

    if "기준일자" in df.columns:
        df["기준일자"] = pd.to_datetime(
            df["기준일자"],
            errors="coerce",
        )

    return df


def _theme_text(value) -> str:
    themes = _json_list(value)
    return ", ".join(str(theme) for theme in themes) if themes else "-"


def show_classification_summary(
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
    latest_score: pd.Series,
    stock_code: str,
):
    code = clean_code(stock_code)
    classification = pd.Series(dtype="object")

    if (
        classification_df is not None
        and not classification_df.empty
        and "종목코드" in classification_df.columns
    ):
        matched = classification_df[
            classification_df["종목코드"] == code
        ]
        if not matched.empty:
            classification = matched.iloc[-1]

    industry = str(
        classification.get(
            "업종",
            latest_score.get("업종", ""),
        )
        or ""
    ).strip()

    representative_theme = str(
        classification.get(
            "대표테마",
            latest_score.get("대표테마", ""),
        )
        or ""
    ).strip()

    theme_json = classification.get(
        "테마JSON",
        latest_score.get("테마JSON", "[]"),
    )
    confidence = int(
        safe_float(
            classification.get(
                "분류신뢰도",
                latest_score.get("분류신뢰도", 0),
            )
        )
    )
    theme_checked = str(
        classification.get(
            "테마확인일자",
            latest_score.get("테마확인일자", ""),
        )
        or ""
    ).strip()

    st.subheader("업종·테마")

    col1, col2, col3 = st.columns(3)
    with col1:
        metric_card("업종", industry or "미분류")
    with col2:
        metric_card("대표테마", representative_theme or "미분류")
    with col3:
        metric_card("분류 신뢰도", f"{confidence}%")

    st.caption(
        f"테마 목록: {_theme_text(theme_json)}"
        + (f" · 마지막 확인: {theme_checked}" if theme_checked else "")
    )

    reason = str(
        classification.get(
            "분류근거",
            latest_score.get("분류근거", ""),
        )
        or ""
    ).strip()
    if reason:
        st.info(reason)

    if (
        theme_history_df is None
        or theme_history_df.empty
        or "종목코드" not in theme_history_df.columns
    ):
        return

    history = theme_history_df[
        theme_history_df["종목코드"] == code
    ].copy()

    if history.empty:
        return

    sort_columns = [
        column
        for column in ["기준일자", "분석일시", "id"]
        if column in history.columns
    ]
    if sort_columns:
        history = history.sort_values(
            sort_columns,
            ascending=False,
        )

    display_columns = [
        column
        for column in [
            "기준일자",
            "대표테마",
            "테마JSON",
            "테마근거",
            "테마신뢰도",
            "분석일시",
        ]
        if column in history.columns
    ]

    with st.expander(f"테마 변경 이력 ({len(history)}건)"):
        st.dataframe(
            history[display_columns].head(30),
            use_container_width=True,
            hide_index=True,
        )


def make_stock_links(stock_name: str, stock_code: str):
    encoded_name = quote(str(stock_name))
    code = clean_code(stock_code)
    is_naver_stock_code = bool(re.fullmatch(r"\d{6}", code))

    st.subheader("관련 링크")
    col1, col2, col3, col4, col5 = st.columns(5)
    if is_naver_stock_code:
        col1.link_button(
            "실시간 시세·차트",
            f"https://finance.naver.com/item/main.naver?code={code}",
        )
        col2.link_button(
            "일봉 차트",
            f"https://finance.naver.com/item/fchart.naver?code={code}",
        )
    else:
        # KIS에는 영문이 포함된 ETF·ETN 등의 종목코드가 있다. 이 코드는
        # 네이버증권의 6자리 숫자 코드가 아니므로 외부 차트 서비스를 쓴다.
        col1.link_button(
            "TradingView 차트",
            f"https://kr.tradingview.com/symbols/KRX-{code}/",
        )
        col2.link_button(
            "한국경제 차트",
            f"https://markets.hankyung.com/stock/{code}/chart",
        )
    col3.link_button(
        "네이버 뉴스",
        f"https://search.naver.com/search.naver?where=news&query={encoded_name}",
    )
    col4.link_button(
        "유튜브 검색",
        f"https://www.youtube.com/results?search_query={encoded_name}+주식",
    )
    col5.link_button(
        "종목토론방",
        f"https://finance.naver.com/item/board.naver?code={code}",
    )


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
    final_score = safe_float(
        latest.get("최종점수", latest.get("총점", 0))
    )
    grade = str(latest.get("등급", "") or "")
    recommend = str(latest.get("최종추천", "") or "")
    ai_reason = str(latest.get("AI추천사유", "") or "").strip()

    st.subheader("최종 평가")

    col1, col2, col3 = st.columns(3)
    col1.metric("최종점수", f"{final_score:g}점")
    col2.metric("등급", grade or "-")
    col3.metric("최종추천", recommend or "-")

    if ai_reason:
        st.markdown("#### 추천 사유")
        reason_items = [
            item.strip()
            for item in re.split(r"[,|\n]+", ai_reason)
            if item.strip()
        ]
        for item in reason_items:
            st.write(item)

    exclusion_reason = str(
        latest.get("추천제외사유", "")
    ).strip()

    if recommend in {"제외", "약세"} or final_score < 55:
        st.markdown("#### 추천 제외 사유")
        if exclusion_reason:
            reasons = [
                reason.strip()
                for reason in exclusion_reason.split("|")
                if reason.strip()
            ]
            for reason in reasons:
                st.write(reason)
        else:
            st.write("관찰 등급 이상 기준에 미달했습니다.")

    st.markdown("#### 점수 구성")

    score_rows = [
        ("거래량 순위", "거래량점수"),
        ("상승률 순위", "상승률점수"),
        ("거래대금 순위", "거래대금점수"),
        ("20일 추세", "20일수익률점수"),
        ("60일 추세", "60일수익률점수"),
        ("거래량 증가", "거래량증가점수"),
        ("이동평균 정배열", "정배열점수"),
        ("60일 신고가", "신고가점수"),
        ("RSI", "RSI점수"),
        ("MACD", "MACD점수"),
    ]

    breakdown = []
    for label, column in score_rows:
        if column in latest.index:
            breakdown.append(
                {
                    "항목": label,
                    "반영점수": round(
                        safe_float(latest.get(column, 0)),
                        2,
                    ),
                }
            )

    if breakdown:
        st.dataframe(
            pd.DataFrame(breakdown),
            use_container_width=True,
            hide_index=True,
        )

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

    if "종가" not in stock_chart_df.columns or not stock_chart_df["종가"].notna().any():
        st.warning("종가 데이터가 없어 차트를 표시할 수 없습니다.")
        return

    st.subheader("종가 차트")
    interval = st.radio(
        "차트 기준",
        ["일봉", "월봉", "연봉"],
        horizontal=True,
        key=f"chart_interval_{clean_code(stock_code)}",
    )
    chart_data = stock_chart_df[["날짜", "종가", "거래량"]].copy()
    chart_data = chart_data.dropna(subset=["날짜", "종가"]).sort_values("날짜")

    if interval == "월봉":
        chart_data = (
            chart_data.set_index("날짜")
            .resample("ME")
            .agg({"종가": "last", "거래량": "sum"})
            .dropna(subset=["종가"])
            .reset_index()
        )
    elif interval == "연봉":
        chart_data = (
            chart_data.set_index("날짜")
            .resample("YE")
            .agg({"종가": "last", "거래량": "sum"})
            .dropna(subset=["종가"])
            .reset_index()
        )

    fig_price = px.line(
        chart_data,
        x="날짜",
        y="종가",
        markers=True,
        title=f"{stock_name} {interval} 종가 추이",
    )
    st.plotly_chart(fig_price, use_container_width=True)

    if chart_data["거래량"].notna().any():
        fig_volume = px.bar(
            chart_data,
            x="날짜",
            y="거래량",
            title=f"{stock_name} {interval} 거래량",
        )
        st.plotly_chart(fig_volume, use_container_width=True)



# -----------------------------
# 수급·뉴스 상세 분석
# -----------------------------
def normalize_supply_df(supply_df: pd.DataFrame) -> pd.DataFrame:
    if supply_df is None or supply_df.empty:
        return pd.DataFrame()

    df = supply_df.copy()

    if "종목코드" in df.columns:
        df["종목코드"] = (
            df["종목코드"]
            .astype(str)
            .str.replace(".0", "", regex=False)
            .str.zfill(6)
        )

    if "날짜" in df.columns:
        df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")

    for column in [
        "외국인순매수량",
        "기관순매수량",
        "개인순매수량",
        "종가",
        "거래량",
    ]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)

    return df


def normalize_news_df(news_df: pd.DataFrame) -> pd.DataFrame:
    if news_df is None or news_df.empty:
        return pd.DataFrame()

    df = news_df.copy()

    if "종목코드" in df.columns:
        df["종목코드"] = (
            df["종목코드"]
            .astype(str)
            .str.replace(".0", "", regex=False)
            .str.zfill(6)
        )

    if "기사발행일시" in df.columns:
        df["기사발행일시"] = pd.to_datetime(
            df["기사발행일시"],
            errors="coerce",
        )

    return df


def format_signed_quantity(value) -> str:
    number = int(safe_float(value, 0))
    return f"{number:+,}주"


def flow_label(value) -> str:
    number = safe_float(value, 0)
    if number > 0:
        return "순매수"
    if number < 0:
        return "순매도"
    return "중립"




def show_supply_analysis(
    supply_df: pd.DataFrame,
    latest_score: pd.Series,
    stock_code: str,
):
    st.subheader("매매동향")

    code = clean_code(stock_code)
    stock_supply = pd.DataFrame()

    if supply_df is not None and not supply_df.empty:
        stock_supply = supply_df[
            supply_df["종목코드"] == code
        ].copy()

    foreign = safe_float(
        latest_score.get("외국인순매수량", 0)
    )
    institution = safe_float(
        latest_score.get("기관순매수량", 0)
    )
    personal = safe_float(
        latest_score.get("개인순매수량", 0)
    )
    foreign_3d = int(
        safe_float(latest_score.get("외국인3일합계", 0))
    )
    institution_3d = int(
        safe_float(latest_score.get("기관3일합계", 0))
    )
    personal_3d = -(foreign_3d + institution_3d)
    latest_date_text = str(
        latest_score.get("수급기준일", "")
    )
    recent5 = pd.DataFrame()

    if not stock_supply.empty:
        stock_supply["날짜"] = pd.to_datetime(
            stock_supply["날짜"],
            errors="coerce",
        )

        for column in [
            "외국인순매수량",
            "기관순매수량",
            "개인순매수량",
        ]:
            stock_supply[column] = pd.to_numeric(
                stock_supply[column],
                errors="coerce",
            ).fillna(0)

        stock_supply = (
            stock_supply
            .dropna(subset=["날짜"])
            .sort_values("날짜")
            .drop_duplicates(subset=["날짜"], keep="last")
        )

        if not stock_supply.empty:
            latest = stock_supply.iloc[-1]
            foreign = safe_float(
                latest.get("외국인순매수량", 0)
            )
            institution = safe_float(
                latest.get("기관순매수량", 0)
            )
            personal = safe_float(
                latest.get("개인순매수량", 0)
            )
            latest_date_text = latest["날짜"].strftime(
                "%Y-%m-%d"
            )

            recent3 = stock_supply.tail(3)
            recent5 = stock_supply.tail(5)

            foreign_3d = int(
                recent3["외국인순매수량"].sum()
            )
            institution_3d = int(
                recent3["기관순매수량"].sum()
            )
            personal_3d = int(
                recent3["개인순매수량"].sum()
            )

    all_missing = (
        foreign == 0
        and institution == 0
        and personal == 0
    )

    reflected_score = safe_float(
        latest_score.get("수급점수", 0)
    )

    if all_missing:
        st.info("매매동향 없음")
        st.caption(
            f"기준일: {latest_date_text or '확인 불가'} · "
            "오늘 투자자별 매매 데이터가 제공되지 않았습니다."
        )
        st.metric("최종점수 반영", "0점")
        return

    st.caption(
        f"기준일: {latest_date_text or '확인 불가'} · "
        "양수는 순매수, 음수는 순매도입니다."
    )

    row1_col1, row1_col2, row1_col3 = st.columns(3)
    row1_col1.metric(
        f"외국인 {_trend_arrow(foreign)}",
        format_signed_quantity(foreign),
    )
    row1_col2.metric(
        f"기관 {_trend_arrow(institution)}",
        format_signed_quantity(institution),
    )
    row1_col3.metric(
        f"개인 {_trend_arrow(personal)}",
        format_signed_quantity(personal),
    )

    row2_col1, row2_col2, row2_col3 = st.columns(3)
    row2_col1.metric(
        f"외국인 최근 3일 {_trend_arrow(foreign_3d)}",
        format_signed_quantity(foreign_3d),
    )
    row2_col2.metric(
        f"기관 최근 3일 {_trend_arrow(institution_3d)}",
        format_signed_quantity(institution_3d),
    )
    row2_col3.metric(
        f"개인 최근 3일 {_trend_arrow(personal_3d)}",
        format_signed_quantity(personal_3d),
    )

    summary_col1, summary_col2 = st.columns(2)
    summary_col1.metric(
        "종합 판단",
        str(latest_score.get("수급판단", "") or "중립"),
    )
    summary_col2.metric(
        "최종점수 반영",
        f"{reflected_score:+g}점",
    )

    if not recent5.empty:
        chart_columns = [
            column
            for column in [
                "외국인순매수량",
                "기관순매수량",
                "개인순매수량",
            ]
            if column in recent5.columns
        ]

        if chart_columns:
            long_df = recent5.melt(
                id_vars=["날짜"],
                value_vars=chart_columns,
                var_name="투자자",
                value_name="순매수량",
            )

            fig = px.bar(
                long_df,
                x="날짜",
                y="순매수량",
                color="투자자",
                barmode="group",
                title="최근 5거래일 투자자별 매매동향",
            )
            st.plotly_chart(
                fig,
                use_container_width=True,
            )

        with st.expander("매매동향 원본 데이터 보기"):
            st.dataframe(
                stock_supply.sort_values(
                    "날짜",
                    ascending=False,
                ).head(20),
                use_container_width=True,
                hide_index=True,
            )

def _json_list(value):
    if isinstance(value, list):
        return value

    try:
        parsed = json.loads(str(value or "[]"))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def show_news_analysis(
    news_df: pd.DataFrame,
    latest_score: pd.Series,
    stock_code: str,
):
    st.subheader("AI 뉴스 분석")

    code = clean_code(stock_code)
    stock_news = pd.DataFrame()

    if news_df is not None and not news_df.empty:
        stock_news = news_df[
            news_df["종목코드"] == code
        ].copy()

    news_score = safe_float(latest_score.get("뉴스점수", 0))
    news_reason = str(
        latest_score.get(
            "뉴스분석사유",
            latest_score.get("AI뉴스분석사유", ""),
        )
    ).strip()
    news_summary = str(
        latest_score.get(
            "뉴스요약",
            latest_score.get("AI뉴스요약", ""),
        )
    ).strip()
    news_count = int(safe_float(latest_score.get("뉴스건수", 0)))
    judgement = str(
        latest_score.get(
            "뉴스판단",
            latest_score.get("AI뉴스판단", ""),
        )
    ).strip()
    influence_period = str(
        latest_score.get(
            "영향기간",
            latest_score.get("AI영향기간", ""),
        )
    ).strip()
    confidence = int(
        safe_float(
            latest_score.get(
                "신뢰도",
                latest_score.get("AI신뢰도", 0),
            )
        )
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("뉴스점수", f"{news_score:+g}점")
    col2.metric("AI 판단", judgement or "중립")
    col3.metric("분석 뉴스", f"{news_count or len(stock_news)}건")
    col4.metric("신뢰도", f"{confidence}%")

    if news_summary and news_summary != "관련 뉴스 없음":
        st.success(news_summary)
    else:
        st.info("최근 뉴스에서 뚜렷한 호재·악재가 확인되지 않았습니다.")

    if news_reason and news_reason.lower() not in {"nan", "none"}:
        st.write(f"**판단 근거:** {news_reason}")

    if influence_period:
        st.write(f"**예상 영향 기간:** {influence_period}")

    positive_items = _json_list(
        latest_score.get("긍정요인JSON", "[]")
    )
    negative_items = _json_list(
        latest_score.get("부정요인JSON", "[]")
    )
    core_items = _json_list(
        latest_score.get("핵심뉴스JSON", "[]")
    )

    if positive_items or negative_items:
        pos_col, neg_col = st.columns(2)

        with pos_col:
            st.markdown("#### 긍정 요인")
            if positive_items:
                for item in positive_items:
                    st.write(f"• {item}")
            else:
                st.write("뚜렷한 긍정 요인 없음")

        with neg_col:
            st.markdown("#### 부정·위험 요인")
            if negative_items:
                for item in negative_items:
                    st.write(f"• {item}")
            else:
                st.write("뚜렷한 부정 요인 없음")

    if core_items:
        st.markdown("#### 핵심 뉴스")
        for index, item in enumerate(core_items[:5], start=1):
            if isinstance(item, dict):
                title = item.get("제목", "")
                decision = item.get("판단", "")
                impact = item.get("영향도", "")
                reason = item.get("근거", "")
                st.markdown(
                    f"**{index}. {title}**  \n"
                    f"판단: {decision} · 영향도: {impact}  \n"
                    f"{reason}"
                )
            else:
                st.write(f"{index}. {item}")

    if stock_news.empty:
        st.warning(
            "AI 뉴스 요약은 score_history에 남아 있지만 원본 뉴스 행은 news_history에서 찾지 못했습니다. "
            "이 종목이 현재 후보 종목에 포함되지 않았거나, 최근 36시간 RSS 검색 결과가 없었거나, "
            "새 Gemini 뉴스 코드 적용 전에 저장된 점수일 수 있습니다. "
            "`python main.py`를 다시 실행하면 새 뉴스가 있을 때 원본도 함께 저장됩니다."
        )
        return

    stock_news = stock_news.sort_values(
        "기사발행일시",
        ascending=False,
    )

    with st.expander(f"원본 뉴스 전체 보기 ({len(stock_news)}건)"):
        for _, row in stock_news.head(50).iterrows():
            title = str(row.get("뉴스제목", "")).strip()
            source = str(row.get("언론사", "")).strip()
            published = row.get("기사발행일시", "")
            description = str(row.get("뉴스설명", "")).strip()
            url = str(row.get("뉴스URL", "")).strip()

            st.markdown(f"**{title}**")
            st.caption(f"{source} · {published}")

            if description:
                st.write(description)

            if url:
                st.link_button(
                    "기사 열기",
                    url,
                    key=f"news_{code}_{hash(url)}",
                )

            st.divider()

# -----------------------------
# 종목 상세
# -----------------------------
def show_stock_detail_by_code(
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
    stock_name: str,
    stock_code: str,
):
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

    show_classification_summary(
        classification_df=classification_df,
        theme_history_df=theme_history_df,
        latest_score=latest,
        stock_code=stock_code,
    )

    st.divider()

    col1, col2, col3 = st.columns(3)
    with col1:
        metric_card(
            "최종점수",
            latest.get(
                "최종점수",
                latest.get("총점", ""),
            ),
        )
    with col2:
        metric_card(
            "최종추천",
            latest.get("최종추천", ""),
        )
    with col3:
        metric_card(
            "등급",
            latest.get("등급", ""),
            grade=True,
        )

    col4, col5, col6 = st.columns(3)
    with col4:
        metric_card("RSI", latest.get("RSI", ""))
    with col5:
        metric_card(
            "시장기준일",
            latest.get("시장기준일", ""),
        )
    with col6:
        updated = (
            f"{latest.get('최종갱신일자', '')} "
            f"{latest.get('최종갱신시간', '')}"
        ).strip()
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

    st.divider()
    show_supply_analysis(
        supply_df=supply_df,
        latest_score=latest,
        stock_code=stock_code,
    )

    st.divider()
    show_news_analysis(
        news_df=news_df,
        latest_score=latest,
        stock_code=stock_code,
    )

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
        "등급", "최종추천", "추천제외사유", "점수변동사유", "AI추천사유",
        "20일수익률(%)", "60일수익률(%)", "거래량증가율(%)",
        "정배열", "신고가돌파", "RSI", "MACD", "뉴스요약",
    ]
    cols = [c for c in cols if c in stock_score_df.columns]
    st.dataframe(stock_score_df[cols].sort_values(["저장일자", "저장시간"], ascending=False), use_container_width=True)


# -----------------------------
# 팝업
# -----------------------------
def make_stock_dialog(
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
    @st.dialog("종목 상세 분석", width="large")
    def stock_detail_popup(stock_name: str, stock_code: str):
        show_stock_detail_by_code(
            score_df=score_df,
            chart_df=chart_df,
            supply_df=supply_df,
            news_df=news_df,
            classification_df=classification_df,
            theme_history_df=theme_history_df,
            stock_name=stock_name,
            stock_code=stock_code,
        )

    return stock_detail_popup


def make_quick_analysis_dialogs(
    score_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
):
    def get_latest_score(stock_code: str) -> pd.Series:
        code = clean_code(stock_code)

        if (
            score_df is None
            or score_df.empty
            or "종목코드" not in score_df.columns
        ):
            return pd.Series(dtype="object")

        matched = score_df[
            score_df["종목코드"] == code
        ].copy()

        if matched.empty:
            return pd.Series(dtype="object")

        sort_columns = [
            column
            for column in ["저장일자", "저장시간"]
            if column in matched.columns
        ]
        if sort_columns:
            matched = matched.sort_values(sort_columns)

        return matched.iloc[-1]

    @st.dialog("매매동향", width="large")
    def trading_popup(stock_name: str, stock_code: str):
        st.subheader(f"{stock_name} ({clean_code(stock_code)})")
        latest_score = get_latest_score(stock_code)

        if latest_score.empty:
            st.warning("추천점수 데이터가 없습니다.")
            return

        show_supply_analysis(
            supply_df=supply_df,
            latest_score=latest_score,
            stock_code=stock_code,
        )

    @st.dialog("뉴스 분석", width="large")
    def news_popup(stock_name: str, stock_code: str):
        st.subheader(f"{stock_name} ({clean_code(stock_code)})")
        latest_score = get_latest_score(stock_code)

        if latest_score.empty:
            st.warning("추천점수 데이터가 없습니다.")
            return

        show_news_analysis(
            news_df=news_df,
            latest_score=latest_score,
            stock_code=stock_code,
        )

    return trading_popup, news_popup


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


def make_news_status_text(row: pd.Series) -> str:
    status = str(row.get("뉴스평가상태", "") or "").strip()
    summary = str(row.get("뉴스요약", "") or "").strip()
    reason = str(row.get("뉴스분석사유", "") or "").strip()
    score = safe_float(row.get("뉴스점수", 0))

    failed_words = ("실패", "오류", "quota", "429", "resource_exhausted")
    combined = f"{status} {summary} {reason}".lower()

    if any(word in combined for word in failed_words):
        return "미분석"

    if status in {"평가대기", "미분석"}:
        return "미분석"

    if status == "뉴스없음" or summary in {
        "",
        "관련 뉴스 없음",
        "뉴스 조회 실패",
        "종목명 없음",
    }:
        return "뉴스 없음"

    if score > 0:
        return "호재"
    if score < 0:
        return "악재"
    return "중립"


def _trend_arrow(value) -> str:
    number = safe_float(value, 0)
    if number > 0:
        return "↑"
    if number < 0:
        return "↓"
    return "-"


def _latest_trading_values(
    row: pd.Series,
    supply_df: pd.DataFrame,
    stock_code: str,
) -> tuple[float, float, float, bool]:
    foreign = safe_float(row.get("외국인순매수량", 0))
    institution = safe_float(row.get("기관순매수량", 0))
    personal = safe_float(row.get("개인순매수량", 0))

    code = clean_code(stock_code)

    if (
        supply_df is not None
        and not supply_df.empty
        and "종목코드" in supply_df.columns
    ):
        stock_supply = supply_df.copy()
        stock_supply["종목코드"] = (
            stock_supply["종목코드"]
            .astype(str)
            .str.replace(".0", "", regex=False)
            .str.zfill(6)
        )
        stock_supply = stock_supply[
            stock_supply["종목코드"] == code
        ].copy()

        if not stock_supply.empty:
            if "날짜" in stock_supply.columns:
                stock_supply["날짜"] = pd.to_datetime(
                    stock_supply["날짜"],
                    errors="coerce",
                )
                stock_supply = stock_supply.sort_values("날짜")

            latest = stock_supply.iloc[-1]

            foreign = safe_float(
                latest.get("외국인순매수량", foreign)
            )
            institution = safe_float(
                latest.get("기관순매수량", institution)
            )
            personal = safe_float(
                latest.get("개인순매수량", personal)
            )

    all_missing = (
        foreign == 0
        and institution == 0
        and personal == 0
    )

    return foreign, institution, personal, all_missing


def make_trading_trend_text(
    row: pd.Series,
    supply_df: pd.DataFrame,
    stock_code: str,
) -> str:
    foreign, institution, personal, all_missing = (
        _latest_trading_values(
            row=row,
            supply_df=supply_df,
            stock_code=stock_code,
        )
    )

    if all_missing:
        return "매매동향 없음"

    return (
        f"외{_trend_arrow(foreign)} "
        f"기{_trend_arrow(institution)} "
        f"개{_trend_arrow(personal)}"
    )


# -----------------------------
# 오늘 추천 TOP30
# -----------------------------



def show_today_top(
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
    selected_date,
):
    today_df = score_df[
        score_df["저장일자"] == selected_date
    ].copy()

    today_df = today_df.sort_values(
        ["최종점수", "시장점수"],
        ascending=False,
    )

    st.subheader(f"{selected_date} 추천점수 TOP30")

    detail_popup = make_stock_dialog(
        score_df=score_df,
        chart_df=chart_df,
        supply_df=supply_df,
        news_df=news_df,
        classification_df=classification_df,
        theme_history_df=theme_history_df,
    )

    trading_popup, news_popup = make_quick_analysis_dialogs(
        score_df=score_df,
        supply_df=supply_df,
        news_df=news_df,
    )

    # 추천 사유와 상세 버튼을 제거하고 종목명 영역을 넓힘
    widths = [
        0.55,  # 순위
        1.0,   # 종목코드
        4.8,   # 종목명
        0.85,  # 시장
        1.65,  # 매매동향
        1.0,   # 뉴스
        0.85,  # 최종
        1.0,   # 추천
    ]

    headers = [
        "순위",
        "종목코드",
        "종목명",
        "시장",
        "매매동향",
        "뉴스",
        "최종",
        "추천",
    ]

    header_cols = st.columns(widths)
    for col, label in zip(header_cols, headers):
        col.markdown(f"**{label}**")

    for _, row in today_df.head(30).iterrows():
        code = clean_code(row.get("종목코드", ""))
        name = str(row.get("종목명", "")).strip()
        cols = st.columns(widths)

        cols[0].markdown(
            f"<div class='normal-text'>{row.get('점수순위', '')}</div>",
            unsafe_allow_html=True,
        )

        cols[1].markdown(
            f"<div class='normal-text'>{code}</div>",
            unsafe_allow_html=True,
        )

        # 종목명 자체를 클릭하면 전체 상세 팝업
        if cols[2].button(
            name,
            key=f"stock_link_{selected_date}_{code}",
        ):
            detail_popup(name, code)

        market_score = safe_float(row.get("시장점수", 0))
        cols[3].markdown(
            f"<div class='normal-text'>{market_score:.2f}</div>",
            unsafe_allow_html=True,
        )

        trading_label = make_trading_trend_text(
            row=row,
            supply_df=supply_df,
            stock_code=code,
        )

        if cols[4].button(
            trading_label,
            key=f"trading_link_{selected_date}_{code}",
        ):
            trading_popup(name, code)

        news_label = make_news_status_text(row)

        if news_label == "호재":
            news_key_prefix = "news_good"
        elif news_label == "악재":
            news_key_prefix = "news_bad"
        elif news_label == "중립":
            news_key_prefix = "news_neutral"
        elif news_label == "뉴스 없음":
            news_key_prefix = "news_none"
        else:
            news_key_prefix = "news_unanalyzed"

        if cols[5].button(
            news_label,
            key=f"{news_key_prefix}_{selected_date}_{code}",
        ):
            news_popup(name, code)

        final_score = safe_float(
            row.get("최종점수", row.get("총점", 0))
        )
        cols[6].markdown(
            f"<div class='normal-text'>{final_score:.2f}</div>",
            unsafe_allow_html=True,
        )

        recommendation = str(
            row.get("최종추천", "")
        ).strip()

        recommendation_colors = {
            "강력관심": "#15803D",
            "관심": "#16A34A",
            "관찰": "#CA8A04",
            "약세": "#EA580C",
            "제외": "#DC2626",
        }
        recommendation_color = recommendation_colors.get(
            recommendation,
            "#374151",
        )

        cols[7].markdown(
            (
                "<div class='normal-text' "
                f"style='color:{recommendation_color};"
                "font-weight:600;'>"
                f"{recommendation}"
                "</div>"
            ),
            unsafe_allow_html=True,
        )

def show_stock_search(
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    master_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
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

    show_stock_detail_by_code(
        score_df=score_df,
        chart_df=chart_df,
        supply_df=supply_df,
        news_df=news_df,
        classification_df=classification_df,
        theme_history_df=theme_history_df,
        stock_name=selected_name,
        stock_code=selected_code,
    )




# -----------------------------
# 시장 현황
# -----------------------------
@st.cache_data(ttl=20, show_spinner=False)
def load_market_overview_cached():
    token = get_access_token()
    if not token:
        return {
            "error": "KIS 접근토큰 발급에 실패했습니다."
        }

    try:
        return get_market_overview(token)
    except Exception as exc:
        return {"error": str(exc)}


def _market_number(value, decimals=2):
    number = safe_float(value, 0)
    return f"{number:,.{decimals}f}"


def _market_delta_text(item):
    change = safe_float(item.get("change", 0))
    rate = safe_float(item.get("change_rate", 0))
    return f"{change:+,.2f} ({rate:+.2f}%)"


def calculate_market_temperature(overview):
    """
    0~100의 단순 시장온도.
    KOSPI·KOSDAQ 등락률과 환율 방향을 이용한 1차 버전이다.
    """
    if not isinstance(overview, dict):
        return 50, "판단불가"

    kospi_rate = safe_float(
        overview.get("KOSPI", {}).get("change_rate", 0)
    )
    kosdaq_rate = safe_float(
        overview.get("KOSDAQ", {}).get("change_rate", 0)
    )
    fx_rate = safe_float(
        overview.get("USD/KRW", {}).get("change_rate", 0)
    )

    score = 50
    score += max(-20, min(20, kospi_rate * 8))
    score += max(-20, min(20, kosdaq_rate * 8))
    # 원·달러 상승은 국내주식에 대체로 부담으로 반영
    score -= max(-10, min(10, fx_rate * 5))
    score = int(max(0, min(100, round(score))))

    if score >= 75:
        label = "강세"
    elif score >= 60:
        label = "다소 강세"
    elif score >= 40:
        label = "중립"
    elif score >= 25:
        label = "다소 약세"
    else:
        label = "약세"

    return score, label


def show_market_overview():
    st.header("시장 현황")

    overview = load_market_overview_cached()

    if "error" in overview:
        st.error(overview["error"])
        return

    kospi = overview.get("KOSPI", {})
    kosdaq = overview.get("KOSDAQ", {})
    usdkrw = overview.get("USD/KRW", {})

    temperature, temperature_label = (
        calculate_market_temperature(overview)
    )

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "코스피",
        _market_number(kospi.get("current")),
        _market_delta_text(kospi),
    )
    col1.caption(
        f"전일 { _market_number(kospi.get('previous')) }"
    )

    col2.metric(
        "코스닥",
        _market_number(kosdaq.get("current")),
        _market_delta_text(kosdaq),
    )
    col2.caption(
        f"전일 { _market_number(kosdaq.get('previous')) }"
    )

    col3.metric(
        "원·달러 환율",
        f"{_market_number(usdkrw.get('current'))}원",
        _market_delta_text(usdkrw),
    )
    col3.caption(
        f"전일 {_market_number(usdkrw.get('previous'))}원"
    )

    col4.metric(
        "시장온도",
        f"{temperature}점",
        temperature_label,
    )

    errors = []
    for name, item in overview.items():
        if item.get("error"):
            errors.append(f"{name}: {item['error']}")

    updated_times = [
        item.get("updated_at")
        for item in overview.values()
        if isinstance(item, dict) and item.get("updated_at")
    ]

    if updated_times:
        st.caption(
            "시장 기준시각: "
            f"{max(updated_times)} · "
            "코스피·코스닥 KIS 조회, 환율은 제공처에 따라 지연될 수 있음"
        )

    if errors:
        for error in errors:
            st.warning(error)

    if st.button("시장 현황 새로고침"):
        load_market_overview_cached.clear()
        st.rerun()



def _show_strength_chart(
    df: pd.DataFrame,
    name_column: str,
    strength_column: str,
    title: str,
):
    if df.empty:
        st.info(f"{title} 데이터가 아직 없습니다.")
        return

    top = df.head(15).copy()

    fig = px.bar(
        top.sort_values(strength_column),
        x=strength_column,
        y=name_column,
        orientation="h",
        hover_data=[
            column
            for column in [
                "후보종목수",
                "평균최종점수",
                "최고최종점수",
                "평균시장점수",
                "평균수급점수",
                "평균뉴스점수",
                "강한종목비율(%)",
                "대표종목",
                "대표종목점수",
            ]
            if column in top.columns
        ],
        title=title,
    )
    fig.update_layout(
        xaxis_title="강도",
        yaxis_title="",
    )
    st.plotly_chart(
        fig,
        use_container_width=True,
    )

    st.dataframe(
        df.head(30),
        use_container_width=True,
        hide_index=True,
    )


def show_sector_strength(
    current_df: pd.DataFrame,
    classification_df: pd.DataFrame,
):
    st.header("업종·테마 강도")

    industry_df = make_industry_strength(
        current_df=current_df,
        classification_df=classification_df,
    )
    theme_df = make_theme_strength(
        current_df=current_df,
        classification_df=classification_df,
    )

    if industry_df.empty and theme_df.empty:
        st.warning(
            "업종·테마 분류 데이터가 없습니다. "
            "`python main.py --once`를 한 번 실행하면 "
            "현재 후보 종목을 Gemini가 분류하고 DB에 저장합니다."
        )
        return

    tab1, tab2 = st.tabs(
        [
            "강한 테마",
            "강한 업종",
        ]
    )

    with tab1:
        _show_strength_chart(
            theme_df,
            "테마",
            "테마강도",
            "현재 강한 투자 테마 TOP15",
        )

    with tab2:
        _show_strength_chart(
            industry_df,
            "업종",
            "업종강도",
            "현재 강한 업종 TOP15",
        )

@st.cache_data(ttl=20, show_spinner=False)
def load_recommendation_quotes(stock_codes: tuple[str, ...]) -> dict[str, dict]:
    """메인 추천 카드에 표시할 KIS 현재가를 한 번에 조회한다."""
    token = get_access_token()
    if not token:
        return {}

    quotes: dict[str, dict] = {}
    for code in stock_codes:
        body = get_current_price(token, code)
        output = body.get("output", {}) if isinstance(body, dict) else {}
        quotes[code] = {
            "price": safe_float(output.get("stck_prpr")),
            "change": safe_float(output.get("prdy_vrss")),
            "change_rate": safe_float(output.get("prdy_ctrt")),
        }
    return quotes


def show_realtime_recommendations(
    current_df: pd.DataFrame,
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
    st.header("실시간 추천 TOP 3")

    if current_df.empty:
        st.info("추천 점수 데이터가 아직 없습니다.")
        return

    df = current_df.copy()
    score_column = "최종점수" if "최종점수" in df.columns else "총점"
    if score_column not in df.columns:
        st.info("추천 점수 컬럼을 찾지 못했습니다.")
        return

    df[score_column] = pd.to_numeric(df[score_column], errors="coerce").fillna(0)
    # 단순 급등·거래량 순위가 아니라 실제 추천 가능한 후보만 메인에 노출한다.
    # 약세/제외 종목은 TOP3 분석 대상일 수는 있어도 추천 카드가 될 수 없다.
    recommendation = df.get("최종추천", pd.Series("", index=df.index)).astype(str)
    eligible = df[(df[score_column] >= 55) & ~recommendation.isin(["약세", "제외"])]
    top3 = eligible.sort_values(score_column, ascending=False).head(3).copy()
    if top3.empty:
        st.info("현재 기준으로 55점 이상인 추천 가능 종목이 없습니다. 급등 테마주를 억지로 추천하지 않습니다.")
        return

    detail_popup = make_stock_dialog(
        score_df=score_df,
        chart_df=chart_df,
        supply_df=supply_df,
        news_df=news_df,
        classification_df=classification_df,
        theme_history_df=theme_history_df,
    )
    codes = tuple(clean_code(code) for code in top3["종목코드"].tolist())
    realtime_enabled = st.toggle(
        "초단위 체결가 반영",
        value=True,
        key="realtime_recommendation_quotes",
    )
    quotes: dict[str, dict] = {}
    realtime_status = ""
    if realtime_enabled:
        hub = get_realtime_quote_hub()
        hub.ensure_codes(codes)
        realtime = hub.snapshot(codes)
        quotes = realtime["quotes"]
        if realtime["connected"]:
            realtime_status = "KIS WebSocket 실시간 연결됨"
        elif realtime["error"]:
            realtime_status = f"실시간 연결 재시도 중: {realtime['error']}"
        else:
            realtime_status = "KIS WebSocket 연결 중"
        st_autorefresh(interval=1000, key="realtime_recommendation_refresh")

    if not quotes:
        quotes = load_recommendation_quotes(codes)

    columns = st.columns(len(top3))
    for index, (_, row) in enumerate(top3.iterrows()):
        code = clean_code(row.get("종목코드", ""))
        name = str(row.get("종목명", code))
        quote_data = quotes.get(code, {})
        price = quote_data.get("price", 0)
        change = quote_data.get("change", 0)
        change_rate = quote_data.get("change_rate", 0)

        if price:
            value = f"{price:,.0f}원"
            delta = f"{change:+,.0f}원 ({change_rate:+.2f}%)"
        else:
            value = "—"
            delta = "KIS 현재가 미수신"

        columns[index].markdown(f"**{index + 1}. {name}**")
        columns[index].metric("현재가", value, delta)
        recommendation = str(row.get("최종추천", "추천 검토"))
        columns[index].caption(
            f"{code} · 점수 {row[score_column]:.2f} · {recommendation}"
        )
        if columns[index].button("상세 분석 보기", key=f"realtime_detail_{code}"):
            detail_popup(name, code)

    if realtime_enabled:
        st.caption(realtime_status)
    else:
        st.caption("현재가는 KIS REST 조회값이며, 약 20초마다 새로 조회됩니다.")


def show_market_home(
    current_df: pd.DataFrame,
    history_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    master_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
    combined_df = pd.concat(
        [history_df, current_df],
        ignore_index=True,
        sort=False,
    )
    combined_df = normalize_score_df(combined_df)

    show_ai_analysis(
        score_df=combined_df,
        chart_df=chart_df,
        master_df=master_df,
    )

    st.divider()
    show_market_overview()

    st.divider()
    show_realtime_recommendations(
        current_df=current_df,
        score_df=combined_df,
        chart_df=chart_df,
        supply_df=supply_df,
        news_df=news_df,
        classification_df=classification_df,
        theme_history_df=theme_history_df,
    )


def show_intraday_flow(snapshot_df: pd.DataFrame):
    st.header("장중 흐름")

    tab1, tab2, tab3 = st.tabs(
        [
            "최근 30분 점수 변화",
            "오늘 TOP30 유지율",
            "진입·이탈 이벤트",
        ]
    )

    with tab1:
        show_intraday_risers(snapshot_df)

    with tab2:
        show_intraday_repeat(snapshot_df)

    with tab3:
        event_df = load_table("market_event_history")

        if event_df.empty:
            st.info("오늘 저장된 진입·이탈 이벤트가 없습니다.")
        else:
            if "이벤트일시" in event_df.columns:
                event_df["이벤트일시"] = pd.to_datetime(
                    event_df["이벤트일시"],
                    errors="coerce",
                )
                today = pd.Timestamp.now().date()
                event_df = event_df[
                    event_df["이벤트일시"].dt.date == today
                ]

            if event_df.empty:
                st.info("오늘 저장된 진입·이탈 이벤트가 없습니다.")
            else:
                event_df = event_df.sort_values(
                    "이벤트일시",
                    ascending=False,
                )
                st.dataframe(
                    event_df,
                    use_container_width=True,
                    hide_index=True,
                )


def show_past_analysis(history_df: pd.DataFrame):
    st.header("과거 분석")

    if history_df.empty:
        st.info("장 마감 일별 데이터가 아직 없습니다.")
        return

    tab1, tab2 = st.tabs(
        [
            "날짜별 추천",
            "일별 반복 추천",
        ]
    )

    with tab1:
        dates = sorted(
            history_df["저장일자"].dropna().unique(),
            reverse=True,
        )
        selected_date = st.selectbox(
            "과거 조회 날짜",
            dates,
            key="past_analysis_date",
        )
        daily = history_df[
            history_df["저장일자"] == selected_date
        ].copy()

        columns = [
            column
            for column in [
                "점수순위",
                "종목코드",
                "종목명",
                "시장점수",
                "수급점수",
                "뉴스점수",
                "최종점수",
                "최종추천",
            ]
            if column in daily.columns
        ]

        st.dataframe(
            daily[columns].sort_values(
                "최종점수",
                ascending=False,
            ),
            use_container_width=True,
            hide_index=True,
        )

    with tab2:
        show_repeat_stocks(history_df)


# -----------------------------
# 장중 30분 분석
# -----------------------------
def show_current_status_banner(current_df: pd.DataFrame):
    if current_df.empty:
        st.error(
            "현재 추천 데이터가 없습니다. "
            "`python main.py`를 실행하세요."
        )
        return

    updated = pd.NaT

    # 현재 실제 score 테이블의 최신 저장시간을 우선 사용
    if (
        "저장일자" in current_df.columns
        and "저장시간" in current_df.columns
    ):
        date_text = current_df["저장일자"].fillna("").astype(str)
        time_text = current_df["저장시간"].fillna("").astype(str)

        updated = pd.to_datetime(
            date_text + " " + time_text,
            errors="coerce",
        ).max()

    # 저장일자/저장시간이 없을 때만 보조 컬럼 사용
    if pd.isna(updated) and "갱신일시" in current_df.columns:
        updated = pd.to_datetime(
            current_df["갱신일시"],
            errors="coerce",
        ).max()

    if (
        pd.isna(updated)
        and "최종갱신일자" in current_df.columns
        and "최종갱신시간" in current_df.columns
    ):
        date_text = (
            current_df["최종갱신일자"]
            .fillna("")
            .astype(str)
        )
        time_text = (
            current_df["최종갱신시간"]
            .fillna("")
            .astype(str)
        )

        updated = pd.to_datetime(
            date_text + " " + time_text,
            errors="coerce",
        ).max()

    if pd.isna(updated):
        st.warning("현재 데이터의 갱신시각을 확인할 수 없습니다.")
        return

    age_minutes = (
        pd.Timestamp.now() - updated
    ).total_seconds() / 60

    if age_minutes > 45:
        st.error(
            f"현재 추천 데이터가 {age_minutes:.0f}분 동안 "
            "갱신되지 않았습니다. "
            "현재 투자 판단에 사용하지 마세요."
        )
    elif age_minutes > 20:
        st.warning(
            f"마지막 갱신: {updated:%Y-%m-%d %H:%M:%S} "
            f"({age_minutes:.0f}분 전)"
        )
    else:
        st.success(
            f"장중 최신 데이터 · 마지막 갱신 "
            f"{updated:%Y-%m-%d %H:%M:%S}"
        )

def show_intraday_risers(snapshot_df: pd.DataFrame):
    st.subheader("장중 점수 상승 종목")

    if snapshot_df.empty:
        st.info("장중 스냅샷이 아직 없습니다.")
        return

    df = snapshot_df.copy()
    df["스냅샷일시"] = pd.to_datetime(
        df["스냅샷일시"],
        errors="coerce",
    )
    today = pd.Timestamp.now().date()
    df = df[df["스냅샷일시"].dt.date == today]

    times = sorted(df["스냅샷일시"].dropna().unique())
    if len(times) < 2:
        st.info("비교하려면 오늘 스냅샷이 최소 2회 필요합니다.")
        return

    previous_time, current_time = times[-2], times[-1]
    previous = df[df["스냅샷일시"] == previous_time][
        ["종목코드", "종목명", "최종점수", "현재순위"]
    ].copy()
    current = df[df["스냅샷일시"] == current_time][
        ["종목코드", "종목명", "최종점수", "현재순위"]
    ].copy()

    compare = pd.merge(
        current,
        previous,
        on="종목코드",
        how="outer",
        suffixes=("_현재", "_이전"),
    )

    compare["종목명"] = compare["종목명_현재"].fillna(
        compare["종목명_이전"]
    )
    compare["점수변화"] = (
        pd.to_numeric(compare["최종점수_현재"], errors="coerce").fillna(0)
        - pd.to_numeric(compare["최종점수_이전"], errors="coerce").fillna(0)
    )
    compare["순위변화"] = (
        pd.to_numeric(compare["현재순위_이전"], errors="coerce")
        - pd.to_numeric(compare["현재순위_현재"], errors="coerce")
    )

    compare = compare.sort_values("점수변화", ascending=False)

    st.caption(
        f"{pd.Timestamp(previous_time):%H:%M} → "
        f"{pd.Timestamp(current_time):%H:%M} 비교"
    )
    st.dataframe(
        compare[
            [
                "종목코드",
                "종목명",
                "최종점수_이전",
                "최종점수_현재",
                "점수변화",
                "현재순위_이전",
                "현재순위_현재",
                "순위변화",
            ]
        ].head(50),
        use_container_width=True,
        hide_index=True,
    )


def show_intraday_repeat(snapshot_df: pd.DataFrame):
    st.subheader("오늘 장중 TOP30 유지 종목")

    if snapshot_df.empty:
        st.info("장중 스냅샷이 아직 없습니다.")
        return

    df = snapshot_df.copy()
    df["스냅샷일시"] = pd.to_datetime(
        df["스냅샷일시"],
        errors="coerce",
    )
    today = pd.Timestamp.now().date()
    df = df[df["스냅샷일시"].dt.date == today]

    if df.empty:
        st.info("오늘 저장된 장중 스냅샷이 없습니다.")
        return

    total_snapshots = df["스냅샷일시"].nunique()
    top30 = df[
        pd.to_numeric(df["현재순위"], errors="coerce") <= 30
    ]

    result = (
        top30.groupby(["종목코드", "종목명"])
        .agg(
            TOP30유지횟수=("스냅샷일시", "nunique"),
            평균점수=("최종점수", "mean"),
            최고점수=("최종점수", "max"),
            최저점수=("최종점수", "min"),
            최근순위=("현재순위", "last"),
        )
        .reset_index()
    )

    result["유지율(%)"] = (
        result["TOP30유지횟수"] / total_snapshots * 100
    ).round(1)

    result = result.sort_values(
        ["TOP30유지횟수", "평균점수"],
        ascending=False,
    )

    st.caption(f"오늘 저장된 스냅샷: {total_snapshots}회")
    st.dataframe(
        result,
        use_container_width=True,
        hide_index=True,
    )

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

    st.session_state.gemini_model = DEFAULT_MODEL

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

    # 화면을 열어 둔 상태에서도 1분마다 새 DB를 확인한다.
    st_autorefresh(
        interval=DB_SYNC_INTERVAL_SECONDS * 1000,
        key="dashboard_db_auto_refresh",
    )
    sync_database_from_github()

    all_score_df = normalize_score_df(
        load_table("score")
    )

    history_df = all_score_df.copy()

    if all_score_df.empty:
        current_df = pd.DataFrame()
    else:
        update_datetime = pd.to_datetime(
            all_score_df["저장일자"].astype(str)
            + " "
            + all_score_df["저장시간"].astype(str),
            errors="coerce",
        )

        latest_update = update_datetime.max()

        current_df = all_score_df[
            update_datetime == latest_update
            ].copy()

    snapshot_df = load_table("intraday_snapshot")

    chart_df = load_table("chart_history")

    supply_df = normalize_supply_df(
        load_table("supply_demand")
    )

    news_df = normalize_news_df(
        load_table("news_history")
    )
    master_df = normalize_master_df(
        load_table("stock_master")
    )

    classification_df = normalize_classification_df(
        load_table("stock_classification")
    )

    theme_history_df = normalize_theme_history_df(
        load_table("stock_theme_history")
    )

    menu = st.sidebar.radio(
        "메뉴",
        [
            "시장 현황",
            "현재 추천 TOP30",
            "종목 검색",
            "업종·테마 강도",
            "장중 흐름",
            "과거 분석",
            "DB 상태",
        ],
    )

    if menu == "시장 현황":
        show_market_home(
            current_df=current_df,
            history_df=history_df,
            chart_df=chart_df,
            master_df=master_df,
            supply_df=supply_df,
            news_df=news_df,
            classification_df=classification_df,
            theme_history_df=theme_history_df,
        )
        return

    if menu == "업종·테마 강도":
        show_sector_strength(
            current_df=current_df,
            classification_df=classification_df,
        )
        return

    if menu == "DB 상태":
        show_db_status()
        return

    if menu == "현재 추천 TOP30":
        show_current_status_banner(current_df)

        if current_df.empty:
            return

        display_df = current_df.copy()
        display_df["저장일자"] = pd.Timestamp.now().date()

        show_today_top(
            score_df=display_df,
            chart_df=chart_df,
            supply_df=supply_df,
            news_df=news_df,
            classification_df=classification_df,
            theme_history_df=theme_history_df,
            selected_date=pd.Timestamp.now().date(),
        )
        return

    if menu == "장중 흐름":
        show_intraday_flow(snapshot_df)
        return

    if menu == "종목 검색":
        combined_df = pd.concat(
            [history_df, current_df],
            ignore_index=True,
            sort=False,
        )
        combined_df = normalize_score_df(combined_df)

        show_stock_search(
            score_df=combined_df,
            chart_df=chart_df,
            supply_df=supply_df,
            news_df=news_df,
            master_df=master_df,
            classification_df=classification_df,
            theme_history_df=theme_history_df,
        )
        return

    if menu == "과거 분석":
        show_past_analysis(history_df)
        return



if __name__ == "__main__":
    main()
