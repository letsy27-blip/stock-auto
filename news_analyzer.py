from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import pandas as pd
import requests

from config import GEMINI_API_KEY


DB_NAME = "stock_data.db"
REQUEST_TIMEOUT = 30
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

MAX_GEMINI_CALLS_PER_RUN = int(os.getenv("GEMINI_MAX_CALLS_PER_RUN", "12"))
GEMINI_RETRY_MINUTES = int(os.getenv("GEMINI_RETRY_MINUTES", "120"))
MAX_ARTICLES_PER_ANALYSIS = int(os.getenv("GEMINI_MAX_ARTICLES_PER_ANALYSIS", "5"))
MAX_DESCRIPTION_CHARS = int(os.getenv("GEMINI_MAX_DESCRIPTION_CHARS", "600"))

# Gemini는 최종 후보의 정밀 판단에만 사용한다. 나머지 종목은 아래의 무료
# 1차 분석을 사용하므로, 무료 API 한도가 소진되어도 뉴스 점수 계산은 멈추지 않는다.
GEMINI_PRIORITY_STOCKS_PER_RUN = int(
    os.getenv("GEMINI_PRIORITY_STOCKS_PER_RUN", "8")
)

POSITIVE_NEWS_KEYWORDS = {
    "수주": 3, "계약 체결": 3, "공급계약": 3, "실적 개선": 3,
    "흑자전환": 3, "매출 증가": 2, "영업이익 증가": 2, "자사주 매입": 2,
    "배당 확대": 2, "상향": 1, "승인": 2, "특허": 1, "투자 유치": 2,
    "신고가": 1,
}
NEGATIVE_NEWS_KEYWORDS = {
    "유상증자": -3, "전환사채": -2, "CB 발행": -2, "BW 발행": -2,
    "감사 의견": -3, "거래정지": -4, "관리종목": -4, "상장폐지": -5,
    "적자전환": -3, "영업손실": -3, "하향": -1, "소송": -2,
    "횡령": -4, "배임": -4, "불성실공시": -2,
}

NO_NEWS_RESULT = {
    "뉴스점수": 0.0,
    "뉴스판단": "뉴스 없음",
    "뉴스요약": "관련 뉴스 없음",
    "뉴스분석사유": "최근 수집된 뉴스가 없습니다.",
    "핵심뉴스": [],
    "긍정요인": [],
    "부정요인": [],
    "영향기간": "판단불가",
    "신뢰도": 0,
}


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_title(title: str) -> str:
    title = _clean_text(title)
    # RSS 제목 뒤 언론사 표기를 제거하되 기사 본문 의미는 유지한다.
    title = re.sub(r"\s+-\s+[^-]+$", "", title)
    return title.strip()


def _safe_json_loads(text: str) -> dict:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        result = json.loads(cleaned)
        return result if isinstance(result, dict) else {}
    except json.JSONDecodeError:
        # 모델이 JSON 앞뒤에 설명을 붙였을 때 첫 객체만 추출한다.
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return {}
        try:
            result = json.loads(match.group(0))
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}


def _clip_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(-10.0, min(10.0, score)), 2)


def _clip_confidence(value: Any) -> int:
    try:
        confidence = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, confidence))


