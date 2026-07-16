import os
import re
import json
import sqlite3
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from ai.gemini_client import DEFAULT_MODEL, stream_chat
from kis_api import get_access_token, get_current_price
from market_data import get_market_overview
from realtime_quotes import get_realtime_quote_hub
from paper_trading import (
    get_account as get_paper_account,
    get_investor_profile,
    get_orders as get_paper_orders,
    get_positions as get_paper_positions,
    is_paper_user_authenticated,
    is_remote_storage_enabled,
    place_order as place_paper_order,
    record_behavior_event,
    reset_account as reset_paper_account,
)
from prediction_tracker import get_prediction_summary
from sector_theme_strength import (
    make_industry_strength,
    make_theme_strength,
)
from supabase_auth import is_admin_user, show_auth_sidebar


# 다크 모드에서 캔버스형 dataframe을 HTML 표로 대체할 때 원본 함수를 보관한다.
_NATIVE_DATAFRAME = st.dataframe

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

        # 로컬 수집기(main.py)가 방금 저장한 DB를 오래된 GitHub 사본으로
        # 덮어쓰면 예측 검증 기록과 최신 수집 결과가 사라질 수 있다.
        remote_modified_text = head.headers.get("Last-Modified")
        if remote_modified_text and DB_PATH.exists():
            try:
                remote_modified = parsedate_to_datetime(remote_modified_text).timestamp()
                local_modified = DB_PATH.stat().st_mtime
                if local_modified >= remote_modified:
                    st.session_state["db_sync_etag"] = remote_etag
                    st.session_state["db_sync_message"] = "로컬 최신 DB 유지"
                    return
            except (TypeError, ValueError, OSError):
                # 헤더 해석에 실패하면 기존 방식대로 GitHub DB를 검증해 반영한다.
                pass

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

