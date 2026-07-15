from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd


# 시장점수 최대값:
# 거래량 15 + 상승률 15 + 거래대금 15
# + 20일 10 + 60일 10 + 거래량증가 10
# + 정배열 5 + 신고가 5 + RSI 5 + MACD 5 = 95점
MARKET_SCORE_RAW_MAX = 95.0
MARKET_SCORE_MAX = 70.0
SUPPLY_SCORE_MIN = -15.0
SUPPLY_SCORE_MAX = 15.0
NEWS_SCORE_MIN = -10.0
NEWS_SCORE_MAX = 10.0
FINAL_SCORE_MIN = 0.0
FINAL_SCORE_MAX = 100.0

NO_NEWS_VALUES = {
    "",
    "관련 뉴스 없음",
    "뉴스 조회 실패",
    "종목명 없음",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clip(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _clean_code(value: Any) -> str:
    code = str(value).replace(".0", "").strip()
    return code.zfill(6) if code else ""


def _rank_score(rank: Any, max_score: float = 15.0) -> float:
    try:
        rank_value = int(float(rank))
    except (TypeError, ValueError):
        return 0.0

    if not 1 <= rank_value <= 30:
        return 0.0

    return round(max_score * (31 - rank_value) / 30, 2)


def _return_score(rate: Any, max_score: float = 10.0) -> float:
    rate_value = _safe_float(rate)

    if rate_value >= 20:
        return max_score
    if rate_value >= 10:
        return max_score * 0.8
    if rate_value >= 5:
        return max_score * 0.6
    if rate_value >= 0:
        return max_score * 0.4
    if rate_value >= -5:
        return max_score * 0.2
    return 0.0


def _volume_increase_score(rate: Any, max_score: float = 10.0) -> float:
    rate_value = _safe_float(rate)

    if rate_value >= 200:
        return max_score
    if rate_value >= 150:
        return max_score * 0.8
    if rate_value >= 100:
        return max_score * 0.6
    if rate_value >= 50:
        return max_score * 0.4
    if rate_value >= 20:
        return max_score * 0.2
    return 0.0


def _grade(score: Any) -> str:
    value = _safe_float(score)

    if value >= 85:
        return "★★★★★"
    if value >= 70:
        return "★★★★☆"
    if value >= 55:
        return "★★★☆☆"
    if value >= 40:
        return "★★☆☆☆"
    return "★☆☆☆☆"


def _recommendation(score: Any) -> str:
    value = _safe_float(score)

    if value >= 85:
        return "강력관심"
    if value >= 70:
        return "관심"
    if value >= 55:
        return "관찰"
    if value >= 40:
        return "약세"
    return "제외"


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    if close is None or close.empty:
        return 50.0

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    valid = rsi.dropna()

    return round(float(valid.iloc[-1]), 2) if not valid.empty else 50.0


def _calc_macd(close: pd.Series) -> tuple[str, float]:
    if close is None or close.empty:
        return "판단불가", 0.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    if len(macd.dropna()) < 2:
        return "판단불가", 0.0

    if macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]:
        return "골든크로스", 5.0
    if macd.iloc[-1] > signal.iloc[-1]:
        return "상승유지", 3.0
    return "약세", 0.0


def _latest_market_date(df: pd.DataFrame) -> str:
    if df.empty or "날짜" not in df.columns:
        return ""

    dates = pd.to_datetime(df["날짜"], errors="coerce").dropna()
    if dates.empty:
        return ""

    return dates.max().strftime("%Y-%m-%d")


def _calc_technical_metrics(
    chart_history_df: pd.DataFrame | None,
    stock_code: str,
) -> dict[str, Any]:
    default = {
        "시장기준일": "",
        "5일수익률": 0.0,
        "20일수익률": 0.0,
        "60일수익률": 0.0,
        "20일변동성": 0.0,
        "거래량증가율": 0.0,
        "MA5": 0.0,
        "MA20": 0.0,
        "MA60": 0.0,
        "정배열": "N",
        "정배열점수": 0.0,
        "신고가돌파": "N",
        "신고가점수": 0.0,
        "RSI": 50.0,
        "RSI점수": 3.0,
        "MACD": "판단불가",
        "MACD점수": 0.0,
    }

    if chart_history_df is None or chart_history_df.empty:
        return default

    required = {"종목코드", "날짜", "종가"}
    if not required.issubset(chart_history_df.columns):
        return default

    code = _clean_code(stock_code)
    df = chart_history_df.copy()
    df["종목코드"] = df["종목코드"].map(_clean_code)
    df = df[df["종목코드"] == code].copy()

    if df.empty:
        return default

    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df["종가"] = pd.to_numeric(df["종가"], errors="coerce")

    if "거래량" not in df.columns:
        df["거래량"] = 0
    df["거래량"] = pd.to_numeric(df["거래량"], errors="coerce").fillna(0)

    df = df.dropna(subset=["날짜", "종가"]).sort_values("날짜")
    df = df.drop_duplicates(subset=["날짜"], keep="last")

    if df.empty:
        return default

    close = df["종가"].astype(float)
    volume = df["거래량"].astype(float)

    current_price = float(close.iloc[-1])
    price_20 = float(close.iloc[-20]) if len(close) >= 20 else float(close.iloc[0])
    price_60 = float(close.iloc[-60]) if len(close) >= 60 else float(close.iloc[0])

    r20 = ((current_price / price_20) - 1) * 100 if price_20 else 0.0
    r60 = ((current_price / price_60) - 1) * 100 if price_60 else 0.0
    price_5 = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
    r5 = ((current_price / price_5) - 1) * 100 if price_5 else 0.0
    daily_returns = close.pct_change().dropna() * 100
    volatility20 = float(daily_returns.tail(20).std()) if not daily_returns.empty else 0.0

    recent_volume = float(volume.tail(5).mean())
    if len(volume) >= 25:
        base_volume = float(volume.iloc[-25:-5].mean())
    elif len(volume) > 5:
        base_volume = float(volume.iloc[:-5].mean())
    else:
        base_volume = float(volume.mean())

    volume_rate = ((recent_volume / base_volume) - 1) * 100 if base_volume else 0.0

    ma5 = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else current_price
    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else current_price
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else current_price

    aligned = "Y" if ma5 > ma20 > ma60 else "N"
    aligned_score = 5.0 if aligned == "Y" else 0.0

    high_60 = float(close.tail(60).max())
    breakout = "Y" if current_price >= high_60 else "N"
    breakout_score = 5.0 if breakout == "Y" else 0.0

    rsi = _calc_rsi(close)
    if 45 <= rsi <= 70:
        rsi_score = 5.0
    elif 35 <= rsi < 45 or 70 < rsi <= 80:
        rsi_score = 3.0
    else:
        rsi_score = 0.0

    macd_status, macd_score = _calc_macd(close)

    return {
        "시장기준일": _latest_market_date(df),
        "5일수익률": round(r5, 2),
        "20일수익률": round(r20, 2),
        "60일수익률": round(r60, 2),
        "20일변동성": round(volatility20, 2),
        "거래량증가율": round(volume_rate, 2),
        "MA5": round(ma5, 2),
        "MA20": round(ma20, 2),
        "MA60": round(ma60, 2),
        "정배열": aligned,
        "정배열점수": aligned_score,
        "신고가돌파": breakout,
        "신고가점수": breakout_score,
        "RSI": rsi,
        "RSI점수": rsi_score,
        "MACD": macd_status,
        "MACD점수": macd_score,
    }




def _calc_supply_metrics(
    supply_demand_df: pd.DataFrame | None,
    stock_code: str,
) -> dict[str, Any]:
    default = {
        "수급기준일": "",
        "외국인순매수량": 0,
        "기관순매수량": 0,
        "개인순매수량": 0,
        "외국인3일합계": 0,
        "기관3일합계": 0,
        "외국인연속순매수일": 0,
        "기관연속순매수일": 0,
        "수급점수": 0.0,
        "수급판단": "데이터 없음",
        "수급사유": "데이터 없음",
    }

    if supply_demand_df is None or supply_demand_df.empty:
        return default

    required = {
        "종목코드",
        "날짜",
        "외국인순매수량",
        "기관순매수량",
        "개인순매수량",
    }
    if not required.issubset(supply_demand_df.columns):
        return default

    code = _clean_code(stock_code)
    df = supply_demand_df.copy()
    df["종목코드"] = df["종목코드"].map(_clean_code)
    df = df[df["종목코드"] == code].copy()

    if df.empty:
        return default

    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")

    for column in [
        "외국인순매수량",
        "기관순매수량",
        "개인순매수량",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        ).fillna(0)

    df = (
        df.dropna(subset=["날짜"])
        .sort_values("날짜")
        .drop_duplicates(subset=["날짜"], keep="last")
    )

    if df.empty:
        return default

    latest = df.iloc[-1]

    foreign_today = int(latest["외국인순매수량"])
    institution_today = int(latest["기관순매수량"])
    personal_today = int(latest["개인순매수량"])

    all_missing = (
        foreign_today == 0
        and institution_today == 0
        and personal_today == 0
    )

    if all_missing:
        return {
            **default,
            "수급기준일": latest["날짜"].strftime("%Y-%m-%d"),
        }

    recent3 = df.tail(3)
    foreign_3d = int(recent3["외국인순매수량"].sum())
    institution_3d = int(recent3["기관순매수량"].sum())

    def consecutive_positive(series: pd.Series) -> int:
        count = 0
        for value in reversed(series.tolist()):
            if value > 0:
                count += 1
            else:
                break
        return count

    foreign_days = consecutive_positive(
        df["외국인순매수량"]
    )
    institution_days = consecutive_positive(
        df["기관순매수량"]
    )

    score = 0.0
    reasons: list[str] = []

    if foreign_today > 0:
        score += 3
        reasons.append("외국인 순매수")
    elif foreign_today < 0:
        score -= 3
        reasons.append("외국인 순매도")

    if institution_today > 0:
        score += 3
        reasons.append("기관 순매수")
    elif institution_today < 0:
        score -= 3
        reasons.append("기관 순매도")

    if foreign_today > 0 and institution_today > 0:
        score += 4
        reasons.append("외국인·기관 동반 순매수")
    elif foreign_today < 0 and institution_today < 0:
        score -= 4
        reasons.append("외국인·기관 동반 순매도")

    if foreign_3d > 0:
        score += 1
    elif foreign_3d < 0:
        score -= 1

    if institution_3d > 0:
        score += 1
    elif institution_3d < 0:
        score -= 1

    if foreign_days >= 3:
        score += 1.5
        reasons.append(f"외국인 {foreign_days}일 연속 순매수")

    if institution_days >= 3:
        score += 1.5
        reasons.append(f"기관 {institution_days}일 연속 순매수")

    if (
        personal_today > 0
        and foreign_today < 0
        and institution_today < 0
    ):
        score -= 2
        reasons.append("개인 매수 집중·외국인기관 이탈")

    score = round(
        _clip(score, SUPPLY_SCORE_MIN, SUPPLY_SCORE_MAX),
        2,
    )

    if score >= 7:
        judgement = "매우 강한 순매수"
    elif score >= 3:
        judgement = "순매수 우위"
    elif score <= -7:
        judgement = "매우 강한 순매도"
    elif score <= -3:
        judgement = "순매도 우위"
    else:
        judgement = "중립"

    return {
        "수급기준일": latest["날짜"].strftime("%Y-%m-%d"),
        "외국인순매수량": foreign_today,
        "기관순매수량": institution_today,
        "개인순매수량": personal_today,
        "외국인3일합계": foreign_3d,
        "기관3일합계": institution_3d,
        "외국인연속순매수일": foreign_days,
        "기관연속순매수일": institution_days,
        "수급점수": score,
        "수급판단": judgement,
        "수급사유": (
            ", ".join(reasons)
            if reasons
            else "뚜렷한 매수 우위 없음"
        ),
    }

def _news_lookup(
    news_summary_df: pd.DataFrame | None,
    stock_code: str,
) -> dict[str, Any]:
    default = {
        "뉴스점수": 0.0,
        "뉴스요약": "관련 뉴스 없음",
        "뉴스분석사유": "",
        "뉴스건수": 0,
        "뉴스평가상태": "뉴스 없음",
        "뉴스신뢰도": 0,
    }
    if news_summary_df is None or news_summary_df.empty:
        return default
    if "종목코드" not in news_summary_df.columns:
        return default

    df = news_summary_df.copy()
    df["종목코드"] = df["종목코드"].map(_clean_code)
    hit = df[df["종목코드"] == _clean_code(stock_code)]
    if hit.empty:
        return default

    row = hit.iloc[-1]
    return {
        "뉴스점수": round(
            _clip(
                _safe_float(row.get("뉴스점수")),
                NEWS_SCORE_MIN,
                NEWS_SCORE_MAX,
            ),
            2,
        ),
        "뉴스요약": str(
            row.get("뉴스요약", "관련 뉴스 없음")
        ),
        "뉴스분석사유": str(
            row.get("뉴스분석사유", "")
        ),
        "뉴스건수": int(
            _safe_float(row.get("뉴스건수"), 0)
        ),
        "뉴스평가상태": str(row.get("뉴스평가상태", "분석 상태 미확인")),
        "뉴스신뢰도": int(_safe_float(row.get("신뢰도"), 0)),
    }

def _extract_news_score(row: pd.Series) -> float:
    """
    ranking/news 단계에서 '뉴스점수'가 전달되면 -10~+10 범위로 사용한다.
    아직 Gemini 뉴스평가를 연결하지 않은 경우 0점이다.
    뉴스가 있다는 이유만으로 자동 +10점은 주지 않는다.
    """
    for key in ("뉴스점수", "뉴스감성점수", "news_score"):
        if key in row.index:
            return round(_clip(_safe_float(row.get(key), 0.0), NEWS_SCORE_MIN, NEWS_SCORE_MAX), 2)
    return 0.0


def _make_reason(row: dict[str, Any]) -> str:
    reasons: list[str] = []

    if row["거래량증가율(%)"] >= 100:
        reasons.append(f"거래량 {row['거래량증가율(%)']}% 증가")
    if row["정배열"] == "Y":
        reasons.append("5일선 > 20일선 > 60일선 정배열")
    if row["신고가돌파"] == "Y":
        reasons.append("60일 신고가 구간")
    if row["20일수익률(%)"] >= 5:
        reasons.append("20일 추세 양호")
    if row["60일수익률(%)"] >= 10:
        reasons.append("60일 우상향")
    if 45 <= row["RSI"] <= 70:
        reasons.append(f"RSI {row['RSI']}로 과열 부담 낮음")
    if row["MACD"] in {"골든크로스", "상승유지"}:
        reasons.append(f"MACD {row['MACD']}")

    news_score = _safe_float(row.get("뉴스점수"))
    if news_score > 0:
        reasons.append(f"뉴스 호재 반영 +{news_score:g}점")
    elif news_score < 0:
        reasons.append(f"뉴스 악재 반영 {news_score:g}점")
    elif row.get("뉴스요약"):
        reasons.append("뉴스는 있으나 감성점수 미평가")

    if not reasons:
        reasons.append("뚜렷한 강점 부족, 관찰 필요")

    return ", ".join(reasons)



def _calc_chase_risk(metrics: dict[str, Any]) -> tuple[int, str, str]:
    """점수와 별개로 급등 추격·변동성 위험을 0~100으로 계산한다."""
    score = 0
    reasons: list[str] = []
    r5 = _safe_float(metrics.get("5일수익률"))
    volatility = _safe_float(metrics.get("20일변동성"))
    volume_rate = _safe_float(metrics.get("거래량증가율"))
    rsi = _safe_float(metrics.get("RSI"), 50.0)

    if r5 >= 20:
        score += 30
        reasons.append(f"5일 {r5:.1f}% 급등")
    elif r5 >= 10:
        score += 20
        reasons.append(f"5일 {r5:.1f}% 상승")
    elif r5 >= 5:
        score += 10
        reasons.append(f"5일 {r5:.1f}% 상승")
    if volatility >= 7:
        score += 25
        reasons.append(f"20일 일간 변동성 {volatility:.1f}%")
    elif volatility >= 4:
        score += 15
        reasons.append(f"20일 일간 변동성 {volatility:.1f}%")
    if volume_rate >= 300:
        score += 20
        reasons.append(f"거래량 {volume_rate:.0f}% 급증")
    elif volume_rate >= 150:
        score += 12
        reasons.append(f"거래량 {volume_rate:.0f}% 증가")
    if rsi >= 80:
        score += 20
        reasons.append(f"RSI {rsi:.0f} 과열")
    elif rsi >= 70:
        score += 10
        reasons.append(f"RSI {rsi:.0f} 주의")

    score = min(100, score)
    level = "매우 높음" if score >= 70 else "높음" if score >= 45 else "보통" if score >= 20 else "낮음"
    return score, level, " · ".join(reasons) if reasons else "추격 과열 신호가 뚜렷하지 않음"


def _make_exclusion_reasons(row: dict[str, Any]) -> str:
    reasons: list[str] = []

    final_score = _safe_float(row.get("최종점수"))
    market_score = _safe_float(row.get("시장점수"))
    supply_score = _safe_float(row.get("수급점수"))
    news_score = _safe_float(row.get("뉴스점수"))
    rsi = _safe_float(row.get("RSI"), 50.0)

    if final_score < 40:
        reasons.append(f"최종점수 {final_score:g}점으로 추천 기준 40점 미달")
    if market_score < 35:
        reasons.append(f"시장점수 {market_score:g}점으로 시장 강도 부족")
    if supply_score <= -3:
        reasons.append(f"수급점수 {supply_score:+g}점으로 순매도 우위")
    elif supply_score < 3:
        reasons.append(f"수급점수 {supply_score:+g}점으로 뚜렷한 매수 우위 없음")
    if news_score < 0:
        reasons.append(f"뉴스점수 {news_score:+g}점으로 악재 반영")
    elif news_score == 0:
        reasons.append("뉴스 호재 점수 없음")
    if rsi >= 80:
        reasons.append(f"RSI {rsi:g}로 과열 부담 큼")
    if row.get("정배열") != "Y":
        reasons.append("5일·20일·60일 이동평균 정배열 아님")
    if row.get("MACD") == "약세":
        reasons.append("MACD 약세")
    if _safe_float(row.get("거래량증가점수")) == 0:
        reasons.append("거래량 증가 점수 없음")

    if not reasons and final_score < 55:
        reasons.append("관찰 이상 추천 기준 55점 미달")

    return " | ".join(reasons)

def make_score_sheet(
    volume_rank_df: pd.DataFrame,
    rise_rank_df: pd.DataFrame,
    trade_value_df: pd.DataFrame,
    chart_history_df: pd.DataFrame | None = None,
    supply_demand_df: pd.DataFrame | None = None,
    news_summary_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    score_map: dict[str, dict[str, Any]] = {}

    def add_base_score(
        df: pd.DataFrame | None,
        score_name: str,
        source_name: str,
    ) -> None:
        if df is None or df.empty:
            return

        for _, source_row in df.iterrows():
            code = _clean_code(source_row.get("종목코드", ""))
            name = str(source_row.get("종목명", "")).strip()
            rank = source_row.get("순위", "")
            news = str(source_row.get("뉴스요약", "")).strip()
            news_score = _extract_news_score(source_row)

            if not code or code == "000000":
                continue

            if code not in score_map:
                score_map[code] = {
                    "종목코드": code,
                    "종목명": name,
                    "거래량점수": 0.0,
                    "상승률점수": 0.0,
                    "거래대금점수": 0.0,
                    "뉴스점수": 0.0,
                    "포함조건": [],
                    "뉴스요약목록": [],
                }

            item = score_map[code]
            if name and not item["종목명"]:
                item["종목명"] = name

            item[score_name] = max(item[score_name], _rank_score(rank, 15.0))
            item["포함조건"].append(source_name)

            if news not in NO_NEWS_VALUES and news not in item["뉴스요약목록"]:
                item["뉴스요약목록"].append(news)

            # 동일 종목이 여러 TOP30 목록에 있더라도 같은 뉴스점수를 중복 합산하지 않는다.
            # 절댓값이 더 큰 평가를 대표 뉴스점수로 사용한다.
            if abs(news_score) > abs(item["뉴스점수"]):
                item["뉴스점수"] = news_score

    add_base_score(volume_rank_df, "거래량점수", "거래량 TOP30")
    add_base_score(rise_rank_df, "상승률점수", "상승률 TOP30")
    add_base_score(trade_value_df, "거래대금점수", "거래대금 TOP30")

    rows: list[dict[str, Any]] = []
    now = datetime.now()

    for code, item in score_map.items():
        metrics = _calc_technical_metrics(chart_history_df, code)
        supply = _calc_supply_metrics(supply_demand_df, code)
        news_info = _news_lookup(news_summary_df, code)
        chase_risk, risk_level, risk_reason = _calc_chase_risk(metrics)

        r20_score = _return_score(metrics["20일수익률"], 10.0)
        r60_score = _return_score(metrics["60일수익률"], 10.0)
        volume_increase_score = _volume_increase_score(metrics["거래량증가율"], 10.0)

        raw_market_score = (
            item["거래량점수"]
            + item["상승률점수"]
            + item["거래대금점수"]
            + r20_score
            + r60_score
            + volume_increase_score
            + metrics["정배열점수"]
            + metrics["신고가점수"]
            + metrics["RSI점수"]
            + metrics["MACD점수"]
        )
        raw_market_score = _clip(
            raw_market_score,
            0.0,
            MARKET_SCORE_RAW_MAX,
        )
        market_score = round(
            raw_market_score
            / MARKET_SCORE_RAW_MAX
            * MARKET_SCORE_MAX,
            2,
        )

        supply_score = round(
            _clip(
                supply["수급점수"],
                SUPPLY_SCORE_MIN,
                SUPPLY_SCORE_MAX,
            ),
            2,
        )

        # 새 뉴스 분석 결과가 있으면 우선 사용하고,
        # 없으면 기존 ranking 단계에서 전달된 뉴스점수를 사용한다.
        news_score = news_info["뉴스점수"]
        if (
            news_info["뉴스요약"] == "관련 뉴스 없음"
            and item["뉴스점수"] != 0
        ):
            news_score = round(
                _clip(
                    item["뉴스점수"],
                    NEWS_SCORE_MIN,
                    NEWS_SCORE_MAX,
                ),
                2,
            )

        final_score = round(
            _clip(
                market_score + supply_score + news_score,
                FINAL_SCORE_MIN,
                FINAL_SCORE_MAX,
            ),
            2,
        )

        news_summary = news_info["뉴스요약"]
        if news_summary == "관련 뉴스 없음":
            legacy_summary = " | ".join(
                item["뉴스요약목록"][:5]
            )
            if legacy_summary:
                news_summary = legacy_summary

        if news_score > 0:
            change_reason = (
                f"시장 {market_score}점, 수급 {supply_score:+g}점, "
                f"호재 뉴스 {news_score:+g}점 반영"
            )
        elif news_score < 0:
            change_reason = (
                f"시장 {market_score}점, 수급 {supply_score:+g}점, "
                f"악재 뉴스 {news_score:+g}점 반영"
            )
        elif news_summary != "관련 뉴스 없음":
            change_reason = (
                f"시장 {market_score}점, 수급 {supply_score:+g}점, "
                "뉴스는 있으나 중립 평가"
            )
        else:
            change_reason = (
                f"시장 {market_score}점, 수급 {supply_score:+g}점, "
                "신규 뉴스 없음"
            )

        row: dict[str, Any] = {
            "종목코드": item["종목코드"],
            "종목명": item["종목명"],
            "시장기준일": metrics["시장기준일"],
            "최종갱신일자": now.strftime("%Y-%m-%d"),
            "최종갱신시간": now.strftime("%H:%M:%S"),
            "시장원점수": round(raw_market_score, 2),
            "시장점수": market_score,
            "수급점수": supply_score,
            "수급기준일": supply["수급기준일"],
            "수급판단": supply["수급판단"],
            "수급사유": supply.get("수급사유", ""),
            "외국인순매수량": supply["외국인순매수량"],
            "기관순매수량": supply["기관순매수량"],
            "개인순매수량": supply["개인순매수량"],
            "외국인3일합계": supply["외국인3일합계"],
            "기관3일합계": supply["기관3일합계"],
            "외국인연속순매수일": supply["외국인연속순매수일"],
            "기관연속순매수일": supply["기관연속순매수일"],
            "뉴스점수": news_score,
            "뉴스분석사유": news_info["뉴스분석사유"],
            "뉴스건수": news_info["뉴스건수"],
            "뉴스평가상태": news_info["뉴스평가상태"],
            "뉴스신뢰도": news_info["뉴스신뢰도"],
            "AI점수": 0.0,
            "최종점수": final_score,
            # 기존 dashboard.py와 DB 호환을 위해 총점도 최종점수와 동일하게 유지한다.
            "총점": final_score,
            "등급": _grade(final_score),
            "최종추천": _recommendation(final_score),
            "추격위험도": chase_risk,
            "추격위험등급": risk_level,
            "추격위험사유": risk_reason,
            "점수변동사유": change_reason,
            "뉴스평가상태": news_info["뉴스평가상태"],
            "거래량점수": item["거래량점수"],
            "상승률점수": item["상승률점수"],
            "거래대금점수": item["거래대금점수"],
            "5일수익률(%)": metrics["5일수익률"],
            "20일수익률(%)": metrics["20일수익률"],
            "20일수익률점수": r20_score,
            "60일수익률(%)": metrics["60일수익률"],
            "20일변동성(%)": metrics["20일변동성"],
            "60일수익률점수": r60_score,
            "거래량증가율(%)": metrics["거래량증가율"],
            "거래량증가점수": volume_increase_score,
            "MA5": metrics["MA5"],
            "MA20": metrics["MA20"],
            "MA60": metrics["MA60"],
            "정배열": metrics["정배열"],
            "정배열점수": metrics["정배열점수"],
            "신고가돌파": metrics["신고가돌파"],
            "신고가점수": metrics["신고가점수"],
            "RSI": metrics["RSI"],
            "RSI점수": metrics["RSI점수"],
            "MACD": metrics["MACD"],
            "MACD점수": metrics["MACD점수"],
            "포함조건": ", ".join(sorted(set(item["포함조건"]))),
            "뉴스요약": news_summary,
        }

        row["AI추천사유"] = _make_reason(row)
        row["추천제외사유"] = _make_exclusion_reasons(row)
        rows.append(row)

    result = pd.DataFrame(rows)

    if result.empty:
        return pd.DataFrame()

    result = result.sort_values(
        ["최종점수", "시장점수"],
        ascending=[False, False],
    ).reset_index(drop=True)
    result.insert(0, "점수순위", range(1, len(result) + 1))

    return result