def fetch_google_news(
    stock_name: str,
    stock_code: str,
    hours: int = 36,
    max_items: int = 10,
) -> pd.DataFrame:
    """
    Google News RSS에서 제목, RSS 설명, 발행시각, 링크를 수집한다.
    Gemini는 제목만이 아니라 RSS 설명까지 함께 읽고 문맥을 판단한다.
    """
    query = urllib.parse.quote(f'"{stock_name}" 주식')
    url = (
        "https://news.google.com/rss/search"
        f"?q={query}&hl=ko&gl=KR&ceid=KR:ko"
    )

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        print(f"{stock_name} 뉴스 수집 실패:", exc)
        return pd.DataFrame()

    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    rows = []

    for item in root.findall(".//item"):
        title = _normalize_title(item.findtext("title", ""))
        description = _clean_text(item.findtext("description", ""))
        link = item.findtext("link", "")
        pub_date_text = item.findtext("pubDate", "")
        source_node = item.find("source")
        source = (
            _clean_text(source_node.text)
            if source_node is not None and source_node.text
            else ""
        )

        try:
            published = parsedate_to_datetime(pub_date_text)
            if published.tzinfo is None:
                published = published.astimezone()
        except Exception:
            published = datetime.now().astimezone()

        if published < cutoff:
            continue

        if not title:
            continue

        rows.append(
            {
                "종목코드": str(stock_code).replace(".0", "").zfill(6),
                "종목명": stock_name,
                "기사발행일시": published.strftime("%Y-%m-%d %H:%M:%S"),
                "뉴스제목": title,
                "뉴스설명": description,
                "뉴스URL": link,
                "언론사": source,
                "수집일시": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        if len(rows) >= max_items:
            break

    return pd.DataFrame(rows)


def collect_news_for_candidates(
    candidate_df: pd.DataFrame,
    hours: int = 36,
    max_items_per_stock: int = 8,
    delay: float = 0.2,
) -> pd.DataFrame:
    if candidate_df is None or candidate_df.empty:
        return pd.DataFrame()

    collected = []

    for _, row in candidate_df.iterrows():
        code = str(row.get("종목코드", "")).replace(".0", "").zfill(6)
        name = str(row.get("종목명", "")).strip()

        print(f"뉴스 수집 중: {name}({code})")
        news_df = fetch_google_news(
            stock_name=name,
            stock_code=code,
            hours=hours,
            max_items=max_items_per_stock,
        )
        if not news_df.empty:
            collected.append(news_df)

        time.sleep(delay)

    if not collected:
        return pd.DataFrame()

    result = pd.concat(collected, ignore_index=True)

    with_url = result[result["뉴스URL"].fillna("").astype(str).str.len() > 0]
    without_url = result[result["뉴스URL"].fillna("").astype(str).str.len() == 0]

    with_url = with_url.drop_duplicates(subset=["뉴스URL"], keep="last")
    without_url = without_url.drop_duplicates(
        subset=["종목코드", "뉴스제목"],
        keep="last",
    )

    return pd.concat([with_url, without_url], ignore_index=True)


def _make_article_hash(row: pd.Series) -> str:
    stock_code = str(row.get("종목코드", "")).replace(".0", "").zfill(6)
    url = str(row.get("뉴스URL", "")).strip()
    title = _normalize_title(str(row.get("뉴스제목", "")))
    published = str(row.get("기사발행일시", "")).strip()
    source = str(row.get("언론사", "")).strip()
    material = "|".join([stock_code, url or title, published, source])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _make_fingerprint(stock_code: str, group: pd.DataFrame) -> str:
    article_hashes = sorted(_make_article_hash(row) for _, row in group.iterrows())
    material = f"{stock_code}\n" + "\n".join(article_hashes)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()

def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_ai_summary (
            종목코드 TEXT NOT NULL, 종목명 TEXT, 뉴스지문 TEXT NOT NULL,
            분석일시 TEXT, 뉴스점수 REAL, 뉴스판단 TEXT, 뉴스요약 TEXT,
            뉴스분석사유 TEXT, 핵심뉴스JSON TEXT, 긍정요인JSON TEXT,
            부정요인JSON TEXT, 영향기간 TEXT, 신뢰도 INTEGER, 모델명 TEXT,
            분석상태 TEXT DEFAULT '완료', 재시도가능일시 TEXT,
            마지막오류 TEXT, 호출횟수 INTEGER DEFAULT 1,
            PRIMARY KEY (종목코드, 뉴스지문)
        )
    """)
    existing={row[1] for row in conn.execute('PRAGMA table_info("news_ai_summary")').fetchall()}
    additions={"분석상태":"TEXT DEFAULT '완료'","재시도가능일시":"TEXT","마지막오류":"TEXT","호출횟수":"INTEGER DEFAULT 1"}
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE "news_ai_summary" ADD COLUMN "{column}" {definition}')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_article_cache (
            기사해시 TEXT PRIMARY KEY, 종목코드 TEXT, 종목명 TEXT,
            뉴스URL TEXT, 뉴스제목 TEXT, 기사발행일시 TEXT,
            최초수집일시 TEXT, 최종수집일시 TEXT
        )
    """)
    conn.commit()


def _save_article_cache(group: pd.DataFrame) -> None:
    if group is None or group.empty:
        return
    conn=sqlite3.connect(DB_NAME)
    try:
        _ensure_cache_table(conn)
        now_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows=[]
        for _, row in group.iterrows():
            rows.append((_make_article_hash(row),str(row.get("종목코드","")).replace(".0","").zfill(6),str(row.get("종목명","")),str(row.get("뉴스URL","")),str(row.get("뉴스제목","")),str(row.get("기사발행일시","")),now_text,now_text))
        conn.executemany("""
            INSERT INTO news_article_cache (
                기사해시, 종목코드, 종목명, 뉴스URL, 뉴스제목,
                기사발행일시, 최초수집일시, 최종수집일시
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(기사해시) DO UPDATE SET 최종수집일시=excluded.최종수집일시
        """, rows)
        conn.commit()
    finally:
        conn.close()

def _load_cached_analysis(stock_code: str, fingerprint: str) -> dict | None:
    conn=sqlite3.connect(DB_NAME)
    try:
        _ensure_cache_table(conn)
        row=conn.execute("""
            SELECT 뉴스점수, 뉴스판단, 뉴스요약, 뉴스분석사유,
                   핵심뉴스JSON, 긍정요인JSON, 부정요인JSON,
                   영향기간, 신뢰도, 분석상태, 재시도가능일시, 마지막오류
            FROM news_ai_summary WHERE 종목코드=? AND 뉴스지문=?
        """, (stock_code,fingerprint)).fetchone()
        if not row: return None
        status=str(row[9] or "완료")
        retry_text=str(row[10] or "").strip()
        if status in {"대기","실패"} and retry_text:
            retry_at=pd.to_datetime(retry_text,errors="coerce")
            if pd.notna(retry_at) and datetime.now() < retry_at.to_pydatetime():
                return {"뉴스점수":0.0,"뉴스판단":"분석 대기","뉴스요약":"AI 분석 재시도 대기 중입니다.","뉴스분석사유":str(row[11] or "API 호출 제한"),"핵심뉴스":[],"긍정요인":[],"부정요인":[],"영향기간":"판단불가","신뢰도":0,"분석상태":status}
            return None
        return {"뉴스점수":row[0],"뉴스판단":row[1],"뉴스요약":row[2],"뉴스분석사유":row[3],"핵심뉴스":json.loads(row[4] or "[]"),"긍정요인":json.loads(row[5] or "[]"),"부정요인":json.loads(row[6] or "[]"),"영향기간":row[7],"신뢰도":row[8],"분석상태":status}
    finally:
        conn.close()

def _save_cached_analysis(stock_code: str, stock_name: str, fingerprint: str, result: dict, model: str) -> None:
    conn=sqlite3.connect(DB_NAME)
    try:
        _ensure_cache_table(conn)
        conn.execute("""
            INSERT OR REPLACE INTO news_ai_summary (
                종목코드, 종목명, 뉴스지문, 분석일시, 뉴스점수, 뉴스판단,
                뉴스요약, 뉴스분석사유, 핵심뉴스JSON, 긍정요인JSON,
                부정요인JSON, 영향기간, 신뢰도, 모델명, 분석상태,
                재시도가능일시, 마지막오류, 호출횟수
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '완료', NULL, NULL, 1)
        """, (stock_code,stock_name,fingerprint,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),result["뉴스점수"],result["뉴스판단"],result["뉴스요약"],result["뉴스분석사유"],json.dumps(result["핵심뉴스"],ensure_ascii=False),json.dumps(result["긍정요인"],ensure_ascii=False),json.dumps(result["부정요인"],ensure_ascii=False),result["영향기간"],result["신뢰도"],model))
        conn.commit()
    finally:
        conn.close()


