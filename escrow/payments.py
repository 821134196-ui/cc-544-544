"""模拟支付适配器（MockPaymentGateway）。

行为：
- submit() 只创建「排队中」支付单，不立即给回执（模拟付款回执延迟）；
- settle() 由对账轮询/手动触发，支付成功并生成唯一回执号；
- verify_receipt() 按回执号查询，重复回执只返回同一笔，绝不形成两笔资金流。

回执号由幂等键确定性派生，重放 submit 同一幂等键返回同一支付单。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .clock import Clock


class PaymentError(Exception):
    pass


@dataclass
class GatewayPayment:
    idem_key: str
    amount: int
    status: str  # queued | paid
    receipt_no: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GatewayPayment":
        return cls(
            idem_key=row["idem_key"],
            amount=row["amount"],
            status=row["status"],
            receipt_no=row["receipt_no"],
        )


class MockPaymentGateway:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    @staticmethod
    def _receipt_for(idem_key: str) -> str:
        digest = hashlib.sha256(idem_key.encode()).hexdigest()[:16].upper()
        return f"RCPT-{digest}"

    def submit(self, idem_key: str, amount: int) -> GatewayPayment:
        """提交支付（幂等）。回执可能尚未生成——状态 queued 即延迟。"""
        if amount <= 0:
            raise PaymentError("金额必须为正数")
        row = self.conn.execute(
            "SELECT * FROM gateway_payments WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if row is not None:
            if row["amount"] != amount:
                raise PaymentError("同一幂等键使用了不同金额")
            return GatewayPayment.from_row(row)
        self.conn.execute(
            "INSERT INTO gateway_payments (idem_key, amount, status, created_at) "
            "VALUES (?, ?, 'queued', ?)",
            (idem_key, amount, self.clock.iso()),
        )
        self.conn.commit()
        return self.get(idem_key)

    def settle(self, idem_key: str) -> GatewayPayment:
        """网关侧确认付款（回执到达）。重复 settle 幂等。"""
        p = self.get(idem_key)
        if p.status == "paid":
            return p
        receipt = self._receipt_for(idem_key)
        self.conn.execute(
            "UPDATE gateway_payments SET status = 'paid', receipt_no = ?, paid_at = ? "
            "WHERE idem_key = ? AND status = 'queued'",
            (receipt, self.clock.iso(), idem_key),
        )
        self.conn.commit()
        return self.get(idem_key)

    def get(self, idem_key: str) -> GatewayPayment:
        row = self.conn.execute(
            "SELECT * FROM gateway_payments WHERE idem_key = ?", (idem_key,)
        ).fetchone()
        if row is None:
            raise PaymentError(f"支付单不存在: {idem_key}")
        return GatewayPayment.from_row(row)

    def verify_receipt(self, receipt_no: str) -> GatewayPayment:
        """按回执号核验。同一回执无论提交多少次都只对应同一笔。"""
        row = self.conn.execute(
            "SELECT * FROM gateway_payments WHERE receipt_no = ?", (receipt_no,)
        ).fetchone()
        if row is None:
            raise PaymentError(f"回执无效: {receipt_no}")
        return GatewayPayment.from_row(row)

    def poll_settle_pending(self) -> list[str]:
        """对账轮询：把所有 queued 支付置为 paid（模拟回执延迟后最终到达）。"""
        rows = self.conn.execute(
            "SELECT idem_key FROM gateway_payments WHERE status = 'queued'"
        ).fetchall()
        settled = [r["idem_key"] for r in rows]
        for key in settled:
            self.settle(key)
        return settled
