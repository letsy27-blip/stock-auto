"""KOSPI·KOSDAQ 장중 흐름으로 신규매수 위험 수준을 제한한다."""

from __future__ import annotations

from typing import Any


EXPOSURE_BY_REGIME = {
    "상승장": 1.0,
    "횡보장": 0.5,
    "하락장": 0.0,
    "급락장": 0.0,
    "판단불가": 0.0,
}


def classify_market_regime(overview: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """두 시장의 당일 등락률을 보수적으로 합쳐 운용 국면을 반환한다."""
    overview = overview or {}
    rates: dict[str, float] = {}
    for name in ("KOSPI", "KOSDAQ"):
        quote = overview.get(name) or {}
        if quote.get("error") or not quote.get("current"):
            continue
        try:
            rates[name] = float(quote.get("change_rate", 0))
        except (TypeError, ValueError):
            continue

    if len(rates) < 2:
        regime = "판단불가"
        reason = "코스피·코스닥 지수 중 일부를 확인하지 못해 신규매수를 중단합니다."
    else:
        values = list(rates.values())
        average = sum(values) / len(values)
        if min(values) <= -3.0 or average <= -2.0:
            regime = "급락장"
            reason = f"코스피 {rates['KOSPI']:+.2f}% · 코스닥 {rates['KOSDAQ']:+.2f}%"
        elif average <= -1.0 or all(value < 0 for value in values):
            regime = "하락장"
            reason = f"코스피 {rates['KOSPI']:+.2f}% · 코스닥 {rates['KOSDAQ']:+.2f}%"
        elif average >= 0.5 and all(value > 0 for value in values):
            regime = "상승장"
            reason = f"코스피 {rates['KOSPI']:+.2f}% · 코스닥 {rates['KOSDAQ']:+.2f}%"
        else:
            regime = "횡보장"
            reason = f"코스피 {rates['KOSPI']:+.2f}% · 코스닥 {rates['KOSDAQ']:+.2f}%"

    return {
        "regime": regime,
        "max_exposure": EXPOSURE_BY_REGIME[regime],
        "rates": rates,
        "reason": reason,
    }
