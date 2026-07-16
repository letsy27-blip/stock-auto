"""KIS WebSocket 기반의 대시보드용 실시간 체결가 수신기."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import requests
import websocket

from config import APP_KEY, APP_SECRET, BASE_URL


WS_URL = "ws://ops.koreainvestment.com:21000"
TRADE_TR_ID = "H0STCNT0"  # 국내주식 실시간 체결가(KRX)


def _number(value: str) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _clean_code(value: str) -> str:
    return str(value or "").replace(".0", "").strip().zfill(6)


class KISRealtimeQuoteHub:
    """대시보드가 열려 있는 동안 TOP 종목만 실시간 체결가를 유지한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._codes: tuple[str, ...] = ()
        self._code_groups: dict[str, tuple[str, ...]] = {}
        self._quotes: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._restart = threading.Event()
        self._stop = threading.Event()
        self._connected = False
        self._last_error = ""

    def ensure_codes(self, codes: tuple[str, ...], source: str = "default") -> None:
        """화면별 구독 목록을 합쳐 한 WebSocket 연결에서 유지한다."""
        normalized = tuple(dict.fromkeys(_clean_code(code) for code in codes if code))
        with self._lock:
            self._code_groups[source] = normalized
            merged = tuple(
                dict.fromkeys(
                    code
                    for group in self._code_groups.values()
                    for code in group
                )
            )
            if merged == self._codes:
                return
            self._codes = merged
            self._quotes = {
                code: quote for code, quote in self._quotes.items() if code in merged
            }
            self._restart.set()

        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run,
                name="kis-realtime-quotes",
                daemon=True,
            )
            self._thread.start()

    def snapshot(self, codes: tuple[str, ...]) -> dict:
        normalized = tuple(_clean_code(code) for code in codes)
        with self._lock:
            return {
                "quotes": {
                    code: dict(self._quotes[code])
                    for code in normalized
                    if code in self._quotes
                },
                "connected": self._connected,
                "error": self._last_error,
            }

    def _approval_key(self) -> str:
        response = requests.post(
            f"{BASE_URL}/oauth2/Approval",
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": APP_KEY,
                "secretkey": APP_SECRET,
            },
            timeout=15,
        )
        response.raise_for_status()
        key = response.json().get("approval_key", "")
        if not key:
            raise RuntimeError("KIS WebSocket 접속키를 받지 못했습니다.")
        return key

    def _subscribe(self, ws, approval_key: str, codes: tuple[str, ...]) -> None:
        for code in codes:
            ws.send(
                json.dumps(
                    {
                        "header": {
                            "approval_key": approval_key,
                            "custtype": "P",
                            "tr_type": "1",
                            "content-type": "utf-8",
                        },
                        "body": {"input": {"tr_id": TRADE_TR_ID, "tr_key": code}},
                    }
                )
            )
            time.sleep(0.12)

    def _handle_trade(self, message: str) -> None:
        # 형식: 0|H0STCNT0|건수|종목코드^체결시간^현재가^전일대비...
        parts = message.split("|", 3)
        if len(parts) != 4 or parts[0] != "0" or parts[1] != TRADE_TR_ID:
            return

        values = parts[3].split("^")
        if len(values) < 6:
            return

        code = _clean_code(values[0])
        quote = {
            "price": _number(values[2]),
            "change": _number(values[3]),
            "change_rate": _number(values[5]),
            "trade_time": values[1],
            "received_at": datetime.now().strftime("%H:%M:%S"),
        }
        with self._lock:
            self._quotes[code] = quote

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                codes = self._codes
            if not codes:
                time.sleep(1)
                continue

            ws = None
            self._restart.clear()
            try:
                approval_key = self._approval_key()
                ws = websocket.create_connection(WS_URL, timeout=10)
                self._subscribe(ws, approval_key, codes)
                with self._lock:
                    self._connected = True
                    self._last_error = ""

                while not self._stop.is_set() and not self._restart.is_set():
                    try:
                        message = ws.recv()
                        if message:
                            self._handle_trade(message)
                    except websocket.WebSocketTimeoutException:
                        continue
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._last_error = str(exc)
                time.sleep(3)
            finally:
                with self._lock:
                    self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass


_hub: KISRealtimeQuoteHub | None = None
_hub_lock = threading.Lock()


def get_realtime_quote_hub() -> KISRealtimeQuoteHub:
    global _hub
    with _hub_lock:
        if _hub is None:
            _hub = KISRealtimeQuoteHub()
        return _hub
