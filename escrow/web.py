"""HTTP 界面（标准库 http.server，零依赖）。

页面：
  /                     订单总览
  /order/{id}           交易页：顶部固定条常驻托管金额、剩余验货时间、双方确认状态
  /dispute/{id}         争议页：商品描述与现场照片并排，证据与仲裁
  /photos/{name}        本地虚构照片
  /api/order/{id}/status  剩余时间等动态字段（供固定条局部刷新）
  /act                  表单动作统一入口

配色仅使用白、深灰与有限绿色（绿色只用于「一致/通过/已放款」结论提示）。
"""

from __future__ import annotations

import html
import json
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import service as svc
from .clock import Clock
from .db import connect, init_db
from .payments import MockPaymentGateway

DARK = "#2b2b2b"
GRAY = "#6b6b6b"
LINE = "#d8d8d8"
GREEN = "#2e7d32"
GREEN_BG = "#e8f3e9"
WHITE = "#ffffff"

CSS = f"""
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       color:{DARK}; background:{WHITE}; line-height:1.55; }}
a {{ color:{DARK}; text-decoration: underline; text-underline-offset:3px; }}
h1,h2,h3 {{ font-weight:600; }}
.wrap {{ max-width:980px; margin:0 auto; padding:20px 24px 60px; }}
.topbar {{ position:sticky; top:0; z-index:10; background:{WHITE};
           border-bottom:2px solid {DARK}; padding:12px 24px;
           display:flex; gap:28px; align-items:center; flex-wrap:wrap; }}
.topbar .metric {{ font-size:13px; color:{GRAY}; }}
.topbar .metric b {{ display:block; font-size:18px; color:{DARK}; }}
.topbar .metric b.green {{ color:{GREEN}; }}
.badge {{ display:inline-block; padding:2px 10px; border:1px solid {DARK};
          border-radius:12px; font-size:12px; color:{DARK}; background:{WHITE}; }}
.badge.green {{ color:{GREEN}; border-color:{GREEN}; background:{GREEN_BG}; }}
.badge.gray {{ color:{GRAY}; border-color:{LINE}; }}
.card {{ border:1px solid {LINE}; border-radius:8px; padding:16px 18px; margin:16px 0; }}
.card h3 {{ margin:0 0 10px; font-size:15px; border-bottom:1px solid {LINE}; padding-bottom:6px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
td,th {{ text-align:left; padding:6px 10px; border-bottom:1px solid {LINE}; vertical-align:top; }}
th {{ color:{GRAY}; font-weight:500; width:130px; }}
form.inline {{ display:inline; }}
button, select, input, textarea {{ font:inherit; color:{DARK}; }}
button {{ background:{DARK}; color:{WHITE}; border:none; border-radius:5px;
          padding:6px 14px; cursor:pointer; }}
button.ghost {{ background:{WHITE}; color:{DARK}; border:1px solid {DARK}; }}
button.green {{ background:{GREEN}; }}
input[type=text], textarea, select {{ border:1px solid {LINE}; border-radius:4px;
          padding:5px 8px; background:{WHITE}; }}
textarea {{ width:100%; }}
.row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:6px 0; }}
.flash {{ border-left:4px solid {DARK}; background:#f4f4f4; padding:10px 14px;
          margin:12px 0; border-radius:0 6px 6px 0; font-size:14px; white-space:pre-wrap; }}
.flash.err {{ border-left-color:{DARK}; background:#eeeeee; font-weight:600; }}
.side {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }}
@media (max-width:760px) {{ .side {{ grid-template-columns:1fr; }} }}
.side .col {{ border:1px solid {LINE}; border-radius:8px; padding:14px; }}
.side img {{ width:100%; border:1px solid {LINE}; border-radius:6px; background:{WHITE}; }}
.evgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
@media (max-width:760px) {{ .evgrid {{ grid-template-columns:1fr; }} }}
.ev {{ border:1px solid {LINE}; border-radius:8px; padding:12px; }}
.ev img {{ width:100%; border:1px solid {LINE}; border-radius:6px; }}
.concl {{ font-weight:600; }}
.concl.ok {{ color:{GREEN}; }}
.timeline {{ font-size:13px; color:{GRAY}; }}
.timeline li {{ margin:3px 0; }}
.muted {{ color:{GRAY}; font-size:13px; }}
.nav {{ display:flex; justify-content:space-between; align-items:center; }}
.amt {{ font-variant-numeric:tabular-nums; }}
"""


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}¥{abs(cents) / 100:,.2f}"


