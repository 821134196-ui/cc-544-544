"""领域定义：交易状态、争议原因、角色与业务异常。"""


class DomainError(Exception):
    """业务规则被拒绝时的基类。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""


class InvalidState(DomainError):
    """当前状态不允许该操作。"""


class NotFound(DomainError):
    """对象不存在。"""


class AmountMismatch(DomainError):
    """回执金额与托管金额不一致。"""


class InspectionClosed(DomainError):
    """验货窗口已关闭。"""


# ---------------------------------------------------------------- 交易单状态
STATE_AGREED = "AGREED"                    # 双方已约定，待买家付款
STATE_FUNDING = "FUNDING"                  # 付款已发起，等待支付回执
STATE_PAYMENT_DELAYED = "PAYMENT_DELAYED"  # 付款回执延迟
STATE_ESCROWED = "ESCROWED"                # 资金已托管，待卖家交付
STATE_INSPECTING = "INSPECTING"            # 验货窗口内
STATE_DISPUTED = "DISPUTED"                # 争议中，资金冻结
STATE_ARBITRATING = "ARBITRATING"          # 仲裁中，资金冻结
STATE_COMPLETED = "COMPLETED"              # 双方确认一致，已放款
STATE_REFUNDED = "REFUNDED"                # 已退款
STATE_CANCELLED = "CANCELLED"              # 已取消（卖家撤销）

STATE_LABELS = {
    STATE_AGREED: "已约定·待付款",
    STATE_FUNDING: "付款中·等待回执",
    STATE_PAYMENT_DELAYED: "付款回执延迟",
    STATE_ESCROWED: "托管中·待交付",
    STATE_INSPECTING: "验货中",
    STATE_DISPUTED: "争议中·资金冻结",
    STATE_ARBITRATING: "仲裁中·资金冻结",
    STATE_COMPLETED: "已完成·已放款",
    STATE_REFUNDED: "已退款",
    STATE_CANCELLED: "已取消",
}

TERMINAL_STATES = {STATE_COMPLETED, STATE_REFUNDED, STATE_CANCELLED}

# ---------------------------------------------------------------- 争议原因
REASON_ITEM_MISMATCH = "ITEM_MISMATCH"                  # 物品与描述不符
REASON_BUYER_NO_SHOW = "BUYER_NO_SHOW"                  # 买家逾期不到
REASON_SELLER_CANCEL = "SELLER_CANCEL"                  # 卖家撤销
REASON_PAYMENT_DELAY = "PAYMENT_DELAY"                  # 付款回执延迟
REASON_HIDDEN_FAULT = "HIDDEN_FAULT"                    # 验货后发现隐藏故障
REASON_CONFLICTING = "CONFLICTING_CONCLUSIONS"          # 双方各自提交相反结论

REASON_LABELS = {
    REASON_ITEM_MISMATCH: "物品与描述不符",
    REASON_BUYER_NO_SHOW: "买家逾期不到",
    REASON_SELLER_CANCEL: "卖家撤销",
    REASON_PAYMENT_DELAY: "付款回执延迟",
    REASON_HIDDEN_FAULT: "验货后发现隐藏故障",
    REASON_CONFLICTING: "双方结论相反",
}

# ---------------------------------------------------------------- 角色
ROLE_LABELS = {
    "buyer": "买家",
    "seller": "卖家",
    "inspector": "验货人",
    "arbitrator": "仲裁员",
    "platform": "平台",
}

# 验货结论
CONCLUSION_MATCH = "MATCH"          # 与描述相符
CONCLUSION_MISMATCH = "MISMATCH"    # 与描述不符

# 仲裁裁决
VERDICT_RELEASE = "RELEASE"  # 放款给卖家
VERDICT_REFUND = "REFUND"    # 退款给买家

# 买家付款的等待窗口（超时未收到回执记为“付款回执延迟”）
PAYMENT_WINDOW_MINUTES = 15
