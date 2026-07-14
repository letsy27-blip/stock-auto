from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "stock_data.db"


TABLE_MAP = {
    "portfolio": "portfolio",
    "volume_rank": "volume_rank",
    "rise_rank": "rise_rank",
    "trade_value": "trade_value",
    "signal": "signal",
    "score": "score",
    "chart_history": "chart_history",
    "supply_demand": "supply_demand",
    "news_history": "news_history",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_code(value: Any) -> str:
    text = str(value or "").replace(".0", "").strip()
    return text.zfill(6) if text else ""


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None

    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False)

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value

    return value


def _quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sanitize_table_name(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_가-힣]", "_", str(name))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "data"


def _prepare_dataframe(
    df: pd.DataFrame | None,
    data_type: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    result = df.copy()

    if "종목코드" in result.columns:
        result["종목코드"] = result["종목코드"].map(_clean_code)

    if "코드" in result.columns:
        result["코드"] = result["코드"].map(_clean_code)

    if "저장일자" not in result.columns:
        result["저장일자"] = datetime.now().strftime("%Y-%m-%d")

    if "저장시간" not in result.columns:
        result["저장시간"] = datetime.now().strftime("%H:%M:%S")

    if "데이터구분" not in result.columns:
        result["데이터구분"] = data_type

    for column in result.columns:
        result[column] = result[column].map(_normalize_scalar)

    return result


def _get_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series):
        return "REAL"
    if pd.api.types.is_bool_dtype(series):
        return "INTEGER"
    return "TEXT"


def _get_existing_columns(
    conn: sqlite3.Connection,
    table_name: str,
) -> dict[str, str]:
    rows = conn.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    return {row["name"]: row["type"] for row in rows}