def h(text) -> str:
    return html.escape("" if text is None else str(text))


def fmt_remaining(sec: int) -> str:
    if sec <= 0:
        return "已截止"
    h_, rest = divmod(sec, 3600)
    m_, s_ = divmod(rest, 60)
    return f"{h_:02d}:{m_:02d}:{s_:02d}"


def page(title: str, body: str, flash: str = "", as_user: str = "",
         users: list | None = None) -> bytes:
    opts = ""
    for u in users or []:
        sel = " selected" if u["id"] == as_user else ""
        opts += f'<option value="{h(u["id"])}"{sel}>{h(u["name"])}（{h(u["id"])}）</option>'
    switch = ""
    if users is not None:
        switch = (
            '<form class="inline" method="post" action="/act/switch_user">'
            f'<select name="user_id">{opts}</select>'
            '<button class="ghost" type="submit">切换操作人</button></form>'
        )
    flash_html = f'<div class="flash {"err" if flash.startswith("ERR:") else ""}">{h(flash[4:] if flash.startswith("ERR:") else flash)}</div>' if flash else ""
    doc = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title><style>{CSS}</style></head>
<body>{flash_html}
<div class="wrap">
<div class="nav"><span class="muted"><a href="/">← 订单总览</a></span>{switch}</div>
{body}
</div></body></html>"""
    return doc.encode()


class App(BaseHTTPRequestHandler):
    server_version = "EscrowDemo/1.0"

    # ---- 依赖由 serve() 注入 ----
    db_path: str = ""
    photos_dir: str = "photos"
    clock: Clock | None = None

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    @property
    def svc(self) -> svc.EscrowService:
        if getattr(self, "_svc", None) is None:
            conn = connect(self.db_path)
            clock = self.clock or Clock()
            self._conn = conn
            self._svc = svc.EscrowService(conn, clock, MockPaymentGateway(conn, clock),
                                          photos_dir=self.photos_dir)
        return self._svc

    def finish(self):
        try:
            if getattr(self, "_conn", None) is not None:
                self._conn.close()
        finally:
            super().finish()

    def _cookie_user(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "as" in cookie:
            return cookie["as"].value
        return ""

    def _users(self, conn):
        return list(conn.execute(
            "SELECT u.*, GROUP_CONCAT(r.role) AS roles FROM users u "
            "LEFT JOIN user_roles r ON r.user_id = u.id GROUP BY u.id ORDER BY u.id"))

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, flash: str = "", set_user: str | None = None):
        headers = []
        if set_user is not None:
            headers.append(("Set-Cookie", f"as={set_user}; Path=/"))
        if flash:
            q = urllib.parse.urlencode({"flash": flash})
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}{q}"
        self.send_response(303)
        self.send_header("Location", path)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def _flash(self) -> str:
        qs = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(qs).get("flash", [""])[0]

    # ============ GET ============
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._index()
            elif path.startswith("/order/") and path.endswith("/status"):
                self._api_status(path.split("/")[2])
            elif path.startswith("/order/"):
                self._order_page(path.split("/")[2])
            elif path.startswith("/dispute/"):
                self._dispute_page(path.split("/")[2])
            elif path.startswith("/photos/"):
                self._photo(path.split("/", 2)[2])
            else:
                self._send(page("未找到", "<p>页面不存在。</p>"), 404)
        except svc.DomainError as e:
            self._send(page("错误", f'<div class="flash err">{h(str(e))}</div>'), 400)
        except FileNotFoundError:
            self._send(b"not found", 404, "text/plain")

    def _index(self):
        s = self.svc
        rows = s.list_orders()
        trs = []
        for o in rows:
            st = svc.STATUS_LABELS[o["status"]]
            green = o["status"] in (svc.RELEASED, svc.INSPECTION_PASSED,
                                    svc.ARBITRATED_RELEASE)
            cls = "badge green" if green else "badge"
            trs.append(
                f'<tr><td><a href="/order/{h(o["id"])}">{h(o["code"])}</a></td>'
                f'<td>{h(o["title"])}</td>'
                f'<td class="amt">{money(o["amount"])}</td>'
                f'<td><span class="{cls}">{h(st)}</span></td>'
                f'<td>{"🔒 冻结中" if o["status"] == svc.DISPUTED else "—"}</td></tr>'
            )
        body = f"""
