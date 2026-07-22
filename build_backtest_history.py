"""Build a compact, reproducible price-only dataset for long-horizon backtests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import FinanceDataReader as fdr
import pandas as pd


DEFAULT_START = "2021-01-01"
PRICE_OUTPUT = Path("backtest_price_history.csv.gz")
INDEX_OUTPUT = Path("backtest_market_index.csv.gz")


def _tracked_universe(db_path: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            'SELECT DISTINCT CAST("종목코드" AS TEXT), "종목명" '
            'FROM chart_history WHERE "종목코드" IS NOT NULL ORDER BY "종목코드"'
        ).fetchall()
    return [(str(code).replace(".0", "").zfill(6), str(name or code)) for code, name in rows]


def _download_stock(item: tuple[str, str], start: str, end: str | None) -> pd.DataFrame:
    code, name = item
    frame = fdr.DataReader(code, start, end)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.reset_index().rename(columns={
        "Date": "날짜", "Open": "시가", "High": "고가", "Low": "저가",
        "Close": "종가", "Volume": "거래량",
    })
    frame.insert(0, "종목명", name)
    frame.insert(0, "종목코드", code)
    return frame[["종목코드", "종목명", "날짜", "시가", "고가", "저가", "종가", "거래량"]]


def build(db_path: Path, start: str, end: str | None, workers: int) -> None:
    universe = _tracked_universe(db_path)
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_download_stock, item, start, end): item for item in universe}
        for completed, future in enumerate(as_completed(futures), start=1):
            item = futures[future]
            try:
                frame = future.result()
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:
                print(f"조회 실패 {item[0]} {item[1]}: {exc}")
            if completed % 25 == 0 or completed == len(universe):
                print(f"장기 일봉 {completed}/{len(universe)}개 조회")

    if not frames:
        raise RuntimeError("저장할 장기 일봉이 없습니다.")
    prices = pd.concat(frames, ignore_index=True).sort_values(["날짜", "종목코드"])
    prices.to_csv(PRICE_OUTPUT, index=False, compression="gzip", encoding="utf-8")

    index_frame = fdr.DataReader("KS11", start, end).reset_index().rename(
        columns={"Date": "날짜", "Close": "종가"}
    )[["날짜", "종가"]]
    index_frame.to_csv(INDEX_OUTPUT, index=False, compression="gzip", encoding="utf-8")
    print(
        f"완료: 종목 {prices['종목코드'].nunique()}개, {prices['날짜'].nunique()}거래일, "
        f"{prices['날짜'].min().date()}~{prices['날짜'].max().date()}"
    )

    from strategy_backtest import (
        REGIME_SUMMARY,
        VALIDATION_META,
        VALIDATION_SUMMARY,
        VALIDATION_TRADES,
        run_long_horizon_validation,
    )

    summary, regimes, trades, metadata = run_long_horizon_validation(
        db_path, years=5, holdout_ratio=0.20, prefer_precomputed=False
    )
    summary.to_csv(VALIDATION_SUMMARY, index=False, encoding="utf-8")
    regimes.to_csv(REGIME_SUMMARY, index=False, encoding="utf-8")
    trades.to_csv(VALIDATION_TRADES, index=False, compression="gzip", encoding="utf-8")
    VALIDATION_META.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"검증 결과 저장: 요약 {len(summary)}행, 국면 {len(regimes)}행, 거래 {len(trades)}행")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("stock_data.db"))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    build(args.db, args.start, args.end, args.workers)
