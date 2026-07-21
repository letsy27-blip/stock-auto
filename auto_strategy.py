"""추천 결과를 실제 체결 규칙으로 검증하는 자동 가상매매 엔진."""

from __future__ import annotations

import sqlite3
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


DB_PATH = Path(__file__).resolve().with_name("stock_data.db")
INITIAL_CASH = 100_000_000.0
BUY_FEE_RATE = 0.00015
SELL_FEE_RATE = 0.00015
SELL_TAX_RATE = 0.0018
RISK_PER_TRADE = 0.01
SELL_SLIPPAGE_RATE = 0.001
STRATEGIES = {"TOP3": 3, "TOP30": 30}
STRATEGY_START_DATE = date.fromisoformat(
    os.getenv("HONGSTOCK_STRATEGY_START_DATE", "2026-07-20")
)


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul"))


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _code(value) -> str:
    text = str(value or "").replace(".0", "").strip()
    return text.zfill(6) if text else ""


def _number(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if pd.notna(result) else default
    except (TypeError, ValueError):
        return default


def initialize_auto_strategies() -> None:
    now = _now().isoformat(timespec="seconds")
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS auto_strategy_accounts (
                strategy TEXT PRIMARY KEY,
                initial_cash REAL NOT NULL,
                cash REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_strategy_positions (
                strategy TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                entry_fee REAL NOT NULL,
                target_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                entry_score REAL NOT NULL,
                entry_rank INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                entry_reason TEXT,
                PRIMARY KEY (strategy, stock_code)
            );
            CREATE TABLE IF NOT EXISTS auto_strategy_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_fee REAL NOT NULL,
                exit_fee REAL NOT NULL,
                tax REAL NOT NULL,
                realized_profit REAL NOT NULL,
                return_rate REAL NOT NULL,
                entry_score REAL NOT NULL,
                entry_rank INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                exit_reason TEXT NOT NULL
            );
            """
        )
        for strategy in STRATEGIES:
            connection.execute(
                "INSERT OR IGNORE INTO auto_strategy_accounts "
                "(strategy, initial_cash, cash, updated_at) VALUES (?, ?, ?, ?)",
                (strategy, INITIAL_CASH, INITIAL_CASH, now),
            )


def _latest_prices(chart_df: pd.DataFrame) -> dict[str, dict[str, float]]:
    if chart_df is None or chart_df.empty or "종목코드" not in chart_df.columns:
        return {}
    frame = chart_df.copy()
    frame["종목코드"] = frame["종목코드"].map(_code)
    frame["날짜"] = pd.to_datetime(frame.get("날짜"), errors="coerce")
    frame = frame.sort_values("날짜").drop_duplicates("종목코드", keep="last")
    result = {}
    for _, row in frame.iterrows():
        close = _number(row.get("종가"))
        if close > 0:
            result[row["종목코드"]] = {
                "close": close,
                "high": _number(row.get("고가"), close),
                "low": _number(row.get("저가"), close),
            }
    return result


def _candidate_sets(scored_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if scored_df is None or scored_df.empty:
        return {name: pd.DataFrame() for name in STRATEGIES}
    frame = scored_df.copy()
    frame["종목코드"] = frame["종목코드"].map(_code)
    frame["최종점수"] = pd.to_numeric(frame.get("최종점수"), errors="coerce").fillna(0)
    frame = frame.sort_values("최종점수", ascending=False).drop_duplicates("종목코드")
    fund_like = frame["종목명"].astype(str).str.contains(
        r"(^KODEX\s|^TIGER\s|^SOL\s|^ACE\s|^RISE\s|^PLUS\s|ETF|ETN|인버스|레버리지|선물)",
        case=False, regex=True, na=False,
    )
    recommendation = frame.get("최종추천", pd.Series("", index=frame.index)).astype(str)
    entry = frame.get("진입판단", pd.Series("", index=frame.index)).astype(str)
    breakout = pd.to_numeric(
        frame.get("돌파신뢰도", pd.Series(0, index=frame.index)), errors="coerce"
    ).fillna(0)
    chase = pd.to_numeric(
        frame.get("추격위험도", pd.Series(100, index=frame.index)), errors="coerce"
    ).fillna(100)
    rsi = pd.to_numeric(
        frame.get("RSI", pd.Series(100, index=frame.index)), errors="coerce"
    ).fillna(100)
    supply = pd.to_numeric(
        frame.get("수급점수", pd.Series(0, index=frame.index)), errors="coerce"
    ).fillna(0)
    buyable = frame[
        ~fund_like
        & recommendation.isin(["강력관심", "관심"])
        & entry.isin(["돌파 확인", "지지선 근처"])
        & (frame["최종점수"] >= 55)
        & (breakout >= 70)
        & (chase < 35)
        & rsi.between(45, 68)
        & (supply >= 0)
    ]
    return {"TOP3": buyable.head(3), "TOP30": buyable.head(30)}


def _analysis_sell_reason(row: pd.Series | dict | None) -> str:
    """현재 분석이 명확한 매도 신호인지 판정한다."""
    if row is None:
        return ""
    recommendation = str(row.get("최종추천", "") or "").strip()
    entry = str(row.get("진입판단", "") or "").strip()
    chase = _number(row.get("추격위험도"), 0)
    reasons = []
    if recommendation in {"약세", "제외"}:
        reasons.append(f"추천 {recommendation}")
    if entry == "위험":
        reasons.append("진입판단 위험")
    if chase >= 70:
        reasons.append(f"추격위험 {chase:.0f}점")
    return " · ".join(reasons)


def _close_position(
    connection: sqlite3.Connection,
    position: sqlite3.Row,
    exit_price: float,
    exit_reason: str,
    closed_at: str,
) -> None:
    gross = exit_price * position["quantity"]
    exit_fee = gross * SELL_FEE_RATE
    tax = gross * SELL_TAX_RATE
    net_exit = gross - exit_fee - tax
    cost = position["entry_price"] * position["quantity"] + position["entry_fee"]
    profit = net_exit - cost
    return_rate = profit / cost * 100 if cost else 0
    connection.execute(
        "INSERT INTO auto_strategy_trades (strategy, stock_code, stock_name, quantity, "
        "entry_price, exit_price, entry_fee, exit_fee, tax, realized_profit, return_rate, "
        "entry_score, entry_rank, opened_at, closed_at, exit_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (position["strategy"], position["stock_code"], position["stock_name"],
         position["quantity"], position["entry_price"], exit_price, position["entry_fee"],
         exit_fee, tax, profit, return_rate, position["entry_score"], position["entry_rank"],
         position["opened_at"], closed_at, exit_reason),
    )
    connection.execute(
        "DELETE FROM auto_strategy_positions WHERE strategy=? AND stock_code=?",
        (position["strategy"], position["stock_code"]),
    )
    connection.execute(
        "UPDATE auto_strategy_accounts SET cash=cash+?, updated_at=? WHERE strategy=?",
        (net_exit, closed_at, position["strategy"]),
    )


def get_open_position_codes() -> list[str]:
    initialize_auto_strategies()
    with _connect() as connection:
        return [
            row[0] for row in connection.execute(
                "SELECT DISTINCT stock_code FROM auto_strategy_positions ORDER BY stock_code"
            ).fetchall()
        ]


def monitor_open_position_risk(
    live_prices: dict[str, float], market_regime: dict | None = None
) -> dict[str, int]:
    """보유종목만 빠르게 감시하고 실제 확인 가격에 슬리피지를 반영해 청산한다."""
    initialize_auto_strategies()
    now = _now().isoformat(timespec="seconds")
    regime = str((market_regime or {}).get("regime", "판단불가"))
    result = {"checked": 0, "closed": 0}
    with _connect() as connection:
        positions = connection.execute("SELECT * FROM auto_strategy_positions").fetchall()
        for position in positions:
            price = _number(live_prices.get(position["stock_code"]))
            if price <= 0:
                continue
            result["checked"] += 1
            reason = ""
            if regime == "급락장":
                reason = "시장 급락 위험회피"
            elif price <= position["stop_price"]:
                reason = "손절선 이탈"
            elif price >= position["target_price"]:
                reason = "목표가 도달"
            if not reason:
                continue
            # 손절선을 건너뛴 급락도 손절가로 낙관하지 않고 확인된 현재가에서 체결한다.
            exit_price = price * (1 - SELL_SLIPPAGE_RATE)
            _close_position(connection, position, exit_price, reason, now)
            result["closed"] += 1
    return result


def update_auto_strategies(
    scored_df: pd.DataFrame,
    chart_df: pd.DataFrame,
    market_regime: dict | None = None,
) -> dict[str, int]:
    """현재 추천만으로 진입·청산한다. 과거 가격을 보고 신호를 소급하지 않는다."""
    initialize_auto_strategies()
    if _now().date() < STRATEGY_START_DATE:
        return {"opened": 0, "closed": 0}
    prices = _latest_prices(chart_df)
    candidates = _candidate_sets(scored_df)
    analysis_rows = {}
    if scored_df is not None and not scored_df.empty:
        latest_analysis = scored_df.copy()
        latest_analysis["종목코드"] = latest_analysis["종목코드"].map(_code)
        latest_analysis = latest_analysis.drop_duplicates("종목코드", keep="last")
        analysis_rows = {
            row["종목코드"]: row for _, row in latest_analysis.iterrows()
        }
    now = _now().isoformat(timespec="seconds")
    result = {"opened": 0, "closed": 0}
    if not prices:
        return result

    with _connect() as connection:
        for strategy, capacity in STRATEGIES.items():
            # 같은 수집 주기에서 청산한 종목을 즉시 다시 사면 거래비용만 반복되고
            # 한 번의 신호를 여러 거래로 부풀리게 되므로 다음 스냅샷까지 재진입하지 않는다.
            closed_codes: set[str] = set()
            candidate_df = candidates[strategy]
            rows_by_code = {
                row["종목코드"]: row for _, row in candidate_df.iterrows()
            }
            positions = connection.execute(
                "SELECT * FROM auto_strategy_positions WHERE strategy = ?",
                (strategy,),
            ).fetchall()

            for position in positions:
                quote = prices.get(position["stock_code"])
                if not quote:
                    continue
                exit_price = None
                exit_reason = ""
                # 신호 발생 전의 당일 고가·저가를 체결로 소급하지 않기 위해
                # 수집 시점의 최신 가격으로만 익절·손절을 판정한다.
                if quote["close"] <= position["stop_price"]:
                    exit_price, exit_reason = quote["close"] * (1 - SELL_SLIPPAGE_RATE), "손절선 이탈"
                elif quote["close"] >= position["target_price"]:
                    exit_price, exit_reason = quote["close"] * (1 - SELL_SLIPPAGE_RATE), "목표가 도달"
                else:
                    analysis_reason = _analysis_sell_reason(
                        analysis_rows.get(position["stock_code"])
                    )
                    if analysis_reason:
                        exit_price = quote["close"] * (1 - SELL_SLIPPAGE_RATE)
                        exit_reason = f"분석 매도 신호 · {analysis_reason}"
                if exit_price is None:
                    continue
                _close_position(connection, position, exit_price, exit_reason, now)
                closed_codes.add(position["stock_code"])
                result["closed"] += 1

            account = connection.execute(
                "SELECT * FROM auto_strategy_accounts WHERE strategy=?", (strategy,)
            ).fetchone()
            held = {
                row[0] for row in connection.execute(
                    "SELECT stock_code FROM auto_strategy_positions WHERE strategy=?", (strategy,)
                ).fetchall()
            }
            exposure = float((market_regime or {}).get("max_exposure", 0.0))
            allowed_positions = int(capacity * exposure)
            if exposure > 0 and allowed_positions == 0:
                allowed_positions = 1
            slots = max(0, allowed_positions - len(held))
            for rank, code in enumerate(candidate_df["종목코드"].tolist(), start=1):
                if slots <= 0 or code in held or code in closed_codes or code not in prices:
                    continue
                row = rows_by_code[code]
                price = prices[code]["close"]
                allocation = min(float(account["cash"]), INITIAL_CASH / capacity)
                target = _number(row.get("목표저항선"))
                stop = _number(row.get("손절기준"))
                # 분석된 지지선·저항선이 유효하지 않으면 고정 비율로 꾸며서
                # 진입하지 않고 다음 분석 신호를 기다린다.
                risk = price - stop
                reward = target - price
                if not (price * 0.90 <= stop < price and price < target <= price * 1.30):
                    continue
                if risk / price < 0.01 or reward / risk < 1.5:
                    continue
                quantity_by_cash = int(allocation / (price * (1 + BUY_FEE_RATE)))
                quantity_by_risk = int((INITIAL_CASH * RISK_PER_TRADE) / risk)
                quantity = min(quantity_by_cash, quantity_by_risk)
                if quantity < 1:
                    continue
                entry_fee = price * quantity * BUY_FEE_RATE
                cost = price * quantity + entry_fee
                connection.execute(
                    "INSERT INTO auto_strategy_positions (strategy, stock_code, stock_name, quantity, "
                    "entry_price, entry_fee, target_price, stop_price, entry_score, entry_rank, "
                    "opened_at, entry_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (strategy, code, str(row.get("종목명", code)), quantity, price, entry_fee,
                     target, stop, _number(row.get("최종점수")), rank, now,
                     str(row.get("AI추천사유", "") or row.get("진입판단사유", ""))),
                )
                connection.execute(
                    "UPDATE auto_strategy_accounts SET cash=cash-?, updated_at=? WHERE strategy=?",
                    (cost, now, strategy),
                )
                account = dict(account)
                account["cash"] = float(account["cash"]) - cost
                held.add(code)
                slots -= 1
                result["opened"] += 1
    return result


def reset_auto_strategies() -> None:
    """시험 거래를 지우고 모든 자동 가상계좌를 초기 자금으로 되돌린다."""
    initialize_auto_strategies()
    now = _now().isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("DELETE FROM auto_strategy_positions")
        connection.execute("DELETE FROM auto_strategy_trades")
        connection.execute(
            "UPDATE auto_strategy_accounts SET cash=initial_cash, updated_at=?", (now,)
        )


def get_strategy_performance(
    period: str = "일간",
    db_path: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if db_path is None:
        initialize_auto_strategies()
    now = datetime.now()
    starts = {
        "일간": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "주간": (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0),
        "월간": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        "연간": now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
    }
    start = starts.get(period, starts["일간"]).isoformat(timespec="seconds")
    connection_context = _connect() if db_path is None else sqlite3.connect(db_path)
    with connection_context as connection:
        accounts = pd.read_sql_query("SELECT * FROM auto_strategy_accounts", connection)
        positions = pd.read_sql_query("SELECT * FROM auto_strategy_positions", connection)
        trades = pd.read_sql_query(
            "SELECT * FROM auto_strategy_trades WHERE closed_at >= ? ORDER BY closed_at DESC",
            connection, params=(start,),
        )
    summaries = []
    for strategy in STRATEGIES:
        subset = trades[trades["strategy"] == strategy] if not trades.empty else trades
        completed = len(subset)
        wins = int((subset["realized_profit"] > 0).sum()) if completed else 0
        profit = float(subset["realized_profit"].sum()) if completed else 0.0
        summaries.append({
            "전략": strategy,
            "청산거래": completed,
            "수익거래": wins,
            "손실거래": completed - wins,
            "성공률(%)": wins / completed * 100 if completed else None,
            "실현손익": profit,
            "기간수익률(%)": profit / INITIAL_CASH * 100,
            "보유종목": int((positions["strategy"] == strategy).sum()) if not positions.empty else 0,
        })
    return pd.DataFrame(summaries), positions, trades


def get_top3_signal_status(db_path: str | Path | None = None) -> pd.DataFrame:
    """현재 TOP3의 자동매수 여부와 대기 이유를 사용자용 표로 반환한다."""
    if db_path is None:
        initialize_auto_strategies()
    connection_context = _connect() if db_path is None else sqlite3.connect(db_path)
    connection_context.row_factory = sqlite3.Row
    with connection_context as connection:
        snapshot_time = connection.execute(
            'SELECT MAX("스냅샷일시") FROM intraday_snapshot'
        ).fetchone()[0]
        if not snapshot_time:
            return pd.DataFrame()
        parsed_snapshot = pd.to_datetime(snapshot_time, errors="coerce")
        if pd.isna(parsed_snapshot) or parsed_snapshot.date() != datetime.now().date():
            return pd.DataFrame()
        top3 = connection.execute(
            'SELECT "현재순위", "종목코드", "종목명", "최종점수" '
            'FROM intraday_snapshot WHERE "스냅샷일시"=? '
            'ORDER BY "현재순위" LIMIT 3', (snapshot_time,),
        ).fetchall()
        held = {
            row[0]: dict(row) for row in connection.execute(
                "SELECT * FROM auto_strategy_positions WHERE strategy='TOP3'"
            ).fetchall()
        }
        output = []
        for item in top3:
            code = item["종목코드"]
            detail = connection.execute(
                'SELECT * FROM score WHERE "종목코드"=? '
                'ORDER BY "최종갱신일자" DESC, "최종갱신시간" DESC LIMIT 1',
                (code,),
            ).fetchone()
            detail = dict(detail) if detail else {}
            price_row = connection.execute(
                'SELECT "종가" FROM chart_history WHERE "종목코드"=? '
                'ORDER BY "날짜" DESC LIMIT 1', (code,),
            ).fetchone()
            current_price = _number(price_row[0]) if price_row else 0
            recommendation = str(detail.get("최종추천") or "분석 갱신 대기")
            entry = str(detail.get("진입판단") or "분석 갱신 대기")
            breakout = _number(detail.get("돌파신뢰도"))
            chase = _number(detail.get("추격위험도"), 100)
            reasons = []
            if recommendation not in {"강력관심", "관심"}:
                reasons.append(f"추천등급 {recommendation}")
            if entry not in {"돌파 확인", "지지선 근처"}:
                reasons.append(f"진입판단 {entry}")
            if breakout < 60:
                reasons.append(f"돌파신뢰도 {breakout:.0f}점")
            if chase >= 45:
                reasons.append(f"추격위험 {chase:.0f}점")
            position = held.get(code)
            if position:
                target = position["target_price"]
                stop = position["stop_price"]
                analysis_reason = _analysis_sell_reason(detail)
                if current_price <= stop:
                    status, reason = "매도 신호", "손절가 도달"
                elif current_price >= target:
                    status, reason = "매도 신호", "목표가 도달"
                elif analysis_reason:
                    status, reason = "매도 신호", f"분석 매도 신호 · {analysis_reason}"
                else:
                    status = "보유 · 매도 신호 감시"
                    reason = "분석 매도 신호와 목표가·손절가를 실시간 확인"
            else:
                status = "매수 대기" if reasons else "매수 신호"
                target = _number(detail.get("목표저항선")) or current_price * 1.05
                if target <= current_price or target > current_price * 1.30:
                    target = current_price * 1.05
                stop = _number(detail.get("손절기준"))
                if stop < current_price * 0.90 or stop >= current_price:
                    stop = current_price * 0.97
                reason = " · ".join(reasons) if reasons else "모든 자동매수 조건 통과"
            output.append({
                "순위": int(item["현재순위"]), "종목명": item["종목명"],
                "현재점수": _number(item["최종점수"]), "상태": status,
                "현재가": current_price, "목표가": target, "손절가": stop,
                "판단 이유": reason, "스냅샷": snapshot_time,
            })
    return pd.DataFrame(output)
