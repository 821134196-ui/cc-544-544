"""领域核心测试：资金守恒、回执重放、验货时限、权限隔离与资金互斥。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from escrow.clock import Clock
from escrow.db import connect, init_db
from escrow.payments import MockPaymentGateway, PaymentError
from escrow import service as svc
from escrow.service import EscrowService


class EscrowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "test.db")
        init_db(self.conn)
        self.clock = Clock("2026-10-02T09:00:00+00:00")
        self.gateway = MockPaymentGateway(self.conn, self.clock)
        self.s = EscrowService(self.conn, self.clock, self.gateway,
                               photos_dir=self.dir / "photos")
        self._users()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _users(self):
        s = self.s
        s.create_user("seller", "卖家", ["seller"])
        s.create_user("buyer", "买家", ["buyer"])
        s.create_user("seller2", "卖家二", ["seller"])
        s.create_user("buyer2", "买家二", ["buyer"])
        s.create_user("insp", "验货人甲", ["inspector"])
        s.create_user("insp2", "验货人乙", ["inspector"])
        s.create_user("arb", "仲裁员（独立）", ["arbiter"])
        s.create_user("arb_involved", "仲裁员（兼验货人）", ["inspector", "arbiter"])

    def new_order(self, amount=100000, window=3600, inspector="insp",
                  seller="seller", buyer="buyer"):
        return self.s.create_order(
            seller_id=seller, buyer_id=buyer, inspector_id=inspector,
            title="测试物品", description="描述：九成新，配件齐全",
            amount=amount, window_sec=window)

    def agree_and_fund(self, oid, delay=30):
        """约定 → 发起付款 → 回执延迟 delay 秒后到达 → 托管确认。返回 hold 幂等键。"""
        self.s.agree(oid, "seller")
        self.s.agree(oid, "buyer")
        key = self.s.request_escrow(oid, "buyer")
        # 回执到达前：托管不能确认
        with self.assertRaises(PaymentError):
            self.s.confirm_by_receipt(self.gateway.get(key).receipt_no or "X")
        self.clock.advance(delay)
        self.gateway.settle(key)
        confirmed = self.s.confirm_by_receipt(self.gateway.get(key).receipt_no)
        self.assertTrue(confirmed)
        return key

    def deliver_inspect(self, oid, conclusion="accept", at=600, **kw):
        self.clock.advance(at)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="外观良好",
                       accessories="充电器", seller_conclusion="conforming",
                       photo_label="DELIVERY SN-1")
        self.clock.advance(kw.pop("advance_after", 120))
        if conclusion:
            self.s.inspect(oid, "buyer", conclusion,
                           photo_label="INSP PHOTO", **kw)

    # ---------- 资金守恒 ----------
    def test_conservation_empty_and_hold(self):
        # 任何资金流动前，账已平
        self.assertEqual(sum(self.s.ledger_balances().values()), 0)
        oid = self.new_order()
        self.agree_and_fund(oid)
        bals = self.s.ledger_balances()
        self.assertEqual(sum(bals.values()), 0)            # 有借必有贷，总额恒为 0
        self.assertEqual(bals["cash:gateway"], 100000)    # 网关在途
        self.assertEqual(self.s.escrow_total(), 100000)   # 冻结额等于托管额

    def test_conservation_after_release(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "accept")
        self.s.confirm_release(oid, "buyer")
        self.s.confirm_release(oid, "seller")
        bals = self.s.ledger_balances()
        self.assertEqual(sum(bals.values()), 0)
        self.assertEqual(bals.get(f"escrow:{oid}", 0), 0)  # 冻结账户已结平
        self.assertEqual(self.s.wallet_balance("seller"), 100000)
        self.assertEqual(self.s.escrow_total(), 0)
        self.assertEqual(self.s.get_order(oid)["status"], svc.RELEASED)

    def test_conservation_after_refund(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "reject")
        self.s.confirm_refund(oid, "seller")
        bals = self.s.ledger_balances()
        # 退款原路出系统：所有账户净值归零，总额仍为 0
        self.assertEqual(sum(bals.values()), 0)
        self.assertEqual(self.s.escrow_total(), 0)
        self.assertEqual(bals.get("cash:gateway", 0), 0)
        self.assertEqual(self.s.get_order(oid)["status"], svc.REFUNDED)

    def test_conservation_under_arbitration_both_rulings(self):
        for ruling, who_pays in (("release_to_seller", "wallet:seller"),
                                 ("refund_to_buyer", "cash:gateway")):
            oid = self.new_order(amount=50000)
            self.agree_and_fund(oid)
            self.deliver_inspect(oid, "reject")
            self.s.open_dispute(oid, "buyer", "opposite_conclusions")
            self.s.arbitrate(oid, "arb", ruling, "证据充分")
            self.assertEqual(sum(self.s.ledger_balances().values()), 0)
            self.assertEqual(self.s.escrow_total(), 0)

    def test_funds_frozen_during_dispute(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "reject")
        self.s.open_dispute(oid, "buyer", "item_mismatch")
        # 争议期间任何一方都不能推动放款/退款
        with self.assertRaises(svc.InvalidStateError):
            self.s.confirm_refund(oid, "seller")
        with self.assertRaises(svc.InvalidStateError):
            self.s.confirm_release(oid, "seller")
        self.assertEqual(self.s.escrow_total(), 100000)  # 钱仍冻着

    def test_hidden_defect_after_passing_still_freezes(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "accept")
        self.s.confirm_release(oid, "buyer")   # 仅买家确认，尚未放款
        self.s.open_dispute(oid, "buyer", "hidden_defect")
        self.assertEqual(self.s.get_order(oid)["status"], svc.DISPUTED)
        self.assertEqual(self.s.escrow_total(), 100000)
        self.s.arbitrate(oid, "arb", "refund_to_buyer", "隐藏故障成立")
        self.assertEqual(self.s.escrow_total(), 0)

    # ---------- 回执重放 ----------
    def test_receipt_replay_confirms_once(self):
        oid = self.new_order()
        self.s.agree(oid, "seller"); self.s.agree(oid, "buyer")
        key = self.s.request_escrow(oid, "buyer")
        # 请求重放：不产生第二笔 hold
        key2 = self.s.request_escrow(oid, "buyer")
        self.assertEqual(key, key2)
        self.assertEqual(
            len(self.s.list_movements(oid)), 1)

        self.clock.advance(10)
        self.gateway.settle(key)
        receipt = self.gateway.get(key).receipt_no
        self.assertTrue(self.s.confirm_by_receipt(receipt))
        # 同一回执重复回调：幂等，不新增流水、不重复记账
        for _ in range(3):
            self.assertFalse(self.s.confirm_by_receipt(receipt))
        mvs = self.s.list_movements(oid)
        self.assertEqual(len(mvs), 1)
        self.assertEqual(mvs[0]["receipt_no"], receipt)
        self.assertEqual(self.s.escrow_total(), 100000)  # 而非 200000
        # 每个 hold 只有一组（2 条）平衡分录
        n_entries = self.conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries le JOIN ledger_transactions lt "
            "ON lt.id = le.tx_id JOIN money_movements mm ON mm.id = lt.movement_id "
            "WHERE mm.order_id = ?", (oid,)).fetchone()["c"]
        self.assertEqual(n_entries, 2)

    def test_gateway_idempotency_different_amount_rejected(self):
        self.gateway.submit("hold:replay-x", 100)
        with self.assertRaises(PaymentError):
            self.gateway.submit("hold:replay-x", 200)
        with self.assertRaises(PaymentError):
            self.gateway.verify_receipt("RCPT-NOPE")

    # ---------- 付款 / 退款 / 放款互斥 ----------
    def test_terminal_movements_are_mutually_exclusive(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "accept")
        self.s.confirm_release(oid, "buyer")
        self.s.confirm_release(oid, "seller")   # 放款
        # 已放款后：不能退款、不能再放款、不能仲裁
        with self.assertRaises(svc.InvalidStateError):
            self.s.confirm_refund(oid, "seller")
        with self.assertRaises(svc.InvalidStateError):
            self.s.arbitrate(oid, "arb", "refund_to_buyer", "x")
        directions = {m["direction"] for m in self.s.list_movements(oid)}
        self.assertEqual(directions, {"hold", "release"})  # 恰好一进一出

    def test_cancel_only_before_escrow(self):
        oid = self.new_order()
        self.s.agree(oid, "seller"); self.s.agree(oid, "buyer")
        self.s.cancel(oid, "seller")
        self.assertEqual(self.s.get_order(oid)["status"], svc.CANCELLED)
        self.assertEqual(self.s.list_movements(oid), [])  # 无资金流动

        oid2 = self.new_order()
        self.agree_and_fund(oid2)
        with self.assertRaises(svc.InvalidStateError):
            self.s.cancel(oid2, "seller")  # 托管后不可撤销

    # ---------- 验货时限 ----------
    def test_inspection_after_window_rejected(self):
        oid = self.new_order(window=3600)
        self.agree_and_fund(oid)
        self.clock.advance(100)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        self.clock.advance(3601)  # 超过窗口
        self.assertEqual(self.s.window_remaining(oid), 0)
        with self.assertRaises(svc.WindowExpiredError):
            self.s.inspect(oid, "buyer", "accept")

    def test_extend_within_window_then_accept(self):
        oid = self.new_order(window=3600)
        self.agree_and_fund(oid)
        self.clock.advance(100)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        self.clock.advance(3500)  # 剩 100 秒
        self.s.extend_window(oid, "insp", 1800, "买家堵车，双方同意顺延")
        self.assertEqual(self.s.window_remaining(oid), 1900)
        self.clock.advance(1500)
        self.s.inspect(oid, "buyer", "accept")  # 原窗口外、延期窗口内，仍可验收
        self.assertEqual(self.s.get_order(oid)["status"], svc.INSPECTION_PASSED)

    def test_extend_after_window_rejected(self):
        oid = self.new_order(window=600)
        self.agree_and_fund(oid)
        self.clock.advance(50)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        self.clock.advance(601)
        with self.assertRaises(svc.WindowExpiredError):
            self.s.extend_window(oid, "insp", 600, "迟来的延期")

    def test_buyer_no_show_flow(self):
        oid = self.new_order(window=600)
        self.agree_and_fund(oid)
        self.clock.advance(50)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        # 窗口未结束不能标逾期
        with self.assertRaises(svc.DomainError):
            self.s.mark_buyer_no_show(oid, "seller")
        self.clock.advance(601)
        self.s.mark_buyer_no_show(oid, "seller")
        self.assertEqual(self.s.get_order(oid)["status"], svc.BUYER_NO_SHOW)
        # 逾期后钱仍冻结，可争议仲裁
        self.s.open_dispute(oid, "seller", "other")
        self.s.arbitrate(oid, "arb", "release_to_seller", "买家逾期不到，放款卖家")
        self.assertEqual(self.s.escrow_total(), 0)

    # ---------- 权限隔离 ----------
    def test_inspector_cannot_touch_funds(self):
        oid = self.new_order()
        self.s.agree(oid, "seller"); self.s.agree(oid, "buyer")
        # 验货人不能发起付款
        with self.assertRaises(svc.AuthError):
            self.s.request_escrow(oid, "insp")
        key = self.s.request_escrow(oid, "buyer")
        self.gateway.settle(key); self.s.poll_gateway()
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        self.s.inspect(oid, "buyer", "accept")
        # 验货人不能确认放款 / 退款 / 仲裁（触碰资金归属）
        with self.assertRaises(svc.AuthError):
            self.s.confirm_release(oid, "insp")
        with self.assertRaises(svc.AuthError):
            self.s.confirm_refund(oid, "insp")
        with self.assertRaises(svc.AuthError):
            self.s.arbitrate(oid, "insp", "release_to_seller", "越权")
        # 验货人能做的：延期与现场备注
        self.s.add_inspector_note(oid, "insp", "见证一致")
        # 托管金额全程未被验货人改动
        self.assertEqual(self.s.get_order(oid)["amount"], 100000)

    def test_extend_only_assigned_inspector(self):
        oid = self.new_order(inspector="insp", window=3600)
        self.agree_and_fund(oid)
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        # 另一名验货人、买家、卖家都不能延期
        with self.assertRaises(svc.AuthError):
            self.s.extend_window(oid, "insp2", 100, "越权")
        with self.assertRaises(svc.AuthError):
            self.s.extend_window(oid, "buyer", 100, "越权")
        with self.assertRaises(svc.AuthError):
            self.s.extend_window(oid, "seller", 100, "越权")
        self.s.extend_window(oid, "insp", 100, "指派验货人延期成功")

    def test_arbiter_conflict_of_interest(self):
        # arb_involved 是该单验货人 → 必须回避
        oid = self.new_order(inspector="arb_involved")
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "reject")
        self.s.open_dispute(oid, "buyer", "opposite_conclusions")
        with self.assertRaises(svc.AuthError):
            self.s.arbitrate(oid, "arb_involved", "release_to_seller", "本人参与，应回避")
        # 买卖双方即便挂了 arbiter 角色也不能仲裁自己的单
        with self.assertRaises(svc.AuthError):
            self.s.arbitrate(oid, "buyer", "refund_to_buyer", "x")
        # 独立仲裁员可以
        self.s.arbitrate(oid, "arb", "release_to_seller", "独立裁决")
        self.assertEqual(self.s.get_order(oid)["status"], svc.ARBITRATED_RELEASE)

    def test_party_identity_checks(self):
        oid = self.new_order()
        with self.assertRaises(svc.AuthError):
            self.s.agree(oid, "insp")           # 无关方不能确认约定
        self.s.agree(oid, "seller"); self.s.agree(oid, "buyer")
        with self.assertRaises(svc.AuthError):
            self.s.request_escrow(oid, "seller")  # 只有买家付款
        key = self.s.request_escrow(oid, "buyer")
        self.gateway.settle(key); self.s.poll_gateway()
        with self.assertRaises(svc.AuthError):
            self.s.deliver(oid, "buyer", serial_no="x", appearance="x",
                           accessories="x", seller_conclusion="conforming")
        self.s.deliver(oid, "seller", serial_no="SN-1", appearance="x",
                       accessories="y", seller_conclusion="conforming")
        with self.assertRaises(svc.AuthError):
            self.s.inspect(oid, "seller", "accept")

    def test_evidence_opposite_conclusions(self):
        oid = self.new_order()
        self.agree_and_fund(oid)
        self.deliver_inspect(oid, "reject")
        self.s.open_dispute(oid, "buyer", "opposite_conclusions")
        self.s.submit_evidence(oid, "seller", "conforming", "我方认为与描述一致",
                               photo_label="S EVIDENCE")
        self.s.submit_evidence(oid, "buyer", "defective", "现场确有划痕",
                               photo_label="B EVIDENCE")
        evs = self.s.list_evidence(oid)
        self.assertEqual({e["conclusion"] for e in evs}, {"conforming", "defective"})
        # 无关方不能提交证据
        with self.assertRaises(svc.AuthError):
            self.s.submit_evidence(oid, "seller2", "conforming", "路人甲",
                                   photo_label="X")


if __name__ == "__main__":
    unittest.main()
