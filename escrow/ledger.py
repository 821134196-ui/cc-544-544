"""托管流水：所有资金变动只通过本模块入账，保证可审计与资金守恒。

三种账务事件（账户：buyer / escrow / seller）：
- HOLD    买家付款进入托管   buyer  -> escrow
- RELEASE 双方确认/仲裁放款  escrow -> seller
- REFUND  撤销/仲裁退款      escrow -> buyer

不变量（conservation_report 校验）：
- 每笔交易：冻结 = HOLD - RELEASE - REFUND >= 0
- 放款与退款互斥；无托管不得结算
- 全局：ΣHOLD = ΣRELEASE + ΣREFUND + Σ冻结
"""

HOLD = "HOLD"
RELEASE = "RELEASE"
REFUND = "REFUND"

_ACCOUNTS = {
    HOLD: ("buyer", "escrow"),
    RELEASE: ("escrow", "seller"),
    REFUND: ("escrow", "buyer"),
}


def post(conn, deal_id, kind, amount_cents, now, receipt_id=None, note=""):
    """入账。唯一索引/触发器会拒绝重复或互斥的资金流，调用方需在事务内。"""
    from_account, to_account = _ACCOUNTS[kind]
    conn.execute(
        """INSERT INTO ledger_events
             (deal_id, kind, amount_cents, from_account, to_account, receipt_id, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (deal_id, kind, amount_cents, from_account, to_account,
         receipt_id, note, now.isoformat(timespec="seconds")),
    )


def sums(conn, deal_id):
    rows = conn.execute(
        "SELECT kind, COALESCE(SUM(amount_cents),0) AS s "
        "FROM ledger_events WHERE deal_id=? GROUP BY kind",
        (deal_id,),
    ).fetchall()
    out = {HOLD: 0, RELEASE: 0, REFUND: 0}
    for r in rows:
        out[r["kind"]] = r["s"]
    return out


def frozen(conn, deal_id):
    """该交易当前仍冻结在托管账户中的金额。"""
    s = sums(conn, deal_id)
    return s[HOLD] - s[RELEASE] - s[REFUND]


def conservation_report(conn):
    """资金守恒校验，返回 {"ok", "problems", "totals", "frozen_total"}。"""
    problems = []
    totals = {HOLD: 0, RELEASE: 0, REFUND: 0}
    frozen_total = 0
    for deal in conn.execute("SELECT id, code FROM deals ORDER BY id").fetchall():
        s = sums(conn, deal["id"])
        for k in totals:
            totals[k] += s[k]
        fr = s[HOLD] - s[RELEASE] - s[REFUND]
        frozen_total += fr
        if fr < 0:
            problems.append(f"{deal['code']}: 冻结金额为负 ({fr})")
        if s[RELEASE] > 0 and s[REFUND] > 0:
            problems.append(f"{deal['code']}: 放款与退款同时存在")
        if s[HOLD] == 0 and (s[RELEASE] > 0 or s[REFUND] > 0):
            problems.append(f"{deal['code']}: 无托管却发生结算")
    if totals[HOLD] != totals[RELEASE] + totals[REFUND] + frozen_total:
        problems.append("全局资金不守恒")
    return {
        "ok": not problems,
        "problems": problems,
        "totals": totals,
        "frozen_total": frozen_total,
    }
