import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import supabase_paper_trading as cloud_paper


INITIAL_CASH = 100_000_000
BUY_FEE_RATE = 0.00015
SELL_FEE_RATE = 0.00015
SELL_TAX_RATE = 0.0018
DB_PATH = Path(__file__).resolve().with_name("paper_trading.db")


def is_remote_storage_enabled() -> bool:
    """Supabase 연결 키가 있으면 공개 앱용 영구 저장소를 사용한다."""
    return cloud_paper.is_enabled()


def is_paper_user_authenticated() -> bool:
    return cloud_paper.is_authenticated()


def _connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize_paper_account():
    if is_remote_storage_enabled():
        if is_paper_user_authenticated():
            cloud_paper.initialize_account()
        return
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash REAL NOT NULL,
                initial_cash REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                stock_code TEXT PRIMARY KEY,
                stock_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity >= 0),
                average_price REAL NOT NULL CHECK (average_price >= 0),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ordered_at TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                price REAL NOT NULL CHECK (price > 0),
                fee REAL NOT NULL,
                tax REAL NOT NULL,
                amount REAL NOT NULL,
                realized_profit REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS investor_behavior_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('search', 'view', 'BUY', 'SELL')),
                stock_code TEXT,
                stock_name TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO paper_account(id, cash, initial_cash, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (INITIAL_CASH, INITIAL_CASH, datetime.now().isoformat(timespec="seconds")),
        )
        # 기능 추가 전의 모의 주문도 성향 분석의 시작 표본으로 한 번만 옮긴다.
        connection.execute(
            "INSERT INTO investor_behavior_events "
            "(occurred_at, event_type, stock_code, stock_name, metadata_json) "
            "SELECT o.ordered_at, o.side, o.stock_code, o.stock_name, '{}' "
            "FROM paper_orders o WHERE NOT EXISTS ("
            "SELECT 1 FROM investor_behavior_events e "
            "WHERE e.occurred_at = o.ordered_at AND e.event_type = o.side "
            "AND e.stock_code = o.stock_code)"
        )


def record_behavior_event(event_type: str, stock_code: str = "", stock_name: str = "", metadata: dict | None = None) -> None:
    """개인 로컬 모의투자 행동을 저장한다. 실제 주문이나 외부 전송은 하지 않는다."""
    if is_remote_storage_enabled():
        # 공개 분석은 비로그인 상태에서도 볼 수 있으나, 개인 행동은 기록하지 않는다.
        if is_paper_user_authenticated():
            try:
                cloud_paper.record_behavior_event(event_type, stock_code, stock_name, metadata)
            except RuntimeError:
                pass
        return
    initialize_paper_account()
    event_type = str(event_type).upper()
    if event_type not in {"SEARCH", "VIEW", "BUY", "SELL"}:
        raise ValueError("지원하지 않는 행동 기록입니다.")
    normalized_type = event_type.lower() if event_type in {"SEARCH", "VIEW"} else event_type
    with _connect() as connection:
        connection.execute(
            "INSERT INTO investor_behavior_events (occurred_at, event_type, stock_code, stock_name, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), normalized_type, str(stock_code or "").replace(".0", "").zfill(6) if stock_code else "", str(stock_name or "").strip(), json.dumps(metadata or {}, ensure_ascii=False)),
        )


