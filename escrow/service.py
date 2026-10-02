"""业务编排：交易约定、托管、验货、争议、仲裁与资金释放。

所有写操作都在单个事务内完成「状态检查 -> 状态迁移 -> 入账」，
配合 db.py 中的唯一索引与触发器，保证：
- 付款、退款与放款互斥；
- 重复回执不会形成第二笔资金流；
- 验货人不能修改托管金额，仲裁员不得处理自己参与的交易。
"""
import sqlite3
from datetime import datetime, timedelta

from . import domain as d
from . import ledger


class EscrowService:
    def __init__(self, db, adapter, now=None):
        self.db = db
        self.adapter = adapter
        self.now = now or datetime.now

    # ------------------------------------------------------------ 基础工具
    def add_user(self, name, role):
        with self.db.tx() as conn:
            cur = conn.execute("INSERT INTO users (name, role) VALUES (?,?)", (name, role))
            return cur.lastrowid

    def _user(self, conn, user_id):
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise d.NotFound(f"用户 {user_id} 不存在")
        return row

    def _deal(self, conn, deal_id):
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            raise d.NotFound(f"交易单 {deal_id} 不存在")
        return row

    def _dispute(self, conn, dispute_id):
        row = conn.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        if not row:
            raise d.NotFound(f"争议单 {dispute_id} 不存在")
        return row

    def _event(self, conn, deal_id, actor_id, action, detail=""):
        conn.execute(
            "INSERT INTO deal_events (deal_id, actor_id, action, detail, created_at)"
            " VALUES (?,?,?,?,?)",
            (deal_id, actor_id, action, detail, self.now().isoformat(timespec="seconds")),
        )

    def _touch(self, conn, deal_id):
        conn.execute(
            "UPDATE deals SET updated_at=?, version=version+1 WHERE id=?",
            (self.now().isoformat(timespec="seconds"), deal_id),
        )

    def _set_state(self, conn, deal_id, state):
        conn.execute("UPDATE deals SET state=? WHERE id=?", (state, deal_id))
        self._touch(conn, deal_id)

    @staticmethod
    def _participants(deal):
        ids = {deal["buyer_id"], deal["seller_id"]}
        if deal["inspector_id"]:
            ids.add(deal["inspector_id"])
        return ids

    @staticmethod
    def _party_of(deal, user_id):
        if user_id == deal["buyer_id"]:
            return "buyer"
        if user_id == deal["seller_id"]:
            return "seller"
        return None

    # ------------------------------------------------------------ 交易约定
    def create_deal(self, actor_id, title, description, amount_cents,
                    buyer_id, seller_id, inspector_id=None,
                    window_minutes=30, code=None, photos=()):
        """买卖双方约定当面验货后，平台（或卖家）生成交易单与托管金额。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            if actor["role"] not in ("platform", "seller"):
                raise d.PermissionDenied("只有平台或卖家可以生成交易单")
            buyer = self._user(conn, buyer_id)
            seller = self._user(conn, seller_id)
            if buyer["role"] != "buyer" or seller["role"] != "seller":
                raise d.PermissionDenied("交易双方角色不正确")
            if inspector_id is not None:
                inspector = self._user(conn, inspector_id)
                if inspector["role"] != "inspector":
                    raise d.PermissionDenied("指定的验货人角色不正确")
            if amount_cents <= 0:
                raise d.DomainError("托管金额必须为正")
            code = code or f"D-{int(self.now().timestamp())}"
            now_s = self.now().isoformat(timespec="seconds")
            pay_deadline = (self.now() + timedelta(minutes=d.PAYMENT_WINDOW_MINUTES))
            cur = conn.execute(
                """INSERT INTO deals
                     (code, title, description, photos, amount_cents,
                      buyer_id, seller_id, inspector_id, state,
                      payment_deadline, inspection_window_minutes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, title, description, ",".join(photos), amount_cents,
                 buyer_id, seller_id, inspector_id, d.STATE_AGREED,
                 pay_deadline.isoformat(timespec="seconds"),
                 window_minutes, now_s, now_s),
            )
            deal_id = cur.lastrowid
            self._event(conn, deal_id, actor_id, "DEAL_AGREED",
                        f"双方约定当面验货，平台生成交易单，托管金额 {amount_cents / 100:.2f} 元")
            return deal_id

    def adjust_escrow_amount(self, actor_id, deal_id, new_amount_cents):
        """仅平台可在买家付款前调整托管金额；验货人等任何其他角色一律无权。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            if actor["role"] != "platform":
                raise d.PermissionDenied(
                    f"{d.ROLE_LABELS[actor['role']]}无权修改托管金额")
            deal = self._deal(conn, deal_id)
            if deal["state"] != d.STATE_AGREED:
                raise d.InvalidState("付款后托管金额不可再修改")
            if new_amount_cents <= 0:
                raise d.DomainError("托管金额必须为正")
            conn.execute("UPDATE deals SET amount_cents=? WHERE id=?",
                         (new_amount_cents, deal_id))
            self._touch(conn, deal_id)
            self._event(conn, deal_id, actor_id, "AMOUNT_ADJUSTED",
                        f"托管金额调整为 {new_amount_cents / 100:.2f} 元")

    # ------------------------------------------------------------ 付款与回执
    def pay(self, deal_id, buyer_id):
        """买家付款：开具支付回执，等待渠道回调（回执可能延迟）。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            buyer = self._user(conn, buyer_id)
            if buyer_id != deal["buyer_id"]:
                raise d.PermissionDenied("只有买家本人可以付款")
            if deal["state"] != d.STATE_AGREED:
                raise d.InvalidState("当前状态不可付款")
            receipt_id = self.adapter.issue(conn, deal, buyer["name"], self.now)
            self._set_state(conn, deal_id, d.STATE_FUNDING)
            self._event(conn, deal_id, buyer_id, "PAYMENT_STARTED",
                        f"买家付款 {deal['amount_cents'] / 100:.2f} 元，等待回执 {receipt_id}")
            return receipt_id

    def process_receipt(self, receipt_id):
        """支付回执回调（幂等）：重复回执不会形成第二笔资金流。"""
        with self.db.tx() as conn:
            rcpt = conn.execute("SELECT * FROM receipts WHERE id=?",
                                (receipt_id,)).fetchone()
            if not rcpt:
                raise d.NotFound(f"未知回执 {receipt_id}")
            if rcpt["status"] == "PROCESSED":
                return "duplicate"  # 回执重放：直接忽略
            deal = self._deal(conn, rcpt["deal_id"])
            if rcpt["amount_cents"] != deal["amount_cents"]:
                raise d.AmountMismatch("回执金额与托管金额不一致")
            if deal["state"] in (d.STATE_FUNDING, d.STATE_PAYMENT_DELAYED):
                ledger.post(conn, deal["id"], ledger.HOLD, deal["amount_cents"],
                            self.now(), receipt_id=receipt_id, note="买家付款进入托管")
                self._set_state(conn, deal["id"], d.STATE_ESCROWED)
                self._event(conn, deal["id"], None, "ESCROW_HELD",
                            f"回执 {receipt_id} 确认，资金进入托管")
            elif deal["state"] == d.STATE_CANCELLED:
                # 回执迟到但交易已取消：钱已付出，先入托管再原路退回，保持守恒
                ledger.post(conn, deal["id"], ledger.HOLD, deal["amount_cents"],
                            self.now(), receipt_id=receipt_id, note="迟到回执入托管")
                ledger.post(conn, deal["id"], ledger.REFUND, deal["amount_cents"],
                            self.now(), note="交易已取消，迟到款项原路退回")
                self._event(conn, deal["id"], None, "LATE_RECEIPT_REFUNDED",
                            f"迟到回执 {receipt_id} 已自动退款")
            else:
                raise d.InvalidState(f"状态 {d.STATE_LABELS[deal['state']]} 不接受支付回执")
            conn.execute(
                "UPDATE receipts SET status='PROCESSED', processed_at=? WHERE id=?",
                (self.now().isoformat(timespec="seconds"), receipt_id),
            )
            return "processed"

    def sweep_payment_timeouts(self):
        """扫描付款超时：回执未按时到达的交易记为「付款回执延迟」。"""
        marked = []
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM deals WHERE state=? AND payment_deadline < ?",
                (d.STATE_FUNDING, self.now().isoformat(timespec="seconds")),
            ).fetchall()
            for deal in rows:
                self._set_state(conn, deal["id"], d.STATE_PAYMENT_DELAYED)
                self._event(conn, deal["id"], None, "PAYMENT_DELAY",
                            "付款回执延迟，等待渠道补发")
                marked.append(deal["id"])
        return marked

    def cancel(self, deal_id, seller_id):
        """卖家撤销：未托管直接取消；已托管则先退款再取消。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            if seller_id != deal["seller_id"]:
                raise d.PermissionDenied("只有卖家本人可以撤销交易")
            if deal["state"] in (d.STATE_AGREED, d.STATE_FUNDING, d.STATE_PAYMENT_DELAYED):
                self._set_state(conn, deal_id, d.STATE_CANCELLED)
                self._event(conn, deal_id, seller_id, "SELLER_CANCEL",
                            "卖家撤销交易，未发生资金托管")
            elif deal["state"] == d.STATE_ESCROWED:
                ledger.post(conn, deal_id, ledger.REFUND, deal["amount_cents"],
                            self.now(), note="卖家撤销，托管资金退回买家")
                self._set_state(conn, deal_id, d.STATE_CANCELLED)
                self._event(conn, deal_id, seller_id, "SELLER_CANCEL",
                            "卖家撤销交易，托管资金已退回买家")
            else:
                raise d.InvalidState("当前状态不可撤销")

    # ------------------------------------------------------------ 交付与验货
    def deliver(self, deal_id, seller_id, serial_number, appearance, accessories,
                photos=()):
        """卖家交付物品并填写验货清单（序列号/外观/配件），验货窗口开启。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            if seller_id != deal["seller_id"]:
                raise d.PermissionDenied("只有卖家本人可以交付")
            if deal["state"] != d.STATE_ESCROWED:
                raise d.InvalidState("资金未托管，不能交付")
            if not serial_number.strip():
                raise d.DomainError("必须填写序列号")
            deadline = self.now() + timedelta(minutes=deal["inspection_window_minutes"])
            conn.execute(
                """INSERT INTO checklists
                     (deal_id, filled_by, serial_number, appearance, accessories, photos, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (deal_id, seller_id, serial_number, appearance, accessories,
                 ",".join(photos), self.now().isoformat(timespec="seconds")),
            )
            conn.execute("UPDATE deals SET state=?, inspection_deadline=? WHERE id=?",
                         (d.STATE_INSPECTING, deadline.isoformat(timespec="seconds"), deal_id))
            self._touch(conn, deal_id)
            self._event(conn, deal_id, seller_id, "DELIVERED",
                        f"卖家交付物品，序列号 {serial_number}，"
                        f"验货窗口 {deal['inspection_window_minutes']} 分钟")

    def request_extension(self, deal_id, buyer_id, extra_minutes):
        """买家申请延长验货窗口。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            if buyer_id != deal["buyer_id"]:
                raise d.PermissionDenied("只有买家可以申请延期")
            if deal["state"] != d.STATE_INSPECTING:
                raise d.InvalidState("仅验货中可申请延期")
            if not 1 <= extra_minutes <= 240:
                raise d.DomainError("延期时长需在 1-240 分钟之间")
            pending = conn.execute(
                "SELECT 1 FROM extensions WHERE deal_id=? AND status='PENDING'",
                (deal_id,)).fetchone()
            if pending:
                raise d.InvalidState("已有待审批的延期申请")
            cur = conn.execute(
                "INSERT INTO extensions (deal_id, requested_by, extra_minutes, status, created_at)"
                " VALUES (?,?,?,'PENDING',?)",
                (deal_id, buyer_id, extra_minutes, self.now().isoformat(timespec="seconds")),
            )
            self._event(conn, deal_id, buyer_id, "EXTENSION_REQUESTED",
                        f"买家申请延期 {extra_minutes} 分钟")
            return cur.lastrowid

    def decide_extension(self, deal_id, seller_id, extension_id, approve):
        """卖家审批延期；同意后验货截止时间顺延。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            if seller_id != deal["seller_id"]:
                raise d.PermissionDenied("只有卖家可以审批延期")
            ext = conn.execute("SELECT * FROM extensions WHERE id=? AND deal_id=?",
                               (extension_id, deal_id)).fetchone()
            if not ext:
                raise d.NotFound("延期申请不存在")
            if ext["status"] != "PENDING":
                raise d.InvalidState("延期申请已处理")
            now_s = self.now().isoformat(timespec="seconds")
            if approve:
                if deal["state"] != d.STATE_INSPECTING:
                    raise d.InvalidState("交易不在验货中")
                base = datetime.fromisoformat(deal["inspection_deadline"])
                new_deadline = (base + timedelta(minutes=ext["extra_minutes"]))
                conn.execute("UPDATE deals SET inspection_deadline=? WHERE id=?",
                             (new_deadline.isoformat(timespec="seconds"), deal_id))
                conn.execute("UPDATE extensions SET status='APPROVED', decided_at=? WHERE id=?",
                             (now_s, extension_id))
                self._event(conn, deal_id, seller_id, "EXTENSION_APPROVED",
                            f"卖家同意延期 {ext['extra_minutes']} 分钟")
            else:
                conn.execute("UPDATE extensions SET status='REJECTED', decided_at=? WHERE id=?",
                             (now_s, extension_id))
                self._event(conn, deal_id, seller_id, "EXTENSION_REJECTED", "卖家拒绝延期")
            self._touch(conn, deal_id)

    def confirm(self, deal_id, user_id):
        """买卖双方各自确认验货一致；双方确认一致后释放款项。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            party = self._party_of(deal, user_id)
            if party is None:
                raise d.PermissionDenied("只有交易双方可以确认")
            if deal["state"] != d.STATE_INSPECTING:
                raise d.InvalidState("当前状态不可确认")
            if party == "buyer":
                deadline = datetime.fromisoformat(deal["inspection_deadline"])
                if self.now() > deadline:
                    raise d.InspectionClosed("验货窗口已关闭，买家逾期未确认")
            try:
                conn.execute(
                    "INSERT INTO confirmations (deal_id, party, user_id, created_at)"
                    " VALUES (?,?,?,?)",
                    (deal_id, party, user_id, self.now().isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError:
                raise d.InvalidState("该方已确认过") from None
            label = "买家" if party == "buyer" else "卖家"
            self._event(conn, deal_id, user_id, "CONFIRM", f"{label}确认验货一致")
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM confirmations WHERE deal_id=?",
                (deal_id,)).fetchone()["c"]
            if count == 2:
                ledger.post(conn, deal_id, ledger.RELEASE, deal["amount_cents"],
                            self.now(), note="双方确认一致，放款给卖家")
                self._set_state(conn, deal_id, d.STATE_COMPLETED)
                self._event(conn, deal_id, None, "RELEASED",
                            f"双方确认一致，{deal['amount_cents'] / 100:.2f} 元已放款给卖家")
            else:
                self._touch(conn, deal_id)

    # ------------------------------------------------------------ 争议
    def reject_and_dispute(self, deal_id, user_id, reason, text="",
                           photos=(), conclusion=d.CONCLUSION_MISMATCH):
        """买家拒收或任一方提起争议：交易进入争议，资金保持冻结。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            party = self._party_of(deal, user_id)
            if party is None:
                raise d.PermissionDenied("只有交易双方可以提起争议")
            if deal["state"] != d.STATE_INSPECTING:
                raise d.InvalidState("当前状态不可提起争议")
            buyer_confirmed = conn.execute(
                "SELECT 1 FROM confirmations WHERE deal_id=? AND party='buyer'",
                (deal_id,)).fetchone()
            if buyer_confirmed and reason != d.REASON_HIDDEN_FAULT:
                raise d.InvalidState("买家已确认，仅能以「隐藏故障」为由申诉")
            if party == "buyer" and not buyer_confirmed:
                deadline = datetime.fromisoformat(deal["inspection_deadline"])
                if self.now() > deadline:
                    raise d.InspectionClosed("验货窗口已关闭")
            dispute_id = self._open_dispute(conn, deal, user_id, reason)
            self._add_evidence(conn, dispute_id, user_id, text, photos, conclusion)
            label = "买家拒收" if party == "buyer" else "卖家提起争议"
            self._event(conn, deal_id, user_id, "DISPUTE_OPENED",
                        f"{label}：{d.REASON_LABELS[reason]}，资金保持冻结")
            return dispute_id

    def _open_dispute(self, conn, deal, opener_id, reason):
        cur = conn.execute(
            "INSERT INTO disputes (deal_id, reason, opened_by, state, created_at)"
            " VALUES (?,?,?,'OPEN',?)",
            (deal["id"], reason, opener_id, self.now().isoformat(timespec="seconds")),
        )
        self._set_state(conn, deal["id"], d.STATE_DISPUTED)
        return cur.lastrowid

    def claim_no_show(self, deal_id, seller_id):
        """买家逾期不到：卖家在验货窗口结束后申诉。"""
        with self.db.tx() as conn:
            deal = self._deal(conn, deal_id)
            if seller_id != deal["seller_id"]:
                raise d.PermissionDenied("只有卖家可以申诉买家逾期")
            if deal["state"] != d.STATE_INSPECTING:
                raise d.InvalidState("当前状态不可申诉逾期")
            deadline = datetime.fromisoformat(deal["inspection_deadline"])
            if self.now() <= deadline:
                raise d.InvalidState("验货窗口未结束，不能申诉逾期")
            confirmed = conn.execute(
                "SELECT 1 FROM confirmations WHERE deal_id=? AND party='buyer'",
                (deal_id,)).fetchone()
            if confirmed:
                raise d.InvalidState("买家已确认，不能申诉逾期")
            dispute_id = self._open_dispute(conn, deal, seller_id, d.REASON_BUYER_NO_SHOW)
            self._event(conn, deal_id, seller_id, "BUYER_NO_SHOW",
                        "买家逾期不到，卖家申诉，资金保持冻结")
            return dispute_id

    def submit_evidence(self, dispute_id, user_id, text="", photos=(), conclusion=None):
        """交易参与者（含验货人）提交证据与结论。

        若买卖双方各自提交相反结论，争议自动标记为「双方结论相反」。
        """
        with self.db.tx() as conn:
            disp = self._dispute(conn, dispute_id)
            if disp["state"] == "RESOLVED":
                raise d.InvalidState("争议已结案")
            deal = self._deal(conn, disp["deal_id"])
            if user_id not in self._participants(deal):
                raise d.PermissionDenied("只有交易参与者（含验货人）可以提交证据")
            self._add_evidence(conn, dispute_id, user_id, text, photos, conclusion)
            rows = conn.execute(
                "SELECT submitted_by, conclusion FROM evidence"
                " WHERE dispute_id=? AND conclusion IS NOT NULL",
                (dispute_id,)).fetchall()
            conclusions = {}
            for row in rows:
                party = self._party_of(deal, row["submitted_by"])
                if party:
                    conclusions[party] = row["conclusion"]
            if (len(conclusions) == 2
                    and conclusions["buyer"] != conclusions["seller"]
                    and disp["reason"] != d.REASON_CONFLICTING):
                conn.execute("UPDATE disputes SET reason=? WHERE id=?",
                             (d.REASON_CONFLICTING, dispute_id))
                self._event(conn, deal["id"], user_id, "CONFLICTING_CONCLUSIONS",
                            "双方各自提交相反结论，争议升级")
            self._event(conn, deal["id"], user_id, "EVIDENCE_SUBMITTED",
                        f"提交证据：{text[:40]}")

    def _add_evidence(self, conn, dispute_id, user_id, text, photos, conclusion):
        conn.execute(
            "INSERT INTO evidence (dispute_id, submitted_by, conclusion, text, photos, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (dispute_id, user_id, conclusion, text, ",".join(photos),
             self.now().isoformat(timespec="seconds")),
        )

    # ------------------------------------------------------------ 仲裁
    def assign_arbitrator(self, dispute_id, platform_id, arbitrator_id):
        """平台指派仲裁员；仲裁员不得处理自己参与的交易。"""
        with self.db.tx() as conn:
            platform = self._user(conn, platform_id)
            if platform["role"] != "platform":
                raise d.PermissionDenied("只有平台可以指派仲裁员")
            arb = self._user(conn, arbitrator_id)
            if arb["role"] != "arbitrator":
                raise d.PermissionDenied("被指派者不是仲裁员")
            disp = self._dispute(conn, dispute_id)
            if disp["state"] == "RESOLVED":
                raise d.InvalidState("争议已结案")
            deal = self._deal(conn, disp["deal_id"])
            if arbitrator_id in self._participants(deal):
                raise d.PermissionDenied("仲裁员不得处理自己参与的交易")
            conn.execute("UPDATE disputes SET arbitrator_id=?, state='ARBITRATING' WHERE id=?",
                         (arbitrator_id, dispute_id))
            self._set_state(conn, deal["id"], d.STATE_ARBITRATING)
            self._event(conn, deal["id"], platform_id, "ARBITRATOR_ASSIGNED",
                        f"平台指派仲裁员 {arb['name']}")

    def rule(self, dispute_id, arbitrator_id, verdict, note=""):
        """仲裁裁决：放款给卖家或退款给买家，资金只能走一个方向。"""
        if verdict not in (d.VERDICT_RELEASE, d.VERDICT_REFUND):
            raise d.DomainError("裁决必须为 RELEASE 或 REFUND")
        with self.db.tx() as conn:
            disp = self._dispute(conn, dispute_id)
            if disp["state"] != "ARBITRATING":
                raise d.InvalidState("争议不在仲裁中")
            if disp["arbitrator_id"] != arbitrator_id:
                raise d.PermissionDenied("只有被指派的仲裁员可以裁决")
            deal = self._deal(conn, disp["deal_id"])
            now_s = self.now().isoformat(timespec="seconds")
            if verdict == d.VERDICT_RELEASE:
                ledger.post(conn, deal["id"], ledger.RELEASE, deal["amount_cents"],
                            self.now(), note="仲裁裁决放款给卖家")
                self._set_state(conn, deal["id"], d.STATE_COMPLETED)
            else:
                ledger.post(conn, deal["id"], ledger.REFUND, deal["amount_cents"],
                            self.now(), note="仲裁裁决退款给买家")
                self._set_state(conn, deal["id"], d.STATE_REFUNDED)
            conn.execute(
                "UPDATE disputes SET state='RESOLVED', verdict=?, verdict_note=?,"
                " resolved_at=? WHERE id=?",
                (verdict, note, now_s, dispute_id),
            )
            label = "放款给卖家" if verdict == d.VERDICT_RELEASE else "退款给买家"
            self._event(conn, deal["id"], arbitrator_id, "ARBITRATION_RULED",
                        f"仲裁裁决：{label}。{note}")

    # ------------------------------------------------------------ 查询
    def list_deals(self):
        return self.db.q("SELECT * FROM deals ORDER BY id")

    def deal_view(self, deal_id):
        deal = self.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
        if not deal:
            raise d.NotFound(f"交易单 {deal_id} 不存在")
        dispute = self.db.one(
            "SELECT * FROM disputes WHERE deal_id=? ORDER BY id DESC LIMIT 1", (deal_id,))
        evidence = self.db.q(
            """SELECT e.*, u.name AS submitter, u.role AS submitter_role
                 FROM evidence e JOIN users u ON u.id = e.submitted_by
                WHERE e.dispute_id=? ORDER BY e.id""",
            (dispute["id"],)) if dispute else []
        return {
            "deal": deal,
            "users": {u["id"]: u for u in self.db.q("SELECT * FROM users ORDER BY id")},
            "checklist": self.db.one(
                "SELECT * FROM checklists WHERE deal_id=? ORDER BY id DESC LIMIT 1",
                (deal_id,)),
            "confirmations": self.db.q(
                "SELECT * FROM confirmations WHERE deal_id=?", (deal_id,)),
            "dispute": dispute,
            "evidence": evidence,
            "extensions": self.db.q(
                "SELECT * FROM extensions WHERE deal_id=? ORDER BY id DESC", (deal_id,)),
            "events": self.db.q(
                "SELECT * FROM deal_events WHERE deal_id=? ORDER BY id", (deal_id,)),
            "ledger": self.db.q(
                "SELECT * FROM ledger_events WHERE deal_id=? ORDER BY id", (deal_id,)),
            "receipts": self.db.q(
                "SELECT * FROM receipts WHERE deal_id=? ORDER BY id", (deal_id,)),
            "frozen": ledger.frozen(self.db.conn, deal_id),
        }

    def conservation_report(self):
        return ledger.conservation_report(self.db.conn)
