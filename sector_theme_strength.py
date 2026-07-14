from __future__ import annotations

import json

import pandas as pd


def clean_code(value) -> str:
    code = str(value or "").replace(".0", "").strip()
    return code.zfill(6) if code else ""


def merge_classification(
    current_df: pd.DataFrame,
    classification_df: pd.DataFrame,
) -> pd.DataFrame:
    if current_df is None or current_df.empty:
        return pd.DataFrame()

    current = current_df.copy()
    current["종목코드"] = current["종목코드"].map(clean_code)

    if (
        classification_df is None
        or classification_df.empty
        or "종목코드" not in classification_df.columns
    ):
        return current

    classification = classification_df.copy()
    classification["종목코드"] = classification["종목코드"].map(
        clean_code
    )

    add_columns = [
        column
        for column in [
            "종목코드",
            "업종",
            "대표테마",
            "테마JSON",
            "분류근거",
            "분류신뢰도",
        ]
        if column in classification.columns
    ]

    for column in add_columns:
        if column != "종목코드" and column in current.columns:
            current = current.drop(columns=[column])

    return current.merge(
        classification[add_columns].drop_duplicates(
            subset=["종목코드"],
            keep="last",
        ),
        on="종목코드",
        how="left",
    )


def _score_group(
    group: pd.DataFrame,
    name_column: str,
    output_name: str,
) -> dict:
    final_scores = pd.to_numeric(
        group["최종점수"],
        errors="coerce",
    ).dropna()

    row = {
        output_name: group.iloc[0][name_column],
        "후보종목수": len(group),
        "평균최종점수": round(final_scores.mean(), 2),
        "최고최종점수": round(final_scores.max(), 2),
        "강한종목수": int((final_scores >= 55).sum()),
        "강한종목비율(%)": round(
            (final_scores >= 55).mean() * 100,
            1,
        ),
    }

    for column, output in [
        ("시장점수", "평균시장점수"),
        ("수급점수", "평균수급점수"),
        ("뉴스점수", "평균뉴스점수"),
    ]:
        if column in group.columns:
            values = pd.to_numeric(
                group[column],
                errors="coerce",
            ).dropna()
            row[output] = (
                round(values.mean(), 2)
                if not values.empty
                else 0
            )

    top = group.assign(
        _score=pd.to_numeric(
            group["최종점수"],
            errors="coerce",
        )
    ).sort_values("_score", ascending=False).iloc[0]

    row["대표종목"] = str(top.get("종목명", ""))
    row["대표종목점수"] = round(
        float(top.get("_score", 0) or 0),
        2,
    )

    row["강도"] = round(
        row["평균최종점수"] * 0.65
        + row["최고최종점수"] * 0.20
        + row["강한종목비율(%)"] * 0.15,
        2,
    )

    return row


def make_industry_strength(
    current_df: pd.DataFrame,
    classification_df: pd.DataFrame,
) -> pd.DataFrame:
    merged = merge_classification(
        current_df,
        classification_df,
    )

    if merged.empty or "업종" not in merged.columns:
        return pd.DataFrame()

    merged["업종"] = (
        merged["업종"]
        .fillna("")
        .astype(str)
        .str.strip()
    )
    merged = merged[
        ~merged["업종"].isin(["", "nan", "None", "기타"])
    ]

    rows = [
        _score_group(group, "업종", "업종")
        for _, group in merged.groupby("업종")
        if not group.empty
    ]

    result = pd.DataFrame(rows)

    if result.empty:
        return result

    result = result.sort_values(
        ["강도", "평균최종점수", "후보종목수"],
        ascending=False,
    ).reset_index(drop=True)
    result.insert(0, "업종순위", range(1, len(result) + 1))

    return result.rename(columns={"강도": "업종강도"})


def _parse_themes(value) -> list[str]:
    if isinstance(value, list):
        return [
            str(theme).strip()
            for theme in value
            if str(theme).strip()
        ]

    try:
        parsed = json.loads(str(value or "[]"))
        if isinstance(parsed, list):
            return [
                str(theme).strip()
                for theme in parsed
                if str(theme).strip()
            ]
    except Exception:
        pass

    return []


def make_theme_strength(
    current_df: pd.DataFrame,
    classification_df: pd.DataFrame,
) -> pd.DataFrame:
    merged = merge_classification(
        current_df,
        classification_df,
    )

    if merged.empty or "테마JSON" not in merged.columns:
        return pd.DataFrame()

    expanded_rows = []

    for _, row in merged.iterrows():
        themes = _parse_themes(row.get("테마JSON"))

        for theme in themes:
            item = row.to_dict()
            item["테마"] = theme
            expanded_rows.append(item)

    expanded = pd.DataFrame(expanded_rows)

    if expanded.empty:
        return expanded

    rows = [
        _score_group(group, "테마", "테마")
        for _, group in expanded.groupby("테마")
        if not group.empty
    ]

    result = pd.DataFrame(rows)

    if result.empty:
        return result

    result = result.sort_values(
        ["강도", "평균최종점수", "후보종목수"],
        ascending=False,
    ).reset_index(drop=True)
    result.insert(0, "테마순위", range(1, len(result) + 1))

    return result.rename(columns={"강도": "테마강도"})
