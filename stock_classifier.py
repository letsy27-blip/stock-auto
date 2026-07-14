from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from config import GEMINI_API_KEY

from pathlib import Path

DB_NAME = Path(__file__).resolve().parent / "stock_data.db"
MODEL_NAME = "gemini-2.5-flash"
API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{MODEL_NAME}:generateContent"
)
REQUEST_TIMEOUT = 90
BATCH_SIZE = 15
THEME_CHECK_DELAY = 0.3


ALLOWED_INDUSTRIES = [
    "반도체",
    "반도체장비",
    "반도체소재",
    "전자부품",
    "소프트웨어",
    "인터넷",
    "AI·데이터",
    "로봇·자동화",
    "통신",
    "디스플레이",
    "2차전지",
    "자동차",
    "자동차부품",
    "조선",
    "기계",
    "방산",
    "항공·우주",
    "건설",
    "건설자재",
    "철강·금속",
    "화학",
    "정유",
    "에너지",
    "원전",
    "태양광·풍력",
    "바이오",
    "제약",
    "의료기기",
    "금융",
    "증권",
    "보험",
    "유통",
    "화장품",
    "식품",
    "엔터테인먼트",
    "게임",
    "미디어",
    "교육",
    "물류·운송",
    "여행·레저",
    "환경",
    "기타",
]


def clean_code(value: Any) -> str:
    code = str(value or "").replace(".0", "").strip()
    return code.zfill(6) if code else ""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(text).strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return {}
        try:
            result = json.loads(match.group(0))
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}


