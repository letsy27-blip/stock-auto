import argparse
import os
import time
from datetime import datetime, timedelta

import pandas as pd

from auto_strategy import DB_PATH as STRATEGY_DB_PATH, initialize_auto_strategies, update_auto_strategies
from central_store import (
    publish_latest_scores,
    publish_strategy_state,
    restore_strategy_state,
)
from database import save_all_data
from excel_writer import save_to_excel
from financial_analyzer import collect_financial_metrics
from kis_api import (
    collect_investor_trends,
    get_access_token,
    get_daily_price_history,
)
from news_analyzer import (
    analyze_news_by_stock_with_gemini,
    collect_news_for_candidates,
)
from market_data import get_market_overview
from market_regime import classify_market_regime
from portfolio import analyze_my_stocks
from prediction_tracker import update_prediction_tracking
from ranking import (
    get_rise_rank,
    get_trade_value_rank,
    get_volume_rank,
)
from score_analyzer import make_score_sheet
from stock_classifier import (
    merge_classification,
    update_stock_classifications,
)


def make_signal_sheet(
    portfolio_df,
    volume_rank_df,
    rise_rank_df,
    trade_value_df,
):
    signals = []

    if portfolio_df is None or portfolio_df.empty:
        return pd.DataFrame(
            [
                {
                    "조회시간": datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "종목명": "",
                    "종목코드": "",
                    "신호": "보유종목 없음",
                    "순위": "",
                    "뉴스요약": "",
                }
            ]
        )

    my_codes = (
        portfolio_df["종목코드"]
        .astype(str)
        .str.replace(".0", "", regex=False)
        .str.zfill(6)
        .tolist()
    )

    checks = [
        (volume_rank_df, "보유종목 거래량 TOP30 진입"),
        (rise_rank_df, "보유종목 상승률 TOP30 진입"),
        (trade_value_df, "보유종목 거래대금 TOP30 진입"),
    ]

    for df, signal_name in checks:
        if df is not None and not df.empty and "종목코드" in df.columns:
            temp_df = df.copy()
            temp_df["종목코드"] = (
                temp_df["종목코드"]
                .astype(str)
                .str.replace(".0", "", regex=False)
                .str.zfill(6)
            )

            matched = temp_df[temp_df["종목코드"].isin(my_codes)]

            for _, row in matched.iterrows():
                signals.append(
                    {
                        "조회시간": datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "종목명": row.get("종목명", ""),
                        "종목코드": row.get("종목코드", ""),
                        "신호": signal_name,
                        "순위": row.get("순위", ""),
                        "뉴스요약": row.get("뉴스요약", ""),
                    }
                )

    if not signals:
        signals.append(
            {
                "조회시간": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "종목명": "",
                "종목코드": "",
                "신호": "보유종목 중 TOP30 진입 종목 없음",
                "순위": "",
                "뉴스요약": "",
            }
        )

    return pd.DataFrame(signals)


def make_candidate_list(
    volume_rank_df,
    rise_rank_df,
    trade_value_df,
):
    candidates = []

    for df in [volume_rank_df, rise_rank_df, trade_value_df]:
        if df is not None and not df.empty and "종목코드" in df.columns:
            for _, row in df.iterrows():
                candidates.append(
                    {
                        "종목코드": (
                            str(row.get("종목코드", ""))
                            .replace(".0", "")
                            .zfill(6)
                        ),
                        "종목명": row.get("종목명", ""),
                    }
                )

    candidate_df = pd.DataFrame(candidates)
    if candidate_df.empty:
        return candidate_df

    return candidate_df.drop_duplicates(
        subset=["종목코드"],
        keep="last",
    )


def make_gemini_priority_codes(volume_rank_df, rise_rank_df, trade_value_df, limit=8):
    """Gemini 정밀 분석을 받을 종목을 세 순위표의 합산 순위로 고른다."""
    priorities: dict[str, float] = {}
    for df in [volume_rank_df, rise_rank_df, trade_value_df]:
        if df is None or df.empty or "종목코드" not in df.columns:
            continue
        for _, row in df.iterrows():
            code = str(row.get("종목코드", "")).replace(".0", "").zfill(6)
            try:
                rank = float(row.get("순위", 31))
            except (TypeError, ValueError):
                rank = 31
            if code and code != "000000":
                priorities[code] = priorities.get(code, 0) + max(0, 31 - rank)

    return [
        code for code, _ in sorted(
            priorities.items(), key=lambda item: item[1], reverse=True
        )[:limit]
    ]


