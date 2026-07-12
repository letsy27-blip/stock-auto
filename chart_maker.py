import sqlite3
import pandas as pd

DB_NAME = "stock_data.db"


def get_portfolio_value_chart_data():
    conn = sqlite3.connect(DB_NAME)

    try:
        df = pd.read_sql("SELECT * FROM portfolio_history", conn)
    except Exception:
        print("portfolio_history 테이블 없음")
        return pd.DataFrame(columns=["저장일자", "평가금액"])
    finally:
        conn.close()

    if df.empty:
        print("차트 생성할 데이터 없음")
        return pd.DataFrame(columns=["저장일자", "평가금액"])

    df["저장일자"] = pd.to_datetime(df["저장일자"], errors="coerce")
    df["평가금액"] = pd.to_numeric(df["평가금액"], errors="coerce").fillna(0)
    df = df.dropna(subset=["저장일자"])

    daily_df = (
        df.groupby("저장일자", as_index=False)["평가금액"]
        .sum()
        .sort_values("저장일자")
    )

    daily_df["저장일자"] = daily_df["저장일자"].dt.strftime("%Y-%m-%d")
    return daily_df