import os
from dotenv import load_dotenv

load_dotenv()

def _get_setting(name: str, default: str | None = None) -> str | None:
    """로컬 .env와 Streamlit Cloud Secrets를 같은 이름으로 지원한다."""
    value = os.getenv(name)
    if value:
        return value

    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return default


APP_KEY = _get_setting("KIS_APP_KEY")
APP_SECRET = _get_setting("KIS_APP_SECRET")
BASE_URL = _get_setting("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

ACCOUNT_NO = _get_setting("KIS_ACCOUNT_NO")
ACCOUNT_CODE = _get_setting("KIS_ACCOUNT_CODE")

GEMINI_API_KEY = _get_setting("GEMINI_API_KEY")
