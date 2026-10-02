"""托管交易平台测试：资金守恒、回执重放、验货时限与权限隔离。

运行：python3 -m unittest discover -s tests -v
"""
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from escrow import domain as d
from escrow import ledger
from escrow.db import DB, Clock
from escrow.payments import MockPaymentAdapter
from escrow.service import EscrowService

T0 = datetime(2026, 10, 2, 12, 0, 0)
AMOUNT = 100_000  # ¥1,000.00


class Base(unittest.TestCase):
    def setUp(self):
        self.db = DB.memory()
        self.db.init_schema()
        self.clock = Clock(T0)
        self.svc = EscrowService(self.db, MockPaymentAdapter(self.db), now=self.clock)
        self.platform = self.svc.add_user("平台", "platform")
        self.buyer = self.svc.add_user("买家", "buyer")
        self.seller = self.svc.add_user("卖家", "seller")
        self.inspector = self.svc.add_user("验货人", "inspector")
        self.arbitrator = self.svc.add_user("仲裁员", "arbitrator")
        self._seq = 0

    def new_deal(self, amount=AMOUNT, window=30):
        self._seq += 1
        return self.svc.create_deal(
            self.platform, f"物品{self._seq}", "描述", amount,
            self.buyer, self.seller, self.inspector,
            window_minutes=window, code=f"T-{self._seq:03d}")

    def escrowed_deal(self, amount=AMOUNT, window=30):
        deal_id = self.new_deal(amount, window)
        self.svc.process_receipt(self.svc.pay(deal_id, self.buyer))
        return deal_id

    def inspecting_deal(self, amount=AMOUNT, window=30):
        deal_id = self.escrowed_deal(amount, window)
        self.svc.deliver(deal_id, self.seller, "SN-1", "外观良好", "配件齐全")
        return deal_id

    def state(self, deal_id):
        return self.db.one("SELECT state FROM deals WHERE id=?", (deal_id,))["state"]

    def frozen(self, deal_id):
        return ledger.frozen(self.db.conn, deal_id)

    def assertConserved(self):
        report = self.svc.conservation_report()
        self.assertTrue(report["ok"], report["problems"])
        return report


# ---------------------------------------------------------------- 资金守恒
class FundConservationTest(Base):
    def test_mixed_flows_conserve_funds(self):
        # 1) 双方确认 → 放款
        d1 = self.inspecting_deal()
        self.svc.confirm(d1, self.buyer)
        self.svc.confirm(d1, self.seller)
        # 2) 争议 → 仲裁退款
        d2 = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(d2, self.buyer, d.REASON_ITEM_MISMATCH)
        self.svc.assign_arbitrator(disp, self.platform, self.arbitrator)
        self.svc.rule(disp, self.arbitrator, d.VERDICT_REFUND)
        # 3) 托管后卖家撤销 → 退款
        d3 = self.escrowed_deal()
        self.svc.cancel(d3, self.seller)
        # 4) 仍冻结在托管中
        d4 = self.escrowed_deal()
        # 5) 争议中，资金冻结
        d5 = self.inspecting_deal()
        self.svc.reject_and_dispute(d5, self.buyer, d.REASON_HIDDEN_FAULT)

        report = self.assertConserved()
        totals = report["totals"]
        self.assertEqual(totals["HOLD"], 5 * AMOUNT)
        self.assertEqual(totals["RELEASE"], AMOUNT)
        self.assertEqual(totals["REFUND"], 2 * AMOUNT)
        self.assertEqual(report["frozen_total"], 2 * AMOUNT)
        self.assertEqual(self.frozen(d1), 0)
        self.assertEqual(self.frozen(d4), AMOUNT)
        self.assertEqual(self.frozen(d5), AMOUNT)

    def test_late_receipt_after_cancel_auto_refunds(self):
        """回执迟到但交易已取消：先入托管再原路退回，资金仍守恒。"""
        deal_id = self.new_deal()
        receipt_id = self.svc.pay(deal_id, self.buyer)
        self.clock.advance(minutes=20)
        self.assertEqual(self.svc.sweep_payment_timeouts(), [deal_id])
        self.assertEqual(self.state(deal_id), d.STATE_PAYMENT_DELAYED)
        self.svc.cancel(deal_id, self.seller)
        self.assertEqual(self.state(deal_id), d.STATE_CANCELLED)
        # 迟到的回执到达
        self.assertEqual(self.svc.process_receipt(receipt_id), "processed")
        sums = ledger.sums(self.db.conn, deal_id)
        self.assertEqual(sums["HOLD"], AMOUNT)
        self.assertEqual(sums["REFUND"], AMOUNT)
        self.assertEqual(self.frozen(deal_id), 0)
        self.assertConserved()