<h1>当面交易 · 托管订单</h1>
<p class="muted">流程：双方约定 → 托管冻结 → 卖家交付（序列号/外观/配件）→ 窗口内买家验收
→ 双方确认一致后放款；争议期间资金保持冻结，由回避的仲裁员裁决。</p>
<div class="card"><table>
<tr><th>订单号</th><th>物品</th><th>托管金额</th><th>状态</th><th>资金</th></tr>
{''.join(trs) or '<tr><td colspan=5 class=muted>暂无订单</td></tr>'}
</table></div>
<p class="muted">账内资金守恒校验：Σ 账户净值 =
<b class="amt">{money(sum(s.ledger_balances().values()))}</b>（恒为 0）；
当前托管冻结合计 <b class="amt">{money(s.escrow_total())}</b>。</p>
"""
        self._send(page("交易托管", body, self._flash(),
                        self._cookie_user(), self._users(s.conn)))

    def _confirm_chips(self, o) -> str:
        def chip(done, label):
            return (f'<span class="badge green">✓ {label}</span>' if done
                    else f'<span class="badge gray">○ {label}</span>')
        agree = chip(o["seller_confirmed_at"], "卖家约定") + " " + \
                chip(o["buyer_confirmed_at"], "买家约定")
        fund = chip(o["release_seller_at"], "卖家确认") + " " + \
               chip(o["release_buyer_at"], "买家确认")
        return f'<span class="metric"><b>约定</b>{agree}</span>' \
               f'<span class="metric"><b>资金确认</b>{fund}</span>'

    def _order_page(self, order_id: str):
        s = self.svc
        o = s.get_order(order_id)
        users = self._users(s.conn)
        as_user = self._cookie_user()
        remaining = s.window_remaining(order_id)
        in_window = o["status"] == svc.DELIVERED
        timer_green = "green" if (in_window and remaining > 0) else ""

        # 固定顶条：托管金额 / 剩余验货时间 / 双方确认状态
        escrow_label = {
            svc.ESCROW_PENDING: "托管（回执延迟中）",
            svc.ESCROWED: "托管已冻结",
            svc.DELIVERED: "托管已冻结",
            svc.DISPUTED: "托管争议冻结",
        }.get(o["status"], "托管金额")
        topbar = f"""
<div class="topbar">
  <span class="metric"><b>{h(escrow_label)}</b>
    <span class="amt">{money(o["amount"])}</span></span>
  <span class="metric"><b>剩余验货时间</b>
    <b id="timer" class="{timer_green}">{fmt_remaining(remaining) if in_window else "—"}</b></span>
  {self._confirm_chips(o)}
  <span class="metric"><b>状态</b><span class="badge">{h(svc.STATUS_LABELS[o["status"]])}</span></span>
