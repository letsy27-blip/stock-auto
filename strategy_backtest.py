"""Walk-forward portfolio backtest using only information known at each signal date."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


INITIAL_CASH = 100_000_000.0
BUY_FEE = 0.00015
SELL_FEE = 0.00015
SELL_TAX = 0.0018
RISK_PER_TRADE = 0.01


@dataclass
class Position:
    code: str
    name: str
    quantity: int
    entry: float
    entry_fee: float
    opened_at: pd.Timestamp
    target: float
    stop: float


def _load_prices(db_path: str | Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as connection:
        frame = pd.read_sql_query(
            'SELECT "종목코드", "종목명", "날짜", "시가", "고가", "저가", "종가", "거래량" '
            'FROM chart_history', connection,
        )
    if frame.empty:
        return frame
    frame["날짜"] = pd.to_datetime(frame["날짜"], errors="coerce")
    for column in ["시가", "고가", "저가", "종가", "거래량"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return (
        frame.dropna(subset=["날짜", "종목코드", "종가"])
        .sort_values(["종목코드", "날짜"])
        .drop_duplicates(["종목코드", "날짜"], keep="last")
    )


def _make_signals(prices: pd.DataFrame) -> pd.DataFrame:
    grouped = prices.groupby("종목코드", group_keys=False)
    prices = prices.copy()
    prices["수익률5"] = grouped["종가"].pct_change(5)
    prices["수익률20"] = grouped["종가"].pct_change(20)
    prices["평균거래량20"] = grouped["거래량"].transform(
        lambda value: value.shift(1).rolling(20).mean()
    )
    prices["거래량배수"] = prices["거래량"] / prices["평균거래량20"]
    prices["MA5"] = grouped["종가"].transform(lambda value: value.rolling(5).mean())
    prices["MA20"] = grouped["종가"].transform(lambda value: value.rolling(20).mean())
    previous_close = grouped["종가"].shift(1)
    true_range = pd.concat(
        [
            prices["고가"] - prices["저가"],
            (prices["고가"] - previous_close).abs(),
            (prices["저가"] - previous_close).abs(),
        ], axis=1,
    ).max(axis=1)
    prices["ATR14"] = true_range.groupby(prices["종목코드"]).transform(
        lambda value: value.rolling(14).mean()
    )
    prices["지지선20"] = grouped["저가"].transform(
        lambda value: value.shift(1).rolling(20).min()
    )
    prices["저항선20"] = grouped["고가"].transform(
        lambda value: value.shift(1).rolling(20).max()
    )
    delta = grouped["종가"].diff()
    gain = delta.clip(lower=0).groupby(prices["종목코드"]).transform(lambda value: value.rolling(14).mean())
    loss = (-delta.clip(upper=0)).groupby(prices["종목코드"]).transform(lambda value: value.rolling(14).mean())
    prices["RSI14"] = 100 - 100 / (1 + gain / loss.replace(0, pd.NA))
    # Cross-sectional percentile ranks avoid using tomorrow's prices and keep unlike units comparable.
    for source, target in [
        ("수익률5", "점수5"), ("수익률20", "점수20"), ("거래량배수", "점수거래량")
    ]:
        prices[target] = prices.groupby("날짜")[source].rank(pct=True) * 100
    prices["추세점수"] = ((prices["종가"] > prices["MA5"]) & (prices["MA5"] > prices["MA20"])).astype(float) * 100
    prices["백테스트점수"] = (
        prices["점수5"] * 0.30
        + prices["점수20"] * 0.35
        + prices["점수거래량"] * 0.20
        + prices["추세점수"] * 0.15
    )
    prices["매수신호"] = (
        (prices["종가"] > prices["MA5"])
        & (prices["MA5"] > prices["MA20"])
        & (prices["수익률5"] > 0)
        & (prices["수익률20"] > 0)
        & prices["거래량배수"].between(1.2, 3.0)
        & prices["RSI14"].between(50, 68)
        & (prices["백테스트점수"] >= 70)
        & ~prices["종목명"].astype(str).str.contains(
            r"(^KODEX\s|^TIGER\s|^SOL\s|^ACE\s|^RISE\s|^PLUS\s|ETF|ETN|인버스|레버리지|선물)",
            case=False, regex=True, na=False,
        )
    )
    prices["매도신호"] = (
        (prices["종가"] < prices["MA5"])
        & (prices["MA5"] < prices["MA20"])
    )
    return prices.dropna(subset=["백테스트점수", "시가", "고가", "저가"])


def _run_strategy(signals: pd.DataFrame, capacity: int, trading_dates: list[pd.Timestamp]) -> dict:
    cash = INITIAL_CASH
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    selection_by_date = {
        day: group[group["매수신호"]].nlargest(capacity, "백테스트점수")
        for day, group in signals[signals["날짜"].isin(trading_dates)].groupby("날짜")
    }
    row_by_date = {
        day: group.set_index("종목코드")
        for day, group in signals[signals["날짜"].isin(trading_dates)].groupby("날짜")
    }

    for index in range(1, len(trading_dates)):
        signal_day, trade_day = trading_dates[index - 1], trading_dates[index]
        selected = selection_by_date.get(signal_day, pd.DataFrame())
        signal_rows = row_by_date.get(signal_day, pd.DataFrame())
        today = row_by_date.get(trade_day, pd.DataFrame())
        closed_today: set[str] = set()

        for code, position in list(positions.items()):
            if code not in today.index:
                continue
            quote = today.loc[code]
            if isinstance(quote, pd.DataFrame):
                quote = quote.iloc[-1]
            target, stop = position.target, position.stop
            exit_price = None
            reason = ""
            signal_quote = signal_rows.loc[code] if code in signal_rows.index else None
            if isinstance(signal_quote, pd.DataFrame):
                signal_quote = signal_quote.iloc[-1]
            # 전일 종가까지 확정된 분석 매도 신호는 다음 거래일 시가에 실행한다.
            if signal_quote is not None and bool(signal_quote["매도신호"]):
                exit_price, reason = float(quote["시가"]), "분석 매도 신호"
            # If both levels occur in one daily candle, use the stop first (conservative ordering).
            elif float(quote["저가"]) <= stop:
                exit_price, reason = stop, "손절가"
            elif float(quote["고가"]) >= target:
                exit_price, reason = target, "목표가"
            if exit_price is None:
                continue
            gross = exit_price * position.quantity
            exit_fee, tax = gross * SELL_FEE, gross * SELL_TAX
            cost = position.entry * position.quantity + position.entry_fee
            profit = gross - exit_fee - tax - cost
            cash += gross - exit_fee - tax
            trades.append({"종목코드": code, "종목명": position.name, "매수일": position.opened_at,
                           "매도일": trade_day, "매수가": position.entry, "매도가": exit_price,
                           "수익률(%)": profit / cost * 100, "실현손익": profit, "청산사유": reason})
            del positions[code]
            closed_today.add(code)

        slots = capacity - len(positions)
        if slots <= 0 or selected.empty or today.empty:
            continue
        allocation = cash / slots
        for _, candidate in selected.iterrows():
            code = str(candidate["종목코드"])
            if slots <= 0 or code in positions or code in closed_today or code not in today.index:
                continue
            quote = today.loc[code]
            if isinstance(quote, pd.DataFrame):
                quote = quote.iloc[-1]
            entry = float(quote["시가"])
            if entry <= 0:
                continue
            support = float(candidate["지지선20"])
            resistance = float(candidate["저항선20"])
            atr = float(candidate["ATR14"])
            # 지지선 아래에 변동성 여유를 둔 손절가와 직전 저항선을 사용한다.
            stop = max(support * 0.98, entry - atr * 1.5)
            risk = entry - stop
            target = resistance if resistance > entry and (resistance - entry) / risk >= 1.5 else entry + risk * 2
            if not (entry * 0.90 <= stop < entry and entry < target <= entry * 1.30):
                continue
            if risk / entry < 0.01 or (target - entry) / risk < 1.5:
                continue
            quantity_by_cash = int(allocation / (entry * (1 + BUY_FEE)))
            quantity_by_risk = int((INITIAL_CASH * RISK_PER_TRADE) / risk)
            quantity = min(quantity_by_cash, quantity_by_risk)
            if quantity < 1:
                continue
            fee = entry * quantity * BUY_FEE
            cash -= entry * quantity + fee
            positions[code] = Position(
                code, str(candidate["종목명"]), quantity, entry, fee, trade_day, target, stop
            )
            slots -= 1

    last_day = trading_dates[-1]
    last_quotes = row_by_date.get(last_day, pd.DataFrame())
    market_value = 0.0
    for code, position in positions.items():
        if code in last_quotes.index:
            quote = last_quotes.loc[code]
            if isinstance(quote, pd.DataFrame):
                quote = quote.iloc[-1]
            market_value += float(quote["종가"]) * position.quantity
        else:
            market_value += position.entry * position.quantity
    equity = cash + market_value
    trade_frame = pd.DataFrame(trades)
    wins = int((trade_frame["실현손익"] > 0).sum()) if not trade_frame.empty else 0
    return {
        "청산거래": len(trade_frame), "수익거래": wins,
        "손실거래": len(trade_frame) - wins,
        "성공률(%)": wins / len(trade_frame) * 100 if len(trade_frame) else None,
        "실현손익": float(trade_frame["실현손익"].sum()) if not trade_frame.empty else 0.0,
        "평가자산": equity, "누적수익률(%)": (equity / INITIAL_CASH - 1) * 100,
        "보유종목": len(positions), "trades": trade_frame,
    }


def run_walk_forward_backtest(db_path: str | Path, trading_days: int = 20) -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = _make_signals(_load_prices(db_path))
    dates = sorted(prices["날짜"].dropna().unique())
    if len(dates) < trading_days + 1:
        return pd.DataFrame(), pd.DataFrame()
    dates = [pd.Timestamp(value) for value in dates[-(trading_days + 1):]]
    summaries, all_trades = [], []
    for name, capacity in [("TOP3", 3), ("TOP30", 30)]:
        result = _run_strategy(prices, capacity, dates)
        trades = result.pop("trades")
        result.update({"전략": name, "시작일": dates[0].date(), "종료일": dates[-1].date()})
        summaries.append(result)
        if not trades.empty:
            trades.insert(0, "전략", name)
            all_trades.append(trades)
    return pd.DataFrame(summaries), pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