# ---------------------------------------------------------------- 回执重放
class ReceiptReplayTest(Base):
    def test_duplicate_receipt_does_not_double_hold(self):
        """重复回执不能形成两笔资金流。"""
        deal_id = self.new_deal()
        receipt_id = self.svc.pay(deal_id, self.buyer)
        self.assertEqual(self.svc.process_receipt(receipt_id), "processed")
        self.assertEqual(self.svc.process_receipt(receipt_id), "duplicate")
        self.assertEqual(self.svc.process_receipt(receipt_id), "duplicate")
        holds = self.db.q(
            "SELECT * FROM ledger_events WHERE deal_id=? AND kind='HOLD'", (deal_id,))
        self.assertEqual(len(holds), 1)
        self.assertEqual(self.frozen(deal_id), AMOUNT)
        self.assertEqual(self.state(deal_id), d.STATE_ESCROWED)
        self.assertConserved()

    def test_unknown_receipt_rejected(self):
        with self.assertRaises(d.NotFound):
            self.svc.process_receipt("RCPT-NOPE-001")

    def test_tampered_amount_rejected(self):
        """回执金额与托管金额不一致时拒绝入账。"""
        deal_id = self.new_deal()
        self.svc.pay(deal_id, self.buyer)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO receipts (id, deal_id, amount_cents, payer, status, created_at)"
                " VALUES ('RCPT-FAKE', ?, ?, '买家', 'PENDING', ?)",
                (deal_id, AMOUNT + 1, T0.isoformat()))
        with self.assertRaises(d.AmountMismatch):
            self.svc.process_receipt("RCPT-FAKE")
        self.assertEqual(self.frozen(deal_id), 0)
        self.assertConserved()

    def test_second_receipt_for_escrowed_deal_rejected(self):
        """已托管的交易不再接受新的支付回执（防双重托管）。"""
        deal_id = self.escrowed_deal()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO receipts (id, deal_id, amount_cents, payer, status, created_at)"
                " VALUES ('RCPT-EXTRA', ?, ?, '买家', 'PENDING', ?)",
                (deal_id, AMOUNT, T0.isoformat()))
        with self.assertRaises(d.InvalidState):
            self.svc.process_receipt("RCPT-EXTRA")
        holds = self.db.q(
            "SELECT * FROM ledger_events WHERE deal_id=? AND kind='HOLD'", (deal_id,))
        self.assertEqual(len(holds), 1)
        self.assertConserved()


# ---------------------------------------------------------------- 互斥
class MutualExclusionTest(Base):
    def test_release_and_refund_are_mutually_exclusive(self):
        """放款与退款互斥：服务层与数据库触发器双重保证。"""
        deal_id = self.inspecting_deal()
        self.svc.confirm(deal_id, self.buyer)
        self.svc.confirm(deal_id, self.seller)
        self.assertEqual(self.state(deal_id), d.STATE_COMPLETED)
        # 已放款后任何退款路径都被状态机拒绝
        with self.assertRaises(d.InvalidState):
            self.svc.cancel(deal_id, self.seller)
        with self.assertRaises(d.InvalidState):
            self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)
        # 数据库触发器兜底：直接写入也被拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as conn:
                ledger.post(conn, deal_id, ledger.REFUND, AMOUNT, T0)
        self.assertConserved()

    def test_refund_then_release_blocked_by_trigger(self):
        deal_id = self.escrowed_deal()
        self.svc.cancel(deal_id, self.seller)  # 退款并取消
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as conn:
                ledger.post(conn, deal_id, ledger.RELEASE, AMOUNT, T0)
        self.assertConserved()

    def test_double_payment_rejected(self):
        deal_id = self.new_deal()
        self.svc.pay(deal_id, self.buyer)
        with self.assertRaises(d.InvalidState):
            self.svc.pay(deal_id, self.buyer)

    def test_double_hold_blocked_by_unique_index(self):
        deal_id = self.escrowed_deal()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.tx() as conn:
                ledger.post(conn, deal_id, ledger.HOLD, AMOUNT, T0,
                            receipt_id="RCPT-DUP-2")
        self.assertConserved()