</div>"""

        seller = s.conn.execute("SELECT name FROM users WHERE id=?", (o["seller_id"],)).fetchone()
        buyer = s.conn.execute("SELECT name FROM users WHERE id=?", (o["buyer_id"],)).fetchone()
        insp = ""
        if o["inspector_id"]:
            r = s.conn.execute("SELECT name FROM users WHERE id=?", (o["inspector_id"],)).fetchone()
            insp = f"<tr><th>验货人</th><td>{h(r['name'])}（{h(o['inspector_id'])}）</td></tr>"

        body = f"""
{topbar}
<h1 style="font-size:20px">{h(o['title'])} <span class="muted" style="font-size:13px">{h(o['code'])}</span></h1>
<div class="card"><table>
<tr><th>卖家</th><td>{h(seller['name'])}（{h(o['seller_id'])}）</td></tr>
<tr><th>买家</th><td>{h(buyer['name'])}（{h(o['buyer_id'])}）</td></tr>
{insp}
<tr><th>验货窗口</th><td>{o['window_sec']} 秒，截止 {h(o['inspection_deadline'])}</td></tr>
</table></div>
{self._action_card(s, o, as_user)}
{self._checklist_card(s, o)}
{self._inspection_card(s, o)}
{self._movements_card(s, o)}
{self._timeline_card(s, o)}
<p class="muted">存在争议？<a href="/dispute/{h(o['id'])}">前往争议页 →</a></p>
<script>
fetch('/order/{h(o['id'])}/status').then(r=>r.json()).then(d=>{{
  const el=document.getElementById('timer');
  if(!el) return;
  if(!d.in_window) {{ el.textContent='—'; el.classList.remove('green'); return; }}
  el.textContent=d.text;
  if(d.remaining>0) el.classList.add('green'); else el.classList.remove('green');
  setTimeout(()=>location.reload(), d.remaining<=0 ? 1500 : 30000);
}});
setInterval(()=>fetch('/order/{h(o['id'])}/status').then(r=>r.json()).then(d=>{{
  const el=document.getElementById('timer');
  if(el && d.in_window) el.textContent=d.text;
}}), 1000);
</script>
"""
        self._send(page(f"订单 {o['code']}", body, self._flash(), as_user, users))

    def _api_status(self, order_id: str):
        s = self.svc
        o = s.get_order(order_id)
        remaining = s.window_remaining(order_id)
        payload = {
            "in_window": o["status"] == svc.DELIVERED,
            "remaining": remaining,
            "text": fmt_remaining(remaining),
            "status": o["status"],
        }
        self._send(json.dumps(payload).encode(), ctype="application/json")

    def _action_card(self, s, o, as_user) -> str:
        act = f'<input type="hidden" name="order_id" value="{h(o["id"])}">'
        actor_note = f'<p class="muted">当前操作人：{h(as_user or "未选择（右上角切换）")}</p>'
        blocks = [actor_note]
        st = o["status"]

        if st == svc.PENDING_AGREEMENT:
            blocks.append(act + """
<div class="row"><button name="action" value="agree">确认约定</button>
<button class="ghost" name="action" value="cancel">卖家撤销交易</button></div>""")
        elif st == svc.AGREED:
            blocks.append(act + """
<div class="row"><button name="action" value="request_escrow">买家发起托管付款</button>
<button class="ghost" name="action" value="cancel">卖家撤销交易</button></div>
<p class="muted">付款经模拟支付网关，回执可能延迟。</p>""")
        elif st == svc.ESCROW_PENDING:
            blocks.append(act + """
<div class="row"><button name="action" value="poll_gateway">拉取付款回执（对账轮询）</button></div>
<p class="muted">回执未到达前托管不生效，资金不会被释放。</p>""")
        elif st == svc.ESCROWED:
            blocks.append(act + """
<h3>卖家交付 · 填写验货清单</h3>
<div class="row">序列号 <input type="text" name="serial_no" value="SN-2024-0817" style="width:200px"></div>
<div class="row">外观 <input type="text" name="appearance" value="95新，边角无磕碰" style="width:320px"></div>
<div class="row">配件 <input type="text" name="accessories" value="充电器、原装盒、数据线" style="width:320px"></div>
<div class="row">卖家自检结论
<select name="seller_conclusion">
<option value="conforming">与描述一致</option>
<option value="defective">发现故障（应如实披露）</option></select></div>
<div class="row"><input type="text" name="photo_label" value="DELIVERY SN-2024-0817" style="width:260px">
（将生成虚构现场照片）</div>
<div class="row"><button name="action" value="deliver">交付并开始验货计时</button></div>""")
        elif st == svc.DELIVERED:
            blocks.append(act + """
