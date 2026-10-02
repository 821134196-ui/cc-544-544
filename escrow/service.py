"""领域服务：交易约定、托管流水、验货清单、争议证据、仲裁与资金释放。

关键约束（均由代码 + 数据库约束共同保证）：
1. 状态机：所有操作只允许在指定前置状态执行。
2. 资金互斥：每笔订单至多一笔 hold、一笔终态流（release/refund）；
   付款、退款、放款通过状态校验 + 部分唯一索引互斥。
3. 幂等：hold 用幂等键，网关回执号唯一；重复回执不会形成第二笔资金流。
4. 验货时限：买家验收必须在窗口内；逾期可标记 BUYER_NO_SHOW；
   只有该单验货人能延期。
5. 权限隔离：验货人无法触碰任何资金动作；仲裁员必须回避自己参与的交易。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass

from .clock import Clock, add_seconds
from .payments import MockPaymentGateway, PaymentError

# ---- 订单状态 ----
PENDING_AGREEMENT = "pending_agreement"   # 已建单，等待双方约定确认
AGREED = "agreed"                         # 双方约定当面验货
ESCROW_PENDING = "escrow_pending"         # 付款已提交，等待网关回执
ESCROWED = "escrowed"                     # 托管资金已确认冻结
DELIVERED = "delivered"                   # 卖家交付并填写清单，验货窗口计时中
INSPECTION_PASSED = "inspection_passed"   # 买家验收通过，待双方确认放款
INSPECTION_REJECTED = "inspection_rejected"  # 买家拒收（物品与描述不符等）
BUYER_NO_SHOW = "buyer_no_show"           # 买家逾期不到
DISPUTED = "disputed"                     # 争议中，资金保持冻结
ARBITRATED_RELEASE = "arbitrated_release"  # 仲裁：放款卖家
ARBITRATED_REFUND = "arbitrated_refund"    # 仲裁：退款买家
RELEASED = "released"                     # 双方确认一致，已放款
REFUNDED = "refunded"                     # 已退款
CANCELLED = "cancelled"                   # 卖家在托管前撤销

TERMINAL_STATES = {RELEASED, REFUNDED, CANCELLED, ARBITRATED_RELEASE, ARBITRATED_REFUND}

STATUS_LABELS = {
    PENDING_AGREEMENT: "待约定",
    AGREED: "已约定待付款",
    ESCROW_PENDING: "付款回执延迟中",
    ESCROWED: "托管已冻结",
    DELIVERED: "验货中",
    INSPECTION_PASSED: "验收通过·待确认",
    INSPECTION_REJECTED: "买家拒收",
    BUYER_NO_SHOW: "买家逾期未到",
    DISPUTED: "争议中（资金冻结）",
    ARBITRATED_RELEASE: "仲裁放款",
    ARBITRATED_REFUND: "仲裁退款",
    RELEASED: "已放款",
    REFUNDED: "已退款",
    CANCELLED: "卖家已撤销",
}


class DomainError(Exception):
    """业务规则错误。"""


class AuthError(DomainError):
    """角色/身份不允许执行该操作。"""


class InvalidStateError(DomainError):
    """当前订单状态不允许该操作。"""


class WindowExpiredError(DomainError):
    """已超过验货窗口。"""


@dataclass
class Balance:
    account: str
    # 借-贷 净值；资产为正，负债（托管/钱包）为负
    net: int


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class EscrowService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        gateway: MockPaymentGateway,
        photos_dir: str = "photos",
    ):
        self.conn = conn
        self.clock = clock
        self.gateway = gateway
        self.photos_dir = photos_dir

    # ============ 基础工具 ============
    def _now(self) -> str:
        return self.clock.iso()

    def get_order(self, order_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise DomainError(f"订单不存在: {order_id}")
        return row

    def _require_status(self, order: sqlite3.Row, *allowed: str) -> None:
        if order["status"] not in allowed:
            raise InvalidStateError(
                f"订单当前状态为「{STATUS_LABELS.get(order['status'], order['status'])}」，"
                f"不能执行此操作（允许状态：{', '.join(STATUS_LABELS.get(s, s) for s in allowed)}）"
            )

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AuthError(f"用户不存在: {user_id}")
        return row

    def _roles(self, user_id: str) -> set[str]:
        return {
            r["role"]
            for r in self.conn.execute(
                "SELECT role FROM user_roles WHERE user_id = ?", (user_id,)
            )
        }

    def require_role(self, user_id: str, role: str) -> None:
        if role not in self._roles(user_id):
            raise AuthError(f"用户 {user_id} 不具备 {role} 角色")

    def _event(self, order_id: str, kind: str, actor_id: str | None, payload=None) -> None:
        self.conn.execute(
            "INSERT INTO order_events (order_id, kind, actor_id, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_id, kind, actor_id, json.dumps(payload, ensure_ascii=False) if payload else None,
             self._now()),
        )

    def _set_status(self, order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, self._now(), order_id),
        )

    # ============ 用户与角色 ============
    def create_user(self, user_id: str, name: str, roles: list[str]) -> None:
        valid = {"buyer", "seller", "inspector", "arbiter"}
        if not set(roles) <= valid:
            raise DomainError(f"非法角色: {roles}")
        self.conn.execute(
            "INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, self._now()),
        )
        for role in roles:
            self.conn.execute(
                "INSERT INTO user_roles (user_id, role) VALUES (?, ?)", (user_id, role)
            )
        self.conn.commit()

    # ============ 1. 交易约定 ============
    def create_order(
        self,
        *,
        seller_id: str,
        buyer_id: str,
        title: str,
        description: str,
        amount: int,
        window_sec: int,
        inspector_id: str | None = None,
    ) -> str:
        self._user(seller_id)
        self._user(buyer_id)
        self.require_role(seller_id, "seller")
        self.require_role(buyer_id, "buyer")
        if seller_id == buyer_id:
            raise DomainError("买家与卖家不能是同一人")
        if inspector_id is not None:
            self._user(inspector_id)
            self.require_role(inspector_id, "inspector")
            if inspector_id in (seller_id, buyer_id):
                raise DomainError("验货人不能由交易双方担任")
        if amount <= 0:
            raise DomainError("托管金额必须为正数")

        order_id = new_id("ord")
        code = order_id.upper().replace("_", "-")
        now = self._now()
        deadline = add_seconds(now, window_sec)
        self.conn.execute(
            """INSERT INTO orders (id, code, seller_id, buyer_id, inspector_id,
                   title, description, amount, status, window_sec,
                   agreed_at, inspection_deadline, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (order_id, code, seller_id, buyer_id, inspector_id, title, description,
             amount, PENDING_AGREEMENT, window_sec, deadline, now),
        )
        self._event(order_id, "order_created", seller_id,
                    {"amount": amount, "window_sec": window_sec})
        self.conn.commit()
        return order_id

    def agree(self, order_id: str, user_id: str) -> None:
        """买卖双方确认约定；双方都确认后订单进入「已约定」。"""
        order = self.get_order(order_id)
        self._require_status(order, PENDING_AGREEMENT)
        if user_id == order["seller_id"]:
            col = "seller_confirmed_at"
        elif user_id == order["buyer_id"]:
            col = "buyer_confirmed_at"
        else:
            raise AuthError("只有交易双方可以确认约定")

        self.conn.execute(
            f"UPDATE orders SET {col} = ?, updated_at = ? WHERE id = ?",
            (self._now(), self._now(), order_id),
        )
        self._event(order_id, "agreed", user_id, {"party": col[: -len("_confirmed_at")]})

        row = self.get_order(order_id)
        if row["seller_confirmed_at"] and row["buyer_confirmed_at"]:
            self.conn.execute(
                "UPDATE orders SET agreed_at = ?, status = ?, updated_at = ? WHERE id = ?",
                (self._now(), AGREED, self._now(), order_id),
            )
            self._event(order_id, "agreement_completed", user_id)
        self.conn.commit()

    def cancel(self, order_id: str, seller_id: str, reason: str = "") -> None:
        """卖家撤销：仅托管付款之前可单方面撤销。"""
        order = self.get_order(order_id)
        if seller_id != order["seller_id"]:
            raise AuthError("只有卖家可以撤销交易")
        self._require_status(order, PENDING_AGREEMENT, AGREED)
        self.conn.execute(
            "UPDATE orders SET status = ?, cancelled_at = ?, updated_at = ? WHERE id = ?",
            (CANCELLED, self._now(), self._now(), order_id),
        )
        self._event(order_id, "cancelled", seller_id, {"reason": reason})
        self.conn.commit()

    # ============ 2. 托管流水（付款 / 放款 / 退款互斥，幂等） ============
    def request_escrow(self, order_id: str, buyer_id: str) -> str:
        """买家发起托管付款。网关回执可能延迟，先入 escrow_pending。

        重放（回执延迟期间重复提交）返回同一幂等键，不产生新流水。
        """
        order = self.get_order(order_id)
        if buyer_id != order["buyer_id"]:
            raise AuthError("只有买家可以发起托管付款")
        idem_key = f"hold:{order_id}"
        if order["status"] == ESCROW_PENDING:
            existing = self.conn.execute(
                "SELECT idem_key FROM money_movements WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if existing:
                return existing["idem_key"]
        self._require_status(order, AGREED)

        # 幂等键/回执约束由 DB 兜底；这里先建网关支付单（queued）
        self.gateway.submit(idem_key, order["amount"])
        try:
            self.conn.execute(
                """INSERT INTO money_movements
                       (order_id, direction, amount, idem_key, status, created_at)
                   VALUES (?, 'hold', ?, ?, 'pending', ?)""",
                (order_id, order["amount"], idem_key, self._now()),
            )
        except sqlite3.IntegrityError:
            # 已有 hold（重放请求），直接复用，绝不形成第二笔
            self.conn.rollback()
            return idem_key
        self._set_status(order_id, ESCROW_PENDING)
        self._event(order_id, "escrow_requested", buyer_id, {"amount": order["amount"]})
        self.conn.commit()
        return idem_key

    def poll_gateway(self) -> int:
        """拉取延迟到达的付款回执，确认托管。返回本次确认的订单数。"""
        # 1) 让网关把 queued 支付推进到 paid（模拟回执延迟后最终到达）
        self.gateway.poll_settle_pending()
        # 2) 把本地所有 pending hold 与网关侧 paid 支付对齐确认
        pending = self.conn.execute(
            "SELECT idem_key FROM money_movements WHERE direction = 'hold' "
            "AND status = 'pending'"
        ).fetchall()
        n = 0
        for row in pending:
            payment = self.gateway.get(row["idem_key"])
            if payment.status == "paid" and self._confirm_hold_if_pending(payment.idem_key):
                n += 1
        return n

    def confirm_by_receipt(self, receipt_no: str) -> str | None:
        """支付网关回执回调入口。同一回执重复回调只生效一次。

        返回订单号；若回执对应一笔已确认的付款（重放），同样返回订单号但不新增流水。
        """
        payment = self.gateway.verify_receipt(receipt_no)  # 无效回执抛 PaymentError
        return self._confirm_hold_if_pending(payment.idem_key, receipt_no)

    def _confirm_hold_if_pending(self, idem_key: str, receipt_no: str | None = None) -> bool:
        """把 hold 由 pending 置为 confirmed 并复式记账。幂等。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            mv = self.conn.execute(
                "SELECT * FROM money_movements WHERE idem_key = ?", (idem_key,)
            ).fetchone()
            if mv is None:
                self.conn.rollback()
                return False
            if mv["status"] == "confirmed":
                # 重复回执：不新增任何账务分录，不形成第二笔资金流
                self.conn.rollback()
                return False

            payment = self.gateway.get(idem_key)
            if payment.status != "paid":
                self.conn.rollback()
                raise PaymentError("付款尚未得到网关确认（回执延迟中）")
            receipt = receipt_no or payment.receipt_no
            order = self.get_order(mv["order_id"])

            self._post_ledger(mv["id"], [
                ("cash:gateway", mv["amount"], 0),   # 借：网关在途现金（资产）
                (f"escrow:{order['id']}", 0, mv["amount"]),  # 贷：托管冻结（负债）
            ])
            self.conn.execute(
                "UPDATE money_movements SET status = 'confirmed', receipt_no = ?, "
                "confirmed_at = ? WHERE id = ?",
                (receipt, self._now(), mv["id"]),
            )
            self.conn.execute(
                "UPDATE orders SET status = ?, escrowed_at = ?, updated_at = ? WHERE id = ?",
                (ESCROWED, self._now(), self._now(), order["id"]),
            )
            self._event(order["id"], "escrow_confirmed", None,
                        {"receipt": receipt, "amount": mv["amount"]})
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def _post_ledger(self, movement_id: int, entries: list[tuple[str, int, int]]) -> None:
        """写一组平衡的复式分录（借合计 == 贷合计）。"""
        total_dr = sum(e[1] for e in entries)
        total_cr = sum(e[2] for e in entries)
        assert total_dr == total_cr and total_dr > 0, f"分录不平: {entries}"
        cur = self.conn.execute(
            "INSERT INTO ledger_transactions (movement_id, created_at) VALUES (?, ?)",
            (movement_id, self._now()),
        )
        tx_id = cur.lastrowid
        for account, debit, credit in entries:
            self.conn.execute(
                "INSERT INTO ledger_entries (tx_id, account, debit, credit) VALUES (?, ?, ?, ?)",
                (tx_id, account, debit, credit),
            )

    def _terminal_movement(self, order: sqlite3.Row, direction: str,
                           entries: list[tuple[str, int, int]], actor_id: str,
                           new_status: str, event: str) -> None:
        """创建终态资金流（release/refund）。互斥 + 幂等的核心关口。"""
        assert direction in ("release", "refund")
        order_id = order["id"]
        idem_key = f"{direction}:{order_id}"

        hold = self.conn.execute(
            "SELECT * FROM money_movements WHERE order_id = ? AND direction = 'hold'",
            (order_id,),
        ).fetchone()
        if hold is None or hold["status"] != "confirmed":
            raise InvalidStateError("托管尚未确认，不能放款或退款")

        existing_terminal = self.conn.execute(
            "SELECT direction FROM money_movements WHERE order_id = ? "
            "AND direction IN ('release','refund')",
            (order_id,),
        ).fetchone()
        if existing_terminal is not None:
            # 付款/退款/放款互斥：已有终态流，拒绝再开
            raise InvalidStateError(
                f"订单已存在 {existing_terminal['direction']} 资金流，"
                "付款、退款与放款互斥，不能重复出账"
            )

        # 与调用方处于同一事务；互斥最终由部分唯一索引 + 幂等键兜底
        try:
            cur = self.conn.execute(
                """INSERT INTO money_movements
                       (order_id, direction, amount, idem_key, status, created_at, confirmed_at)
                   VALUES (?, ?, ?, ?, 'confirmed', ?, ?)""",
                (order_id, direction, hold["amount"], idem_key, self._now(), self._now()),
            )
            self._post_ledger(cur.lastrowid, entries)
            ts_col = "released_at" if direction == "release" else "refunded_at"
            self.conn.execute(
                f"UPDATE orders SET status = ?, {ts_col} = ?, updated_at = ? WHERE id = ?",
                (new_status, self._now(), self._now(), order_id),
            )
            self._event(order_id, event, actor_id, {"amount": hold["amount"]})
            self.conn.commit()
        except sqlite3.IntegrityError:
            # 部分唯一索引 / 幂等键冲突：并发下另一笔终态流已胜出
            self.conn.rollback()
            raise InvalidStateError("并发资金冲突：该订单已有终态资金流，本操作被拒绝")
        except Exception:
            self.conn.rollback()
            raise

    # ============ 3. 验货清单 ============
    def deliver(
        self,
        order_id: str,
        seller_id: str,
        *,
        serial_no: str,
        appearance: str,
        accessories: str,
        seller_conclusion: str = "conforming",
        note: str = "",
        photo_label: str | None = None,
        photo_rgb: tuple[int, int, int] = (200, 206, 212),
    ) -> None:
        order = self.get_order(order_id)
        if seller_id != order["seller_id"]:
            raise AuthError("只有卖家可以交付并填写验货清单")
        self._require_status(order, ESCROWED)
        if seller_conclusion not in ("conforming", "defective"):
            raise DomainError("卖家结论必须是 conforming 或 defective")
        if not serial_no.strip():
            raise DomainError("序列号必填")

        from .photos import make_photo  # 局部导入避免循环
        photo_path = None
        if photo_label:
            photo_path = make_photo(self.photos_dir, photo_label, photo_rgb)

        deadline = add_seconds(self._now(), order["window_sec"])
        self.conn.execute(
            """INSERT INTO delivery_checklists
                   (order_id, serial_no, appearance, accessories,
                    seller_conclusion, note, photo_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, serial_no.strip(), appearance, accessories,
             seller_conclusion, note, photo_path, self._now()),
        )
        self.conn.execute(
            "UPDATE orders SET status = ?, delivered_at = ?, "
            "inspection_deadline = ?, updated_at = ? WHERE id = ?",
            (DELIVERED, self._now(), deadline, self._now(), order_id),
        )
        self._event(order_id, "delivered", seller_id,
                    {"serial_no": serial_no, "deadline": deadline})
        self.conn.commit()

    def get_checklist(self, order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM delivery_checklists WHERE order_id = ?", (order_id,)
        ).fetchone()

    def window_remaining(self, order_id: str) -> int:
        order = self.get_order(order_id)
        from datetime import datetime
        return max(0, int((datetime.fromisoformat(order["inspection_deadline"])
                           - self.clock.now()).total_seconds()))

    def extend_window(self, order_id: str, inspector_id: str, extra_sec: int,
                      reason: str) -> None:
        """验货人延期：只有被指派到该单的验货人可以操作，且窗口仍有效。"""
        order = self.get_order(order_id)
        self.require_role(inspector_id, "inspector")
        if not order["inspector_id"]:
            raise AuthError("该交易未指派验货人")
        if inspector_id != order["inspector_id"]:
            raise AuthError("只有被指派到本单的验货人可以延期")
        self._require_status(order, DELIVERED)
        if extra_sec <= 0:
            raise DomainError("延时时长必须为正数")
        if self.window_remaining(order_id) <= 0:
            raise WindowExpiredError("验货窗口已结束，不能延期，应按逾期/争议处理")

        new_deadline = add_seconds(order["inspection_deadline"], extra_sec)
        self.conn.execute(
            """INSERT INTO window_extensions
                   (order_id, inspector_id, extra_sec, new_deadline, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, inspector_id, extra_sec, new_deadline, reason, self._now()),
        )
        self.conn.execute(
            "UPDATE orders SET inspection_deadline = ?, updated_at = ? WHERE id = ?",
            (new_deadline, self._now(), order_id),
        )
        self._event(order_id, "window_extended", inspector_id,
                    {"extra_sec": extra_sec, "reason": reason, "new_deadline": new_deadline})
        self.conn.commit()

    def mark_buyer_no_show(self, order_id: str, actor_id: str) -> None:
        """买家逾期不到：窗口结束后，由卖家或验货人标记。"""
        order = self.get_order(order_id)
        if actor_id not in (order["seller_id"], order["inspector_id"]):
            raise AuthError("只有卖家或验货人可以标记买家逾期")
        self._require_status(order, DELIVERED)
        if self.window_remaining(order_id) > 0:
            raise DomainError("验货窗口尚未结束，不能标记逾期")
        self.conn.execute(
            "UPDATE orders SET status = ?, no_show_at = ?, updated_at = ? WHERE id = ?",
            (BUYER_NO_SHOW, self._now(), self._now(), order_id),
        )
        self._event(order_id, "buyer_no_show", actor_id, None)
        self.conn.commit()

    # ============ 买家验收 ============
    def inspect(self, order_id: str, buyer_id: str, conclusion: str,
                note: str = "", photo_label: str | None = None,
                photo_rgb: tuple[int, int, int] = (200, 206, 212)) -> None:
        order = self.get_order(order_id)
        if buyer_id != order["buyer_id"]:
            raise AuthError("只有买家可以提交验收结论")
        self._require_status(order, DELIVERED)
        if conclusion not in ("accept", "reject"):
            raise DomainError("验收结论必须是 accept 或 reject")
        if self.window_remaining(order_id) <= 0:
            raise WindowExpiredError(
                "已超过验货窗口，不能再提交验收；请联系验货人处理逾期或申请争议"
            )

        from .photos import make_photo
        photo_path = make_photo(self.photos_dir, photo_label, photo_rgb) if photo_label else None
        self.conn.execute(
            """INSERT INTO inspections (order_id, conclusion, note, photo_path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, conclusion, note, photo_path, self._now()),
        )
        new_status = INSPECTION_PASSED if conclusion == "accept" else INSPECTION_REJECTED
        # 买家提交验收即视为买家侧对资金去向（放款/退款）的确认；
        # 卖家侧仍需在 confirm_release / confirm_refund 中确认。
        self.conn.execute(
            "UPDATE orders SET status = ?, release_buyer_at = ?, "
            "accepted_at = CASE WHEN ? = 'accept' THEN ? ELSE accepted_at END, "
            "rejected_at = CASE WHEN ? = 'reject' THEN ? ELSE rejected_at END, "
            "updated_at = ? WHERE id = ?",
            (new_status, self._now(),
             conclusion, self._now(),
             conclusion, self._now(),
             self._now(), order_id),
        )
        self._event(order_id, f"inspection_{conclusion}", buyer_id, {"note": note})
        self.conn.commit()

    def get_inspection(self, order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inspections WHERE order_id = ?", (order_id,)
        ).fetchone()

    # ============ 双方确认 → 资金释放 ============
    def confirm_release(self, order_id: str, user_id: str) -> None:
        """验收通过后，双方确认一致 → 放款给卖家。"""
        order = self.get_order(order_id)
        self._require_status(order, INSPECTION_PASSED)
        if user_id == order["seller_id"]:
            col = "release_seller_at"
        elif user_id == order["buyer_id"]:
            col = "release_buyer_at"
        else:
            raise AuthError("只有交易双方可以确认放款")
        if order[col]:
            return  # 幂等：同一方重复确认无副作用
        self.conn.execute(
            f"UPDATE orders SET {col} = ?, updated_at = ? WHERE id = ?",
            (self._now(), self._now(), order_id),
        )
        self._event(order_id, "release_confirmed", user_id, {"party": col[:-3]})

        row = self.get_order(order_id)
        if row["release_seller_at"] and row["release_buyer_at"]:
            self._terminal_movement(
                row, "release",
                [
                    (f"escrow:{order_id}", order["amount"], 0),       # 借：冲销冻结
                    (f"wallet:{order['seller_id']}", 0, order["amount"]),  # 贷：卖家余额
                ],
                user_id, RELEASED, "released",
            )
        else:
            self.conn.commit()

    def confirm_refund(self, order_id: str, user_id: str) -> None:
        """拒收后双方确认一致 → 退款给买家。买家的拒收本身算买家侧确认。"""
        order = self.get_order(order_id)
        if user_id not in (order["seller_id"], order["buyer_id"]):
            raise AuthError("只有交易双方可以确认退款")
        self._require_status(order, INSPECTION_REJECTED)
        if user_id == order["seller_id"]:
            if order["release_seller_at"]:
                return
            self.conn.execute(
                "UPDATE orders SET release_seller_at = ?, updated_at = ? WHERE id = ?",
                (self._now(), self._now(), order_id),
            )
            self._event(order_id, "refund_confirmed", user_id, {"party": "seller"})
        # 买家侧已由 inspect(reject) 置位 release_buyer_at
        row = self.get_order(order_id)
        if row["release_seller_at"] and row["release_buyer_at"]:
            self._terminal_movement(
                row, "refund",
                [
                    (f"escrow:{order_id}", order["amount"], 0),   # 借：冲销冻结
                    ("cash:gateway", 0, order["amount"]),         # 贷：现金退回（出系统）
                ],
                user_id, REFUNDED, "refunded",
            )
        else:
            self.conn.commit()

    # ============ 4. 争议与证据 ============
    def open_dispute(self, order_id: str, user_id: str, reason: str,
                     detail: str = "") -> None:
        """开争议。资金在争议全程保持冻结。

        - item_mismatch：物品与描述不符；
        - opposite_conclusions：双方结论相反；
        - hidden_defect：验货通过后发现隐藏故障（放款确认前仍可追回冻结款）。
        """
        order = self.get_order(order_id)
        if user_id not in (order["seller_id"], order["buyer_id"]):
            raise AuthError("只有交易双方可以发起争议")
        self._require_status(order, INSPECTION_REJECTED, INSPECTION_PASSED,
                             DELIVERED, BUYER_NO_SHOW)
        if reason not in ("item_mismatch", "opposite_conclusions",
                          "hidden_defect", "other"):
            raise DomainError("非法争议原因")
        self.conn.execute(
            """INSERT INTO disputes (order_id, reason, opened_by, status, created_at)
               VALUES (?, ?, ?, 'open', ?)""",
            (order_id, reason, user_id, self._now()),
        )
        self.conn.execute(
            "UPDATE orders SET status = ?, dispute_reason = ?, disputed_at = ?, "
            "updated_at = ? WHERE id = ?",
            (DISPUTED, reason, self._now(), self._now(), order_id),
        )
        self._event(order_id, "dispute_opened", user_id,
                    {"reason": reason, "detail": detail})
        self.conn.commit()

    def submit_evidence(self, order_id: str, user_id: str, conclusion: str,
                        note: str, photo_label: str,
                        photo_rgb: tuple[int, int, int] = (200, 206, 212)) -> int:
        """提交争议证据。双方可分别提交 conforming / defective 的相反结论。"""
        order = self.get_order(order_id)
        if user_id not in (order["seller_id"], order["buyer_id"], order["inspector_id"]):
            raise AuthError("只有交易双方或验货人可以提交证据")
        self._require_status(order, DISPUTED)
        if conclusion not in ("conforming", "defective"):
            raise DomainError("证据结论必须是 conforming 或 defective")
        if not note.strip():
            raise DomainError("证据说明必填")

        from .photos import make_photo
        photo_path = make_photo(self.photos_dir, photo_label, photo_rgb)
        cur = self.conn.execute(
            """INSERT INTO evidence (order_id, submitter_id, conclusion, note,
                                     photo_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, conclusion, note, photo_path, self._now()),
        )
        self._event(order_id, "evidence_submitted", user_id,
                    {"conclusion": conclusion, "evidence_id": cur.lastrowid})
        self.conn.commit()
        return cur.lastrowid

    def list_evidence(self, order_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT e.*, u.name AS submitter_name FROM evidence e "
            "JOIN users u ON u.id = e.submitter_id WHERE e.order_id = ? "
            "ORDER BY e.id", (order_id,)
        ))

    def add_inspector_note(self, order_id: str, inspector_id: str, note: str) -> None:
        order = self.get_order(order_id)
        self.require_role(inspector_id, "inspector")
        if not order["inspector_id"] or inspector_id != order["inspector_id"]:
            raise AuthError("只有被指派到本单的验货人可以记录现场备注")
        self.conn.execute(
            """INSERT INTO inspector_notes (order_id, inspector_id, note, created_at)
               VALUES (?, ?, ?, ?)""",
            (order_id, inspector_id, note, self._now()),
        )
        self._event(order_id, "inspector_note", inspector_id, {"note": note})
        self.conn.commit()

    # ============ 5. 仲裁 ============
    def arbitrate(self, order_id: str, arbiter_id: str, ruling: str,
                  rationale: str) -> None:
        """仲裁员裁决并立即执行资金动作。仲裁回避：不得处理自己参与的交易。"""
        order = self.get_order(order_id)
        self.require_role(arbiter_id, "arbiter")
        # 回避：买家、卖家、被指派验货人均属「参与过该交易」
        participants = {order["seller_id"], order["buyer_id"]}
        if order["inspector_id"]:
            participants.add(order["inspector_id"])
        if arbiter_id in participants:
            raise AuthError("仲裁员必须回避：不得仲裁本人参与过的交易")
        self._require_status(order, DISPUTED)
        if ruling not in ("release_to_seller", "refund_to_buyer"):
            raise DomainError("裁决必须是 release_to_seller 或 refund_to_buyer")
        if not rationale.strip():
            raise DomainError("裁决理由必填")

        self.conn.execute(
            """INSERT INTO arbitrations (order_id, arbiter_id, ruling, rationale, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, arbiter_id, ruling, rationale, self._now()),
        )
        self.conn.execute(
            "UPDATE disputes SET status = 'ruled', ruled_at = ? WHERE order_id = ?",
            (self._now(), order_id),
        )
        self.conn.execute(
            "UPDATE orders SET arbiter_id = ?, ruling = ?, ruled_at = ? WHERE id = ?",
            (arbiter_id, ruling, self._now(), order_id),
        )
        self._event(order_id, "arbitrated", arbiter_id, {"ruling": ruling})

        if ruling == "release_to_seller":
            self._terminal_movement(
                order, "release",
                [
                    (f"escrow:{order_id}", order["amount"], 0),
                    (f"wallet:{order['seller_id']}", 0, order["amount"]),
                ],
                arbiter_id, ARBITRATED_RELEASE, "arbitrated_release",
            )
        else:
            self._terminal_movement(
                order, "refund",
                [
                    (f"escrow:{order_id}", order["amount"], 0),
                    ("cash:gateway", 0, order["amount"]),
                ],
                arbiter_id, ARBITRATED_REFUND, "arbitrated_refund",
            )

    # ============ 查询与对账 ============
    def list_orders(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM orders ORDER BY id"))

    def list_events(self, order_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)
        ))

    def list_movements(self, order_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM money_movements WHERE order_id = ? ORDER BY id", (order_id,)
        ))

    def ledger_balances(self) -> dict[str, int]:
        """全部账户净值（借-贷）。守恒不变量：所有账户净值之和恒为 0。"""
        rows = self.conn.execute(
            "SELECT account, SUM(debit) - SUM(credit) AS net "
            "FROM ledger_entries GROUP BY account"
        ).fetchall()
        return {r["account"]: r["net"] for r in rows}

    def escrow_total(self) -> int:
        """当前冻结中的托管总额（escrow:* 负债账户的贷方净额，取正数）。"""
        return -sum(
            net for acct, net in self.ledger_balances().items()
            if acct.startswith("escrow:")
        )

    def wallet_balance(self, user_id: str) -> int:
        net = self.ledger_balances().get(f"wallet:{user_id}", 0)
        return -net  # 钱包是负债账户，贷方为正
