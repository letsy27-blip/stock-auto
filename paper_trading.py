import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd


INITIAL_CASH = 100_000_000
BUY_FEE_RATE = 0.00015
SELL_FEE_RATE = 0.00015
SELL_TAX_RATE = 0.0018
DB_PATH = Path(__file__).resolve().with_name("paper_trading.db")


def _connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize_paper_account():
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
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO paper_account(id, cash, initial_cash, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (INITIAL_CASH, INITIAL_CASH, datetime.now().isoformat(timespec="seconds")),
        )


def get_account():
    initialize_paper_account()
    with _connect() as connection:
        return dict(connection.execute("SELECT * FROM paper_account WHERE id = 1").fetchone())


def get_positions():
    initialize_paper_account()
    with _connect() as connection:
        return pd.read_sql_query(
            "SELECT stock_code, stock_name, quantity, average_price, updated_at "
            "FROM paper_positions WHERE quantity > 0 ORDER BY stock_name",
            connection,
        )


def get_orders(limit=200):
    initialize_paper_account()
    with _connect() as connection:
        return pd.read_sql_query(
            "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?",
            connection,
            params=(int(limit),),
        )


def place_order(side, stock_code, stock_name, quantity, price):
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
    return {"side": side, "amount": amount, "fee": fee, "tax": tax}


def reset_account():
    initialize_paper_account()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM paper_orders")
        connection.execute("DELETE FROM paper_positions")
        connection.execute(
            "UPDATE paper_account SET cash = ?, initial_cash = ?, updated_at = ? WHERE id = 1",
            (INITIAL_CASH, INITIAL_CASH, datetime.now().isoformat(timespec="seconds")),
        )