def _parse_event_metadata(value) -> dict:
    """SQLite 문자열과 Supabase JSON 객체를 같은 형태로 정규화한다."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_investor_profile(days: int = 30) -> dict:
    """최근 행동을 바탕으로 성향과 충동 진입 경고를 계산한다."""
    since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
    default = {"profile": "분석 중", "summary": "아직 행동 표본이 적습니다. 종목을 검색하고 모의 주문을 기록하면 성향이 표시됩니다.", "warnings": [], "total_actions": 0, "searches": 0, "views": 0, "buys": 0, "sells": 0, "high_risk_ratio": None, "rapid_entry_count": 0, "favorites": pd.DataFrame(columns=["종목코드", "종목명", "열람·검색", "매수", "매도"])}
    if is_remote_storage_enabled():
        if not is_paper_user_authenticated():
            return default
        events = cloud_paper.get_events_since(since)
    else:
        initialize_paper_account()
        with _connect() as connection:
            events = pd.read_sql_query("SELECT * FROM investor_behavior_events WHERE occurred_at >= ? ORDER BY occurred_at", connection, params=(since,))
    if events.empty:
        return default
    events["occurred_at"] = pd.to_datetime(events["occurred_at"], errors="coerce")
    events["metadata"] = events["metadata_json"].map(_parse_event_metadata)
    events["chase_risk"] = events["metadata"].map(lambda value: float(value.get("chase_risk", 0) or 0))
    counts = events["event_type"].value_counts()
    searches, views, buys, sells = (int(counts.get(key, 0)) for key in ("search", "view", "BUY", "SELL"))
    risk_samples = events[events["event_type"].isin(["search", "view"])]
    high_risk_ratio = float((risk_samples["chase_risk"] >= 45).mean() * 100) if not risk_samples.empty else None
    rapid_entry_count = 0
    for _, buy in events[events["event_type"] == "BUY"].iterrows():
        prior = events[(events["stock_code"] == buy["stock_code"]) & (events["event_type"].isin(["search", "view"])) & (events["occurred_at"] <= buy["occurred_at"])]
        if not prior.empty and 0 <= (buy["occurred_at"] - prior["occurred_at"].iloc[-1]).total_seconds() / 60 <= 10:
            rapid_entry_count += 1
    activity = events.groupby(["stock_code", "stock_name", "event_type"]).size().unstack(fill_value=0)
    values = lambda key: activity[key].to_numpy() if key in activity.columns else pd.Series(0, index=activity.index).to_numpy()
    favorites = pd.DataFrame({"종목코드": activity.index.get_level_values("stock_code"), "종목명": activity.index.get_level_values("stock_name"), "열람·검색": values("search") + values("view"), "매수": values("BUY"), "매도": values("SELL")})
    favorites = favorites[favorites["종목코드"] != ""].sort_values(["열람·검색", "매수"], ascending=False).head(5)
    warnings = []
    if rapid_entry_count >= 2:
        warnings.append(f"검색·열람 후 10분 안에 모의 매수한 경우가 {rapid_entry_count}건입니다. 주문 전 10분 대기 규칙을 권장합니다.")
    if high_risk_ratio is not None and high_risk_ratio >= 40:
        warnings.append(f"열람한 종목 중 추격위험 45점 이상 비중이 {high_risk_ratio:.0f}%입니다. 급등주 진입 전 돌파 확인을 기다리세요.")
    if buys >= 8 and buys > sells * 2:
        warnings.append("매수 횟수가 매도보다 많습니다. 새 진입 전 보유 종목과 손절 기준을 먼저 점검하세요.")
    if len(events) < 8:
        profile, summary = "분석 중", "표본이 아직 적습니다. 행동 8건부터 성향을 더 신뢰도 있게 분류합니다."
    elif (high_risk_ratio is not None and high_risk_ratio >= 40) or rapid_entry_count >= 2:
        profile, summary = "공격 성향", "고변동성 후보 열람 또는 빠른 진입이 상대적으로 많습니다. 수익 기회와 함께 추격 위험 관리가 중요합니다."
    elif (high_risk_ratio is None or high_risk_ratio < 20) and buys <= max(1, searches // 3):
        profile, summary = "안정 성향", "진입 전 탐색 비중이 높고 고위험 후보 집중도가 낮습니다. 다만 관망만 길어지지 않도록 진입 기준을 정해 두세요."
    else:
        profile, summary = "균형 성향", "탐색과 진입의 비중이 비교적 균형적입니다. 현재의 손절·분할매수 원칙을 유지하세요."
    return {"profile": profile, "summary": summary, "warnings": warnings, "total_actions": len(events), "searches": searches, "views": views, "buys": buys, "sells": sells, "high_risk_ratio": high_risk_ratio, "rapid_entry_count": rapid_entry_count, "favorites": favorites}


def get_account():
    if is_remote_storage_enabled():
        return cloud_paper.get_account()
    initialize_paper_account()
    with _connect() as connection:
        return dict(connection.execute("SELECT * FROM paper_account WHERE id = 1").fetchone())


def get_positions():
    if is_remote_storage_enabled():
        return cloud_paper.get_positions()
    initialize_paper_account()
    with _connect() as connection:
        return pd.read_sql_query(
            "SELECT stock_code, stock_name, quantity, average_price, updated_at "
            "FROM paper_positions WHERE quantity > 0 ORDER BY stock_name",
            connection,
        )


def get_orders(limit=200):
    if is_remote_storage_enabled():
        return cloud_paper.get_orders(limit)
    initialize_paper_account()
    with _connect() as connection:
        return pd.read_sql_query(
            "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?",
            connection,
            params=(int(limit),),
        )


def place_order(side, stock_code, stock_name, quantity, price):
    if is_remote_storage_enabled():
        return cloud_paper.place_order(side, stock_code, stock_name, quantity, price)
    initialize_paper_account()
    side = str(side).upper()
    stock_code = str(stock_code).replace(".0", "").zfill(6)
    stock_name = str(stock_name).strip() or stock_code
    quantity = int(quantity)
    price = float(price)
    if side not in {"BUY", "SELL"} or quantity <= 0 or price <= 0:
        raise ValueError("주문 정보가 올바르지 않습니다.")

    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        account = connection.execute(
            "SELECT cash FROM paper_account WHERE id = 1"
        ).fetchone()
        position = connection.execute(
            "SELECT quantity, average_price FROM paper_positions WHERE stock_code = ?",
            (stock_code,),
        ).fetchone()
        held_quantity = int(position["quantity"]) if position else 0
        average_price = float(position["average_price"]) if position else 0.0
        gross = price * quantity

        if side == "BUY":
            fee = round(gross * BUY_FEE_RATE)
            tax = 0
            amount = gross + fee
            if float(account["cash"]) < amount:
                raise ValueError("주문 가능 금액이 부족합니다.")
            new_quantity = held_quantity + quantity
            new_average = ((held_quantity * average_price) + gross + fee) / new_quantity
            new_cash = float(account["cash"]) - amount
            realized_profit = 0
        else:
            if held_quantity < quantity:
                raise ValueError("보유 수량보다 많이 매도할 수 없습니다.")
            fee = round(gross * SELL_FEE_RATE)
            tax = round(gross * SELL_TAX_RATE)
            amount = gross - fee - tax
            new_quantity = held_quantity - quantity
            new_average = average_price if new_quantity else 0
            new_cash = float(account["cash"]) + amount
            realized_profit = amount - (average_price * quantity)

        connection.execute(
            "UPDATE paper_account SET cash = ?, updated_at = ? WHERE id = 1",
            (new_cash, now),
        )
        if new_quantity:
            connection.execute(
                "INSERT INTO paper_positions(stock_code, stock_name, quantity, average_price, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(stock_code) DO UPDATE SET "
                "stock_name=excluded.stock_name, quantity=excluded.quantity, "
                "average_price=excluded.average_price, updated_at=excluded.updated_at",
                (stock_code, stock_name, new_quantity, new_average, now),
            )
        else:
            connection.execute("DELETE FROM paper_positions WHERE stock_code = ?", (stock_code,))
        connection.execute(
            "INSERT INTO paper_orders(ordered_at, side, stock_code, stock_name, quantity, "
            "price, fee, tax, amount, realized_profit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, side, stock_code, stock_name, quantity, price, fee, tax, amount, realized_profit),
        )
        connection.execute(
            "INSERT INTO investor_behavior_events (occurred_at, event_type, stock_code, stock_name, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (now, side, stock_code, stock_name, json.dumps({"quantity": quantity, "price": price}, ensure_ascii=False)),
        )
    return {"side": side, "amount": amount, "fee": fee, "tax": tax}


def reset_account():
    if is_remote_storage_enabled():
        cloud_paper.reset_account()
        return
    initialize_paper_account()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM paper_orders")
        connection.execute("DELETE FROM paper_positions")
        connection.execute(
            "UPDATE paper_account SET cash = ?, initial_cash = ?, updated_at = ? WHERE id = 1",
            (INITIAL_CASH, INITIAL_CASH, datetime.now().isoformat(timespec="seconds")),
        )