def _save_failed_analysis(stock_code: str, stock_name: str, fingerprint: str, error: Exception, model: str) -> None:
    error_text=str(error)
    retry_at=datetime.now()+timedelta(minutes=GEMINI_RETRY_MINUTES)
    conn=sqlite3.connect(DB_NAME)
    try:
        _ensure_cache_table(conn)
        conn.execute("""
            INSERT INTO news_ai_summary (
                종목코드, 종목명, 뉴스지문, 분석일시, 뉴스점수, 뉴스판단,
                뉴스요약, 뉴스분석사유, 핵심뉴스JSON, 긍정요인JSON,
                부정요인JSON, 영향기간, 신뢰도, 모델명, 분석상태,
                재시도가능일시, 마지막오류, 호출횟수
            ) VALUES (?, ?, ?, ?, 0, '분석 대기', ?, ?, '[]', '[]', '[]', '판단불가', 0, ?, '대기', ?, ?, 1)
            ON CONFLICT(종목코드, 뉴스지문) DO UPDATE SET
                분석일시=excluded.분석일시, 분석상태='대기',
                재시도가능일시=excluded.재시도가능일시,
                마지막오류=excluded.마지막오류,
                호출횟수=COALESCE(news_ai_summary.호출횟수,0)+1
        """, (stock_code,stock_name,fingerprint,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"AI 분석 재시도 대기 중입니다.",error_text,model,retry_at.strftime("%Y-%m-%d %H:%M:%S"),error_text))
        conn.commit()
    finally:
        conn.close()