def make_chart_history_data(token, candidate_df, days=60):
    all_chart_data = []

    if candidate_df is None or candidate_df.empty:
        return pd.DataFrame()

    for _, row in candidate_df.iterrows():
        code = str(row["종목코드"]).zfill(6)
        name = row["종목명"]

        print(f"일봉 데이터 조회 중: {name}({code})")
        df = get_daily_price_history(
            token=token,
            stock_code=code,
            stock_name=name,
            days=days,
        )

        if df is not None and not df.empty:
            all_chart_data.append(df)

        time.sleep(0.3)

    if not all_chart_data:
        return pd.DataFrame()

    return pd.concat(all_chart_data, ignore_index=True)


def run_once():
    initialize_auto_strategies()
    try:
        restored = restore_strategy_state(STRATEGY_DB_PATH)
        print("중앙 자동매매 상태 복원 완료" if restored else "중앙 자동매매 최초 실행 · 로컬 초기 상태 사용")
    except Exception as exc:
        print(f"중앙 자동매매 상태 복원 실패 · GitHub DB 상태 사용: {exc}")

    token = get_access_token()
    if not token:
        # 호출 측(GitHub Actions 포함)이 수집 실패를 정상 완료로 오해하지 않도록
        # 실패를 명시적으로 전파한다.
        raise RuntimeError("접근토큰 발급에 실패했습니다. KIS API 키와 비밀키를 확인하세요.")

    print("토큰 발급 성공")

    portfolio_df = analyze_my_stocks(token)
    volume_rank_df = get_volume_rank(token)
    rise_rank_df = get_rise_rank(token)
    trade_value_df = get_trade_value_rank(token)

    signal_df = make_signal_sheet(
        portfolio_df,
        volume_rank_df,
        rise_rank_df,
        trade_value_df,
    )

    candidate_df = make_candidate_list(
        volume_rank_df,
        rise_rank_df,
        trade_value_df,
    )

    chart_history_df = make_chart_history_data(
        token=token,
        candidate_df=candidate_df,
        days=60,
    )

    supply_demand_df = collect_investor_trends(
        token=token,
        candidate_df=candidate_df,
        days=10,
    )

    raw_news_df = collect_news_for_candidates(
        candidate_df=candidate_df,
        hours=36,
        max_items_per_stock=8,
    )

    gemini_priority_codes = make_gemini_priority_codes(
        volume_rank_df,
        rise_rank_df,
        trade_value_df,
    )
    print(f"Gemini 정밀 뉴스 분석 우선 종목: {', '.join(gemini_priority_codes)}")

    news_history_df, news_summary_df = (
        analyze_news_by_stock_with_gemini(
            raw_news_df,
            priority_stock_codes=gemini_priority_codes,
        )
    )

    financial_df = collect_financial_metrics(candidate_df)
    if not financial_df.empty:
        evaluated = int((financial_df.get("재무등급") != "평가 제외").sum())
        print(f"재무제표 반영: {evaluated}개 / 후보 {len(financial_df)}개")

    scored_df = make_score_sheet(
        volume_rank_df=volume_rank_df,
        rise_rank_df=rise_rank_df,
        trade_value_df=trade_value_df,
        chart_history_df=chart_history_df,
        supply_demand_df=supply_demand_df,
        news_summary_df=news_summary_df,
        financial_df=financial_df,
    )

    classification_df = update_stock_classifications(
        candidate_df=candidate_df,
        news_summary_df=news_summary_df,
    )
    scored_df = merge_classification(
        scored_df=scored_df,
        classification_df=classification_df,
    )

    print("\n[추천점수 TOP10]")
    if not scored_df.empty:
        display_columns = [
            column
            for column in [
                "점수순위",
                "종목명",
                "시장점수",
                "수급점수",
                "뉴스점수",
                "최종점수",
                "최종추천",
            ]
            if column in scored_df.columns
        ]
        print(scored_df[display_columns].head(10))
    else:
        print(scored_df)

    save_all_data(
        portfolio_df=portfolio_df,
        volume_rank_df=volume_rank_df,
        rise_rank_df=rise_rank_df,
        trade_value_df=trade_value_df,
        signal_df=signal_df,
        scored_df=scored_df,
        chart_history_df=chart_history_df,
        supply_demand_df=supply_demand_df,
        news_history_df=news_history_df,
        financial_df=financial_df,
    )

    market_regime = classify_market_regime(get_market_overview(token))
    print(
        f"시장 국면: {market_regime['regime']} · "
        f"신규투자 허용 {market_regime['max_exposure'] * 100:.0f}% · "
        f"{market_regime['reason']}"
    )
    strategy_result = update_auto_strategies(
        scored_df=scored_df,
        chart_df=chart_history_df,
        market_regime=market_regime,
    )
    prediction_saved, prediction_evaluated = update_prediction_tracking(
        scored_df=scored_df,
        chart_history_df=chart_history_df,
    )
    try:
        strategy_time = publish_strategy_state(STRATEGY_DB_PATH)
        print(f"중앙 자동매매 상태 저장 완료: {strategy_time}")
    except Exception as exc:
        print(f"중앙 자동매매 상태 저장 실패 · GitHub DB 커밋으로 보존: {exc}")
    try:
        central_time = publish_latest_scores(
            scored_df,
            source="github-actions" if os.getenv("GITHUB_ACTIONS") else "local-collector",
        )
        print(f"중앙 DB 갱신 완료: {central_time}")
    except Exception as exc:
        # 수집 데이터는 로컬에도 먼저 저장되어 있으므로 중앙 장애가 수집 자체를 망치지 않게 한다.
        print(f"중앙 DB 갱신 건너뜀: {exc}")
    print(
        "자동전략 갱신: "
        f"가상 매수 {strategy_result['opened']}건, 가상 청산 {strategy_result['closed']}건"
    )
    print(
        "예측 검증 갱신: "
        f"새 추천 표본 {prediction_saved}건, 결과 평가 {prediction_evaluated}건"
    )

    save_to_excel(
        portfolio_df=portfolio_df,
        volume_rank_df=volume_rank_df,
        rise_rank_df=rise_rank_df,
        trade_value_df=trade_value_df,
        signal_df=signal_df,
        scored_df=scored_df,
        chart_history_df=chart_history_df,
    )