# ---------------------------------------------------------------- 验货时限
class InspectionWindowTest(Base):
    def test_confirm_after_deadline_rejected(self):
        deal_id = self.inspecting_deal(window=30)
        self.clock.advance(minutes=31)
        with self.assertRaises(d.InspectionClosed):
            self.svc.confirm(deal_id, self.buyer)

    def test_extension_restores_window(self):
        deal_id = self.inspecting_deal(window=30)
        ext = self.svc.request_extension(deal_id, self.buyer, 40)
        self.svc.decide_extension(deal_id, self.seller, ext, approve=True)
        self.clock.advance(minutes=45)  # 超过原窗口，但在延长后的窗口内
        self.svc.confirm(deal_id, self.buyer)
        self.svc.confirm(deal_id, self.seller)
        self.assertEqual(self.state(deal_id), d.STATE_COMPLETED)
        self.assertConserved()

    def test_rejected_extension_keeps_deadline(self):
        deal_id = self.inspecting_deal(window=30)
        ext = self.svc.request_extension(deal_id, self.buyer, 40)
        self.svc.decide_extension(deal_id, self.seller, ext, approve=False)
        self.clock.advance(minutes=31)
        with self.assertRaises(d.InspectionClosed):
            self.svc.confirm(deal_id, self.buyer)

    def test_no_show_claim_requires_deadline_passed(self):
        deal_id = self.inspecting_deal(window=30)
        with self.assertRaises(d.InvalidState):
            self.svc.claim_no_show(deal_id, self.seller)
        self.clock.advance(minutes=31)
        dispute_id = self.svc.claim_no_show(deal_id, self.seller)
        disp = self.db.one("SELECT * FROM disputes WHERE id=?", (dispute_id,))
        self.assertEqual(disp["reason"], d.REASON_BUYER_NO_SHOW)
        self.assertEqual(self.state(deal_id), d.STATE_DISPUTED)
        self.assertEqual(self.frozen(deal_id), AMOUNT)  # 争议期间资金冻结
        self.assertConserved()

    def test_reject_after_deadline_rejected(self):
        deal_id = self.inspecting_deal(window=30)
        self.clock.advance(minutes=31)
        with self.assertRaises(d.InspectionClosed):
            self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)


# ---------------------------------------------------------------- 权限隔离
class PermissionIsolationTest(Base):
    def test_inspector_cannot_modify_escrow_amount(self):
        """验货人不能直接修改托管金额。"""
        deal_id = self.new_deal()
        with self.assertRaises(d.PermissionDenied):
            self.svc.adjust_escrow_amount(self.inspector, deal_id, 1)
        with self.assertRaises(d.PermissionDenied):
            self.svc.adjust_escrow_amount(self.buyer, deal_id, 1)
        with self.assertRaises(d.PermissionDenied):
            self.svc.adjust_escrow_amount(self.seller, deal_id, 1)
        # 平台在付款前可以调整
        self.svc.adjust_escrow_amount(self.platform, deal_id, 50_000)
        row = self.db.one("SELECT amount_cents FROM deals WHERE id=?", (deal_id,))
        self.assertEqual(row["amount_cents"], 50_000)

    def test_amount_locked_after_payment(self):
        deal_id = self.new_deal()
        self.svc.pay(deal_id, self.buyer)
        with self.assertRaises(d.InvalidState):
            self.svc.adjust_escrow_amount(self.platform, deal_id, 1)

    def test_arbitrator_must_not_handle_own_deal(self):
        """仲裁员不得处理自己参与的交易。"""
        deal_id = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)
        for participant in (self.buyer, self.seller, self.inspector):
            with self.assertRaises(d.PermissionDenied):
                self.svc.assign_arbitrator(disp, self.platform, participant)
        # 非参与者可以；非仲裁员角色也不行
        with self.assertRaises(d.PermissionDenied):
            self.svc.assign_arbitrator(disp, self.platform, self.platform)
        self.svc.assign_arbitrator(disp, self.platform, self.arbitrator)

    def test_only_assigned_arbitrator_can_rule(self):
        deal_id = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)
        other = self.svc.add_user("另一位仲裁员", "arbitrator")
        self.svc.assign_arbitrator(disp, self.platform, self.arbitrator)
        with self.assertRaises(d.PermissionDenied):
            self.svc.rule(disp, other, d.VERDICT_REFUND)
        with self.assertRaises(d.PermissionDenied):
            self.svc.rule(disp, self.platform, d.VERDICT_REFUND)

    def test_strangers_cannot_confirm_or_dispute(self):
        deal_id = self.inspecting_deal()
        with self.assertRaises(d.PermissionDenied):
            self.svc.confirm(deal_id, self.inspector)
        with self.assertRaises(d.PermissionDenied):
            self.svc.reject_and_dispute(deal_id, self.inspector, d.REASON_ITEM_MISMATCH)
        with self.assertRaises(d.PermissionDenied):
            self.svc.pay(deal_id, self.seller)

    def test_evidence_only_from_participants(self):
        deal_id = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)
        with self.assertRaises(d.PermissionDenied):
            self.svc.submit_evidence(disp, self.arbitrator, text="仲裁员不能举证")
        self.svc.submit_evidence(disp, self.inspector, text="验货人现场核实",
                                 conclusion=d.CONCLUSION_MISMATCH)