st.set_page_config(page_title="HONG STOCK | 이유를 기록하는 주식 분석", layout="wide")

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

    .hongstock-welcome-note {
        padding: 18px 4px 8px;
        color: #111827;
        background: transparent;
    }

    .hongstock-welcome-meta {
        margin-bottom: 26px;
        color: #6B7280;
        font-size: 14px;
        font-weight: 600;
    }

    .hongstock-welcome-title {
        margin: 0 0 18px;
        color: #111827;
        font-size: clamp(28px, 3.2vw, 42px);
        font-weight: 800;
        line-height: 1.2;
        letter-spacing: -0.04em;
    }

    .hongstock-welcome-copy {
        max-width: 780px;
        margin: 0 0 12px;
        color: #374151;
        font-size: 16px;
        line-height: 1.85;
    }

    .hongstock-welcome-principles {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 0;
        margin-top: 28px;
        border-top: 1px solid #E5E7EB;
        border-bottom: 1px solid #E5E7EB;
    }

    .hongstock-welcome-principles > div {
        padding: 17px 18px 18px;
    }

    .hongstock-welcome-principles > div + div {
        border-left: 1px solid #E5E7EB;
    }

    .hongstock-welcome-principles em {
        display: block;
        margin-bottom: 9px;
        color: #6B7280;
        font-size: 13px;
        font-style: normal;
        font-weight: 800;
    }

    .hongstock-welcome-principles strong {
        display: block;
        margin-bottom: 5px;
        color: #111827;
        font-size: 15px;
    }

    .hongstock-welcome-principles span {
        color: #4B5563;
        font-size: 13px;
        line-height: 1.5;
    }

    .hongstock-welcome-signature {
        margin-top: 22px;
        color: #4B5563;
        font-size: 14px;
        line-height: 1.65;
    }

    .hongstock-welcome-signature b {
        color: #111827;
    }

    @media (max-width: 700px) {
        .hongstock-welcome-note { padding: 34px 22px 24px; }
        .hongstock-welcome-principles { grid-template-columns: 1fr; }
        .hongstock-welcome-principles > div + div { border-left: 0; border-top: 1px solid rgba(109, 79, 37, 0.35); }
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
    [class*="st-key-news_unanalyzed_"] button,
    [class*="st-key-top30_paper_buy_"] button {
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
    [class*="st-key-news_unanalyzed_"] button:hover,
    [class*="st-key-top30_paper_buy_"] button:hover {
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
    [class*="st-key-news_unanalyzed_"] button:focus,
    [class*="st-key-top30_paper_buy_"] button:focus {
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

    [class*="st-key-top30_paper_buy_"] button {
        font-weight: 600 !important;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------
# 공통 유틸
# -----------------------------
def _database_version() -> int:
    """DB가 교체되면 캐시를 자동으로 무효화할 수 있는 파일 버전값."""
    try:
        return DB_PATH.stat().st_mtime_ns
    except OSError:
        return 0


@st.cache_data(ttl=30, show_spinner=False)
def _load_table_cached(table_name: str, database_version: int) -> pd.DataFrame:
    conn = sqlite3.connect(DB_NAME)
    try:
        df = pd.read_sql(f'SELECT * FROM "{table_name}"', conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    return df


def load_table(table_name: str) -> pd.DataFrame:
    """30초 동안 같은 DB 읽기를 재사용하고, DB 변경 시 즉시 다시 읽는다."""
    return _load_table_cached(table_name, _database_version())


def show_theme_aware_table(dataframe: pd.DataFrame, *args, **kwargs) -> None:
    """캔버스 데이터그리드 색상 문제를 피해 다크 테마에서도 표를 선명하게 표시한다."""
    if st.session_state.get("dashboard_theme", "화이트") != "다크":
        _NATIVE_DATAFRAME(dataframe, *args, **kwargs)
        return

    table = dataframe if isinstance(dataframe, pd.DataFrame) else pd.DataFrame(dataframe)
    table_html = table.to_html(index=False, escape=True, classes="theme-aware-table")
    st.markdown(
        f"<div class='theme-aware-table-wrap'>{table_html}</div>",
        unsafe_allow_html=True,
    )


def format_paper_positions_for_display(positions: pd.DataFrame) -> pd.DataFrame:
    """모의 보유종목의 금액·수익률을 읽기 쉬운 표기법으로 바꾼다."""
    display = positions.rename(
        columns={
            "stock_code": "종목코드",
            "stock_name": "종목명",
            "quantity": "수량",
            "average_price": "평균단가",
        }
    )
    columns = ["종목코드", "종목명", "수량", "평균단가", "현재가", "평가금액", "평가손익", "수익률(%)"]
    display = display[columns].copy()
    display["수량"] = display["수량"].map(lambda value: f"{safe_float(value):,.0f}")
    display["평균단가"] = display["평균단가"].map(lambda value: f"{safe_float(value):,.2f}원")
    display["현재가"] = display["현재가"].map(lambda value: f"{safe_float(value):,.0f}원")
    display["평가금액"] = display["평가금액"].map(lambda value: f"{safe_float(value):,.0f}원")
    display["평가손익"] = display["평가손익"].map(lambda value: f"{safe_float(value):+,.0f}원")
    display["수익률(%)"] = display["수익률(%)"].map(lambda value: f"{safe_float(value):+.2f}%")
    return display


def apply_display_theme(theme: str) -> None:
    """화이트/다크 화면에서 배경과 글자 대비를 함께 맞춘다."""
    if theme != "다크":
        return

    st.markdown(
        """
        <style>
        [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
            background: #101317;
            color: #E5E7EB;
        }
        [data-testid="stSidebar"] {
            background: #171B21;
        }
        [data-testid="stSidebar"] .stButton > button,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p {
            color: #E5E7EB !important;
        }
        [class*="st-key-top30_paper_buy_"] button {
            color: #E5E7EB !important;
        }
        [data-testid="stSidebar"] .stButton > button:hover,
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: #263B55 !important;
            color: #FFFFFF !important;
        }
        [data-testid="stAppViewContainer"] h1,
        [data-testid="stAppViewContainer"] h2,
        [data-testid="stAppViewContainer"] h3,
        [data-testid="stAppViewContainer"] p,
        [data-testid="stAppViewContainer"] label,
        [data-testid="stMetricLabel"],
        [data-testid="stMetricValue"],
        .normal-text, .stock-name-text, .reason-text,
        .metric-label, .metric-value, .stock-grade {
            color: #E5E7EB !important;
        }
        [data-testid="stDataFrame"],
        [data-testid="stDataFrame"] * {
            background-color: #171B21 !important;
            color: #E5E7EB !important;
        }
        /* st.dataframe은 Glide Data Grid 캔버스라 전용 색상 변수가 필요하다. */
        [data-testid="stDataFrame"] {
            --gdg-bg-cell: #171B21;
            --gdg-bg-cell-medium: #1D232C;
            --gdg-bg-header: #202833;
            --gdg-bg-header-has-focus: #263B55;
            --gdg-bg-header-hovered: #2C3E54;
            --gdg-text-dark: #F3F4F6;
            --gdg-text-medium: #D1D5DB;
            --gdg-text-light: #9CA3AF;
            --gdg-text-header: #F9FAFB;
            --gdg-border-color: #374151;
            --gdg-horizontal-border-color: #2B3440;
            --gdg-accent-color: #60A5FA;
            --gdg-accent-light: #263B55;
            --gdg-bg-bubble: #263B55;
            --gdg-bg-bubble-selected: #34557A;
        }
        [data-testid="stTable"] table,
        [data-testid="stTable"] th,
        [data-testid="stTable"] td,
        [data-testid="stTable"] tr {
            background: #171B21 !important;
            color: #E5E7EB !important;
            border-color: #374151 !important;
        }
        .theme-aware-table-wrap {
            overflow-x: auto;
            border: 1px solid #374151;
            border-radius: 8px;
        }
        .theme-aware-table {
            width: 100%;
            border-collapse: collapse;
            background: #171B21;
            color: #E5E7EB;
        }
        .theme-aware-table th {
            background: #202833;
            color: #F9FAFB;
            font-weight: 700;
        }
        .theme-aware-table th, .theme-aware-table td {
            padding: 9px 11px;
            border-bottom: 1px solid #374151;
            text-align: left;
            white-space: nowrap;
        }
        .theme-aware-table tr:hover td {
            background: #202833;
        }
        [data-baseweb="popover"],
        [data-baseweb="popover"] [role="listbox"],
        [data-baseweb="popover"] [role="option"],
        [data-baseweb="menu"] {
            background: #171B21 !important;
            color: #E5E7EB !important;
        }
        [data-baseweb="popover"] [role="option"]:hover,
        [data-baseweb="popover"] [aria-selected="true"] {
            background: #263B55 !important;
            color: #FFFFFF !important;
        }
        [data-testid="stAlert"],
        [data-testid="stAlert"] * {
            color: #E5E7EB;
        }
        [data-testid="stExpander"],
        [data-testid="stExpander"] details,
        [data-testid="stTabs"] button,
        [data-testid="stVerticalBlockBorderWrapper"] {
            background: #171B21 !important;
            color: #E5E7EB !important;
            border-color: #374151 !important;
        }
        [data-testid="stAppViewContainer"] input,
        [data-testid="stAppViewContainer"] textarea,
        [data-testid="stAppViewContainer"] [data-baseweb="select"] > div {
            background: #171B21 !important;
            color: #E5E7EB !important;
            border-color: #4B5563 !important;
        }
        [data-testid="stSelectbox"] [data-baseweb="select"],
        [data-testid="stSelectbox"] [data-baseweb="select"] > div,
        [data-testid="stSelectbox"] [data-baseweb="select"] span,
        [data-testid="stSelectbox"] svg {
            background: #171B21 !important;
            color: #E5E7EB !important;
            fill: #E5E7EB !important;
        }
        [data-testid="stSelectbox"] [data-baseweb="select"] *,
        [data-testid="stSelectbox"] [data-baseweb="select"] > div > div,
        [data-testid="stNumberInput"] [data-baseweb="base-input"],
        [data-testid="stNumberInput"] [data-baseweb="base-input"] > div,
        [data-testid="stNumberInput"] button {
            background: #171B21 !important;
            color: #E5E7EB !important;
            border-color: #4B5563 !important;
        }
        [role="listbox"], [role="listbox"] *,
        [data-baseweb="popover"] > div,
        [data-baseweb="popover"] > div > div {
            background: #171B21 !important;
            color: #E5E7EB !important;
            border-color: #4B5563 !important;
        }
        [data-testid="stChatInput"],
        [data-testid="stChatInput"] > div,
        [data-testid="stChatInput"] [data-baseweb="input"],
        [data-testid="stChatInput"] [data-baseweb="base-input"],
        [data-testid="stChatInput"] [data-baseweb="input"] > div {
            background: #171B21 !important;
            border-color: #4B5563 !important;
            color: #E5E7EB !important;
        }
        [data-testid="stChatInput"] textarea,
        [data-testid="stChatInput"] textarea::placeholder {
            background: #171B21 !important;
            color: #9CA3AF !important;
        }
        [data-testid="stChatInput"] button {
            background: #263B55 !important;
            color: #E5E7EB !important;
            border-color: #4B5563 !important;
        }
        [data-testid="stChatMessage"],
        [data-testid="stChatMessage"] > div {
            background: #171B21 !important;
            color: #E5E7EB !important;
        }
        /* Streamlit 채팅 입력창은 하단 고정 컨테이너를 별도로 그린다. */
        [data-testid="stBottomBlockContainer"],
        [data-testid="stBottomBlockContainer"] > div,
        [data-testid="stBottomBlockContainer"] [data-testid="stVerticalBlock"],
        [data-testid="stBottomBlockContainer"] [data-testid="stElementContainer"] {
            background: #101317 !important;
        }
        [data-testid="stBottomBlockContainer"]::before,
        [data-testid="stBottomBlockContainer"]::after {
            background: #101317 !important;
        }
        [class*="st-key-paper_buy_button"] button {
            background: #DC2626 !important;
            color: #FFFFFF !important;
            border-color: #DC2626 !important;
        }
        [class*="st-key-paper_sell_button"] button {
            background: #2563EB !important;
            color: #FFFFFF !important;
            border-color: #2563EB !important;
        }
        [class*="st-key-paper_buy_button"] button:disabled,
        [class*="st-key-paper_sell_button"] button:disabled {
            opacity: 0.45;
            color: #E5E7EB !important;
        }
        [class*="st-key-realtime_detail_"] button {
            background: #202833 !important;
            color: #E5E7EB !important;
            border-color: #4B5563 !important;
        }
        [class*="st-key-realtime_detail_"] button:hover {
            background: #263B55 !important;
            border-color: #60A5FA !important;
            color: #FFFFFF !important;
        }
        [data-testid="stAppViewContainer"] hr {
            border-color: #374151;
        }
        div[data-testid="stDialog"] div[role="dialog"] {
            background: #101317 !important;
            color: #E5E7EB !important;
            border: 1px solid #374151;
        }
        div[data-testid="stDialog"] .hongstock-welcome-note {
            color: #E5E7EB;
            background: transparent;
        }
        div[data-testid="stDialog"] .hongstock-welcome-meta,
        div[data-testid="stDialog"] .hongstock-welcome-copy,
        div[data-testid="stDialog"] .hongstock-welcome-principles span,
        div[data-testid="stDialog"] .hongstock-welcome-signature {
            color: #9CA3AF !important;
        }
        div[data-testid="stDialog"] .hongstock-welcome-title,
        div[data-testid="stDialog"] .hongstock-welcome-principles strong,
        div[data-testid="stDialog"] .hongstock-welcome-signature b {
            color: #F9FAFB !important;
        }
        div[data-testid="stDialog"] .hongstock-welcome-principles,
        div[data-testid="stDialog"] .hongstock-welcome-principles > div + div {
            border-color: #374151;
        }
        div[data-testid="stDialog"] .hongstock-welcome-principles em {
            color: #9CA3AF !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
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

    col1, col2, col3, col4 = st.columns(4)
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

    col1, col2, col3, col4 = st.columns(4)
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
    with col4:
        has_chase_risk = "추격위험도" in latest.index and pd.notna(latest.get("추격위험도"))
        if has_chase_risk:
            risk = safe_float(latest.get("추격위험도"))
            risk_level = str(latest.get("추격위험등급", ""))
            metric_card("추격 위험", f"{risk:.0f}점 · {risk_level}")
        else:
            metric_card("추격 위험", "—")

    risk_reason = str(latest.get("추격위험사유", "")).strip()
    if risk_reason:
        st.caption(f"추격 위험 근거: {risk_reason}")

    st.subheader("진입 판단")
    entry = str(latest.get("진입판단", "데이터 갱신 대기"))
    entry_reason = str(latest.get("진입판단사유", "")).strip()
    timing_score = safe_float(latest.get("진입타이밍점수", 0))
    st.info(f"{entry} · 타이밍 점수 {timing_score:+.0f}점" + (f"\n\n{entry_reason}" if entry_reason else ""))
    entry_col1, entry_col2, entry_col3, entry_col4 = st.columns(4)
    entry_col1.metric("최근 지지선", f"{safe_float(latest.get('최근지지선')):,.0f}원")
    entry_col2.metric("최근 저항선", f"{safe_float(latest.get('최근저항선')):,.0f}원")
    entry_col3.metric("손절 기준", f"{safe_float(latest.get('손절기준')):,.0f}원")
    entry_col4.metric("돌파 신뢰도", f"{safe_float(latest.get('돌파신뢰도')):.0f}%")

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
        "등급", "최종추천", "진입판단", "진입타이밍점수", "최근지지선", "최근저항선", "손절기준", "목표저항선", "돌파신뢰도", "추격위험도", "추격위험등급", "추격위험사유", "추천제외사유", "점수변동사유", "AI추천사유",
        "5일수익률(%)", "20일수익률(%)", "60일수익률(%)", "20일변동성(%)", "거래량증가율(%)",
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
    def close_stock_detail_dialog():
        st.session_state["realtime_detail_dialog_open"] = False

    @st.dialog("종목 상세 분석", width="large", on_dismiss=close_stock_detail_dialog)
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


def make_top30_paper_buy_dialog():
    """TOP30 화면에서 바로 사용할 수 있는 모의 매수 확인창을 만든다."""

    @st.dialog("모의 매수 확인", width="small")
    def paper_buy_popup(stock_name: str, stock_code: str, fallback_price: float = 0.0):
        if is_remote_storage_enabled() and not is_paper_user_authenticated():
            st.info("모의 매수는 로그인한 사용자 계정에만 저장됩니다. 왼쪽 메뉴에서 로그인해 주세요.")
            return
        code = clean_code(stock_code)
        st.subheader(f"{stock_name} ({code})")
        st.caption("실제 주문은 전송되지 않습니다. 매수 실행 직전에 현재가를 한 번 더 조회합니다.")

        quote_price = 0.0
        token = get_access_token()
        if token:
            try:
                body = get_current_price(token, code)
                quote_price = safe_float((body.get("output") or {}).get("stck_prpr", 0))
            except Exception:
                quote_price = 0.0

        initial_price = quote_price or safe_float(fallback_price)
        quantity = st.number_input(
            "수량",
            min_value=1,
            value=1,
            step=1,
            key=f"top30_paper_buy_quantity_{code}",
        )

        if initial_price > 0:
            st.metric("조회 현재가", f"{initial_price:,.0f}원")
            st.caption("현재가가 바뀌면 실제 모의 체결가는 매수 실행 시점 가격으로 반영됩니다.")
        else:
            initial_price = st.number_input(
                "현재가 미수신 · 체결 가격",
                min_value=1.0,
                value=1.0,
                step=100.0,
                key=f"top30_paper_buy_price_{code}",
            )
            st.caption("KIS 현재가를 받지 못했습니다. 입력한 가격으로 모의 체결합니다.")

        st.metric("예상 주문금액", f"{quantity * initial_price:,.0f}원")
        if st.button(
            "이 가격으로 모의 매수",
            key=f"top30_paper_buy_confirm_{code}",
            type="primary",
            use_container_width=True,
        ):
            execution_price = initial_price
            if token:
                try:
                    body = get_current_price(token, code)
                    latest_price = safe_float((body.get("output") or {}).get("stck_prpr", 0))
                    if latest_price > 0:
                        execution_price = latest_price
                except Exception:
                    pass
            try:
                result = place_paper_order(
                    "BUY",
                    code,
                    stock_name,
                    int(quantity),
                    execution_price,
                )
                st.session_state["top30_paper_order_confirmation"] = (
                    f"{stock_name} {int(quantity):,}주를 {execution_price:,.0f}원에 모의 매수했습니다. "
                    f"체결금액 {result['amount']:,.0f}원"
                )
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"모의 매수 처리 중 오류가 발생했습니다: {exc}")

    return paper_buy_popup



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

    st.subheader(f"{selected_date} 추천점수 TOP30")
    order_confirmation = st.session_state.pop("top30_paper_order_confirmation", None)
    if order_confirmation:
        st.success(order_confirmation)

    filter_col, recommendation_col = st.columns(2)
    sort_option = filter_col.selectbox(
        "정렬 기준",
        [
            "최종점수 높은순",
            "시장점수 높은순",
            "기관 순매수 높은순",
            "기관 순매도 높은순",
            "외국인 순매수 높은순",
            "외국인 순매도 높은순",
            "뉴스점수 높은순",
            "추격위험 낮은순",
        ],
        key=f"top30_sort_{selected_date}",
    )
    recommendation_filter = recommendation_col.selectbox(
        "추천 구분",
        ["전체", "강력관심", "관심", "관찰", "약세", "제외"],
        key=f"top30_recommendation_{selected_date}",
    )

    if recommendation_filter != "전체" and "최종추천" in today_df.columns:
        today_df = today_df[today_df["최종추천"] == recommendation_filter].copy()

    sort_rules = {
        "최종점수 높은순": ("최종점수", False),
        "시장점수 높은순": ("시장점수", False),
        "기관 순매수 높은순": ("기관순매수량", False),
        "기관 순매도 높은순": ("기관순매수량", True),
        "외국인 순매수 높은순": ("외국인순매수량", False),
        "외국인 순매도 높은순": ("외국인순매수량", True),
        "뉴스점수 높은순": ("뉴스점수", False),
        "추격위험 낮은순": ("추격위험도", True),
    }
    sort_column, ascending = sort_rules[sort_option]
    if sort_column in today_df.columns:
        today_df[sort_column] = pd.to_numeric(
            today_df[sort_column], errors="coerce"
        ).fillna(0)
        today_df = today_df.sort_values(
            [sort_column, "최종점수"],
            ascending=[ascending, False],
        )
    else:
        st.info(f"{sort_option}에 필요한 데이터가 아직 없습니다. 최종점수순으로 표시합니다.")
        today_df = today_df.sort_values(["최종점수", "시장점수"], ascending=False)

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
    paper_buy_popup = make_top30_paper_buy_dialog()

    # 추천 사유와 상세 버튼을 제거하고 종목명 영역을 넓힘
    widths = [
        0.55,  # 순위
        1.0,   # 종목코드
        4.8,   # 종목명
        0.85,  # 시장
        1.65,  # 매매동향
        1.0,   # 뉴스
        0.85,  # 최종
        1.2,   # 추격 위험
        1.0,   # 추천
        1.25,  # 모의 매수
    ]

    headers = [
        "순위",
        "종목코드",
        "종목명",
        "시장",
        "매매동향",
        "뉴스",
        "최종",
        "추격 위험",
        "추천",
        "모의투자",
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

        has_chase_risk = "추격위험도" in row.index and pd.notna(row.get("추격위험도"))
        chase_risk = safe_float(row.get("추격위험도"))
        risk_level = str(row.get("추격위험등급", ""))
        cols[7].markdown(
            (
                f"<div class='normal-text'>{chase_risk:.0f}점<br><small>{risk_level}</small></div>"
                if has_chase_risk else "<div class='normal-text'>—</div>"
            ),
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

        cols[8].markdown(
            (
                "<div class='normal-text' "
                f"style='color:{recommendation_color};"
                "font-weight:600;'>"
                f"{recommendation}"
                "</div>"
            ),
            unsafe_allow_html=True,
        )

        fallback_price = 0.0
        for price_column in ("현재가", "종가", "기준종가"):
            if price_column in row.index:
                fallback_price = safe_float(row.get(price_column, 0))
                if fallback_price > 0:
                    break
        if cols[9].button(
            "모의 매수",
            key=f"top30_paper_buy_{selected_date}_{code}",
            use_container_width=True,
        ):
            paper_buy_popup(name, code, fallback_price)

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

    profile_event_key = f"{keyword.strip()}::{clean_code(selected_code)}"
    if st.session_state.get("last_profile_search_event") != profile_event_key:
        selected_score = score_df[score_df["종목코드"] == clean_code(selected_code)].copy()
        chase_risk = 0.0
        if not selected_score.empty and "추격위험도" in selected_score.columns:
            chase_risk = safe_float(selected_score.iloc[-1].get("추격위험도", 0))
        record_behavior_event("search", selected_code, selected_name, {"chase_risk": chase_risk})
        st.session_state["last_profile_search_event"] = profile_event_key

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

def show_prediction_performance_summary():
    """메인 화면에서 5거래일 기준 추천 성과를 간단히 보여준다."""
    st.subheader("예측 검증 성과")
    summary = get_prediction_summary()

    success_rate = summary["success_rate"]
    average_return = summary["average_return"]
    columns = st.columns(4)
    columns[0].metric(
        "예측 성공률",
        f"{success_rate:.1f}%" if success_rate is not None else "집계 대기",
        "추천 후 5거래일 상승 기준" if success_rate is not None else None,
    )
    columns[1].metric(
        "검증 완료",
        f"{summary['completed']:,}건",
        f"전체 표본 {summary['total']:,}건",
    )
    columns[2].metric("검증 대기", f"{summary['waiting']:,}건")
    columns[3].metric(
        "평균 5일 수익률",
        f"{average_return:+.2f}%" if average_return is not None else "집계 대기",
    )
    st.caption(
        "관심·관찰 추천을 하루 한 번 기록하고, 추천 다음 5거래일 종가가 "
        "추천 시점 종가보다 높으면 성공으로 집계합니다. 표본이 쌓일수록 신뢰도가 높아집니다."
    )



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
        template=(
            "plotly_dark"
            if st.session_state.get("dashboard_theme", "화이트") == "다크"
            else "plotly_white"
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={
            "color": (
                "#E5E7EB"
                if st.session_state.get("dashboard_theme", "화이트") == "다크"
                else "#111827"
            )
        },
        xaxis={
            "gridcolor": (
                "#374151"
                if st.session_state.get("dashboard_theme", "화이트") == "다크"
                else "#E5E7EB"
            ),
            "zerolinecolor": (
                "#4B5563"
                if st.session_state.get("dashboard_theme", "화이트") == "다크"
                else "#D1D5DB"
            ),
        },
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
            "open": safe_float(output.get("stck_oprc")),
            "previous_close": safe_float(output.get("stck_sdpr")),
        }
    return quotes


def show_recommendation_mini_chart(
    container,
    chart_df: pd.DataFrame,
    stock_code: str,
    quote_data: dict,
) -> None:
    """추천 카드 안에 최근 일봉과 현재가 기준선을 간단히 표시한다."""
    stock_chart = get_stock_chart_df(chart_df, stock_code).tail(20).copy()
    if stock_chart.empty or "종가" not in stock_chart.columns:
        container.caption("표시할 일봉 데이터가 없습니다.")
        return

    for column in ["시가", "고가", "저가", "종가"]:
        if column in stock_chart.columns:
            stock_chart[column] = pd.to_numeric(stock_chart[column], errors="coerce")

    price = safe_float(quote_data.get("price"))
    opening_price = safe_float(quote_data.get("open"))
    if not opening_price and "시가" in stock_chart.columns:
        opening_price = safe_float(stock_chart["시가"].iloc[-1])

    is_up_from_open = price >= opening_price if price and opening_price else True
    price_color = "#EF4444" if is_up_from_open else "#2563EB"
    has_ohlc = {"시가", "고가", "저가", "종가"}.issubset(stock_chart.columns)

    if has_ohlc:
        figure = go.Figure(
            data=[
                go.Candlestick(
                    x=stock_chart["날짜"],
                    open=stock_chart["시가"],
                    high=stock_chart["고가"],
                    low=stock_chart["저가"],
                    close=stock_chart["종가"],
                    increasing_line_color="#EF4444",
                    increasing_fillcolor="#EF4444",
                    decreasing_line_color="#2563EB",
                    decreasing_fillcolor="#2563EB",
                    name="일봉",
                )
            ]
        )
    else:
        figure = go.Figure(
            data=[
                go.Scatter(
                    x=stock_chart["날짜"],
                    y=stock_chart["종가"],
                    mode="lines",
                    line={"color": "#94A3B8", "width": 2},
                    name="종가",
                )
            ]
        )

    if opening_price:
        figure.add_hline(
            y=opening_price,
            line_color="#94A3B8",
            line_width=2,
            line_dash="dot",
            annotation_text=f"당일 시작가 {opening_price:,.0f}원",
            annotation_font_color="#94A3B8",
            annotation_position="top right",
        )

    is_dark_theme = st.session_state.get("dashboard_theme", "화이트") == "다크"
    figure.update_layout(
        height=220,
        margin={"l": 4, "r": 4, "t": 18, "b": 4},
        showlegend=False,
        template="plotly_dark" if is_dark_theme else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"size": 10},
        xaxis={"rangeslider": {"visible": False}, "showgrid": False},
        yaxis={"tickformat": ",.0f", "showgrid": True, "gridcolor": "#334155"},
    )
    container.plotly_chart(
        figure,
        use_container_width=True,
        config={"displayModeBar": False},
    )

    if price and opening_price:
        opening_rate = ((price / opening_price) - 1) * 100
        direction = "당일 시작가 대비 상승" if opening_rate >= 0 else "당일 시작가 대비 하락"
        container.caption(f"{direction} {opening_rate:+.2f}% · 빨강=상승 / 파랑=하락")


@st.fragment(run_every="1s")
def show_paper_order_live_price(stock_code: str, fallback_price: float = 0.0):
    """주문 화면 전체를 다시 실행하지 않고 선택 종목의 현재가만 갱신한다."""
    code = clean_code(stock_code)
    hub = get_realtime_quote_hub()
    hub.ensure_codes((code,), source="paper_order")
    realtime = hub.snapshot((code,))
    quote = realtime["quotes"].get(code, {})
    price = safe_float(quote.get("price", 0)) or safe_float(fallback_price)
    change = safe_float(quote.get("change", 0))
    change_rate = safe_float(quote.get("change_rate", 0))

    if price > 0:
        live_quotes = dict(st.session_state.get("paper_order_live_quotes", {}))
        live_quotes[code] = {
            "price": price,
            "change": change,
            "change_rate": change_rate,
        }
        st.session_state["paper_order_live_quotes"] = live_quotes
        st.metric(
            "실시간 현재가",
            f"{price:,.0f}원",
            f"{change:+,.0f}원 ({change_rate:+.2f}%)",
        )
        st.caption("KIS WebSocket 실시간 체결가")
    elif realtime["error"]:
        st.metric("실시간 현재가", "연결 재시도 중")
        st.caption("체결 시에는 KIS 현재가를 다시 조회합니다.")
    else:
        st.metric("실시간 현재가", "연결 중")


def show_realtime_recommendations(
    current_df: pd.DataFrame,
    score_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
    st.header("현재 매수 판단")
    st.caption("전일 장 준비와 별개로, 현재 점수·돌파 확인·추격위험을 다시 계산한 결과입니다.")

    if current_df.empty:
        st.info("추천 점수 데이터가 아직 없습니다.")
        return

    df = current_df.copy()
    score_column = "최종점수" if "최종점수" in df.columns else "총점"
    if score_column not in df.columns:
        st.info("추천 점수 컬럼을 찾지 못했습니다.")
        return

    df[score_column] = pd.to_numeric(df[score_column], errors="coerce").fillna(0)
    # 테마·급등주도 후보에서 배제하지 않는다. 대신 상세 근거와 위험 판정을
    # 같이 보여줘 사용자가 모멘텀 매매 후보인지, 실제 매수 추천인지 구분한다.
    for column, default in [("돌파신뢰도", 0), ("추격위험도", 100)]:
        if column not in df.columns:
            df[column] = default
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(default)

    # 점수만 높다고 매수 후보가 되는 것은 아니다. 돌파가 확인되지 않았거나
    # 급등 추격 위험이 큰 종목은 관찰 후보로만 분리한다.
    recommendation = df.get("최종추천", pd.Series("", index=df.index)).astype(str)
    entry = df.get("진입판단", pd.Series("", index=df.index)).astype(str)
    buy_mask = (
        recommendation.isin(["강력관심", "관심"])
        & entry.isin(["돌파 확인", "지지선 근처"])
        & (df["돌파신뢰도"] >= 60)
        & (df["추격위험도"] < 45)
    )
    top3 = df.loc[buy_mask].sort_values(score_column, ascending=False).head(3).copy()
    watch3 = df.loc[~buy_mask].sort_values(score_column, ascending=False).head(3).copy()
    has_buyable_top3 = not top3.empty
    if not has_buyable_top3:
        # 빈 카드 영역 대신 관찰 후보를 명확히 표시한다. 이 경우에도 매수 추천으로
        # 오해되지 않도록 아래에 경고를 보여준다.
        top3 = watch3.copy()

    detail_popup = make_stock_dialog(
        score_df=score_df,
        chart_df=chart_df,
        supply_df=supply_df,
        news_df=news_df,
        classification_df=classification_df,
        theme_history_df=theme_history_df,
    )
    display_df = pd.concat([top3, watch3]).drop_duplicates("종목코드", keep="first")
    codes = tuple(clean_code(code) for code in display_df["종목코드"].tolist())
    realtime_enabled = st.toggle(
        "초단위 체결가 반영",
        value=True,
        key="realtime_recommendation_quotes",
    )
    quotes: dict[str, dict] = {}
    realtime_status = ""
    if realtime_enabled:
        hub = get_realtime_quote_hub()
        hub.ensure_codes(codes, source="dashboard_top3")
        realtime = hub.snapshot(codes)
        quotes = realtime["quotes"]
        if realtime["connected"]:
            realtime_status = "KIS WebSocket 실시간 연결됨"
        elif realtime["error"]:
            realtime_status = f"실시간 연결 재시도 중: {realtime['error']}"
        else:
            realtime_status = "KIS WebSocket 연결 중"
        if not st.session_state.get("realtime_detail_dialog_open", False):
            st_autorefresh(interval=1000, key="realtime_recommendation_refresh")

    # WebSocket은 체결가 중심이고 시가 정보가 없으므로, REST 현재가의 시가를
    # 함께 사용해 카드 차트의 빨강/파랑 기준을 정확히 표시한다.
    rest_quotes = load_recommendation_quotes(codes)

    st.subheader("매수 가능 TOP 3" if has_buyable_top3 else "현재 관찰 후보 TOP 3")
    if not has_buyable_top3:
        st.info("현재는 점수·돌파 확인·추격위험 조건을 모두 통과한 매수 가능 종목이 없습니다. 아래 종목은 관찰용이며 매수 추천이 아닙니다.")

    columns = st.columns(len(top3))
    for index, (_, row) in enumerate(top3.iterrows()):
        code = clean_code(row.get("종목코드", ""))
        name = str(row.get("종목명", code))
        quote_data = {
            **rest_quotes.get(code, {}),
            **quotes.get(code, {}),
        }
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
        show_recommendation_mini_chart(
            columns[index],
            chart_df,
            code,
            quote_data,
        )
        recommendation = str(row.get("최종추천", "추천 검토"))
        has_chase_risk = "추격위험도" in row.index and pd.notna(row.get("추격위험도"))
        chase_risk = safe_float(row.get("추격위험도"))
        risk_level = str(row.get("추격위험등급", ""))
        risk_text = f" · 추격위험 {chase_risk:.0f}점({risk_level})" if has_chase_risk else ""
        columns[index].caption(
            f"{code} · 점수 {row[score_column]:.2f} · {recommendation} · 진입 {row.get('진입판단', '갱신 대기')}{risk_text}"
        )
        if recommendation not in {"강력관심", "관심"} or not has_buyable_top3:
            columns[index].warning(
                "관찰 후보입니다. 돌파 확인과 추격 위험 조건을 통과하기 전에는 매수하지 않습니다."
            )
        if columns[index].button("상세 분석 보기", key=f"realtime_detail_{code}"):
            st.session_state["realtime_detail_dialog_open"] = True
            record_behavior_event(
                "view",
                code,
                name,
                {"chase_risk": chase_risk, "recommendation": recommendation},
            )
            detail_popup(name, code)

    if has_buyable_top3 and not watch3.empty:
        st.subheader("현재 관찰 후보 TOP 3")
        watch_view = watch3.copy()
        watch_view["사유"] = (
            "진입 " + watch_view["진입판단"].fillna("관찰")
            + " · 돌파신뢰도 " + watch_view["돌파신뢰도"].map(lambda value: f"{safe_float(value):.0f}점")
            + " · 추격위험 " + watch_view["추격위험도"].map(lambda value: f"{safe_float(value):.0f}점")
        )
        view_columns = [column for column in ["종목코드", "종목명", score_column, "최종추천", "사유"] if column in watch_view.columns]
        st.dataframe(watch_view[view_columns], use_container_width=True, hide_index=True)

    if realtime_enabled:
        st.caption(realtime_status)
    else:
        st.caption("현재가는 KIS REST 조회값이며, 약 20초마다 새로 조회됩니다.")


def show_morning_briefing(history_df: pd.DataFrame, supply_df: pd.DataFrame):
    st.header("오늘 장 준비")
    st.caption("전일 마감 기준의 사전 관찰 계획입니다. 아래 현재 매수 판단과 종목이 달라질 수 있으며, 매수 확정 신호가 아닙니다.")

    if history_df is None or history_df.empty or "저장일자" not in history_df.columns:
        st.info("전일 마감 분석 데이터가 아직 없습니다.")
        return

    briefing = history_df.copy()
    briefing["저장일자"] = pd.to_datetime(briefing["저장일자"], errors="coerce").dt.date
    available_dates = sorted(briefing["저장일자"].dropna().unique())
    if not available_dates:
        st.info("분석 기준일을 확인할 수 없습니다.")
        return

    today = pd.Timestamp.now().date()
    previous_dates = [date for date in available_dates if date < today]
    reference_date = max(previous_dates) if previous_dates else max(available_dates)
    briefing = briefing[briefing["저장일자"] == reference_date].copy()
    if "저장시간" in briefing.columns:
        briefing = briefing.sort_values("저장시간").drop_duplicates("종목코드", keep="last")

    score_column = "최종점수" if "최종점수" in briefing.columns else "총점"
    briefing[score_column] = pd.to_numeric(briefing[score_column], errors="coerce").fillna(0)
    if "추격위험도" in briefing.columns:
        briefing["추격위험도"] = pd.to_numeric(briefing["추격위험도"], errors="coerce").fillna(50)
        safe_candidates = briefing[briefing["추격위험도"] <= 70].copy()
        if not safe_candidates.empty:
            briefing = safe_candidates
    briefing = briefing.sort_values(score_column, ascending=False).head(5)

    latest_supply_map = {}
    if supply_df is not None and not supply_df.empty and "종목코드" in supply_df.columns:
        supply_before_open = supply_df.copy()
        supply_before_open["날짜"] = pd.to_datetime(supply_before_open["날짜"], errors="coerce")
        supply_before_open = supply_before_open[supply_before_open["날짜"].dt.date <= reference_date]
        for code, group in supply_before_open.groupby("종목코드"):
            group = group.sort_values("날짜").drop_duplicates("날짜", keep="last")
            recent3 = group.tail(3)
            latest_supply_map[clean_code(code)] = {
                "외국인3일": safe_float(recent3.get("외국인순매수량", pd.Series(dtype=float)).sum()),
                "기관3일": safe_float(recent3.get("기관순매수량", pd.Series(dtype=float)).sum()),
            }

    st.info(f"분석 기준일: {reference_date} 장 마감 · 오늘은 시초가와 거래량을 확인한 뒤 계획을 유지하거나 취소하세요.")
    rows = []
    for _, row in briefing.iterrows():
        code = clean_code(row.get("종목코드", ""))
        supply = latest_supply_map.get(code, {})
        foreign_3d = safe_float(supply.get("외국인3일", row.get("외국인3일합계", 0)))
        institution_3d = safe_float(supply.get("기관3일", row.get("기관3일합계", 0)))
        risk = safe_float(row.get("추격위험도", 0))
        resistance = safe_float(row.get("최근저항선", row.get("목표저항선", 0)))
        support = safe_float(row.get("최근지지선", row.get("손절기준", 0)))
        entry_value = row.get("진입판단", "")
        entry = str(entry_value).strip() if pd.notna(entry_value) else ""
        if not entry or entry.lower() == "nan":
            recommendation_value = row.get("최종추천", "")
            recommendation = str(recommendation_value).strip() if pd.notna(recommendation_value) else ""
            entry = recommendation if recommendation and recommendation.lower() != "nan" else "관찰 대기"
        supply_parts = []
        if foreign_3d:
            supply_parts.append(f"외국인 3일 {foreign_3d:+,.0f}주")
        if institution_3d:
            supply_parts.append(f"기관 3일 {institution_3d:+,.0f}주")
        reason_value = row.get("AI추천사유", "")
        fallback_reason = str(reason_value).strip() if pd.notna(reason_value) else ""
        if not fallback_reason or fallback_reason.lower() == "nan":
            fallback_reason = "전일 점수 상위 · 장 시작 후 수급과 거래량 확인 필요"
        reason = " · ".join(supply_parts) or fallback_reason
        trigger = f"{resistance:,.0f}원 돌파 확인" if resistance > 0 else "시초가와 거래량 확인"
        cancel = f"{support:,.0f}원 이탈 시 제외" if support > 0 else "시초가 급등 시 추격 금지"
        rows.append(
            {
                "종목": f"{row.get('종목명', code)} ({code})",
                "전일점수": safe_float(row.get(score_column, 0)),
                "오늘 계획": entry,
                "관찰 조건": trigger,
                "계획 취소": cancel,
                "추격위험": f"{risk:.0f}점",
                "선정 근거": reason,
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("장 시작 후 시초가가 전일 종가보다 크게 뛰거나 지지선을 이탈하면 전일 분석보다 오늘 가격 행동을 우선합니다.")


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
    show_market_overview()

    st.divider()
    show_ai_analysis(
        score_df=history_df,
        chart_df=chart_df,
        master_df=master_df,
    )

    st.divider()
    show_prediction_performance_summary()


def show_today_preparation_and_recommendations(
    current_df: pd.DataFrame,
    history_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    supply_df: pd.DataFrame,
    news_df: pd.DataFrame,
    classification_df: pd.DataFrame,
    theme_history_df: pd.DataFrame,
):
    show_morning_briefing(history_df=history_df, supply_df=supply_df)

    st.divider()
    show_realtime_recommendations(
        current_df=current_df,
        score_df=history_df,
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


def show_investor_profile():
    profile = get_investor_profile(days=30)
    st.header("투자 성향")
    st.caption("최근 30일의 검색·열람·모의 주문 기록을 바탕으로 본인 행동을 점검합니다. 투자 적합성 판정이나 매수 추천은 아닙니다.")
    st.subheader(profile["profile"])
    st.write(profile["summary"])
    columns = st.columns(5)
    columns[0].metric("행동 기록", f"{profile['total_actions']}건")
    columns[1].metric("종목 검색·열람", f"{profile['searches'] + profile['views']}건")
    columns[2].metric("모의 매수", f"{profile['buys']}건")
    columns[3].metric("모의 매도", f"{profile['sells']}건")
    risk_ratio = profile["high_risk_ratio"]
    columns[4].metric("고위험 후보 열람", f"{risk_ratio:.0f}%" if risk_ratio is not None else "집계 대기")
    st.subheader("조심 알림")
    if profile["warnings"]:
        for warning in profile["warnings"]:
            st.warning(warning)
    else:
        st.success("현재 기록에서는 반복적 충동 진입 신호가 뚜렷하지 않습니다.")
    st.subheader("자주 보는 종목")
    if profile["favorites"].empty:
        st.info("종목 검색이나 모의 주문을 기록하면 관심 종목 패턴이 표시됩니다.")
    else:
        st.dataframe(profile["favorites"], use_container_width=True, hide_index=True)


def show_paper_trading(master_df, current_df, supply_df, section="모의 주문"):
    st.header("모의투자")
    st.caption("실제 주문은 전송되지 않습니다. 초기 자금 1억 원, 매수·매도 수수료와 매도세가 반영됩니다.")
    if section == "투자 성향":
        show_investor_profile()
        return
    order_confirmation = st.session_state.pop("paper_order_confirmation", None)
    if order_confirmation:
        st.success(order_confirmation)
    profit_detail_open = st.session_state.get("show_paper_profit_detail", False)
    needs_live_price = section in {"모의 주문", "보유 종목"}
    # 주문 화면은 버튼 클릭이 자동 새로고침과 겹치지 않게 한다.
    # 초단위 갱신은 평가손익이 필요한 보유 종목 화면에서만 사용한다.
    needs_realtime_refresh = section == "보유 종목"
    if needs_realtime_refresh and not profit_detail_open:
        st_autorefresh(interval=1000, key="paper_trading_realtime_refresh")
        st.caption("장이 열려 있을 때 보유 종목과 수익률을 1초마다 갱신합니다.")
    elif needs_realtime_refresh:
        st.caption("손익 분석을 보는 동안 실시간 갱신이 잠시 멈춥니다.")

    def open_profit_detail():
        st.session_state["show_paper_profit_detail"] = True

    def close_profit_detail():
        st.session_state["show_paper_profit_detail"] = False

    account = get_paper_account()
    positions = get_paper_positions()
    paper_orders = get_paper_orders(limit=10000)
    previous_price_map = st.session_state.get("paper_previous_prices", {})
    price_map = {}
    if not positions.empty and needs_live_price:
        position_codes = tuple(clean_code(code) for code in positions["stock_code"].astype(str))
        hub = get_realtime_quote_hub()
        hub.ensure_codes(position_codes, source="paper_positions")
        realtime_quotes = hub.snapshot(position_codes).get("quotes", {})
        fallback_quotes = load_recommendation_quotes(position_codes)
        price_map = {
            code: safe_float(realtime_quotes.get(code, {}).get("price") or fallback_quotes.get(code, {}).get("price"))
            for code in position_codes
        }

    order_token = get_access_token() if section == "모의 주문" else None

    if positions.empty:
        market_value = 0.0
        unrealized = 0.0
    else:
        positions["현재가"] = positions.apply(
            lambda row: price_map.get(clean_code(row["stock_code"]), 0) or row["average_price"], axis=1
        )
        positions["평가금액"] = positions["현재가"] * positions["quantity"]
        positions["평가손익"] = positions["평가금액"] - positions["average_price"] * positions["quantity"]
        positions["수익률(%)"] = positions["평가손익"] / (positions["average_price"] * positions["quantity"]) * 100
        positions["직전가격"] = positions.apply(
            lambda row: previous_price_map.get(
                clean_code(row["stock_code"]), row["현재가"]
            ),
            axis=1,
        )
        positions["1초변동손익"] = (
            positions["현재가"] - positions["직전가격"]
        ) * positions["quantity"]
        market_value = float(positions["평가금액"].sum())
        unrealized = float(positions["평가손익"].sum())
        st.session_state["paper_previous_prices"] = {
            clean_code(row["stock_code"]): float(row["현재가"])
            for _, row in positions.iterrows()
        }

    gross_average_map = {}
    if paper_orders is not None and not paper_orders.empty:
        reconstructed = {}
        for _, order in paper_orders.sort_values("id").iterrows():
            code = clean_code(order["stock_code"])
            state = reconstructed.setdefault(code, {"quantity": 0, "gross_average": 0.0})
            order_quantity = int(order["quantity"])
            if str(order["side"]).upper() == "BUY":
                new_quantity = state["quantity"] + order_quantity
                state["gross_average"] = (
                    state["quantity"] * state["gross_average"]
                    + order_quantity * float(order["price"])
                ) / new_quantity
                state["quantity"] = new_quantity
            else:
                state["quantity"] = max(0, state["quantity"] - order_quantity)
                if state["quantity"] == 0:
                    state["gross_average"] = 0.0
        gross_average_map = {
            code: state["gross_average"] for code, state in reconstructed.items()
        }

    supply_context = {}
    if supply_df is not None and not supply_df.empty and "종목코드" in supply_df.columns:
        for code in positions.get("stock_code", pd.Series(dtype=str)).astype(str):
            clean_stock_code = clean_code(code)
            stock_supply = supply_df[supply_df["종목코드"] == clean_stock_code].copy()
            if stock_supply.empty:
                continue
            stock_supply = stock_supply.sort_values("날짜").drop_duplicates("날짜", keep="last")
            latest_supply = stock_supply.iloc[-1]
            recent3 = stock_supply.tail(3)
            supply_context[clean_stock_code] = {
                "기준일": latest_supply["날짜"].strftime("%Y-%m-%d") if pd.notna(latest_supply["날짜"]) else "확인 불가",
                "외국인": safe_float(latest_supply.get("외국인순매수량", 0)),
                "기관": safe_float(latest_supply.get("기관순매수량", 0)),
                "개인": safe_float(latest_supply.get("개인순매수량", 0)),
                "외국인3일": safe_float(recent3.get("외국인순매수량", pd.Series(dtype=float)).sum()),
                "기관3일": safe_float(recent3.get("기관순매수량", pd.Series(dtype=float)).sum()),
            }

    total_assets = float(account["cash"]) + market_value
    total_return = total_assets - float(account["initial_cash"])
    cols = st.columns(4)
    cols[0].metric("총 자산", f"{total_assets:,.0f}원", f"{total_return:,.0f}원")
    cols[1].metric("주문 가능 금액", f"{float(account['cash']):,.0f}원")
    cols[2].metric("주식 평가액", f"{market_value:,.0f}원")
    st.markdown(
        """
        <style>
        .st-key-paper_profit_card {
            margin-top: -8px;
        }
        .st-key-paper_profit_card button {
            min-height: 104px;
            padding: 8px 12px;
            background: transparent;
            border: 1px solid transparent;
            border-radius: 10px;
            color: #111827;
            text-align: left;
            justify-content: flex-start;
            transition: background-color .15s ease, border-color .15s ease, color .15s ease;
        }
        .st-key-paper_profit_card button div {
            width: 100%;
            align-items: flex-start;
        }
        .st-key-paper_profit_card button p {
            width: 100%;
            text-align: left;
            margin: 0;
        }
        .st-key-paper_profit_card button p:first-child {
            font-size: 14px;
            font-weight: 400;
            margin-bottom: 12px;
        }
        .st-key-paper_profit_card button p:last-child {
            font-size: 32px;
            line-height: 1.2;
            font-weight: 400;
        }
        .st-key-paper_profit_card button:hover {
            background: #2563eb;
            border-color: #2563eb;
            color: #ffffff;
        }
        .st-key-paper_profit_card button:hover p {
            color: #ffffff;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with cols[3]:
        with st.container(key="paper_profit_card"):
            st.button(
                f"평가손익\n\n{unrealized:,.0f}원",
                key="paper_profit_detail_button",
                use_container_width=True,
                on_click=open_profit_detail,
            )

    if st.session_state.get("show_paper_profit_detail", False):
        st.subheader("평가손익 변동 이유")
        if positions.empty:
            st.info("보유 종목이 없어 분석할 평가손익이 없습니다.")
        else:
            one_second_change = float(positions["1초변동손익"].sum())
            direction = "올랐습니다" if one_second_change > 0 else "떨어졌습니다" if one_second_change < 0 else "변동이 없습니다"
            st.info(
                f"직전 1초와 비교해 평가손익이 {one_second_change:+,.0f}원 {direction}. "
                "현재가 변동 × 보유수량으로 계산한 값입니다."
            )
            st.caption(
                "외국인·기관 수급은 가격 변동의 참고 근거이며 원인을 확정하지는 않습니다. "
                "장 마감 후에는 마지막 거래일 기준 수급을 표시합니다."
            )
            positions["수급 기준일"] = positions["stock_code"].map(
                lambda code: supply_context.get(clean_code(code), {}).get("기준일", "자료 없음")
            )
            for label in ["외국인", "기관", "개인", "외국인3일", "기관3일"]:
                positions[label] = positions["stock_code"].map(
                    lambda code, key=label: supply_context.get(clean_code(code), {}).get(key, 0)
                )

            def describe_supply(row):
                foreign = safe_float(row["외국인"])
                institution = safe_float(row["기관"])
                personal = safe_float(row["개인"])
                price_profit = safe_float(row["평가손익"])
                buyers = []
                sellers = []
                for investor, value in [("외국인", foreign), ("기관", institution), ("개인", personal)]:
                    if value > 0:
                        buyers.append((investor, value))
                    elif value < 0:
                        sellers.append((investor, value))
                buyers.sort(key=lambda item: item[1], reverse=True)
                sellers.sort(key=lambda item: item[1])
                movement = "상승" if price_profit > 0 else "하락" if price_profit < 0 else "보합"
                if buyers and sellers:
                    return (
                        f"{movement} 구간 · {buyers[0][0]} {buyers[0][1]:+,.0f}주 순매수, "
                        f"{sellers[0][0]} {sellers[0][1]:+,.0f}주 순매도"
                    )
                if buyers:
                    return f"{movement} 구간 · {buyers[0][0]} {buyers[0][1]:+,.0f}주 순매수 우세"
                if sellers:
                    return f"{movement} 구간 · {sellers[0][0]} {sellers[0][1]:+,.0f}주 순매도 우세"
                return f"{movement} 구간 · 투자자별 수급 자료 없음"

            positions["수급 해석"] = positions.apply(describe_supply, axis=1)
            positions["수수료 제외 평균매수가"] = positions.apply(
                lambda row: gross_average_map.get(
                    clean_code(row["stock_code"]), float(row["average_price"])
                ),
                axis=1,
            )
            positions["주가 변동 손익"] = (
                positions["현재가"] - positions["수수료 제외 평균매수가"]
            ) * positions["quantity"]
            positions["매수 수수료"] = (
                positions["average_price"] - positions["수수료 제외 평균매수가"]
            ) * positions["quantity"]

            def describe_direct_cause(row):
                price_effect = safe_float(row["주가 변동 손익"])
                buy_fee = max(0.0, safe_float(row["매수 수수료"]))
                total_profit = safe_float(row["평가손익"])
                if abs(price_effect) < 0.5 and buy_fee >= 0.5:
                    return f"현재가 변동 없음 · 손실 {abs(total_profit):,.0f}원은 매수 수수료"
                price_word = "상승 이익" if price_effect > 0 else "하락 손실"
                if total_profit >= 0:
                    return (
                        f"주가 {price_word} {abs(price_effect):,.0f}원에서 "
                        f"매수 수수료 {buy_fee:,.0f}원을 뺀 순이익"
                    )
                return (
                    f"주가 {price_word} {abs(price_effect):,.0f}원과 "
                    f"매수 수수료 {buy_fee:,.0f}원이 만든 순손실"
                )

            positions["직접 원인"] = positions.apply(describe_direct_cause, axis=1)
            portfolio_price_effect = float(positions["주가 변동 손익"].sum())
            portfolio_buy_fees = float(positions["매수 수수료"].sum())
            st.success(
                f"현재 평가손익 {unrealized:+,.0f}원의 직접 구성: "
                f"주가 변동 {portfolio_price_effect:+,.0f}원 - "
                f"매수 수수료 {portfolio_buy_fees:,.0f}원. "
                "외국인·기관 수급은 아래에 참고 배경으로만 표시합니다."
            )
            detail_df = positions[
                [
                    "stock_name",
                    "quantity",
                    "average_price",
                    "직전가격",
                    "현재가",
                    "1초변동손익",
                    "평가손익",
                    "수익률(%)",
                    "수급 기준일",
                    "외국인",
                    "기관",
                    "개인",
                    "외국인3일",
                    "기관3일",
                    "수급 해석",
                    "수수료 제외 평균매수가",
                    "주가 변동 손익",
                    "매수 수수료",
                    "직접 원인",
                ]
            ].copy()
            detail_df = detail_df.sort_values("1초변동손익", ascending=False)
            detail_df = detail_df.rename(
                columns={
                    "stock_name": "종목명",
                    "quantity": "보유수량",
                    "average_price": "평균매수가",
                    "직전가격": "1초 전 가격",
                    "현재가": "현재가",
                    "1초변동손익": "1초 손익변동",
                    "평가손익": "누적 평가손익",
                    "외국인": "외국인 당일 순매수",
                    "기관": "기관 당일 순매수",
                    "개인": "개인 당일 순매수",
                    "외국인3일": "외국인 최근 3일",
                    "기관3일": "기관 최근 3일",
                }
            )
            summary_df = detail_df[
                [
                    "종목명",
                    "누적 평가손익",
                    "수익률(%)",
                    "직접 원인",
                    "외국인 당일 순매수",
                    "기관 당일 순매수",
                ]
            ].copy()
            summary_df["누적 평가손익"] = summary_df["누적 평가손익"].map(lambda value: f"{value:+,.0f}원")
            summary_df["수익률(%)"] = summary_df["수익률(%)"].map(lambda value: f"{value:+.2f}%")
            summary_df["외국인 당일 순매수"] = summary_df["외국인 당일 순매수"].map(
                lambda value: f"{value:+,.0f}주"
            )
            summary_df["기관 당일 순매수"] = summary_df["기관 당일 순매수"].map(
                lambda value: f"{value:+,.0f}주"
            )
            st.dataframe(
                summary_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "직접 원인": st.column_config.TextColumn(width="large"),
                },
            )
            with st.expander("가격·개인·최근 3일 상세 데이터 보기"):
                st.dataframe(detail_df, use_container_width=True, hide_index=True)
            biggest_up = detail_df.iloc[0]
            biggest_down = detail_df.iloc[-1]
            reason_cols = st.columns(2)
            reason_cols[0].metric(
                "가장 크게 올린 종목",
                str(biggest_up["종목명"]),
                f"{biggest_up['1초 손익변동']:+,.0f}원",
            )
            reason_cols[1].metric(
                "가장 크게 내린 종목",
                str(biggest_down["종목명"]),
                f"{biggest_down['1초 손익변동']:+,.0f}원",
            )
        st.button(
            "손익 분석 닫기",
            key="paper_profit_detail_close_button",
            use_container_width=True,
            on_click=close_profit_detail,
        )

    if section == "보유 종목":
        st.subheader("보유 종목")
        if positions.empty:
            st.info("보유 중인 모의투자 종목이 없습니다.")
        else:
            show_theme_aware_table(format_paper_positions_for_display(positions))
        return

    if section == "거래 내역":
        st.subheader("거래 내역")
        history_orders = paper_orders.copy()
        if history_orders.empty:
            st.info("아직 체결된 모의 주문이 없습니다.")
        else:
            history_orders["주문일시"] = pd.to_datetime(
                history_orders["ordered_at"], errors="coerce"
            )
            period_options = ["오늘", "최근 1주", "최근 1개월", "직접 설정", "전체"]
            selected_period = st.radio(
                "조회 기간",
                period_options,
                horizontal=True,
                key="paper_order_period",
            )
            today = date.today()
            start_date = None
            end_date = today
            if selected_period == "오늘":
                start_date = today
            elif selected_period == "최근 1주":
                start_date = today - timedelta(days=6)
            elif selected_period == "최근 1개월":
                start_date = today - timedelta(days=29)
            elif selected_period == "직접 설정":
                start_col, end_col = st.columns(2)
                start_date = start_col.date_input(
                    "시작일", value=today - timedelta(days=6), key="paper_order_start_date"
                )
                end_date = end_col.date_input(
                    "종료일", value=today, key="paper_order_end_date"
                )
                if start_date > end_date:
                    st.error("시작일은 종료일보다 늦을 수 없습니다.")
                    return

            filtered_orders = history_orders.copy()
            if start_date is not None:
                filtered_orders = filtered_orders[
                    filtered_orders["주문일시"].dt.date >= start_date
                ]
            if selected_period != "전체":
                filtered_orders = filtered_orders[
                    filtered_orders["주문일시"].dt.date <= end_date
                ]

            buy_orders = filtered_orders[filtered_orders["side"] == "BUY"]
            sell_orders = filtered_orders[filtered_orders["side"] == "SELL"]
            buy_total = float(buy_orders["amount"].sum())
            sell_total = float(sell_orders["amount"].sum())
            order_count = len(filtered_orders)
            total_fees = float(filtered_orders["fee"].sum() + filtered_orders["tax"].sum())

            amount_cols = st.columns(4)
            amount_cols[0].metric("매수 총금액", f"{buy_total:,.0f}원")
            amount_cols[1].metric("매도 총금액", f"{sell_total:,.0f}원")
            amount_cols[2].metric("매도 실현손익", f"{float(sell_orders['realized_profit'].sum()):+,.0f}원")
            amount_cols[3].metric("체결 건수", f"{order_count:,}건", f"비용 {total_fees:,.0f}원")
            st.caption(
                "매수 총금액은 매수 수수료를 포함한 실제 지출액이고, "
                "매도 총금액은 매도 수수료·세금을 뺀 실제 입금액입니다."
            )

            if filtered_orders.empty:
                st.info("선택한 기간에 체결된 모의 주문이 없습니다.")
            else:
                display_orders = filtered_orders.copy()
                display_orders["side"] = display_orders["side"].map({"BUY": "매수", "SELL": "매도"})
                display_orders["주문일시"] = display_orders["주문일시"].dt.strftime("%Y-%m-%d %H:%M:%S")
                display_orders = display_orders.rename(
                    columns={
                        "side": "구분",
                        "stock_code": "종목코드",
                        "stock_name": "종목명",
                        "quantity": "수량",
                        "price": "체결가",
                        "fee": "수수료",
                        "tax": "세금",
                        "amount": "실제 금액",
                        "realized_profit": "실현손익",
                    }
                )
                display_orders["수량"] = display_orders["수량"].map(
                    lambda value: f"{safe_float(value):,.0f}"
                )
                for column in ["체결가", "실제 금액", "수수료", "세금"]:
                    display_orders[column] = display_orders[column].map(
                        lambda value: f"{safe_float(value):,.0f}원"
                    )
                display_orders["실현손익"] = display_orders["실현손익"].map(
                    lambda value: f"{safe_float(value):+,.0f}원"
                )
                show_theme_aware_table(
                    display_orders[
                        ["주문일시", "구분", "종목코드", "종목명", "수량", "체결가", "실제 금액", "수수료", "세금", "실현손익"]
                    ]
                )
        return

    st.subheader("모의 주문")
    candidates = pd.concat([master_df, current_df], ignore_index=True, sort=False)
    if not candidates.empty and {"종목코드", "종목명"}.issubset(candidates.columns):
        candidates = candidates[["종목코드", "종목명"]].dropna().drop_duplicates("종목코드")
        candidates["종목코드"] = candidates["종목코드"].map(clean_code)
        options = {
            f"{row['종목명']} ({row['종목코드']})": (row["종목코드"], row["종목명"])
            for _, row in candidates.sort_values("종목명").iterrows()
        }
        selected_label = st.selectbox("종목", list(options))
        code, name = options[selected_label]
        live_price = 0.0
        if order_token:
            try:
                body = get_current_price(order_token, code)
                live_price = float((body.get("output") or {}).get("stck_prpr", 0) or 0)
            except Exception:
                live_price = 0.0
        order_cols = st.columns(3)
        quantity = order_cols[0].number_input("수량", min_value=1, value=1, step=1)
        latest_quote = st.session_state.get("paper_order_live_quotes", {}).get(code, {})
        display_price = safe_float(latest_quote.get("price", 0)) or live_price
        with order_cols[1]:
            show_paper_order_live_price(code, live_price)
            if display_price > 0:
                price = display_price
            else:
                price = st.number_input(
                    "현재가 미수신 · 체결 가격",
                    min_value=1.0,
                    value=1.0,
                    step=100.0,
                )
                st.caption("KIS 연결을 확인해 주세요. 체결 시 현재가를 다시 조회합니다.")
        order_cols[2].metric("예상 주문금액", f"{quantity * price:,.0f}원")

        def execute_paper_order(side):
            try:
                execution_price = price
                if order_token:
                    latest_body = get_current_price(order_token, code)
                    latest_price = float(
                        (latest_body.get("output") or {}).get("stck_prpr", 0) or 0
                    )
                    if latest_price > 0:
                        execution_price = latest_price
                result = place_paper_order(
                    side,
                    code,
                    name,
                    quantity,
                    execution_price,
                )
                side_label = "매수" if side == "BUY" else "매도"
                st.session_state["paper_order_confirmation"] = (
                    f"{name} {quantity:,}주 {side_label}가 현재가 "
                    f"{execution_price:,.0f}원에 체결되었습니다. "
                    f"체결금액 {result['amount']:,.0f}원"
                )
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"모의 주문 처리 중 오류가 발생했습니다: {exc}")

        buy_col, sell_col = st.columns(2)
        if buy_col.button(
            "매수",
            key="paper_buy_button",
            type="primary",
            use_container_width=True,
        ):
            execute_paper_order("BUY")
        if sell_col.button(
            "매도",
            key="paper_sell_button",
            use_container_width=True,
        ):
            execute_paper_order("SELL")
    else:
        st.warning("주문 가능한 종목 목록이 없습니다.")

    if section == "모의 주문":
        return

    st.subheader("보유 종목")
    if positions.empty:
        st.info("보유 중인 모의투자 종목이 없습니다.")
    else:
        display = positions.rename(columns={"stock_code": "종목코드", "stock_name": "종목명", "quantity": "수량", "average_price": "평균단가"})
        st.dataframe(display[["종목코드", "종목명", "수량", "평균단가", "현재가", "평가금액", "평가손익", "수익률(%)"]], use_container_width=True, hide_index=True)

    st.subheader("거래 내역")
    orders = get_paper_orders()
    if orders.empty:
        st.info("아직 체결된 모의 주문이 없습니다.")
    else:
        orders["side"] = orders["side"].map({"BUY": "매수", "SELL": "매도"})
        st.dataframe(orders, use_container_width=True, hide_index=True)

    with st.expander("모의계좌 초기화"):
        if st.button("초기 자금 1억 원으로 초기화"):
            reset_paper_account()
            st.success("모의계좌를 초기화했습니다.")
            st.rerun()


# -----------------------------
# 시작 안내
# -----------------------------
def show_hongstock_welcome() -> bool:
    """브라우저 세션에서 처음 한 번만 서비스 소개 팝업을 표시한다."""
    if st.session_state.get("hongstock_welcome_seen", False):
        return False

    def dismiss_welcome():
        st.session_state["hongstock_welcome_seen"] = True

    @st.dialog("Welcome to HONG STOCK", width="large", on_dismiss=dismiss_welcome)
    def welcome_dialog():
        st.markdown(
            """
            <section class="hongstock-welcome-note">
                <div class="hongstock-welcome-meta">안녕하세요, HONG STOCK입니다.</div>
                <h1 class="hongstock-welcome-title">추천의 이유를 기록하는<br>주식 분석 프로그램입니다.</h1>
                <p class="hongstock-welcome-copy">
                    HONG STOCK은 단순히 종목을 보여주거나 매수를 권하는 프로그램이 아닙니다.
                    가격, 거래량, 수급, 뉴스, 기술적 위치를 함께 살펴보고 왜 이 종목이 후보가 되었는지 보여줍니다.
                </p>
                <p class="hongstock-welcome-copy">
                    이후 실제 주가가 어떻게 움직였는지도 다시 확인합니다. 예측이 맞았을 때는 어떤 근거가 유효했는지,
                    예상과 달랐을 때는 무엇이 부족했는지 기록해 다음 분석을 개선하는 것이 HONG STOCK의 핵심입니다.
                </p>
                <div class="hongstock-welcome-principles">
                    <div><em>01</em><strong>근거 확인</strong><span>추천 점수와 함께 진입 조건·추격 위험을 확인합니다.</span></div>
                    <div><em>02</em><strong>후보 구분</strong><span>매수 가능 후보와 관찰 후보를 분리해 보여줍니다.</span></div>
                    <div><em>03</em><strong>결과 학습</strong><span>실제 결과와 이유를 DB에 남겨 분석을 누적 개선합니다.</span></div>
                </div>
                <div class="hongstock-welcome-signature"><b>HONG STOCK</b>은 투자 판단을 대신하지 않습니다. 더 나은 판단을 위해 근거와 결과를 정리하는 도구입니다.</div>
            </section>
            """,
            unsafe_allow_html=True,
        )
        st.caption("HONG STOCK은 투자 판단의 보조 정보와 모의투자 도구입니다. 실제 투자 손익의 책임은 사용자에게 있습니다.")
        if st.button("근거부터 확인하기", type="primary", use_container_width=True):
            st.session_state["hongstock_welcome_seen"] = True
            st.rerun()

    welcome_dialog()
    return True


# 메인
# -----------------------------
def main():
    st.title("HONG STOCK")
    st.caption("추천보다 이유를 기록하는 주식 분석")

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

    if "active_dashboard_page" not in st.session_state:
        st.session_state["active_dashboard_page"] = "홈"
    elif st.session_state["active_dashboard_page"] == "오늘 장 준비·시장 현황":
        # 기존 화면을 열어 둔 사용자는 새 홈 메뉴로 자연스럽게 이동시킨다.
        st.session_state["active_dashboard_page"] = "홈"

    selected_theme = st.sidebar.radio(
        "화면 테마",
        ["화이트", "다크"],
        horizontal=True,
        key="dashboard_theme",
    )
    # 다크에서는 Streamlit 캔버스 표가 검은 글자를 남기는 경우가 있어,
    # 앱 전역의 dataframe 출력을 읽기 쉬운 HTML 표로 통일한다.
    st.dataframe = show_theme_aware_table if selected_theme == "다크" else _NATIVE_DATAFRAME

    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] .stButton > button {
            min-height: 29px;
            padding: 3px 8px;
            border: 0 !important;
            border-radius: 6px;
            background: transparent;
            box-shadow: none !important;
            color: #374151;
            justify-content: flex-start;
            text-align: left;
            font-weight: 400;
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2 {
            margin: 0.45rem 0 0.1rem;
            font-size: 1rem;
            line-height: 1.2;
        }
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
            gap: 0.08rem;
        }
        [data-testid="stSidebar"] .stButton > button:hover {
            background: #eff6ff;
            color: #2563eb;
        }
        [data-testid="stSidebar"] .stButton > button[kind="primary"] {
            background: #eff6ff;
            color: #2563eb;
            font-weight: 700;
        }
        [data-testid="stSidebar"] .stButton > button:focus:not(:active) {
            color: #2563eb;
            border: 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    apply_display_theme(selected_theme)
    signed_in_user = show_auth_sidebar()
    is_master_account = is_admin_user(signed_in_user)

    def sidebar_page_button(label, page, key):
        active = st.session_state["active_dashboard_page"] == page
        if st.sidebar.button(
            label,
            key=key,
            use_container_width=True,
            type="primary" if active else "secondary",
        ):
            st.session_state["active_dashboard_page"] = page
            st.rerun()

    st.sidebar.markdown("## 🏠 홈")
    sidebar_page_button("홈", "홈", "nav_home")

    st.sidebar.markdown("## 🗓️ 오늘의 투자")
    sidebar_page_button(
        "오늘 장 준비·실시간 추천",
        "오늘 장 준비·실시간 추천",
        "nav_today_recommendations",
    )
    sidebar_page_button("장중 흐름", "장중 흐름", "nav_intraday")
    sidebar_page_button("추천 TOP30", "추천 TOP30", "nav_top30")

    st.sidebar.markdown("## 🔎 종목 분석")
    sidebar_page_button("종목 검색", "종목 검색", "nav_search")
    sidebar_page_button("업종·테마", "업종·테마", "nav_sector")
    sidebar_page_button("과거 분석", "과거 분석", "nav_history")

    st.sidebar.markdown("## 💰 모의투자")
    sidebar_page_button("모의 주문", "모의 주문", "nav_paper_order")
    sidebar_page_button("보유 종목", "보유 종목", "nav_paper_positions")
    sidebar_page_button("거래 내역", "거래 내역", "nav_paper_history")
    sidebar_page_button("투자 성향", "투자 성향", "nav_investor_profile")

    if not is_remote_storage_enabled() or is_master_account:
        st.sidebar.markdown("## ⚙️ 관리")
        sidebar_page_button("DB 상태", "DB 상태", "nav_db")

    menu = st.session_state["active_dashboard_page"]

    if menu in {"모의 주문", "보유 종목", "거래 내역", "투자 성향"}:
        if is_remote_storage_enabled() and not is_paper_user_authenticated():
            st.header("내 모의투자")
            st.info("모의 계좌·주문·투자 성향은 로그인한 본인에게만 저장됩니다. 왼쪽 메뉴에서 로그인해 주세요.")
            return
        show_paper_trading(
            master_df=master_df,
            current_df=current_df,
            supply_df=supply_df,
            section=menu,
        )
        return

    if menu == "DB 상태":
        if is_remote_storage_enabled() and not is_master_account:
            st.session_state["active_dashboard_page"] = "홈"
            st.rerun()
        show_db_status()
        return

    if menu == "홈":
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

    if menu == "오늘 장 준비·실시간 추천":
        show_today_preparation_and_recommendations(
            current_df=current_df,
            history_df=history_df,
            chart_df=chart_df,
            supply_df=supply_df,
            news_df=news_df,
            classification_df=classification_df,
            theme_history_df=theme_history_df,
        )
        return

    if menu == "업종·테마":
        show_sector_strength(
            current_df=current_df,
            classification_df=classification_df,
        )
        return

    if menu == "추천 TOP30":
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
        show_stock_search(
            score_df=history_df,
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
