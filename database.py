import sqlite3
import pandas as pd
from datetime import datetime

DB_NAME = "stock_data.db"


def add_save_date(df, data_type):
    df = df.copy()
    df["저장일자"] = datetime.now().strftime("%Y-%m-%d")
    df["저장시간"] = datetime.now().strftime("%H:%M:%S")
    df["데이터구분"] = data_type
    return df


def save_dataframe_unique(df, table_name, data_type):
    if df is None or df.empty:
        print(f"저장할 데이터 없음: {table_name}")
        return

    df = add_save_date(df, data_type)

    conn = sqlite3.connect(DB_NAME)

    try:
        try:
            old_df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
        except Exception:
            old_df = pd.DataFrame()

        if not old_df.empty:
            combined_df = pd.concat([old_df, df], ignore_index=True)

            if "종목코드" in combined_df.columns:
                if "날짜" in combined_df.columns:
                    subset_cols = ["저장일자", "종목코드", "날짜", "데이터구분"]
                else:
                    subset_cols = ["저장일자", "종목코드", "데이터구분"]

                combined_df = combined_df.drop_duplicates(
                    subset=subset_cols,
                    keep="last"
                )
            else:
                combined_df = combined_df.drop_duplicates(keep="last")
        else:
            combined_df = df

        combined_df.to_sql(
            table_name,
            conn,
            if_exists="replace",
            index=False
        )

        print(f"DB 누적 저장 완료: {table_name} / 총 {len(combined_df)}행")

    except Exception as e:
        print(f"DB 저장 실패: {table_name}")
        print(e)

    finally:
        conn.close()


def save_all_data(
    portfolio_df,
    volume_rank_df,
    rise_rank_df,
    trade_value_df,
    signal_df,
    scored_df=None,
    chart_history_df=None
):
    save_dataframe_unique(portfolio_df, "portfolio_history", "보유종목")
    save_dataframe_unique(volume_rank_df, "volume_rank_history", "거래량TOP30")
    save_dataframe_unique(rise_rank_df, "rise_rank_history", "상승률TOP30")
    save_dataframe_unique(trade_value_df, "trade_value_rank_history", "거래대금TOP30")
    save_dataframe_unique(signal_df, "signal_history", "관심신호")
    save_dataframe_unique(scored_df, "score_history", "추천점수")
    save_dataframe_unique(chart_history_df, "chart_history", "차트원본")