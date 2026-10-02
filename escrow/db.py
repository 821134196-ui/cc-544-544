"""SQLite 连接管理与 schema。

所有金额以整数「分」存储。账务采用标准复式记账：
cash:gateway（模拟支付网关在途资金）、escrow:<order>（托管冻结）、
wallet:<user>（用户平台余额）、ext:<user>（站外资金来源）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id),
    role    TEXT NOT NULL CHECK (role IN ('buyer','seller','inspector','arbiter')),
    PRIMARY KEY (user_id, role)
);

CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,
    seller_id           TEXT NOT NULL REFERENCES users(id),
    buyer_id            TEXT NOT NULL REFERENCES users(id),
    inspector_id        TEXT REFERENCES users(id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    amount              INTEGER NOT NULL CHECK (amount > 0),
    status              TEXT NOT NULL,
    window_sec          INTEGER NOT NULL CHECK (window_sec > 0),
    agreed_at           TEXT,
    inspection_deadline TEXT NOT NULL,
    escrow_requested_at TEXT,
    escrowed_at         TEXT,
    delivered_at        TEXT,
    accepted_at         TEXT,
    rejected_at         TEXT,
    seller_confirmed_at TEXT,
    buyer_confirmed_at  TEXT,
    release_seller_at   TEXT,
    release_buyer_at    TEXT,
    dispute_reason      TEXT,
    disputed_at         TEXT,
    no_show_at          TEXT,
    cancelled_at        TEXT,
    arbiter_id          TEXT REFERENCES users(id),
    ruling              TEXT,
    ruled_at            TEXT,
    release_after       TEXT,
    released_at         TEXT,
    refunded_at         TEXT,
    updated_at          TEXT NOT NULL
);

-- 卖家交付时填写的验货清单（序列号 / 外观 / 配件）
CREATE TABLE IF NOT EXISTS delivery_checklists (
    order_id          TEXT PRIMARY KEY REFERENCES orders(id),
    serial_no         TEXT NOT NULL,
    appearance        TEXT NOT NULL,
    accessories       TEXT NOT NULL,
    seller_conclusion TEXT NOT NULL CHECK (seller_conclusion IN ('conforming','defective')),
    note              TEXT,
    photo_path        TEXT,
    created_at        TEXT NOT NULL
);

-- 买家在约定窗口内的验收结论
CREATE TABLE IF NOT EXISTS inspections (
    order_id   TEXT PRIMARY KEY REFERENCES orders(id),
    conclusion TEXT NOT NULL CHECK (conclusion IN ('accept','reject')),
    note       TEXT,
    photo_path TEXT,
    created_at TEXT NOT NULL
);

-- 验货人现场见证备注
CREATE TABLE IF NOT EXISTS inspector_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    inspector_id TEXT NOT NULL REFERENCES users(id),
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 验货延期记录
CREATE TABLE IF NOT EXISTS window_extensions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    inspector_id TEXT NOT NULL REFERENCES users(id),
    extra_sec    INTEGER NOT NULL CHECK (extra_sec > 0),
    new_deadline TEXT NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
    order_id   TEXT PRIMARY KEY REFERENCES orders(id),
    reason     TEXT NOT NULL CHECK (reason IN
                 ('item_mismatch','opposite_conclusions','hidden_defect','other')),
    opened_by  TEXT NOT NULL REFERENCES users(id),
    status     TEXT NOT NULL CHECK (status IN ('open','ruled')) DEFAULT 'open',
    created_at TEXT NOT NULL,
    ruled_at   TEXT
);

-- 争议证据：双方可各自提交相反结论
CREATE TABLE IF NOT EXISTS evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    submitter_id TEXT NOT NULL REFERENCES users(id),
    conclusion   TEXT NOT NULL CHECK (conclusion IN ('conforming','defective')),
    note         TEXT NOT NULL,
    photo_path   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arbitrations (
    order_id   TEXT PRIMARY KEY REFERENCES orders(id),
    arbiter_id TEXT NOT NULL REFERENCES users(id),
    ruling     TEXT NOT NULL CHECK (ruling IN ('release_to_seller','refund_to_buyer')),
    rationale  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 资金流（每笔订单：一次入账 HOLD，最多一次终态出账 RELEASE/REFUND）
CREATE TABLE IF NOT EXISTS money_movements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    direction    TEXT NOT NULL CHECK (direction IN ('hold','release','refund')),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    idem_key     TEXT NOT NULL UNIQUE,           -- 幂等键：重复提交不产生新流
    receipt_no   TEXT UNIQUE,                    -- 支付网关回执号：重放只生效一次
    status       TEXT NOT NULL CHECK (status IN ('pending','confirmed')) DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_hold ON money_movements(order_id)
    WHERE direction = 'hold';
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_terminal ON money_movements(order_id)
    WHERE direction IN ('release','refund');

-- 模拟支付网关侧的支付单
CREATE TABLE IF NOT EXISTS gateway_payments (
    idem_key   TEXT PRIMARY KEY,
    amount     INTEGER NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('queued','paid')),
    receipt_no TEXT UNIQUE,
    created_at TEXT NOT NULL,
    paid_at    TEXT
);

-- 复式记账
CREATE TABLE IF NOT EXISTS ledger_transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_id INTEGER NOT NULL REFERENCES money_movements(id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id   INTEGER NOT NULL REFERENCES ledger_transactions(id),
    account TEXT NOT NULL,
    debit   INTEGER NOT NULL DEFAULT 0,
    credit  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_le_account ON ledger_entries(account);

CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    kind       TEXT NOT NULL,
    actor_id   TEXT REFERENCES users(id),
    payload    TEXT,
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
