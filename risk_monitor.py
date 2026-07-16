"""GitHub Actions에서 보유종목만 빠르게 확인하는 5분 위험 감시기."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from auto_strategy import (
    DB_PATH,
    get_open_position_codes,
    initialize_auto_strategies,
    monitor_open_position_risk,
)
from central_store import publish_strategy_state, restore_strategy_state
from kis_api import get_access_token, get_current_price
from market_data import get_market_overview
from market_regime import classify_market_regime


def _market_is_open() -> bool:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    return now.weekday() < 5 and time(9, 0) <= now.time() <= time(15, 30)


def _price_from_response(body: dict) -> float:
    output = (body or {}).get("output") or {}
    try:
        return float(str(output.get("stck_prpr", 0)).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def run_once() -> dict[str, int]:
    initialize_auto_strategies()
    try:
        restore_strategy_state(DB_PATH)
    except Exception as exc:
        print(f"중앙 상태 복원 실패 · GitHub DB 사용: {exc}")

    codes = get_open_position_codes()
    if not codes:
        print("보유종목 없음 · 위험 감시 종료")
        return {"checked": 0, "closed": 0}
    if not _market_is_open():
        print("정규장 시간이 아님 · 위험 감시 종료")
        return {"checked": 0, "closed": 0}

    token = get_access_token()
    if not token:
        raise RuntimeError("KIS 접근토큰 발급 실패")
    regime = classify_market_regime(get_market_overview(token))
    prices = {
        code: _price_from_response(get_current_price(token, code)) for code in codes
    }
    result = monitor_open_position_risk(prices, regime)
    publish_strategy_state(DB_PATH)
    print(
        f"시장 {regime['regime']} · 보유종목 {result['checked']}건 확인 · "
        f"가상 청산 {result['closed']}건"
    )
    return result


if __name__ == "__main__":
    run_once()