def _build_prompt(
    stock_name: str,
    stock_code: str,
    group: pd.DataFrame,
) -> str:
    article_blocks = []

    limited_group = group.sort_values("기사발행일시", ascending=False).head(MAX_ARTICLES_PER_ANALYSIS)

    for number, (_, row) in enumerate(limited_group.iterrows(), start=1):
        article_blocks.append(
            f"""
[기사 {number}]
발행시각: {row.get('기사발행일시', '')}
언론사: {row.get('언론사', '')}
제목: {row.get('뉴스제목', '')}
RSS 설명: {_clean_text(row.get('뉴스설명', ''))[:MAX_DESCRIPTION_CHARS]}
""".strip()
        )

    articles = "\n\n".join(article_blocks)

    return f"""
너는 한국 주식시장 뉴스 분석가다.
아래는 {stock_name}({stock_code}) 관련 최근 뉴스 묶음이다.

중요 원칙:
1. 단순 키워드 탐지가 아니라 제목과 RSS 설명의 문맥을 함께 해석한다.
2. 같은 사건을 여러 언론사가 반복 보도한 경우 하나의 이슈로 묶는다.
3. 확인되지 않은 기대·가능성·루머는 실제 계약이나 확정 공시보다 낮게 평가한다.
4. 종목과 직접 관련성이 낮은 동명이인·산업 일반 뉴스는 제외한다.
5. 긍정과 부정이 함께 있으면 상쇄해서 종합한다.
6. 뉴스점수는 -10부터 +10까지다.
   +8~+10: 기업가치에 매우 큰 확정적 호재
   +4~+7: 의미 있는 호재
   +1~+3: 제한적 또는 불확실한 긍정
   0: 중립, 영향 불명확, 관련성 부족
   -1~-3: 제한적 부정
   -4~-7: 의미 있는 악재
   -8~-10: 존립·상장·재무에 중대한 악재
7. 투자 권유가 아니라 뉴스 영향 분석만 한다.
8. 아래 JSON 스키마 외의 문장은 출력하지 않는다.

출력 JSON:
{{
  "뉴스점수": 0,
  "뉴스판단": "강한 호재|호재|중립|악재|강한 악재",
  "뉴스요약": "전체 뉴스를 2~4문장으로 종합한 설명",
  "뉴스분석사유": "왜 이 점수인지 구체적으로 설명",
  "핵심뉴스": [
    {{
      "제목": "핵심 이슈 제목",
      "판단": "호재|중립|악재",
      "영향도": "높음|중간|낮음",
      "근거": "판단 근거"
    }}
  ],
  "긍정요인": ["긍정 요인"],
  "부정요인": ["부정 요인 또는 위험요인"],
  "영향기간": "단기|중기|장기|단기·중기|중기·장기|불명확",
  "신뢰도": 0
}}

뉴스:
{articles}
""".strip()


