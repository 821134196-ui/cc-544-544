"""SQLite 存储：订单、账务事件与各类业务表。

资金安全的两道防线：
1. 服务层在事务内做状态检查后再入账；
2. 数据库级唯一索引与触发器兜底：
   - 每笔交易至多一条 HOLD / RELEASE / REFUND；
   - 同一回执号至多入账一次（回执重放不会形成第二笔资金流）；
   - 放款与退款互斥（触发器）。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL UNIQUE,
  role  TEXT NOT NULL CHECK (role IN ('buyer','seller','inspector','arbitrator','platform'))
);

CREATE TABLE IF NOT EXISTS deals (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  code                      TEXT NOT NULL UNIQUE,
  title                     TEXT NOT NULL,
  description               TEXT NOT NULL,
  photos                    TEXT NOT NULL DEFAULT '',   -- 商品描述照片，逗号分隔
  amount_cents              INTEGER NOT NULL CHECK (amount_cents > 0),
  buyer_id                  INTEGER NOT NULL REFERENCES users(id),
  seller_id                 INTEGER NOT NULL REFERENCES users(id),
  inspector_id              INTEGER REFERENCES users(id),
  state                     TEXT NOT NULL,
  payment_deadline          TEXT,
  inspection_deadline       TEXT,
  inspection_window_minutes INTEGER NOT NULL DEFAULT 30,
  created_at                TEXT NOT NULL,
  updated_at                TEXT NOT NULL,
  version                   INTEGER NOT NULL DEFAULT 0
);

-- 托管流水：所有资金变动只经由此表，方向由 kind 决定
CREATE TABLE IF NOT EXISTS ledger_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id      INTEGER NOT NULL REFERENCES deals(id),
  kind         TEXT NOT NULL CHECK (kind IN ('HOLD','RELEASE','REFUND')),
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  from_account TEXT NOT NULL,
  to_account   TEXT NOT NULL,
  receipt_id   TEXT,
  note         TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_hold_per_deal
  ON ledger_events(deal_id) WHERE kind = 'HOLD';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_release_per_deal
  ON ledger_events(deal_id) WHERE kind = 'RELEASE';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_refund_per_deal
  ON ledger_events(deal_id) WHERE kind = 'REFUND';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_receipt
  ON ledger_events(receipt_id) WHERE receipt_id IS NOT NULL;

-- 付款、退款与放款互斥；已结算的交易不得再入托管
CREATE TRIGGER IF NOT EXISTS trg_no_release_after_refund
BEFORE INSERT ON ledger_events
WHEN NEW.kind = 'RELEASE'
     AND EXISTS (SELECT 1 FROM ledger_events WHERE deal_id = NEW.deal_id AND kind = 'REFUND')
BEGIN SELECT RAISE(ABORT, '退款已入账，禁止再放款'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_refund_after_release
BEFORE INSERT ON ledger_events
WHEN NEW.kind = 'REFUND'
     AND EXISTS (SELECT 1 FROM ledger_events WHERE deal_id = NEW.deal_id AND kind = 'RELEASE')
BEGIN SELECT RAISE(ABORT, '放款已入账，禁止再退款'); END;

CREATE TRIGGER IF NOT EXISTS trg_no_hold_after_settlement
BEFORE INSERT ON ledger_events
WHEN NEW.kind = 'HOLD'
     AND EXISTS (SELECT 1 FROM ledger_events WHERE deal_id = NEW.deal_id AND kind IN ('RELEASE','REFUND'))
BEGIN SELECT RAISE(ABORT, '交易已结算，禁止再入托管'); END;

-- 支付渠道回执（模拟适配器开具；PENDING = 已开具未送达，可能延迟）
CREATE TABLE IF NOT EXISTS receipts (
  id           TEXT PRIMARY KEY,
  deal_id      INTEGER NOT NULL REFERENCES deals(id),
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  payer        TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('PENDING','PROCESSED')),
  created_at   TEXT NOT NULL,
  processed_at TEXT
);

-- 验货清单：卖家交付时填写序列号、外观、配件
CREATE TABLE IF NOT EXISTS checklists (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id       INTEGER NOT NULL REFERENCES deals(id),
  filled_by     INTEGER NOT NULL REFERENCES users(id),
  serial_number TEXT NOT NULL,
  appearance    TEXT NOT NULL,
  accessories   TEXT NOT NULL,
  photos        TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    INTEGER NOT NULL REFERENCES deals(id),
  party      TEXT NOT NULL CHECK (party IN ('buyer','seller')),
  user_id    INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  UNIQUE (deal_id, party)
);

CREATE TABLE IF NOT EXISTS disputes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id       INTEGER NOT NULL REFERENCES deals(id),
  reason        TEXT NOT NULL,
  opened_by     INTEGER NOT NULL REFERENCES users(id),
  state         TEXT NOT NULL CHECK (state IN ('OPEN','ARBITRATING','RESOLVED')),
  arbitrator_id INTEGER REFERENCES users(id),
  verdict       TEXT CHECK (verdict IN ('RELEASE','REFUND')),
  verdict_note  TEXT,
  created_at    TEXT NOT NULL,
  resolved_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_dispute_per_deal
  ON disputes(deal_id) WHERE state != 'RESOLVED';

-- 争议证据：双方各自提交结论（MATCH/MISMATCH）与现场照片
CREATE TABLE IF NOT EXISTS evidence (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  dispute_id   INTEGER NOT NULL REFERENCES disputes(id),
  submitted_by INTEGER NOT NULL REFERENCES users(id),
  conclusion   TEXT CHECK (conclusion IN ('MATCH','MISMATCH')),
  text         TEXT NOT NULL DEFAULT '',
  photos       TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);

-- 验货窗口延期申请
CREATE TABLE IF NOT EXISTS extensions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id       INTEGER NOT NULL REFERENCES deals(id),
  requested_by  INTEGER NOT NULL REFERENCES users(id),
  extra_minutes INTEGER NOT NULL CHECK (extra_minutes > 0),
  status        TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED')),
  created_at    TEXT NOT NULL,
  decided_at    TEXT
);

-- 审计日志
CREATE TABLE IF NOT EXISTS deal_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    INTEGER NOT NULL REFERENCES deals(id),
  actor_id   INTEGER REFERENCES users(id),
  action     TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""


class Clock:
    """可注入的时钟：测试中可手动推进时间，线上用真实时间。"""

    def __init__(self, start=None):
        self._now = start or datetime.now()

    def __call__(self):
        return self._now

    def set(self, value):
        self._now = value

    def advance(self, **kwargs):
        self._now = self._now + timedelta(**kwargs)


class DB:
    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # 手动控制事务
        self.conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def memory(cls):
        return cls(":memory:")

    def init_schema(self):
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """写事务：检查与入账在同一事务内完成，避免并发下双重结算。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()