RUN_MINUTES = {0, 30}
MARKET_START_HOUR = 9
MARKET_END_HOUR = 15
MARKET_END_MINUTE = 30


def is_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def is_market_time(now: datetime) -> bool:
    if not is_weekday(now):
        return False

    start_ok = (
        now.hour > MARKET_START_HOUR
        or (now.hour == MARKET_START_HOUR and now.minute >= 0)
    )
    end_ok = (
        now.hour < MARKET_END_HOUR
        or (
            now.hour == MARKET_END_HOUR
            and now.minute <= MARKET_END_MINUTE
        )
    )
    return start_ok and end_ok


def is_scheduled_slot(now: datetime) -> bool:
    return is_market_time(now) and now.minute in RUN_MINUTES


def next_run_time(now: datetime) -> datetime | None:
    candidate = now.replace(second=0, microsecond=0)

    # 최대 8일까지만 찾는다.
    for _ in range(8 * 24 * 60):
        candidate += timedelta(minutes=1)

        if is_scheduled_slot(candidate):
            return candidate

    return None


def run_once_safely() -> bool:
    started = datetime.now()

    print("")
    print("=" * 70)
    print(f"데이터 수집 시작: {started:%Y-%m-%d %H:%M:%S}")
    print("=" * 70)

    try:
        run_once()
        print(f"데이터 수집 완료: {datetime.now():%Y-%m-%d %H:%M:%S}")
        return True
    except Exception as exc:
        print(f"데이터 수집 실패: {exc}")
        return False


def run_intraday_scheduler() -> None:
    """
    평일 정규장 중 00분·30분마다 실행하고 장 종료 후에도
    다음 거래일까지 대기한다.
    """
    print("30분 장중 분석기를 시작합니다.")
    print("평일 09:00~15:30 동안 30분마다 실행합니다.")
    print("중지하려면 Ctrl+C를 누르세요.")

    now = datetime.now()

    # 장중에 실행기를 열면 현재 자료를 즉시 한 번 갱신한다.
    if is_market_time(now):
        run_once_safely()

    # 정확히 정각/30분에 실행했다면 같은 슬롯에서 중복 실행 방지
    last_slot = (
        now.strftime("%Y-%m-%d %H:%M")
        if is_scheduled_slot(now)
        else None
    )

    while True:
        now = datetime.now()
        slot = now.strftime("%Y-%m-%d %H:%M")

        if is_scheduled_slot(now) and slot != last_slot:
            run_once_safely()
            last_slot = slot

        next_time = next_run_time(now)
        if next_time is None:
            print("다음 실행 시각을 계산하지 못해 종료합니다.")
            return

        wait_seconds = max(
            5,
            min(30, int((next_time - now).total_seconds())),
        )

        print(
            f"\r다음 실행 예정: {next_time:%Y-%m-%d %H:%M} "
            f"(현재 {now:%H:%M:%S})",
            end="",
            flush=True,
        )
        time.sleep(wait_seconds)


def parse_args():
    parser = argparse.ArgumentParser(
        description="주식 데이터 수집 및 30분 장중 분석"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="한 번만 실행하고 종료",
    )
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="즉시 한 번 실행 후 장중 30분마다 자동 실행",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.once:
        if not run_once_safely():
            raise SystemExit(1)
    else:
        # 기본값도 장중 자동 모드로 동작
        run_intraday_scheduler()