def _call_gemini_json(
    prompt: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            ".env에 GEMINI_API_KEY가 없습니다. "
            "기존 Gemini 키를 확인하세요."
        )

    url = (
        f"{GEMINI_API_BASE}/models/{model}:generateContent"
        f"?key={GEMINI_API_KEY}"
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "뉴스점수": {"type": "NUMBER"},
            "뉴스판단": {"type": "STRING"},
            "뉴스요약": {"type": "STRING"},
            "뉴스분석사유": {"type": "STRING"},
            "핵심뉴스": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "제목": {"type": "STRING"},
                        "판단": {"type": "STRING"},
                        "영향도": {"type": "STRING"},
                        "근거": {"type": "STRING"},
                    },
                    "required": ["제목", "판단", "영향도", "근거"],
                },
            },
            "긍정요인": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "부정요인": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "영향기간": {"type": "STRING"},
            "신뢰도": {"type": "INTEGER"},
        },
        "required": [
            "뉴스점수",
            "뉴스판단",
            "뉴스요약",
            "뉴스분석사유",
            "핵심뉴스",
            "긍정요인",
            "부정요인",
            "영향기간",
            "신뢰도",
        ],
    }

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.15,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    response = requests.post(
        url,
        headers={"content-type": "application/json"},
        json=payload,
        timeout=90,
    )
    response.raise_for_status()
    body = response.json()

    candidates = body.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"Gemini 응답 후보 없음: {body}")

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )
    text = "".join(str(part.get("text", "")) for part in parts)
    result = _safe_json_loads(text)

    if not result:
        raise RuntimeError(f"Gemini JSON 해석 실패: {text[:500]}")

    return {
        "뉴스점수": _clip_score(result.get("뉴스점수")),
        "뉴스판단": str(result.get("뉴스판단", "중립")),
        "뉴스요약": str(result.get("뉴스요약", "")).strip(),
        "뉴스분석사유": str(result.get("뉴스분석사유", "")).strip(),
        "핵심뉴스": (
            result.get("핵심뉴스", [])
            if isinstance(result.get("핵심뉴스"), list)
            else []
        ),
        "긍정요인": (
            result.get("긍정요인", [])
            if isinstance(result.get("긍정요인"), list)
            else []
        ),
        "부정요인": (
            result.get("부정요인", [])
            if isinstance(result.get("부정요인"), list)
            else []
        ),
        "영향기간": str(result.get("영향기간", "불명확")),
        "신뢰도": _clip_confidence(result.get("신뢰도")),
    }


def _analyze_news_with_rules(stock_name: str, group: pd.DataFrame) -> dict:
    """API 없이 제목·RSS 요약에서 명확한 재료를 1차 분류한다.

    이 결과는 Gemini의 정밀 분석보다 낮은 신뢰도로 표시한다. 분석 실패를
    '뉴스 없음'이나 0점으로 잘못 해석하지 않기 위한 안전장치다.
    """
    hits: list[tuple[str, int]] = []
    seen_titles: set[str] = set()

    for _, row in group.iterrows():
        title = _clean_text(row.get("뉴스제목", ""))
        description = _clean_text(row.get("뉴스설명", ""))
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        text = f"{title} {description}".lower()
        for keyword, weight in {**POSITIVE_NEWS_KEYWORDS, **NEGATIVE_NEWS_KEYWORDS}.items():
            if keyword.lower() in text:
                hits.append((keyword, weight))

    raw_score = sum(weight for _, weight in hits)
    score = round(max(-4.0, min(4.0, float(raw_score))), 2)
    positives = [keyword for keyword, weight in hits if weight > 0]
    negatives = [keyword for keyword, weight in hits if weight < 0]

    if score >= 2:
        judgement = "긍정"
    elif score <= -2:
        judgement = "부정"
    else:
        judgement = "중립"

    if hits:
        reason = "무료 규칙 1차 분석: " + ", ".join(dict.fromkeys(keyword for keyword, _ in hits))
        summary = f"{stock_name} 관련 기사에서 명확한 재료 키워드를 1차 분류했습니다."
        confidence = 55
    else:
        reason = "무료 규칙 분석에서 명확한 호재·악재 키워드를 찾지 못했습니다."
        summary = f"{stock_name} 관련 뉴스는 있으나 Gemini 정밀 분석 전입니다."
        confidence = 35

    return {
        "뉴스점수": score,
        "뉴스판단": judgement,
        "뉴스요약": summary,
        "뉴스분석사유": reason,
        "핵심뉴스": [],
        "긍정요인": list(dict.fromkeys(positives)),
        "부정요인": list(dict.fromkeys(negatives)),
        "영향기간": "판단불가",
        "신뢰도": confidence,
        "분석상태": "로컬 1차 분석",
    }