def _normalize_themes(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            value = [value]

    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        theme = str(item or "").strip()
        if theme and theme not in result:
            result.append(theme)

    return result[:5]


def _themes_json(value: Any) -> str:
    return json.dumps(
        _normalize_themes(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_classification (
            종목코드 TEXT PRIMARY KEY,
            종목명 TEXT,
            업종 TEXT,
            대표테마 TEXT,
            테마JSON TEXT,
            분류근거 TEXT,
            분류신뢰도 INTEGER,
            분류모델 TEXT,
            업종분류일시 TEXT,
            테마확인일자 TEXT,
            테마확인일시 TEXT,
            분류갱신일시 TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_theme_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            기준일자 TEXT NOT NULL,
            종목코드 TEXT NOT NULL,
            종목명 TEXT,
            대표테마 TEXT,
            테마JSON TEXT,
            테마근거 TEXT,
            테마신뢰도 INTEGER,
            분석모델 TEXT,
            분석일시 TEXT,
            UNIQUE(기준일자, 종목코드, 대표테마, 테마JSON)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_theme_history_code_date
        ON stock_theme_history(종목코드, 기준일자 DESC)
        """
    )

    conn.commit()


def _migrate_old_table_if_needed(conn: sqlite3.Connection) -> None:
    _ensure_tables(conn)

    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(stock_classification)"
        ).fetchall()
    }

    required_columns = {
        "종목코드": "TEXT",
        "종목명": "TEXT",
        "업종": "TEXT",
        "대표테마": "TEXT",
        "테마JSON": "TEXT",
        "분류근거": "TEXT",
        "분류신뢰도": "INTEGER",
        "분류모델": "TEXT",
        "업종분류일시": "TEXT",
        "테마확인일자": "TEXT",
        "테마확인일시": "TEXT",
        "분류갱신일시": "TEXT",
    }

    for column, sql_type in required_columns.items():
        if column not in columns:
            conn.execute(
                f'ALTER TABLE stock_classification '
                f'ADD COLUMN "{column}" {sql_type}'
            )

    conn.commit()


def load_classification_table() -> pd.DataFrame:
    conn = sqlite3.connect(DB_NAME)
    try:
        _migrate_old_table_if_needed(conn)
        df = pd.read_sql_query(
            "SELECT * FROM stock_classification",
            conn,
        )
    finally:
        conn.close()

    if not df.empty and "종목코드" in df.columns:
        df["종목코드"] = df["종목코드"].map(clean_code)

    return df


def load_theme_history(
    stock_code: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    conn = sqlite3.connect(DB_NAME)
    try:
        _ensure_tables(conn)

        sql = """
        SELECT *
        FROM stock_theme_history
        """
        params: list[Any] = []

        if stock_code:
            sql += " WHERE 종목코드 = ?"
            params.append(clean_code(stock_code))

        sql += " ORDER BY 기준일자 DESC, id DESC"

        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        return pd.read_sql_query(
            sql,
            conn,
            params=tuple(params),
        )
    finally:
        conn.close()


def _save_master_rows(
    rows: list[dict[str, Any]],
    existing_map: dict[str, dict[str, Any]],
) -> None:
    if not rows:
        return

    conn = sqlite3.connect(DB_NAME)

    try:
        _migrate_old_table_if_needed(conn)

        for item in rows:
            code = clean_code(item.get("종목코드"))
            if not code:
                continue

            previous = existing_map.get(code, {})
            old_industry = str(previous.get("업종", "") or "").strip()
            new_industry = str(item.get("업종", "") or "").strip()

            industry = old_industry or new_industry or "기타"
            if industry not in ALLOWED_INDUSTRIES:
                industry = "기타"

            industry_time = str(
                previous.get("업종분류일시", "")
                or previous.get("분류갱신일시", "")
                or _now()
            )

            conn.execute(
                """
                INSERT INTO stock_classification (
                    종목코드,
                    종목명,
                    업종,
                    대표테마,
                    테마JSON,
                    분류근거,
                    분류신뢰도,
                    분류모델,
                    업종분류일시,
                    테마확인일자,
                    테마확인일시,
                    분류갱신일시
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(종목코드) DO UPDATE SET
                    종목명 = excluded.종목명,
                    업종 = stock_classification.업종,
                    대표테마 = excluded.대표테마,
                    테마JSON = excluded.테마JSON,
                    분류근거 = excluded.분류근거,
                    분류신뢰도 = excluded.분류신뢰도,
                    분류모델 = excluded.분류모델,
                    업종분류일시 = stock_classification.업종분류일시,
                    테마확인일자 = excluded.테마확인일자,
                    테마확인일시 = excluded.테마확인일시,
                    분류갱신일시 = excluded.분류갱신일시
                """,
                (
                    code,
                    str(item.get("종목명", "")).strip(),
                    industry,
                    str(item.get("대표테마", "")).strip(),
                    _themes_json(item.get("테마JSON", [])),
                    str(item.get("분류근거", "")).strip(),
                    int(item.get("분류신뢰도", 0)),
                    str(item.get("분류모델", MODEL_NAME)),
                    industry_time,
                    _today(),
                    _now(),
                    _now(),
                ),
            )

        conn.commit()
    finally:
        conn.close()


def _save_theme_changes(
    rows: list[dict[str, Any]],
    existing_map: dict[str, dict[str, Any]],
) -> None:
    if not rows:
        return

    conn = sqlite3.connect(DB_NAME)

    try:
        _ensure_tables(conn)

        for item in rows:
            code = clean_code(item.get("종목코드"))
            if not code:
                continue

            previous = existing_map.get(code, {})
            old_representative = str(
                previous.get("대표테마", "") or ""
            ).strip()
            old_themes = _themes_json(
                previous.get("테마JSON", [])
            )

            new_representative = str(
                item.get("대표테마", "") or ""
            ).strip()
            new_themes = _themes_json(
                item.get("테마JSON", [])
            )

            is_new_stock = not previous
            is_changed = (
                old_representative != new_representative
                or old_themes != new_themes
            )

            if not is_new_stock and not is_changed:
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO stock_theme_history (
                    기준일자,
                    종목코드,
                    종목명,
                    대표테마,
                    테마JSON,
                    테마근거,
                    테마신뢰도,
                    분석모델,
                    분석일시
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _today(),
                    code,
                    str(item.get("종목명", "")).strip(),
                    new_representative,
                    new_themes,
                    str(item.get("분류근거", "")).strip(),
                    int(item.get("분류신뢰도", 0)),
                    str(item.get("분류모델", MODEL_NAME)),
                    _now(),
                ),
            )

        conn.commit()
    finally:
        conn.close()


def _make_news_context(
    news_summary_df: pd.DataFrame | None,
    stock_code: str,
) -> str:
    if news_summary_df is None or news_summary_df.empty:
        return ""

    if "종목코드" not in news_summary_df.columns:
        return ""

    df = news_summary_df.copy()
    df["종목코드"] = df["종목코드"].map(clean_code)

    hit = df[df["종목코드"] == clean_code(stock_code)]
    if hit.empty:
        return ""

    row = hit.iloc[-1]

    parts = [
        str(row.get("뉴스요약", "")),
        str(row.get("뉴스분석사유", "")),
        str(row.get("핵심뉴스JSON", "")),
    ]

    return " | ".join(
        part
        for part in parts
        if part and part.lower() not in {"nan", "none"}
    )[:1500]


def _call_gemini_batch(
    batch_df: pd.DataFrame,
    news_summary_df: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "config.py 또는 .env에 GEMINI_API_KEY가 없습니다."
        )

    stocks = []

    for _, row in batch_df.iterrows():
        stocks.append(
            {
                "종목코드": clean_code(row.get("종목코드")),
                "종목명": str(row.get("종목명", "")).strip(),
                "시장구분": str(row.get("시장구분", "")).strip(),
                "기존업종": str(row.get("기존업종", "")).strip(),
                "기존대표테마": str(
                    row.get("기존대표테마", "")
                ).strip(),
                "기존테마": _normalize_themes(
                    row.get("기존테마JSON", [])
                ),
                "최근뉴스": _make_news_context(
                    news_summary_df,
                    row.get("종목코드"),
                ),
            }
        )

    prompt = f"""
너는 한국 상장기업 업종·투자테마 분류 전문가다.

아래 종목들을 오늘 기준으로 분류하라.

분류 원칙:
1. 기존업종이 비어 있으면 회사의 주력 매출·사업을 기준으로 업종 1개를 새로 선택한다.
2. 기존업종이 있으면 업종을 절대 변경하지 말고 그대로 반환한다.
3. 업종은 반드시 다음 목록 중 하나를 사용한다:
{", ".join(ALLOWED_INDUSTRIES)}
4. 대표테마와 테마 목록은 오늘의 실제 사업, 제품, 공시, 최근 뉴스와 시장 관심을 반영해 다시 판단한다.
5. 테마는 실제 연관성이 있는 항목만 최대 5개까지 작성한다.
6. 기존 테마가 아직 유효하면 그대로 유지해도 된다.
7. 단순 언급, 근거 없는 루머, 동명이인 뉴스는 제외한다.
8. 종목코드와 종목명은 입력값을 그대로 반환한다.
9. JSON 외에는 출력하지 않는다.

입력:
{json.dumps(stocks, ensure_ascii=False)}

출력 형식:
{{
  "results": [
    {{
      "종목코드": "005930",
      "종목명": "삼성전자",
      "업종": "반도체",
      "대표테마": "AI반도체",
      "테마": ["AI반도체", "HBM", "파운드리"],
      "분류근거": "업종과 오늘 테마를 선택한 이유",
      "신뢰도": 90
    }}
  ]
}}
""".strip()

    schema = {
        "type": "OBJECT",
        "properties": {
            "results": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "종목코드": {"type": "STRING"},
                        "종목명": {"type": "STRING"},
                        "업종": {"type": "STRING"},
                        "대표테마": {"type": "STRING"},
                        "테마": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        },
                        "분류근거": {"type": "STRING"},
                        "신뢰도": {"type": "INTEGER"},
                    },
                    "required": [
                        "종목코드",
                        "종목명",
                        "업종",
                        "대표테마",
                        "테마",
                        "분류근거",
                        "신뢰도",
                    ],
                },
            }
        },
        "required": ["results"],
    }

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    response = requests.post(
        API_URL,
        params={"key": GEMINI_API_KEY},
        headers={"content-type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    body = response.json()
    candidates = body.get("candidates", [])

    if not candidates:
        raise RuntimeError(f"Gemini 분류 응답 없음: {body}")

    text = "".join(
        str(part.get("text", ""))
        for part in (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )
    )

    parsed = _safe_json(text)
    results = parsed.get("results", [])

    if not isinstance(results, list):
        return []

    input_map = {
        clean_code(row.get("종목코드")): row
        for _, row in batch_df.iterrows()
    }

    cleaned = []

    for item in results:
        if not isinstance(item, dict):
            continue

        code = clean_code(item.get("종목코드"))
        source = input_map.get(code)

        if source is None:
            continue

        fixed_industry = str(
            source.get("기존업종", "")
        ).strip()

        industry = fixed_industry or str(
            item.get("업종", "기타")
        ).strip()

        if industry not in ALLOWED_INDUSTRIES:
            industry = "기타"

        themes = _normalize_themes(item.get("테마", []))
        representative = str(
            item.get("대표테마", "")
        ).strip()

        if representative and representative not in themes:
            themes.insert(0, representative)
            themes = themes[:5]

        if not representative and themes:
            representative = themes[0]

        try:
            confidence = int(float(item.get("신뢰도", 0)))
        except (TypeError, ValueError):
            confidence = 0

        cleaned.append(
            {
                "종목코드": code,
                "종목명": str(
                    source.get("종목명", "")
                ).strip(),
                "업종": industry,
                "대표테마": representative,
                "테마JSON": _themes_json(themes),
                "분류근거": str(
                    item.get("분류근거", "")
                ).strip(),
                "분류신뢰도": max(0, min(100, confidence)),
                "분류모델": MODEL_NAME,
            }
        )

    return cleaned


def update_stock_classifications(
    candidate_df: pd.DataFrame,
    news_summary_df: pd.DataFrame | None = None,
    force: bool = False,
) -> pd.DataFrame:
    if candidate_df is None or candidate_df.empty:
        return load_classification_table()

    candidates = candidate_df.copy()
    candidates["종목코드"] = candidates["종목코드"].map(clean_code)
    candidates = candidates.drop_duplicates(
        subset=["종목코드"],
        keep="last",
    )

    existing = load_classification_table()

    existing_map: dict[str, dict[str, Any]] = {}
    if not existing.empty:
        existing_map = {
            clean_code(row.get("종목코드")): row.to_dict()
            for _, row in existing.iterrows()
        }

    candidates["기존업종"] = candidates["종목코드"].map(
        lambda code: str(
            existing_map.get(code, {}).get("업종", "") or ""
        ).strip()
    )
    candidates["기존대표테마"] = candidates["종목코드"].map(
        lambda code: str(
            existing_map.get(code, {}).get("대표테마", "") or ""
        ).strip()
    )
    candidates["기존테마JSON"] = candidates["종목코드"].map(
        lambda code: existing_map.get(code, {}).get(
            "테마JSON",
            "[]",
        )
    )
    candidates["마지막테마확인일자"] = candidates["종목코드"].map(
        lambda code: str(
            existing_map.get(code, {}).get(
                "테마확인일자",
                "",
            )
            or ""
        )
    )

    if force:
        pending = candidates.copy()
    else:
        pending = candidates[
            candidates["마지막테마확인일자"] != _today()
        ].copy()

    if pending.empty:
        print("오늘 업종·테마 확인 완료")
        return existing

    all_results: list[dict[str, Any]] = []

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending.iloc[start:start + BATCH_SIZE]
        names = ", ".join(
            batch["종목명"].astype(str).tolist()
        )
        print(f"Gemini 업종·테마 확인 중: {names}")

        try:
            batch_results = _call_gemini_batch(
                batch_df=batch,
                news_summary_df=news_summary_df,
            )
            all_results.extend(batch_results)
        except Exception as exc:
            print("업종·테마 확인 실패:", exc)

        time.sleep(THEME_CHECK_DELAY)

    if all_results:
        _save_theme_changes(
            rows=all_results,
            existing_map=existing_map,
        )
        _save_master_rows(
            rows=all_results,
            existing_map=existing_map,
        )

    return load_classification_table()


def merge_classification(
    scored_df: pd.DataFrame,
    classification_df: pd.DataFrame,
) -> pd.DataFrame:
    if scored_df is None or scored_df.empty:
        return scored_df

    if classification_df is None or classification_df.empty:
        return scored_df

    left = scored_df.copy()
    right = classification_df.copy()

    left["종목코드"] = left["종목코드"].map(clean_code)
    right["종목코드"] = right["종목코드"].map(clean_code)

    columns = [
        column
        for column in [
            "종목코드",
            "업종",
            "대표테마",
            "테마JSON",
            "분류근거",
            "분류신뢰도",
            "테마확인일자",
        ]
        if column in right.columns
    ]

    return left.merge(
        right[columns].drop_duplicates(
            subset=["종목코드"],
            keep="last",
        ),
        on="종목코드",
        how="left",
    )


if __name__ == "__main__":
    print(load_classification_table().head())
    print(load_theme_history(limit=20))
