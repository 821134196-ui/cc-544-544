"""模拟支付渠道适配器。

语义：
- issue() 开具付款回执（PENDING），代表买家已在渠道侧完成支付；
- 回执由渠道异步回调送达（EscrowService.process_receipt），可能延迟；
- 渠道可能重复推送同一回执，处理方必须幂等。
"""


class MockPaymentAdapter:
    def __init__(self, db):
        self.db = db
        self._seq = 0

    def issue(self, conn, deal, payer_name, now):
        """在调用方事务内开具回执，返回回执号。回执等待渠道回调送达。"""
        self._seq += 1
        receipt_id = f"RCPT-{deal['code']}-{self._seq:03d}"
        conn.execute(
            "INSERT INTO receipts (id, deal_id, amount_cents, payer, status, created_at)"
            " VALUES (?,?,?,?,'PENDING',?)",
            (receipt_id, deal["id"], deal["amount_cents"], payer_name,
             now().isoformat(timespec="seconds")),
        )
        return receipt_id

    def get(self, receipt_id):
        return self.db.one("SELECT * FROM receipts WHERE id=?", (receipt_id,))

    def pending(self):
        return self.db.q("SELECT * FROM receipts WHERE status='PENDING' ORDER BY id")