def _ensure_table(
    conn: sqlite3.Connection,
    table_name: str,
    df: pd.DataFrame,
) -> None:
    if df.empty:
        return

    existing = _get_existing_columns(conn, table_name)

    if not existing:
        definitions = []
        for column in df.columns:
            sql_type = _get_sql_type(df[column])
            definitions.append(
                f"{_quote_identifier(column)} {sql_type}"
            )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_quote_identifier(table_name)} (
                {", ".join(definitions)}
            )
            """
        )
        existing = _get_existing_columns(conn, table_name)

    for column in df.columns:
        if column not in existing:
            sql_type = _get_sql_type(df[column])
            conn.execute(
                f"""
                ALTER TABLE {_quote_identifier(table_name)}
                ADD COLUMN {_quote_identifier(column)} {sql_type}
                """
            )

    conn.commit()


def _build_dedup_columns(
    table_name: str,
    df: pd.DataFrame,
) -> list[str]:
    candidates_by_table = {
        "portfolio": [
            ["저장일자", "종목코드"],
            ["저장일자", "종목명"],
        ],
        "volume_rank": [
            ["저장일자", "종목코드", "순위"],
            ["저장일자", "종목코드"],
        ],
        "rise_rank": [
            ["저장일자", "종목코드", "순위"],
            ["저장일자", "종목코드"],
        ],
        "trade_value": [
            ["저장일자", "종목코드", "순위"],
            ["저장일자", "종목코드"],
        ],
        "signal": [
            ["조회시간", "종목코드", "신호"],
            ["저장일자", "종목코드", "신호"],
        ],
        "score": [
            ["최종갱신일자", "종목코드"],
            ["저장일자", "종목코드"],
        ],
        "chart_history": [
            ["종목코드", "날짜"],
            ["저장일자", "종목코드", "날짜"],
        ],
        "supply_demand": [
            ["종목코드", "날짜"],
            ["저장일자", "종목코드", "날짜"],
        ],
        "news_history": [
            ["뉴스URL"],
            ["종목코드", "기사발행일시", "뉴스제목"],
            ["저장일자", "종목코드", "뉴스제목"],
        ],
    }

    for candidate in candidates_by_table.get(table_name, []):
        if all(column in df.columns for column in candidate):
            return candidate

    return []


def _delete_matching_rows(
    conn: sqlite3.Connection,
    table_name: str,
    df: pd.DataFrame,
    key_columns: list[str],
) -> None:
    if df.empty or not key_columns:
        return

    unique_keys = df[key_columns].drop_duplicates()

    where_clause = " AND ".join(
        f"{_quote_identifier(column)} IS ?"
        for column in key_columns
    )

    sql = (
        f"DELETE FROM {_quote_identifier(table_name)} "
        f"WHERE {where_clause}"
    )

    values = [
        tuple(_normalize_scalar(row[column]) for column in key_columns)
        for _, row in unique_keys.iterrows()
    ]

    conn.executemany(sql, values)


def _insert_dataframe(
    conn: sqlite3.Connection,
    table_name: str,
    df: pd.DataFrame,
) -> int:
    if df.empty:
        return 0

    _ensure_table(conn, table_name, df)

    key_columns = _build_dedup_columns(table_name, df)
    _delete_matching_rows(
        conn=conn,
        table_name=table_name,
        df=df,
        key_columns=key_columns,
    )

    columns = list(df.columns)
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    placeholders = ", ".join(["?"] * len(columns))

    sql = (
        f"INSERT INTO {_quote_identifier(table_name)} "
        f"({column_sql}) VALUES ({placeholders})"
    )

    values = [
        tuple(_normalize_scalar(row[column]) for column in columns)
        for _, row in df.iterrows()
    ]

    conn.executemany(sql, values)
    return len(values)


def save_dataframe(
    table_name: str,
    df: pd.DataFrame | None,
    data_type: str | None = None,
) -> int:
    table_name = _sanitize_table_name(table_name)
    prepared = _prepare_dataframe(
        df=df,
        data_type=data_type or table_name,
    )

    if prepared.empty:
        return 0

    conn = _connect()
    try:
        count = _insert_dataframe(
            conn=conn,
            table_name=table_name,
            df=prepared,
        )
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_all_data(
    portfolio_df: pd.DataFrame | None = None,
    volume_rank_df: pd.DataFrame | None = None,
    rise_rank_df: pd.DataFrame | None = None,
    trade_value_df: pd.DataFrame | None = None,
    signal_df: pd.DataFrame | None = None,
    scored_df: pd.DataFrame | None = None,
    chart_history_df: pd.DataFrame | None = None,
    supply_demand_df: pd.DataFrame | None = None,
    news_history_df: pd.DataFrame | None = None,
) -> None:
    datasets = [
        ("portfolio", portfolio_df),
        ("volume_rank", volume_rank_df),
        ("rise_rank", rise_rank_df),
        ("trade_value", trade_value_df),
        ("signal", signal_df),
        ("score", scored_df),
        ("chart_history", chart_history_df),
        ("supply_demand", supply_demand_df),
        ("news_history", news_history_df),
    ]

    conn = _connect()
    saved_counts: dict[str, int] = {}

    try:
        conn.execute("BEGIN")

        for table_name, df in datasets:
            prepared = _prepare_dataframe(
                df=df,
                data_type=table_name,
            )

            count = _insert_dataframe(
                conn=conn,
                table_name=table_name,
                df=prepared,
            )
            saved_counts[table_name] = count

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    summary = ", ".join(
        f"{name} {count}건"
        for name, count in saved_counts.items()
        if count > 0
    )

    print(
        "DB 저장 완료"
        + (f": {summary}" if summary else ": 저장할 데이터 없음")
    )


def load_table(
    table_name: str,
    limit: int | None = None,
    order_by: str | None = None,
    descending: bool = True,
) -> pd.DataFrame:
    table_name = _sanitize_table_name(table_name)
    conn = _connect()

    try:
        existing = _get_existing_columns(conn, table_name)
        if not existing:
            return pd.DataFrame()

        sql = f"SELECT * FROM {_quote_identifier(table_name)}"

        if order_by and order_by in existing:
            direction = "DESC" if descending else "ASC"
            sql += f" ORDER BY {_quote_identifier(order_by)} {direction}"

        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)

        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def load_sheet(
    name: str,
    limit: int | None = None,
) -> pd.DataFrame:
    aliases = {
        "보유종목": "portfolio",
        "portfolio": "portfolio",
        "거래량TOP30": "volume_rank",
        "volume_rank": "volume_rank",
        "상승률TOP30": "rise_rank",
        "rise_rank": "rise_rank",
        "거래대금TOP30": "trade_value",
        "trade_value": "trade_value",
        "신호": "signal",
        "signal": "signal",
        "추천점수": "score",
        "score": "score",
        "차트원본": "chart_history",
        "chart_history": "chart_history",
        "수급": "supply_demand",
        "supply_demand": "supply_demand",
        "뉴스": "news_history",
        "news_history": "news_history",
    }

    table_name = aliases.get(name, name)

    order_candidates = {
        "portfolio": "저장일자",
        "volume_rank": "저장일자",
        "rise_rank": "저장일자",
        "trade_value": "저장일자",
        "signal": "조회시간",
        "score": "최종갱신일자",
        "chart_history": "날짜",
        "supply_demand": "날짜",
        "news_history": "기사발행일시",
    }

    return load_table(
        table_name=table_name,
        limit=limit,
        order_by=order_candidates.get(table_name),
        descending=True,
    )


def load_latest_scores(limit: int = 30) -> pd.DataFrame:
    df = load_table("score")

    if df.empty:
        return df

    if "최종갱신일자" in df.columns:
        latest = df["최종갱신일자"].dropna().astype(str).max()
        df = df[df["최종갱신일자"].astype(str) == latest]

    sort_columns = [
        column
        for column in ["최종점수", "총점", "점수순위"]
        if column in df.columns
    ]

    if sort_columns:
        ascending = [
            column == "점수순위"
            for column in sort_columns
        ]
        df = df.sort_values(
            sort_columns,
            ascending=ascending,
        )

    return df.head(limit).reset_index(drop=True)


def search_stock(
    keyword: str,
    limit: int = 100,
) -> pd.DataFrame:
    keyword = str(keyword or "").strip()
    if not keyword:
        return pd.DataFrame()

    sources = [
        "score",
        "chart_history",
        "portfolio",
        "volume_rank",
        "rise_rank",
        "trade_value",
    ]

    collected = []

    for table_name in sources:
        df = load_table(table_name)
        if df.empty:
            continue

        code_column = next(
            (
                column
                for column in ["종목코드", "코드"]
                if column in df.columns
            ),
            None,
        )
        name_column = next(
            (
                column
                for column in ["종목명", "이름"]
                if column in df.columns
            ),
            None,
        )

        if code_column is None and name_column is None:
            continue

        mask = pd.Series(False, index=df.index)

        if code_column is not None:
            mask = mask | df[code_column].astype(str).str.contains(
                keyword,
                case=False,
                na=False,
            )

        if name_column is not None:
            mask = mask | df[name_column].astype(str).str.contains(
                keyword,
                case=False,
                na=False,
            )

        hit = df.loc[mask].copy()
        if hit.empty:
            continue

        hit["검색출처"] = table_name
        collected.append(hit)

    if not collected:
        return pd.DataFrame()

    result = pd.concat(collected, ignore_index=True, sort=False)

    dedup_columns = [
        column
        for column in ["종목코드", "종목명"]
        if column in result.columns
    ]

    if dedup_columns:
        result = result.drop_duplicates(
            subset=dedup_columns,
            keep="first",
        )

    return result.head(limit).reset_index(drop=True)


def delete_data_by_date(
    table_name: str,
    date_value: str,
    date_column: str = "저장일자",
) -> int:
    table_name = _sanitize_table_name(table_name)
    conn = _connect()

    try:
        existing = _get_existing_columns(conn, table_name)
        if date_column not in existing:
            return 0

        cursor = conn.execute(
            f"""
            DELETE FROM {_quote_identifier(table_name)}
            WHERE {_quote_identifier(date_column)} = ?
            """,
            (date_value,),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def vacuum_database() -> None:
    conn = _connect()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"Database Ready: {DB_PATH}")
    print(load_latest_scores(10))
