import time
import pandas as pd
from datetime import datetime

from kis_api import get_access_token, get_daily_price_history
from portfolio import analyze_my_stocks
from ranking import get_volume_rank, get_rise_rank, get_trade_value_rank
from excel_writer import save_to_excel
from database import save_all_data
from score_analyzer import make_score_sheet


def make_signal_sheet(portfolio_df, volume_rank_df, rise_rank_df, trade_value_df):
    signals = []

    if portfolio_df is None or portfolio_df.empty:
        return pd.DataFrame([{
            "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "종목명": "",
            "종목코드": "",
            "신호": "보유종목 없음",
            "순위": "",
            "뉴스요약": ""
        }])

    my_codes = portfolio_df["종목코드"].astype(str).str.zfill(6).tolist()

    checks = [
        (volume_rank_df, "보유종목 거래량 TOP30 진입"),
        (rise_rank_df, "보유종목 상승률 TOP30 진입"),
        (trade_value_df, "보유종목 거래대금 TOP30 진입"),
    ]

    for df, signal_name in checks:
        if df is not None and not df.empty and "종목코드" in df.columns:
            temp_df = df.copy()
            temp_df["종목코드"] = temp_df["종목코드"].astype(str).str.zfill(6)

            matched = temp_df[temp_df["종목코드"].isin(my_codes)]

            for _, row in matched.iterrows():
                signals.append({
                    "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "종목명": row.get("종목명", ""),
                    "종목코드": row.get("종목코드", ""),
                    "신호": signal_name,
                    "순위": row.get("순위", ""),
                    "뉴스요약": row.get("뉴스요약", "")
                })

    if not signals:
        signals.append({
            "조회시간": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "종목명": "",
            "종목코드": "",
            "신호": "보유종목 중 TOP30 진입 종목 없음",
            "순위": "",
            "뉴스요약": ""
        })

    return pd.DataFrame(signals)


def make_candidate_list(volume_rank_df, rise_rank_df, trade_value_df):
    candidates = []

    for df in [volume_rank_df, rise_rank_df, trade_value_df]:
        if df is not None and not df.empty and "종목코드" in df.columns:
            for _, row in df.iterrows():
                candidates.append({
                    "종목코드": str(row.get("종목코드", "")).zfill(6),
                    "종목명": row.get("종목명", "")
                })

    candidate_df = pd.DataFrame(candidates)

    if candidate_df.empty:
        return candidate_df

    candidate_df = candidate_df.drop_duplicates(subset=["종목코드"])
    return candidate_df


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
            days=days
        )

        if df is not None and not df.empty:
            all_chart_data.append(df)

        time.sleep(0.3)

    if not all_chart_data:
        return pd.DataFrame()

    return pd.concat(all_chart_data, ignore_index=True)


def main():
    token = get_access_token()

    if not token:
        print("토큰 발급 실패")
        return

    print("토큰 발급 성공")

    portfolio_df = analyze_my_stocks(token)
    volume_rank_df = get_volume_rank(token)
    rise_rank_df = get_rise_rank(token)
    trade_value_df = get_trade_value_rank(token)

    signal_df = make_signal_sheet(
        portfolio_df,
        volume_rank_df,
        rise_rank_df,
        trade_value_df
    )

    candidate_df = make_candidate_list(
        volume_rank_df,
        rise_rank_df,
        trade_value_df
    )

    chart_history_df = make_chart_history_data(
        token=token,
        candidate_df=candidate_df,
        days=60
    )

    scored_df = make_score_sheet(
        volume_rank_df,
        rise_rank_df,
        trade_value_df,
        chart_history_df
    )

    print("\n[추천점수 TOP10]")
    print(scored_df.head(10))

    save_all_data(
        portfolio_df,
        volume_rank_df,
        rise_rank_df,
        trade_value_df,
        signal_df,
        scored_df,
        chart_history_df
    )
    save_to_excel(
        portfolio_df=portfolio_df,
        volume_rank_df=volume_rank_df,
        rise_rank_df=rise_rank_df,
        trade_value_df=trade_value_df,
        signal_df=signal_df,
        scored_df=scored_df,
        chart_history_df=chart_history_df
    )


if __name__ == "__main__":
    main()