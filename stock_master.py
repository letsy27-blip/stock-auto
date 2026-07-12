import sqlite3
import pandas as pd
import FinanceDataReader as fdr

DB_NAME = "stock_data.db"


def save_stock_master():
    rows = []

    markets = [
        ("KOSPI", "KOSPI"),
        ("KOSDAQ", "KOSDAQ"),
        ("KONEX", "KONEX"),
    ]

    for market_code, market_name in markets:
        try:
            df = fdr.StockListing(market_code)

            if df is None or df.empty:
                print(f"{market_name} 데이터 없음")
                continue

            for _, row in df.iterrows():
                code = str(row.get("Code", "")).zfill(6)
                name = row.get("Name", "")

                if not code or not name:
                    continue

                rows.append({
                    "종목코드": code,
                    "종목명": name,
                    "시장구분": market_name
                })

            print(f"{market_name} 저장 완료: {len(df)}개")

        except Exception as e:
            print(f"{market_name} 조회 실패: {e}")

    master_df = pd.DataFrame(rows)

    if master_df.empty:
        print("저장할 종목 데이터가 없습니다.")
        return

    master_df = master_df.drop_duplicates(subset=["종목코드"])

    conn = sqlite3.connect(DB_NAME)
    master_df.to_sql("stock_master", conn, if_exists="replace", index=False)
    conn.close()

    print(f"stock_master 저장 완료: {len(master_df)}개 종목")


if __name__ == "__main__":
    save_stock_master()