<h3>买家窗口内验收</h3>
<div class="row"><button class="green" name="action" value="inspect_accept">验收通过</button>
<button name="action" value="inspect_reject">拒收（物品与描述不符）</button></div>
<div class="row">备注 <input type="text" name="note" style="width:360px"></div>
<h3>验货人操作</h3>
<div class="row"><input type="number" name="extra_sec" value="3600" min="1" style="width:110px"> 秒
<input type="text" name="reason" value="买家路途延误，双方同意顺延" style="width:280px">
<button class="ghost" name="action" value="extend">验货人延期</button>
<button class="ghost" name="action" value="no_show">标记买家逾期不到</button></div>
<h3>争议</h3>
<div class="row"><select name="reason">
<option value="item_mismatch">物品与描述不符</option>
<option value="opposite_conclusions">双方结论相反</option>
<option value="hidden_defect">验货后发现隐藏故障</option>
<option value="other">其他</option></select>
<button class="ghost" name="action" value="open_dispute">发起争议（资金冻结）</button></div>""")
        elif st == svc.INSPECTION_PASSED:
            blocks.append(act + """
<div class="row"><button class="green" name="action" value="confirm_release">确认一致 · 同意放款</button></div>
<p class="muted">买卖双方都确认后款项才释放给卖家。</p>
<div class="row"><button class="ghost" name="action" value="open_dispute_hidden">
以「验货后发现隐藏故障」发起争议</button></div>
<input type="hidden" name="reason" value="hidden_defect">""")
        elif st == svc.INSPECTION_REJECTED:
            blocks.append(act + """
<div class="row"><button name="action" value="confirm_refund">卖家认可拒收 · 同意退款</button></div>
<p class="muted">买家拒收即买家侧确认；卖家确认后退款。有分歧则发起争议。</p>
<div class="row"><select name="reason">
<option value="item_mismatch">物品与描述不符</option>
<option value="opposite_conclusions">双方结论相反</option>
<option value="hidden_defect">验货后发现隐藏故障</option>
<option value="other">其他</option></select>
<button class="ghost" name="action" value="open_dispute">发起争议（资金冻结）</button></div>""")
        elif st == svc.BUYER_NO_SHOW:
            blocks.append(act + """
<div class="row"><select name="reason">
<option value="opposite_conclusions">双方结论相反</option>
<option value="other">其他</option></select>
<button class="ghost" name="action" value="open_dispute">发起争议</button></div>
<p class="muted">逾期后资金仍保持冻结，争议由仲裁裁决归属。</p>""")
        elif st in svc.TERMINAL_STATES:
            label = {"released": "款项已放款给卖家", "refunded": "款项已退款",
                     "cancelled": "交易已撤销（无资金流动）"}.get(st, "仲裁已执行资金裁决")
            blocks.append(f'<p class="concl ok">✓ {h(label)}</p>')

        if st == svc.DISPUTED:
            blocks.append(f'<p><a href="/dispute/{h(o["id"])}">→ 进入争议页提交证据 / 仲裁</a></p>')

        return ('<div class="card"><h3>操作</h3>'
                '<form method="post" action="/act">'
                + "".join(blocks) + "</form></div>")

    def _checklist_card(self, s, o) -> str:
        c = s.get_checklist(o["id"])
        if not c:
            return ""
        img = f'<img src="/photos/{h(c["photo_path"])}" alt="交付照片">' if c["photo_path"] else ""
        concl = ('<span class="concl ok">卖家自检：与描述一致</span>'
                 if c["seller_conclusion"] == "conforming"
                 else '<span class="concl">卖家自检：已披露故障</span>')
        return f"""
