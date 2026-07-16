"""OpenDART-based fundamental data collection and scoring for HONG STOCK."""

from __future__ import annotations

import io
import json
import os
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_FILE = DATA_DIR / "opendart_financial_cache.json"
CORP_CODE_FILE = DATA_DIR / "opendart_corp_codes.json"
API_BASE = "https://opendart.fss.or.kr/api"

def _number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None

def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

def _api_key() -> str:
    return os.getenv("OPENDART_API_KEY", "").strip()

def _corp_codes() -> dict[str, str]:
    cached = _read_json(CORP_CODE_FILE)
    if cached:
        return {str(code).zfill(6): str(corp) for code, corp in cached.items()}
    key = _api_key()
    if not key:
        return {}
    response = requests.get(f"{API_BASE}/corpCode.xml", params={"crtfc_key": key}, timeout=30)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
        root = ET.fromstring(archive.read(xml_name))
    result: dict[str, str] = {}
    for node in root.findall("list"):
        stock_code = (node.findtext("stock_code") or "").strip()
        corp_code = (node.findtext("corp_code") or "").strip()
        if stock_code and corp_code:
            result[stock_code.zfill(6)] = corp_code
    _write_json(CORP_CODE_FILE, result)
    return result

def _metric_value(rows: list[dict[str, Any]], *keywords: str) -> float | None:
    for row in rows:
        label = str(row.get("idx_nm") or row.get("idx_cl_nm") or "").lower().replace(" ", "")
        if any(keyword.lower().replace(" ", "") in label for keyword in keywords):
            value = _number(row.get("idx_val"))
            if value is not None:
                return value
    return None

def _fetch_one(stock_code: str, corp_code: str) -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"종목코드": stock_code, "재무상태": "API 키 없음"}
    business_year = str(datetime.now().year - 1)
    rows: list[dict[str, Any]] = []
    last_message = "재무 데이터 없음"
    for class_code in ("M210000", "M220000", "M230000"):
        response = requests.get(f"{API_BASE}/fnlttSinglIndx.json", params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": business_year, "reprt_code": "11011", "idx_cl_code": class_code}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        last_message = str(payload.get("message") or last_message)
        rows.extend(payload.get("list") or [])
    if not rows:
        return {"종목코드": stock_code, "재무상태": last_message}
    return {"종목코드": stock_code, "재무기준일": str(rows[0].get("stlm_dt") or ""), "매출증가율": _metric_value(rows, "매출액증가율", "매출증가율"), "이익증가율": _metric_value(rows, "영업이익증가율", "순이익증가율"), "영업이익률": _metric_value(rows, "영업이익률"), "ROE": _metric_value(rows, "roe"), "부채비율": _metric_value(rows, "부채비율"), "유동비율": _metric_value(rows, "유동비율"), "재무상태": "정상"}

def _evaluate(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("재무상태") != "정상":
        return {"재무점수": 0.0, "재무등급": "평가 제외", "재무사유": str(row.get("재무상태") or "데이터 없음")}
    score = 0.0; reasons: list[str] = []; warnings: list[str] = []
    sales_growth = _number(row.get("매출증가율")); profit_growth = _number(row.get("이익증가율")); margin = _number(row.get("영업이익률")); roe = _number(row.get("ROE")); debt = _number(row.get("부채비율")); current = _number(row.get("유동비율"))
    if sales_growth is not None:
        if sales_growth >= 10: score += 2; reasons.append("매출 성장")
        elif sales_growth < 0: score -= 2; warnings.append("매출 역성장")
    if profit_growth is not None:
        if profit_growth >= 10: score += 2; reasons.append("이익 개선")
        elif profit_growth < 0: score -= 2; warnings.append("이익 둔화")
    if margin is not None:
        if margin >= 10: score += 2; reasons.append("영업수익성 양호")
        elif margin < 0: score -= 3; warnings.append("영업적자")
    if roe is not None:
        if roe >= 10: score += 2; reasons.append("ROE 양호")
        elif roe < 0: score -= 2; warnings.append("ROE 음수")
    if debt is not None:
        if debt >= 300: score -= 5; warnings.append("부채비율 매우 높음")
        elif debt >= 200: score -= 3; warnings.append("부채비율 높음")
        elif debt <= 100: score += 1; reasons.append("부채 부담 낮음")
    if current is not None:
        if current < 100: score -= 2; warnings.append("유동성 주의")
        elif current >= 150: score += 1; reasons.append("유동성 양호")
    score = round(max(-10.0, min(10.0, score)), 2)
    grade = "양호" if score >= 5 else "주의" if score <= -4 else "보통"
    return {"재무점수": score, "재무등급": grade, "재무사유": ", ".join(reasons + warnings) or "핵심 재무지표 확인"}

def collect_financial_metrics(candidate_df: pd.DataFrame, cache_hours: int = 24) -> pd.DataFrame:
    if candidate_df is None or candidate_df.empty:
        return pd.DataFrame()
    cache = _read_json(CACHE_FILE); corp_codes = _corp_codes(); now = datetime.now(); output: list[dict[str, Any]] = []
    for _, source in candidate_df.drop_duplicates(subset=["종목코드"]).iterrows():
        code = str(source.get("종목코드", "")).replace(".0", "").zfill(6); cached = cache.get(code, {})
        try: fresh = now - datetime.fromisoformat(cached.get("cached_at", "")) < timedelta(hours=cache_hours)
        except ValueError: fresh = False
        if fresh: item = dict(cached.get("data") or {})
        elif code not in corp_codes: item = {"종목코드": code, "재무상태": "재무평가 제외(ETF·비공시 종목)"}
        else:
            try: item = _fetch_one(code, corp_codes[code])
            except requests.RequestException as exc: item = {"종목코드": code, "재무상태": f"조회 보류: {exc.__class__.__name__}"}
            cache[code] = {"cached_at": now.isoformat(), "data": item}; time.sleep(0.12)
        item["종목명"] = str(source.get("종목명", "")); item.update(_evaluate(item)); output.append(item)
    _write_json(CACHE_FILE, cache)
    return pd.DataFrame(output)
