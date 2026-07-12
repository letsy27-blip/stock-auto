import os
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
BASE_URL = os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")

ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_CODE = os.getenv("KIS_ACCOUNT_CODE")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")