<div class="card"><h3>卖家验货清单</h3>
<div class="side"><div class="col"><table>
<tr><th>序列号</th><td>{h(c['serial_no'])}</td></tr>
<tr><th>外观</th><td>{h(c['appearance'])}</td></tr>
<tr><th>配件</th><td>{h(c['accessories'])}</td></tr>
<tr><th>结论</th><td>{concl}</td></tr>
<tr><th>备注</th><td>{h(c['note']) or '—'}</td></tr>
</table></div><div class="col">{img}</div></div></div>"""

    def _inspection_card(self, s, o) -> str:
        ins = s.get_inspection(o["id"])
        if not ins:
            return '<div class="card"><h3>买家验收</h3><p class="muted">尚未提交验收结论。</p></div>'
        img = f'<tr><th>现场照片</th><td><img src="/photos/{h(ins["photo_path"])}" style="max-width:300px"></td></tr>' if ins["photo_path"] else ""
        if ins["conclusion"] == "accept":
            concl = '<span class="concl ok">✓ 验收通过（与描述一致）</span>'
        else:
            concl = '<span class="concl">✗ 拒收（物品与描述不符）</span>'
        return f"""
<div class="card"><h3>买家验收结论</h3><table>
<tr><th>结论</th><td>{concl}</td></tr>
<tr><th>备注</th><td>{h(ins['note']) or '—'}</td></tr>
<tr><th>时间</th><td>{h(ins['created_at'])}</td></tr>{img}</table></div>"""

    def _movements_card(self, s, o) -> str:
        mvs = s.list_movements(o["id"])
        if not mvs:
            return ""
        rows = []
        label = {"hold": "托管冻结", "release": "放款", "refund": "退款"}
        for m in mvs:
            rcpt = h(m["receipt_no"] or "（回执未到）")
            st_cls = "badge green" if m["status"] == "confirmed" else "badge gray"
            rows.append(
                f'<tr><td>{label[m["direction"]]}</td>'
                f'<td class="amt">{money(m["amount"])}</td>'
                f'<td><span class="{st_cls}">{h(m["status"])}</span></td>'
                f'<td class="muted">{rcpt}</td></tr>')
        return f"""
<div class="card"><h3>托管流水（付款 / 放款 / 退款互斥，回执幂等）</h3>
<table><tr><th>方向</th><th>金额</th><th>状态</th><th>回执</th></tr>{''.join(rows)}</table></div>"""

    def _timeline_card(self, s, o) -> str:
        events = s.list_events(o["id"])
        lis = []
        for e in reversed(events):
            payload = f' <span class="muted">{h(e["payload"])}</span>' if e["payload"] else ""
            lis.append(f'<li>{h(e["created_at"])} · {h(e["kind"])} · {h(e["actor_id"] or "系统")}{payload}</li>')
        return f'<div class="card"><h3>事件流</h3><ul class="timeline">{"".join(lis)}</ul></div>'

    # ============ 争议页 ============
    def _dispute_page(self, order_id: str):
        s = self.svc
        o = s.get_order(order_id)
        users = self._users(s.conn)
        as_user = self._cookie_user()
        c = s.get_checklist(o["id"])
        ins = s.get_inspection(o["id"])

        # 左：商品描述；右：现场照片（交付 + 验收）
        desc_img = ""
        delivery_img = f'<p class="muted">卖家交付时未附照片</p>'
        if c and c["photo_path"]:
            delivery_img = (f'<img src="/photos/{h(c["photo_path"])}" alt="交付照片">'
                            f'<p class="muted">卖家交付照片 · 序列号 {h(c["serial_no"])}</p>')
        buyer_img = ""
        if ins and ins["photo_path"]:
            buyer_img = (f'<img src="/photos/{h(ins["photo_path"])}" alt="验收照片" '
                         f'style="margin-top:10px"><p class="muted">买家验收照片</p>')

        reason = {"item_mismatch": "物品与描述不符", "opposite_conclusions": "双方结论相反",
                  "hidden_defect": "验货后发现隐藏故障", "other": "其他"}.get(
            o["dispute_reason"], "—")

        # 证据列表
        evs = s.list_evidence(o["id"])
        ev_cards = []
        for e in evs:
            ok = e["conclusion"] == "conforming"
            chip = ('<span class="concl ok">主张：与描述一致</span>' if ok
                    else '<span class="concl">主张：存在不符/故障</span>')
            img = f'<img src="/photos/{h(e["photo_path"])}">' if e["photo_path"] else ""
            ev_cards.append(f"""
