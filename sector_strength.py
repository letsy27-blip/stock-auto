from __future__ import annotations

from typing import Iterable

import pandas as pd


SECTOR_COLUMN_CANDIDATES = [
    "업종명",
    "업종",
    "섹터",
    "sector",
    "industry",
    "업종대분류",
    "업종중분류",
]


def clean_code(value) -> str:
    code = str(value or "").replace(".0", "").strip()
    return code.zfill(6) if code else ""


def find_sector_column(columns: Iterable[str]) -> str | None:
    column_set = set(columns)

    for candidate in SECTOR_COLUMN_CANDIDATES:
        if candidate in column_set:
            return candidate

    return None


def make_sector_strength(
    current_df: pd.DataFrame,
    master_df: pd.DataFrame,
) -> tuple[pd.DataFrame, str]:
    """
    현재 추천 종목을 업종별로 묶어 업종 강도를 계산한다.

    반환:
    - 업종 강도 DataFrame
    - 사용한 업종 컬럼명
    """
    if current_df is None or current_df.empty:
        return pd.DataFrame(), ""

    current = current_df.copy()

    if "종목코드" not in current.columns:
        return pd.DataFrame(), ""

    current["종목코드"] = current["종목코드"].map(clean_code)

    sector_column = ""

    # score_current 자체에 업종 정보가 있으면 우선 사용
    current_sector_column = find_sector_column(current.columns)

    if current_sector_column:
        sector_column = current_sector_column
        current["업종표시"] = (
            current[current_sector_column]
            .fillna("")
            .astype(str)
            .str.strip()
        )

    # 없으면 stock_master와 병합
    elif (
        master_df is not None
        and not master_df.empty
        and "종목코드" in master_df.columns
    ):
        master = master_df.copy()
        master["종목코드"] = master["종목코드"].map(clean_code)

        master_sector_column = find_sector_column(master.columns)

        if master_sector_column:
            sector_column = master_sector_column

            sector_map = (
                master[["종목코드", master_sector_column]]
                .drop_duplicates(subset=["종목코드"], keep="last")
                .rename(columns={master_sector_column: "업종표시"})
            )

            current = current.merge(
                sector_map,
                on="종목코드",
                how="left",
            )

    if "업종표시" not in current.columns:
        return pd.DataFrame(), ""

    current["업종표시"] = (
        current["업종표시"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    current = current[
        ~current["업종표시"].isin(
            ["", "nan", "None", "미분류", "기타"]
        )
    ].copy()

    if current.empty:
        return pd.DataFrame(), sector_column

    for column in [
        "최종점수",
        "시장점수",
        "수급점수",
        "뉴스점수",
        "현재순위",
        "점수순위",
    ]:
        if column in current.columns:
            current[column] = pd.to_numeric(
                current[column],
                errors="coerce",
            )

    if "최종점수" not in current.columns:
        return pd.DataFrame(), sector_column

    rank_column = (
        "현재순위"
        if "현재순위" in current.columns
        else "점수순위"
        if "점수순위" in current.columns
        else None
    )

    grouped = current.groupby("업종표시", dropna=False)

    rows = []

    for sector_name, group in grouped:
        final_scores = group["최종점수"].dropna()

        if final_scores.empty:
            continue

        row = {
            "업종": sector_name,
            "후보종목수": len(group),
            "평균최종점수": round(final_scores.mean(), 2),
            "최고최종점수": round(final_scores.max(), 2),
            "강한종목수": int((final_scores >= 55).sum()),
            "강한종목비율(%)": round(
                (final_scores >= 55).mean() * 100,
                1,
            ),
        }

        if "시장점수" in group.columns:
            row["평균시장점수"] = round(
                group["시장점수"].dropna().mean(),
                2,
            )

        if "수급점수" in group.columns:
            row["평균수급점수"] = round(
                group["수급점수"].dropna().mean(),
                2,
            )

        if "뉴스점수" in group.columns:
            row["평균뉴스점수"] = round(
                group["뉴스점수"].dropna().mean(),
                2,
            )

        if rank_column:
            valid_ranks = group[rank_column].dropna()
            row["최고순위"] = (
                int(valid_ranks.min())
                if not valid_ranks.empty
                else None
            )

        top_stock_row = group.sort_values(
            "최종점수",
            ascending=False,
        ).iloc[0]

        row["대표종목"] = str(
            top_stock_row.get("종목명", "")
        )
        row["대표종목점수"] = round(
            float(top_stock_row.get("최종점수", 0)),
            2,
        )

        rows.append(row)

    result = pd.DataFrame(rows)

    if result.empty:
        return result, sector_column

    result["업종강도"] = (
        result["평균최종점수"] * 0.65
        + result["최고최종점수"] * 0.20
        + result["강한종목비율(%)"] * 0.15
    ).round(2)

    result = result.sort_values(
        ["업종강도", "평균최종점수", "후보종목수"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    result.insert(0, "업종순위", range(1, len(result) + 1))

    return result, sector_column