def analyze_news_by_stock_with_gemini(
    news_df: pd.DataFrame,
    model: str = DEFAULT_MODEL,
    delay: float = 1.0,
    priority_stock_codes: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if news_df is None or news_df.empty:
        empty_summary=pd.DataFrame(columns=["종목코드","종목명","뉴스점수","뉴스판단","뉴스요약","뉴스분석사유","뉴스건수","핵심뉴스JSON","긍정요인JSON","부정요인JSON","영향기간","신뢰도","뉴스평가상태"])
        return pd.DataFrame(), empty_summary

    analyzed_groups=[]
    summary_rows=[]
    api_call_count=0
    priority_codes = {
        str(code).replace(".0", "").zfill(6)
        for code in (priority_stock_codes or [])
        if str(code).strip()
    }
    if not priority_codes:
        # 호출자가 우선순위를 주지 않은 경우에도 코드순으로 전체 호출하지 않는다.
        priority_codes = {
            str(code).replace(".0", "").zfill(6)
            for code in news_df["종목코드"].drop_duplicates().head(
                GEMINI_PRIORITY_STOCKS_PER_RUN
            )
        }

    for stock_code, group in news_df.groupby("종목코드"):
        group=group.copy()
        stock_code=str(stock_code).replace(".0","").zfill(6)
        stock_name=str(group.iloc[0].get("종목명","")).strip()
        _save_article_cache(group)
        fingerprint=_make_fingerprint(stock_code,group)
        cached=_load_cached_analysis(stock_code,fingerprint)

        # Gemini 호출 실패 기록은 재시도 시점까지 그대로 '분석 대기'로
        # 노출하지 않는다. 무료 규칙 분석을 즉시 사용해야 뉴스가 미분석으로
        # 남지 않고, Gemini가 복구되면 그때 정밀 분석으로 갱신할 수 있다.
        if cached is not None and str(cached.get("분석상태", "")) in {"대기", "실패"}:
            result = _analyze_news_with_rules(stock_name, group)
            result["뉴스분석사유"] += " Gemini 정밀 분석은 재시도 대기 중이며 무료 1차 분석 결과를 표시합니다."
            print(f"무료 1차 뉴스 분석 유지: {stock_name}({stock_code})")
        elif cached is not None:
            result=cached
            print(f"Gemini 뉴스 캐시 사용: {stock_name}({stock_code})")
        elif (
            stock_code not in priority_codes
            or api_call_count >= MAX_GEMINI_CALLS_PER_RUN
        ):
            result = _analyze_news_with_rules(stock_name, group)
            print(f"무료 1차 뉴스 분석 사용: {stock_name}({stock_code})")
        else:
            print(f"Gemini 뉴스 문맥 분석 중: {stock_name}({stock_code})")
            prompt=_build_prompt(stock_name,stock_code,group)
            api_call_count += 1
            try:
                result=_call_gemini_json(prompt,model=model)
                result["분석상태"]="완료"
                _save_cached_analysis(stock_code,stock_name,fingerprint,result,model)
            except Exception as exc:
                print(f"{stock_name} Gemini 뉴스 분석 실패:",exc)
                _save_failed_analysis(stock_code,stock_name,fingerprint,exc,model)
                result = _analyze_news_with_rules(stock_name, group)
                result["뉴스분석사유"] += " Gemini 정밀 분석 실패로 무료 1차 결과를 사용합니다."
            time.sleep(delay)

        analysis_status=str(result.get("분석상태","완료"))
        group["AI뉴스점수"]=result["뉴스점수"]
        group["AI뉴스판단"]=result["뉴스판단"]
        group["AI뉴스요약"]=result["뉴스요약"]
        group["AI뉴스분석사유"]=result["뉴스분석사유"]
        group["AI영향기간"]=result["영향기간"]
        group["AI신뢰도"]=result["신뢰도"]
        group["AI분석상태"]=analysis_status
        group["AI분석모델"]=model
        group["AI분석일시"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        group["기사해시"]=group.apply(_make_article_hash,axis=1)
        analyzed_groups.append(group)

        summary_rows.append({"종목코드":stock_code,"종목명":stock_name,"뉴스점수":result["뉴스점수"],"뉴스판단":result["뉴스판단"],"뉴스요약":result["뉴스요약"],"뉴스분석사유":result["뉴스분석사유"],"뉴스건수":len(group),"핵심뉴스JSON":json.dumps(result["핵심뉴스"],ensure_ascii=False),"긍정요인JSON":json.dumps(result["긍정요인"],ensure_ascii=False),"부정요인JSON":json.dumps(result["부정요인"],ensure_ascii=False),"영향기간":result["영향기간"],"신뢰도":result["신뢰도"],"뉴스평가상태":analysis_status})

    print(f"Gemini 실제 호출: {api_call_count}건 / 상한 {MAX_GEMINI_CALLS_PER_RUN}건")
    return pd.concat(analyzed_groups,ignore_index=True), pd.DataFrame(summary_rows)

# 기존 코드와의 호환용 이름이다.
def summarize_news_by_stock(news_df: pd.DataFrame) -> pd.DataFrame:
    _, summary_df = analyze_news_by_stock_with_gemini(news_df)
    return summary_df