<div class="ev">{img}<p>{chip}</p>
<p>{h(e['note'])}</p><p class="muted">{h(e['submitter_name'])} · {h(e['created_at'])}</p></div>""")

        dispute_row = s.conn.execute(
            "SELECT * FROM disputes WHERE order_id=?", (order_id,)).fetchone()
        arb = s.conn.execute(
            "SELECT * FROM arbitrations WHERE order_id=?", (order_id,)).fetchone()

        if arb:
            arbiter = s.conn.execute("SELECT name FROM users WHERE id=?",
                                     (arb["arbiter_id"],)).fetchone()
            ruling = "放款给卖家" if arb["ruling"] == "release_to_seller" else "退款给买家"
            arb_html = f"""<div class="card"><h3>仲裁裁决</h3>
<p class="concl ok">✓ {ruling}</p>
<p>{h(arb['rationale'])}</p><p class="muted">仲裁员：{h(arbiter['name'])} · {h(arb['created_at'])}</p></div>"""
        elif dispute_row and dispute_row["status"] == "open":
            arb_html = f"""
<div class="card"><h3>仲裁（仅仲裁员；须回避本人参与的交易）</h3>
<form method="post" action="/act">
<input type="hidden" name="order_id" value="{h(o['id'])}">
<div class="row">裁决：
<select name="ruling"><option value="release_to_seller">放款给卖家</option>
<option value="refund_to_buyer">退款给买家</option></select></div>
<div class="row"><textarea name="rationale" rows="2" placeholder="裁决理由（必填）"></textarea></div>
<div class="row"><button name="action" value="arbitrate">作出裁决并执行资金动作</button></div>
</form></div>"""
        else:
            arb_html = '<div class="card"><h3>争议 / 仲裁</h3><p class="muted">本单暂无争议。</p></div>'

        evidence_form = ""
        if dispute_row and dispute_row["status"] == "open":
            evidence_form = f"""
<div class="card"><h3>提交证据（双方可提交相反结论）</h3>
<form method="post" action="/act">
<input type="hidden" name="order_id" value="{h(o['id'])}">
<div class="row">结论：<select name="conclusion">
<option value="conforming">与描述一致</option>
<option value="defective">不符 / 存在故障</option></select>
<input type="text" name="photo_label" value="EVIDENCE PHOTO" style="width:220px"></div>
<div class="row"><textarea name="note" rows="2" placeholder="证据说明（必填）"></textarea></div>
<div class="row"><button name="action" value="evidence">提交证据（生成虚构照片）</button></div>
</form></div>"""

        body = f"""
<div class="topbar"><span class="metric"><b>争议状态</b>
<span class="badge">{h(reason)}</span></span>
<span class="metric"><b>托管金额（冻结中）</b><span class="amt">{money(o['amount'])}</span></span>
<span class="metric"><b>订单</b>{h(o['code'])}</span></div>
<h1>争议处置</h1>
<div class="side">
  <div class="col"><h3 style="margin-top:0">商品描述（约定口径）</h3>
    <p><b>{h(o['title'])}</b></p><p>{h(o['description'])}</p>
    <hr style="border:none;border-top:1px solid {LINE}">
    <p class="muted">卖家清单：{h(c['serial_no'] if c else '—')} · {h(c['appearance'] if c else '—')}<br>
    配件：{h(c['accessories'] if c else '—')}</p></div>
  <div class="col"><h3 style="margin-top:0">现场照片（交付 / 验收）</h3>
    {delivery_img}{buyer_img or '<p class="muted">买家验收时未附照片</p>'}</div>
