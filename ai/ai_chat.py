import re

from ai.gemini_client import ask_ai
from ai.ai_tools import (
    find_stock,
    get_stock_score,
    get_stock_price,
    get_stock_history,
    get_today_top30,
    get_news_summary,
    get_stock_context,
)


def extract_stock_keyword(question: str) -> str:
    question = question.strip()

    remove_words = [
        "어제", "오늘", "최근", "종가", "가격", "주가", "알려줘",
        "요약", "분석", "왜", "상승", "하락", "뉴스", "관련뉴스",
        "유튜브", "추천", "점수", "등급", "어때", "뭐야", "좀",
        "해줘", "찾아줘", "보여줘"
    ]

    keyword = question

    for word in remove_words:
        keyword = keyword.replace(word, "")

    keyword = re.sub(r"\s+", " ", keyword).strip()

    return keyword


def build_tool_context(question: str) -> str:
    q = question.strip()

    if "오늘 추천" in q or "추천종목" in q or "TOP30" in q.upper():
        return get_today_top30()

    keyword = extract_stock_keyword(q)

    if not keyword:
        return "질문에서 종목명을 찾지 못했습니다."

    contexts = []

    if "검색" in q or "찾아" in q:
        contexts.append(find_stock(keyword))

    if "어제" in q and ("종가" in q or "가격" in q or "주가" in q):
        contexts.append(get_stock_price(keyword, target="어제"))

    elif "종가" in q or "가격" in q or "주가" in q:
        contexts.append(get_stock_price(keyword))

    if "점수" in q or "추천" in q or "등급" in q:
        contexts.append(get_stock_score(keyword))

    if "뉴스" in q:
        contexts.append(get_news_summary(keyword))

    if "요약" in q or "분석" in q or "왜" in q or "상승" in q or "하락" in q or "어때" in q:
        contexts.append(get_stock_context(keyword))

    if not contexts:
        contexts.append(get_stock_context(keyword))

    return "\n\n".join(contexts)


def ask_stock_ai(question: str) -> str:
    tool_context = build_tool_context(question)

    prompt = f"""
너는 한국 주식 분석 보조 AI다.

아래 사용자의 질문에 답변하라.

반드시 아래 제공된 데이터만 근거로 답변하라.
데이터가 부족하면 부족하다고 말하라.
투자 권유처럼 단정하지 말고, 참고용 분석이라고 말하라.
모르는 가격이나 뉴스는 지어내지 마라.

[사용자 질문]
{question}

[프로그램 DB 조회 결과]
{tool_context}

[답변 형식]
- 핵심 답변
- 근거
- 주의할 점
"""

    return ask_ai(prompt)