# ---------------------------------------------------------------- 业务流程
class DisputeFlowTest(Base):
    def test_hidden_fault_after_confirm_freezes_funds(self):
        """验货后发现隐藏故障：买家已确认仍可申诉，资金保持冻结。"""
        deal_id = self.inspecting_deal()
        self.svc.confirm(deal_id, self.buyer)
        disp = self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_HIDDEN_FAULT,
                                           text="回家后发现无法开机")
        self.assertEqual(self.state(deal_id), d.STATE_DISPUTED)
        self.assertEqual(self.frozen(deal_id), AMOUNT)
        self.svc.assign_arbitrator(disp, self.platform, self.arbitrator)
        self.svc.rule(disp, self.arbitrator, d.VERDICT_REFUND, note="故障属实")
        self.assertEqual(self.state(deal_id), d.STATE_REFUNDED)
        self.assertEqual(self.frozen(deal_id), 0)
        self.assertConserved()

    def test_confirmed_buyer_cannot_reject_for_other_reasons(self):
        deal_id = self.inspecting_deal()
        self.svc.confirm(deal_id, self.buyer)
        with self.assertRaises(d.InvalidState):
            self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)

    def test_conflicting_conclusions_auto_flagged(self):
        """双方各自提交相反结论 → 争议标记为「双方结论相反」。"""
        deal_id = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(
            deal_id, self.buyer, d.REASON_ITEM_MISMATCH,
            conclusion=d.CONCLUSION_MISMATCH)
        self.svc.submit_evidence(disp, self.seller, text="交付时完好",
                                 conclusion=d.CONCLUSION_MATCH)
        row = self.db.one("SELECT reason FROM disputes WHERE id=?", (disp,))
        self.assertEqual(row["reason"], d.REASON_CONFLICTING)

    def test_arbitrated_release_completes_deal(self):
        deal_id = self.inspecting_deal()
        disp = self.svc.reject_and_dispute(deal_id, self.buyer, d.REASON_ITEM_MISMATCH)
        self.svc.assign_arbitrator(disp, self.platform, self.arbitrator)
        self.svc.rule(disp, self.arbitrator, d.VERDICT_RELEASE, note="证据不足")
        self.assertEqual(self.state(deal_id), d.STATE_COMPLETED)
        sums = ledger.sums(self.db.conn, deal_id)
        self.assertEqual(sums["RELEASE"], AMOUNT)
        self.assertEqual(sums["REFUND"], 0)
        self.assertConserved()

    def test_payment_delay_state_and_recovery(self):
        """付款回执延迟：超时标记，回执补发后正常入托管。"""
        deal_id = self.new_deal()
        receipt_id = self.svc.pay(deal_id, self.buyer)
        self.clock.advance(minutes=20)
        self.assertEqual(self.svc.sweep_payment_timeouts(), [deal_id])
        self.assertEqual(self.state(deal_id), d.STATE_PAYMENT_DELAYED)
        self.assertEqual(self.svc.process_receipt(receipt_id), "processed")
        self.assertEqual(self.state(deal_id), d.STATE_ESCROWED)
        self.assertConserved()


# ---------------------------------------------------------------- Web 冒烟
class WebSmokeTest(unittest.TestCase):
    def setUp(self):
        import seed as seed_module
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB.memory()
        self.db.init_schema()
        seed_module.seed_if_empty(self.db, self.tmp.name)
        from escrow.web import App
        self.app = App(self.db, EscrowService(self.db, MockPaymentAdapter(self.db)),
                       self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, path):
        status_headers = {}

        def start_response(status, headers):
            status_headers["status"] = status

        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path.split("?")[0],
                   "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
                   "wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": "0"}
        body = b"".join(self.app(environ, start_response)).decode("utf-8")
        return status_headers["status"], body

    def test_home_lists_deals_and_conservation(self):
        status, body = self.get("/")
        self.assertEqual(status, "200 OK")
        self.assertIn("托管", body)
        self.assertIn("资金守恒校验通过", body)
        self.assertIn("D-1002", body)

    def test_deal_page_shows_escrow_countdown_confirmations(self):
        status, body = self.get("/deal?id=1")
        self.assertEqual(status, "200 OK")
        self.assertIn("托管金额", body)
        self.assertIn("剩余验货时间", body)
        self.assertIn("买家确认", body)
        self.assertIn("卖家确认", body)

    def test_dispute_page_shows_description_and_site_photos(self):
        dispute_deal = self.db.one(
            "SELECT deal_id FROM disputes ORDER BY id LIMIT 1")["deal_id"]
        status, body = self.get(f"/dispute?id={dispute_deal}")
        self.assertEqual(status, "200 OK")
        self.assertIn("商品描述", body)
        self.assertIn("现场照片", body)
        self.assertIn("双方结论相反", body)

    def test_photo_served(self):
        status, body = self.get("/photos/d1002-desc-1.svg")
        self.assertEqual(status, "200 OK")
        self.assertIn("<svg", body)


if __name__ == "__main__":
    unittest.main()