</div>
{evidence_form}
<div class="card"><h3>双方证据（相反结论并列）</h3>
<div class="evgrid">{''.join(ev_cards) or '<p class="muted">暂无证据。</p>'}</div></div>
{arb_html}
<p><a href="/order/{h(o['id'])}">← 返回交易页</a></p>"""
        self._send(page(f"争议 {o['code']}", body, self._flash(), as_user, users))

    def _photo(self, name: str):
        # 只允许文件名，杜绝路径穿越
        name = Path(name).name
        data = (Path(self.photos_dir) / name).read_bytes()
        self._send(data, ctype="image/png")

    # ============ POST 动作 ============
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
        f = lambda k, d="": form.get(k, [d])[0]
        try:
            if parsed.path == "/act/switch_user":
                self._redirect("/", set_user=f("user_id"))
                return
            self._dispatch(f, parsed.path)
        except Exception as e:  # DomainError / PaymentError 等统一回显
            back = f"/order/{f('order_id')}" if f("order_id") else "/"
            self._redirect(back, f"ERR:{e}")

    def _dispatch(self, f, path):
        s = self.svc
        user = self._cookie_user()
        action = f("action")
        order_id = f("order_id")
        if not user:
            raise svc.AuthError("请先在右上角选择当前操作人")

        back = f"/order/{order_id}" if order_id else "/"

        if action == "agree":
            s.agree(order_id, user)
        elif action == "cancel":
            s.cancel(order_id, user, f("reason"))
        elif action == "request_escrow":
            s.request_escrow(order_id, user)
            self._redirect(back, "付款已提交至模拟网关，回执延迟中，稍后请点「拉取付款回执」。")
            return
        elif action == "poll_gateway":
            n = s.poll_gateway()
            self._redirect(back, f"对账轮询完成，{n} 笔延迟回执已确认，托管资金冻结。" if n
                           else "暂无新回执（付款仍在延迟中）。")
            return
        elif action == "deliver":
            s.deliver(order_id, user, serial_no=f("serial_no"), appearance=f("appearance"),
                      accessories=f("accessories"), seller_conclusion=f("seller_conclusion"),
                      note=f("note"), photo_label=f("photo_label") or None)
        elif action == "inspect_accept":
            s.inspect(order_id, user, "accept", note=f("note"),
                      photo_label="BUYER ACCEPT PHOTO", photo_rgb=(214, 228, 216))
        elif action == "inspect_reject":
            s.inspect(order_id, user, "reject", note=f("note") or "物品与描述不符",
                      photo_label="BUYER REJECT PHOTO", photo_rgb=(176, 176, 176))
        elif action == "extend":
            s.extend_window(order_id, user, int(f("extra_sec") or "0"), f("reason"))
        elif action == "no_show":
            s.mark_buyer_no_show(order_id, user)
        elif action in ("open_dispute", "open_dispute_hidden"):
            reason = f("reason") if action == "open_dispute" else "hidden_defect"
            s.open_dispute(order_id, user, reason)
            self._redirect(f"/dispute/{order_id}", "争议已开启，托管资金保持冻结。")
            return
        elif action == "confirm_release":
            s.confirm_release(order_id, user)
            o = s.get_order(order_id)
            if o["status"] == svc.RELEASED:
                self._redirect(back, "双方确认一致，款项已放款给卖家。")
                return
        elif action == "confirm_refund":
            s.confirm_refund(order_id, user)
            o = s.get_order(order_id)
            if o["status"] == svc.REFUNDED:
                self._redirect(back, "卖家认可拒收，款项已退款。")
                return
        elif action == "evidence":
            label = f("photo_label") or "EVIDENCE"
            rgb = (214, 228, 216) if f("conclusion") == "conforming" else (176, 176, 176)
            s.submit_evidence(order_id, user, f("conclusion"), f("note"),
                              photo_label=label, photo_rgb=rgb)
            self._redirect(f"/dispute/{order_id}", "证据已提交。")
            return
        elif action == "arbitrate":
            s.arbitrate(order_id, user, f("ruling"), f("rationale"))
            self._redirect(f"/dispute/{order_id}", "裁决已作出，资金动作已执行。")
            return
        else:
            raise svc.DomainError(f"未知操作: {action}")

        self._redirect(back, "操作成功。")


def serve(db_path: str, photos_dir: str, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = False, clock: Clock | None = None) -> None:
    init_db(connect(db_path))
    App.db_path = db_path
    App.photos_dir = photos_dir
    App.clock = clock
    httpd = ThreadingHTTPServer((host, port), App)
    url = f"http://{host}:{port}/"
    print(f"✅ 当面交易托管服务已启动：{url}", flush=True)
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
