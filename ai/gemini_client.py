from __future__ import annotations

import os
from collections.abc import Iterable

from google import genai
from google.genai import types

from config import GEMINI_API_KEY

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_INSTRUCTION = """
너는 주식 프로그램 안에서 동작하는 글로벌 투자 분석 AI다.

한국 주식과 미국 주식을 모두 다룬다.
주식, ETF, 기업, 산업, 경제, 환율, 금리, 시장지수, 투자전략 질문에
자연스럽고 구체적으로 답한다.

사용자가 최신 뉴스, 오늘 뉴스, 최근 공시, 현재 시장 상황, 특정 날짜의 지수나 종가처럼
최신 정보가 필요한 질문을 하면 Google 검색 도구를 적극적으로 사용한다.
검색한 최신 정보와 일반적인 분석을 구분하고, 가능한 경우 출처가 드러나게 답한다.

사용자가 종목에 대해 살까 말까, 전망, 매수 여부를 물으면 무조건 거절하지 말고
긍정 요인, 부정 요인, 단기 관점, 중장기 관점, 위험요인, 최종 의견 순서로 분석한다.

확정적인 수익을 보장하지 않으며 확인하지 못한 숫자를 만들어내지 않는다.
한국 종목은 원화, 미국 종목은 달러 기준임을 구분하고 환율 영향을 고려한다.
답변은 기본적으로 한국어로 작성한다.
쓸데없이 긴 면책 문구를 반복하지 않는다.
""".strip()


def _client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return genai.Client(api_key=GEMINI_API_KEY)


def _build_prompt(messages: list[dict[str, str]]) -> str:
    lines: list[str] = []

    for message in messages:
        role = message.get("role", "user")
        content = str(message.get("content", "")).strip()
        if not content:
            continue

        label = "사용자" if role == "user" else "Gemini"
        lines.append(f"{label}: {content}")

    lines.append("Gemini:")
    return "\n\n".join(lines)


def stream_chat(
    messages: list[dict[str, str]],
    model: str | None = None,
) -> Iterable[str]:
    """
    Gemini 연속대화 + Google Search Grounding.
    최신 뉴스/시세/시장 질문이면 모델이 필요에 따라 Google 검색을 실행한다.
    """
    client = _client()
    prompt = _build_prompt(messages)

    response = client.models.generate_content_stream(
        model=model or DEFAULT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.7,
            tools=[
                types.Tool(
                    google_search=types.GoogleSearch()
                )
            ],
        ),
    )

    for chunk in response:
        text = getattr(chunk, "text", None)
        if text:
            yield text


def ask_ai(question: str, model: str | None = None) -> str:
    return "".join(
        stream_chat(
            [{"role": "user", "content": question}],
            model=model,
        )
    )