"""Web 界面（仅标准库 WSGI）：交易列表、交易页、争议页。

视觉约束：白底、深灰文字与边框，绿色仅用于结论性提示
（双方确认、仲裁结论、资金守恒校验通过）。
"""
import html
import os
import re
from datetime import datetime
from urllib.parse import parse_qs, quote

from . import domain as d

CSS = """
:root{--ink:#2b2f33;--muted:#6b7280;--line:#e5e7eb;--soft:#f3f4f6;--green:#1a7f37}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
     font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--ink)}
header.top{border-bottom:1px solid var(--line);padding:12px 24px;display:flex;
           justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header.top .brand{font-weight:700}
header.top nav a{margin-left:10px;color:var(--muted);text-decoration:none;font-size:13px}
header.top nav a.on{color:var(--ink);font-weight:600;border-bottom:2px solid var(--ink)}
.wrap{max-width:1080px;margin:0 auto;padding:18px 24px 64px}
h1{font-size:20px;margin:6px 0}
h2{font-size:16px;margin:0 0 10px}
h3{font-size:14px;margin:12px 0 6px}
.muted{color:var(--muted)}
.ok{color:var(--green)}
.sticky{position:sticky;top:0;z-index:5;background:#fff;border:1px solid var(--line);
        border-radius:10px;padding:12px 18px;display:flex;gap:30px;flex-wrap:wrap;
        box-shadow:0 1px 4px rgba(0,0,0,.06);margin:14px 0}
.metric span{display:block;color:var(--muted);font-size:12px}
.metric b{font-size:17px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:13px}
.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;
       padding:1px 10px;font-size:12px;color:var(--muted);white-space:nowrap}
.badge.green{color:var(--green);border-color:var(--green)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin-bottom:16px}
.photos{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.photos img{width:170px;border:1px solid var(--line);border-radius:8px;display:block}
form.act{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:12px}
form.act h3{margin:0 0 6px}
form.act p.hint{margin:0 0 8px;color:var(--muted);font-size:13px}
input[type=text],input[type=number],select,textarea{width:100%;padding:7px 9px;
  border:1px solid var(--line);border-radius:6px;margin:4px 0 10px;font:inherit;
  background:#fff;color:var(--ink)}
button,.btn{background:var(--ink);color:#fff;border:none;border-radius:6px;
  padding:8px 16px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.flash{border:1px solid var(--green);color:var(--green);border-radius:8px;
       padding:10px 14px;margin-bottom:14px}
.err{border:1px solid var(--ink);background:var(--soft);border-radius:8px;
     padding:10px 14px;margin-bottom:14px}
.timeline{list-style:none;padding-left:0;margin:0}
.timeline li{border-left:2px solid var(--line);padding:4px 0 4px 12px;margin-left:4px}
.timeline .t{color:var(--muted);font-size:12px;margin-right:8px;font-variant-numeric:tabular-nums}
.verdict{border:1px solid var(--green);border-radius:10px;padding:14px 16px;color:var(--green)}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 12px;margin:0}
.kv dt{color:var(--muted);font-size:13px}
.kv dd{margin:0}
.checks{display:flex;gap:10px;flex-wrap:wrap;margin:4px 0 10px}
.checks label{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px}
.checks img{width:110px;display:block;border-radius:6px;margin-bottom:4px}
"""

ACTION_LABELS = {
    "DEAL_AGREED": "交易约定",
    "AMOUNT_ADJUSTED": "调整托管金额",
    "PAYMENT_STARTED": "买家付款",
    "ESCROW_HELD": "资金托管",
    "PAYMENT_DELAY": "回执延迟",
    "LATE_RECEIPT_REFUNDED": "迟到回执退款",
    "SELLER_CANCEL": "卖家撤销",
    "DELIVERED": "卖家交付",
    "EXTENSION_REQUESTED": "申请延期",
    "EXTENSION_APPROVED": "同意延期",
    "EXTENSION_REJECTED": "拒绝延期",
    "CONFIRM": "确认验货",
    "DISPUTE_OPENED": "提起争议",
    "BUYER_NO_SHOW": "买家逾期申诉",
    "EVIDENCE_SUBMITTED": "提交证据",
    "CONFLICTING_CONCLUSIONS": "结论相反",
    "ARBITRATOR_ASSIGNED": "指派仲裁员",
    "ARBITRATION_RULED": "仲裁裁决",
    "RELEASED": "放款",
}

LEDGER_LABELS = {"HOLD": "托管冻结", "RELEASE": "放款给卖家", "REFUND": "退款给买家"}
ACCOUNT_LABELS = {"buyer": "买家", "escrow": "平台托管", "seller": "卖家"}
CONCLUSION_LABELS = {"MATCH": "与描述相符", "MISMATCH": "与描述不符"}


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def yuan(cents):
    return f"¥{cents / 100:,.2f}"


def split_photos(s):
    return [p for p in (s or "").split(",") if p]


def photo_imgs(names):
    if not names:
        return '<p class="muted">暂无照片</p>'
    return '<div class="photos">' + "".join(
        f'<a href="/photos/{esc(n)}" target="_blank"><img src="/photos/{esc(n)}" alt="{esc(n)}"></a>'
        for n in names) + "</div>"


class App:
    def __init__(self, db, service, photos_dir):
        self.db = db
        self.svc = service
        self.photos_dir = photos_dir

    # ------------------------------------------------------------ 基础
    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        query = parse_qs(environ.get("QUERY_STRING", ""))
        try:
            if path == "/" and method == "GET":
                return self._html(start_response, self.home(query))
            if path == "/deal" and method == "GET":
                return self._html(start_response, self.deal_page(query))
            if path == "/dispute" and method == "GET":
                return self._html(start_response, self.dispute_page(query))
            if path == "/action" and method == "POST":
                return self.action(environ, start_response)
            if path == "/static/style.css" and method == "GET":
                return self._respond(start_response, CSS, "text/css; charset=utf-8")
            if path.startswith("/photos/") and method == "GET":
                return self.photo(path, start_response)
            return self._html(start_response, self.shell("未找到", "<p>页面不存在。</p>", None),
                              "404 Not Found")
        except d.DomainError as exc:
            body = (f'<div class="err">操作被拒绝：{esc(exc)}</div>'
                    f'<p><a class="btn ghost" href="/">返回首页</a></p>')
            return self._html(start_response, self.shell("操作被拒绝", body, None),
                              "400 Bad Request")

    def _respond(self, start_response, body, content_type, status="200 OK"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        start_response(status, [("Content-Type", content_type),
                                ("Content-Length", str(len(data)))])
        return [data]

    def _html(self, start_response, body, status="200 OK"):
        return self._respond(start_response, body, "text/html; charset=utf-8", status)

    def _redirect(self, start_response, location):
        start_response("303 See Other", [("Location", location)])
        return [b""]

    def photo(self, path, start_response):
        name = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.svg", name):
            return self._respond(start_response, "not found", "text/plain", "404 Not Found")
        fp = os.path.join(self.photos_dir, name)
        if not os.path.isfile(fp):
            return self._respond(start_response, "not found", "text/plain", "404 Not Found")
        with open(fp, "rb") as fh:
            return self._respond(start_response, fh.read(), "image/svg+xml")

    def _current_user(self, query):
        users = self.db.q("SELECT * FROM users ORDER BY id")
        if not users:
            return None, []
        as_id = (query.get("as") or [""])[0]
        current = None
        if as_id.isdigit():
            current = next((u for u in users if u["id"] == int(as_id)), None)
        if current is None:  # 默认视角：第一位买家
            current = next((u for u in users if u["role"] == "buyer"), users[0])
        return current, users

    def shell(self, title, body, current_user, users=None, query_as=None):
        users = users if users is not None else self.db.q("SELECT * FROM users ORDER BY id")
        switcher = " ".join(
            '<a class="{on}" href="?as={id}">{name}·{role}</a>'.format(
                on="on" if current_user and u["id"] == current_user["id"] else "",
                id=u["id"], name=esc(u["name"]), role=d.ROLE_LABELS[u["role"]])
            for u in users)
        as_q = f"?as={current_user['id']}" if current_user else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 当面验货托管</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<header class="top">
  <div><a class="brand" href="/{as_q}">当面验货托管</a></div>
  <nav><span class="muted">视角切换</span> {switcher}</nav>
</header>
<div class="wrap">{body}</div>
</body></html>"""

    def _banner(self, query):
        out = []
        if query.get("msg"):
            out.append(f'<div class="flash">{esc(query["msg"][0])}</div>')
        if query.get("err"):
            out.append(f'<div class="err">{esc(query["err"][0])}</div>')
        return "".join(out)

    # ------------------------------------------------------------ 首页
    def home(self, query):
        user, users = self._current_user(query)
        report = self.svc.conservation_report()
        deals = self.svc.list_deals()
        rows = []
        for deal in deals:
            frozen = self._frozen(deal["id"])
            badge_cls = "badge green" if deal["state"] == d.STATE_COMPLETED else "badge"
            rows.append(
                f'<tr><td><a href="/deal?id={deal["id"]}&as={user["id"]}">{esc(deal["code"])}</a></td>'
                f"<td>{esc(deal['title'])}</td>"
                f"<td>{yuan(deal['amount_cents'])}</td>"
                f'<td><span class="{badge_cls}">{d.STATE_LABELS[deal["state"]]}</span></td>'
                f"<td>{yuan(frozen)}</td>"
                f"<td class='muted'>{esc(deal['updated_at'])}</td></tr>")
        table = ("<table><tr><th>单号</th><th>物品</th><th>托管金额</th><th>状态</th>"
                 "<th>当前冻结</th><th>更新时间</th></tr>" + "".join(rows) + "</table>")
        if report["ok"]:
            check = '<span class="ok">✓ 资金守恒校验通过</span>'
        else:
            check = '<span>✗ 资金异常：' + esc("；".join(report["problems"])) + "</span>"
        t = report["totals"]
        overview = (
            f'<div class="card"><h2>平台资金总览</h2>'
            f'<p>累计托管 <b>{yuan(t["HOLD"])}</b> · 已放款 <b>{yuan(t["RELEASE"])}</b> · '
            f'已退款 <b>{yuan(t["REFUND"])}</b> · 冻结中 <b>{yuan(report["frozen_total"])}</b></p>'
            f"<p>{check}</p></div>")
        create_form = ""
        if user and user["role"] in ("platform", "seller"):
            create_form = self._create_form(user, users)
        body = (self._banner(query)
                + "<h1>交易单</h1>"
                + '<p class="muted">约定 → 托管 → 当面验货 → 双方确认 → 放款；争议期间资金保持冻结。</p>'
                + overview + table + create_form)
        return self.shell("交易单", body, user, users)

    def _frozen(self, deal_id):
        from . import ledger
        return ledger.frozen(self.db.conn, deal_id)

    def _create_form(self, user, users):
        buyers = [u for u in users if u["role"] == "buyer"]
        sellers = [u for u in users if u["role"] == "seller"]
        inspectors = [u for u in users if u["role"] == "inspector"]
        opts = lambda us: "".join(f'<option value="{u["id"]}">{esc(u["name"])}</option>' for u in us)
        return f"""<div class="card"><h2>生成交易单（双方已约定当面验货）</h2>
<form class="act" method="post" action="/action">
<input type="hidden" name="as" value="{user['id']}">
<input type="hidden" name="action" value="create_deal">
<p class="hint">物品名称</p><input type="text" name="title" required>
<p class="hint">商品描述</p><textarea name="description" rows="2" required></textarea>
<p class="hint">托管金额（元）</p><input type="number" name="amount" min="1" step="0.01" required>
<p class="hint">买家</p><select name="buyer_id">{opts(buyers)}</select>
<p class="hint">卖家</p><select name="seller_id">{opts(sellers)}</select>
<p class="hint">验货人</p><select name="inspector_id">{opts(inspectors)}</select>
<p class="hint">验货窗口（分钟）</p><input type="number" name="window" value="30" min="5" max="240">
<button type="submit">生成交易单</button>
</form></div>"""

    # ------------------------------------------------------------ 交易页
    def deal_page(self, query):
        user, users = self._current_user(query)
        deal_id = int(query["id"][0])
        view = self.svc.deal_view(deal_id)
        deal = view["deal"]
        confirmed = {c["party"] for c in view["confirmations"]}
        buyer_mark = ('<b class="ok">✓ 已确认</b>' if "buyer" in confirmed
                      else "<b>待确认</b>")
        seller_mark = ('<b class="ok">✓ 已确认</b>' if "seller" in confirmed
                       else "<b>待确认</b>")
        if deal["state"] == d.STATE_INSPECTING and deal["inspection_deadline"]:
            deadline = datetime.fromisoformat(deal["inspection_deadline"])
            remain = (deadline - datetime.now()).total_seconds()
            cd = (f'<b id="countdown" data-deadline="{esc(deal["inspection_deadline"])}">'
                  f'{"已逾期" if remain <= 0 else "…"}</b>')
            countdown_js = COUNTDOWN_JS
        else:
            cd = "<b>—</b>"
            countdown_js = ""
        sticky = f"""<div class="sticky">
  <div class="metric"><span>托管金额</span><b>{yuan(deal['amount_cents'])}</b></div>
  <div class="metric"><span>剩余验货时间</span>{cd}</div>
  <div class="metric"><span>买家确认</span>{buyer_mark}</div>
  <div class="metric"><span>卖家确认</span>{seller_mark}</div>
  <div class="metric"><span>状态</span><b>{d.STATE_LABELS[deal['state']]}</b></div>
</div>"""
        parts = [self._banner(query),
                 f'<p><a href="/?as={user["id"]}">← 全部交易单</a>'
                 + (f' · <a href="/dispute?id={deal_id}&as={user["id"]}">争议详情 →</a>'
                    if view["dispute"] else "")
                 + "</p>",
                 f"<h1>{esc(deal['code'])} · {esc(deal['title'])}</h1>",
                 sticky,
                 self._deal_info_card(view),
                 self._checklist_card(view),
                 self._actions_card(view, user),
                 self._ledger_card(view),
                 self._timeline_card(view),
                 countdown_js]
        return self.shell(f"{deal['code']} 交易", "".join(parts), user, users)

    def _deal_info_card(self, view):
        deal, users = view["deal"], view["users"]
        inspector = (users[deal["inspector_id"]]["name"] if deal["inspector_id"] else "—")
        return f"""<div class="card"><h2>交易信息</h2>
<dl class="kv">
<dt>商品描述</dt><dd>{esc(deal['description'])}</dd>
<dt>托管金额</dt><dd>{yuan(deal['amount_cents'])}（当前冻结 {yuan(view['frozen'])}）</dd>
<dt>买家</dt><dd>{esc(users[deal['buyer_id']]['name'])}</dd>
<dt>卖家</dt><dd>{esc(users[deal['seller_id']]['name'])}</dd>
<dt>验货人</dt><dd>{esc(inspector)}</dd>
<dt>验货窗口</dt><dd>{deal['inspection_window_minutes']} 分钟</dd>
<dt>验货截止</dt><dd>{esc(deal['inspection_deadline'] or '—')}</dd>
</dl>{photo_imgs(split_photos(deal['photos']))}</div>"""

    def _checklist_card(self, view):
        cl = view["checklist"]
        if not cl:
            return ""
        return f"""<div class="card"><h2>验货清单（卖家交付时填写）</h2>
<dl class="kv">
<dt>序列号</dt><dd>{esc(cl['serial_number'])}</dd>
<dt>外观</dt><dd>{esc(cl['appearance'])}</dd>
<dt>配件</dt><dd>{esc(cl['accessories'])}</dd>
</dl>{photo_imgs(split_photos(cl['photos']))}</div>"""

    def _ledger_card(self, view):
        if not view["ledger"]:
            return ""
        rows = "".join(
            f"<tr><td>{LEDGER_LABELS[e['kind']]}</td><td>{yuan(e['amount_cents'])}</td>"
            f"<td>{ACCOUNT_LABELS[e['from_account']]} → {ACCOUNT_LABELS[e['to_account']]}</td>"
            f"<td class='muted'>{esc(e['note'])}</td>"
            f"<td class='muted'>{esc(e['created_at'])}</td></tr>"
            for e in view["ledger"])
        return (f'<div class="card"><h2>托管流水</h2><table>'
                f"<tr><th>类型</th><th>金额</th><th>流向</th><th>备注</th><th>时间</th></tr>"
                f"{rows}</table></div>")

    def _timeline_card(self, view):
        items = "".join(
            f"<li><span class='t'>{esc(e['created_at'])}</span>"
            f"<b>{ACTION_LABELS.get(e['action'], e['action'])}</b> {esc(e['detail'])}</li>"
            for e in view["events"])
        return f'<div class="card"><h2>时间线</h2><ul class="timeline">{items}</ul></div>'

    # ------------------------------------------------------------ 操作区
    def _actions_card(self, view, user):
        if not user:
            return ""
        deal = view["deal"]
        state = deal["state"]
        forms = []
        hidden = (f'<input type="hidden" name="as" value="{user["id"]}">'
                  f'<input type="hidden" name="deal_id" value="{deal["id"]}">')

        if user["id"] == deal["buyer_id"]:
            if state == d.STATE_AGREED:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="pay">
<h3>买家付款</h3><p class="hint">付款 {yuan(deal['amount_cents'])} 进入平台托管，等待支付回执。</p>
<button type="submit">付款</button></form>""")
            if state == d.STATE_INSPECTING:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="confirm">
<h3>确认验货一致</h3><p class="hint">在验货窗口内确认物品与描述一致。</p>
<button type="submit">确认一致</button></form>""")
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="reject">
<h3>拒收并提起争议</h3><p class="hint">资金将保持冻结，等待仲裁。</p>
<select name="reason">
<option value="{d.REASON_ITEM_MISMATCH}">物品与描述不符</option>
<option value="{d.REASON_HIDDEN_FAULT}">验货后发现隐藏故障</option>
</select>
<textarea name="text" rows="2" placeholder="问题说明" required></textarea>
{self._photo_picker()}
<button type="submit">拒收并冻结资金</button></form>""")
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="request_extension">
<h3>申请延期</h3><p class="hint">验货时间不足时申请延长窗口（需卖家同意）。</p>
<input type="number" name="minutes" value="30" min="1" max="240">
<button type="submit">申请延期</button></form>""")

        if user["id"] == deal["seller_id"]:
            if state in (d.STATE_AGREED, d.STATE_FUNDING, d.STATE_PAYMENT_DELAYED,
                         d.STATE_ESCROWED):
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="cancel">
<h3>撤销交易</h3><p class="hint">已托管的资金将原路退回买家。</p>
<button type="submit" class="ghost">撤销交易</button></form>""")
            if state == d.STATE_ESCROWED:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="deliver">
<h3>交付物品并填写验货清单</h3>
<input type="text" name="serial" placeholder="序列号" required>
<input type="text" name="appearance" placeholder="外观状况" required>
<input type="text" name="accessories" placeholder="配件清单" required>
{self._photo_picker()}
<button type="submit">交付并开启验货窗口</button></form>""")
            if state == d.STATE_INSPECTING:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="confirm">
<h3>确认验货一致</h3><p class="hint">双方均确认后，平台放款给卖家。</p>
<button type="submit">确认一致</button></form>""")
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="claim_no_show">
<h3>申诉买家逾期不到</h3><p class="hint">验货窗口结束后买家仍未确认时可申诉。</p>
<button type="submit" class="ghost">申诉逾期</button></form>""")
            pending_ext = [e for e in view["extensions"] if e["status"] == "PENDING"]
            for ext in pending_ext:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="decide_extension">
<input type="hidden" name="extension_id" value="{ext['id']}">
<h3>延期申请待审批</h3><p class="hint">买家申请延长验货窗口 {ext['extra_minutes']} 分钟。</p>
<button type="submit" name="decision" value="approve">同意</button>
<button type="submit" name="decision" value="reject" class="ghost">拒绝</button></form>""")

        if user["role"] == "platform":
            if state == d.STATE_AGREED:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="adjust_amount">
<h3>调整托管金额（仅平台，付款前）</h3>
<input type="number" name="amount" min="1" step="0.01" value="{deal['amount_cents'] / 100:.2f}">
<button type="submit">调整</button></form>""")
            for rcpt in view["receipts"]:
                if rcpt["status"] == "PENDING":
                    forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="deliver_receipt">
<input type="hidden" name="receipt_id" value="{esc(rcpt['id'])}">
<h3>模拟渠道回执送达</h3><p class="hint">回执 {esc(rcpt['id'])} 尚未送达，点击模拟渠道回调。</p>
<button type="submit">送达回执</button></form>""")
            if state == d.STATE_FUNDING:
                forms.append(f"""<form class="act" method="post" action="/action">{hidden}
<input type="hidden" name="action" value="sweep">
<h3>扫描付款超时</h3><p class="hint">将回执超时的交易标记为「付款回执延迟」。</p>
<button type="submit" class="ghost">立即扫描</button></form>""")

        if not forms:
            return ""
        return '<div class="card"><h2>可执行操作</h2>' + "".join(forms) + "</div>"

    def _photo_picker(self):
        try:
            names = sorted(n for n in os.listdir(self.photos_dir) if n.endswith(".svg"))
        except OSError:
            names = []
        if not names:
            return ""
        boxes = "".join(
            f'<label><img src="/photos/{esc(n)}" alt=""><br>'
            f'<input type="checkbox" name="photo" value="{esc(n)}"> {esc(n)}</label>'
            for n in names)
        return f'<p class="hint">附加现场照片（可选）</p><div class="checks">{boxes}</div>'

    # ------------------------------------------------------------ 争议页
    def dispute_page(self, query):
        user, users = self._current_user(query)
        deal_id = int(query["id"][0])
        view = self.svc.deal_view(deal_id)
        deal, disp = view["deal"], view["dispute"]
        if not disp:
            body = (f'<p><a href="/deal?id={deal_id}&as={user["id"]}">← 返回交易</a></p>'
                    "<p>该交易暂无争议。</p>")
            return self.shell("争议", body, user, users)
        checklist = view["checklist"]
        site_photos = set(split_photos(checklist["photos"]) if checklist else [])
        for ev in view["evidence"]:
            site_photos.update(split_photos(ev["photos"]))
        left = f"""<div class="card"><h2>商品描述</h2>
<p>{esc(deal['description'])}</p>
{photo_imgs(split_photos(deal['photos']))}
<h3>卖家交付清单</h3>
<dl class="kv">
<dt>序列号</dt><dd>{esc(checklist['serial_number']) if checklist else '—'}</dd>
<dt>外观</dt><dd>{esc(checklist['appearance']) if checklist else '—'}</dd>
<dt>配件</dt><dd>{esc(checklist['accessories']) if checklist else '—'}</dd>
</dl></div>"""
        right = f"""<div class="card"><h2>现场照片</h2>
{photo_imgs(sorted(site_photos))}</div>"""
        ev_rows = []
        for ev in view["evidence"]:
            concl = ev["conclusion"]
            if concl == "MATCH":
                badge = '<span class="badge green">与描述相符</span>'
            elif concl == "MISMATCH":
                badge = '<span class="badge">与描述不符</span>'
            else:
                badge = '<span class="badge">未表态</span>'
            ev_rows.append(
                f"<tr><td>{esc(ev['submitter'])}（{d.ROLE_LABELS[ev['submitter_role']]}）</td>"
                f"<td>{badge}</td><td>{esc(ev['text'])}</td>"
                f"<td class='muted'>{esc(ev['created_at'])}</td></tr>")
        evidence_card = (
            '<div class="card"><h2>证据与结论</h2><table>'
            "<tr><th>提交人</th><th>结论</th><th>说明</th><th>时间</th></tr>"
            + "".join(ev_rows) + "</table></div>") if ev_rows else ""
        arb_card = self._arbitration_card(view, user)
        ev_form = self._evidence_form(view, user)
        state_label = {"OPEN": "待指派仲裁", "ARBITRATING": "仲裁中",
                       "RESOLVED": "已结案"}[disp["state"]]
        body = (self._banner(query)
                + f'<p><a href="/deal?id={deal_id}&as={user["id"]}">← 返回交易</a></p>'
                + f"<h1>争议 · {esc(deal['code'])} {esc(deal['title'])}</h1>"
                + f'<p><span class="badge">{d.REASON_LABELS[disp["reason"]]}</span> '
                + f'<span class="badge">{state_label}</span> '
                + f'<span class="muted">托管 {yuan(deal["amount_cents"])} 保持冻结</span></p>'
                + f'<div class="grid2">{left}{right}</div>'
                + evidence_card + arb_card + ev_form)
        return self.shell(f"{deal['code']} 争议", body, user, users)

    def _arbitration_card(self, view, user):
        disp = view["dispute"]
        deal = view["deal"]
        users = view["users"]
        out = []
        if disp["state"] == "RESOLVED":
            label = "放款给卖家" if disp["verdict"] == "RELEASE" else "退款给买家"
            out.append(f'<div class="verdict"><b>仲裁结论：{label}</b>'
                       f'<br>{esc(disp["verdict_note"] or "")}'
                       f'<br><span class="muted">{esc(disp["resolved_at"] or "")}</span></div>')
        else:
            arb = users[disp["arbitrator_id"]]["name"] if disp["arbitrator_id"] else "待指派"
            out.append(f'<div class="card"><h2>仲裁</h2><p>仲裁员：{esc(arb)}</p>')
            if user and user["role"] == "platform" and disp["state"] == "OPEN":
                arbs = [u for u in users.values() if u["role"] == "arbitrator"]
                opts = "".join(f'<option value="{a["id"]}">{esc(a["name"])}</option>' for a in arbs)
                out.append(f"""<form class="act" method="post" action="/action">
<input type="hidden" name="as" value="{user['id']}">
<input type="hidden" name="deal_id" value="{deal['id']}">
<input type="hidden" name="dispute_id" value="{disp['id']}">
<input type="hidden" name="action" value="assign_arbitrator">
<h3>指派仲裁员</h3><p class="hint">仲裁员不得处理自己参与的交易。</p>
<select name="arbitrator_id">{opts}</select>
<button type="submit">指派</button></form>""")
            if (user and disp["state"] == "ARBITRATING"
                    and disp["arbitrator_id"] == user["id"]):
                out.append(f"""<form class="act" method="post" action="/action">
<input type="hidden" name="as" value="{user['id']}">
<input type="hidden" name="deal_id" value="{deal['id']}">
<input type="hidden" name="dispute_id" value="{disp['id']}">
<input type="hidden" name="action" value="rule">
<h3>作出裁决</h3>
<select name="verdict">
<option value="RELEASE">放款给卖家</option>
<option value="REFUND">退款给买家</option>
</select>
<textarea name="note" rows="2" placeholder="裁决理由"></textarea>
<button type="submit">裁决</button></form>""")
            out.append("</div>")
        return "".join(out)

    def _evidence_form(self, view, user):
        disp = view["dispute"]
        deal = view["deal"]
        if not user or disp["state"] == "RESOLVED":
            return ""
        participants = {deal["buyer_id"], deal["seller_id"], deal["inspector_id"]}
        if user["id"] not in participants:
            return ""
        return f"""<div class="card"><h2>提交证据</h2>
<form class="act" method="post" action="/action">
<input type="hidden" name="as" value="{user['id']}">
<input type="hidden" name="deal_id" value="{deal['id']}">
<input type="hidden" name="dispute_id" value="{disp['id']}">
<input type="hidden" name="action" value="evidence">
<select name="conclusion">
<option value="">不表态</option>
<option value="MATCH">与描述相符</option>
<option value="MISMATCH">与描述不符</option>
</select>
<textarea name="text" rows="2" placeholder="证据说明" required></textarea>
{self._photo_picker()}
<button type="submit">提交证据</button></form></div>"""

    # ------------------------------------------------------------ 动作分发
    def action(self, environ, start_response):
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length).decode("utf-8")
        multi = parse_qs(raw)
        form = {k: v[0] for k, v in multi.items()}
        photos = multi.get("photo", [])
        user_id = int(form.get("as") or 0)
        deal_id = form.get("deal_id")
        back = f"/deal?id={deal_id}" if deal_id else "/"
        try:
            msg, back = self._dispatch(form, photos, user_id, deal_id, back)
            return self._redirect(start_response, f"{back}&as={user_id}&msg={quote(msg)}")
        except d.DomainError as exc:
            return self._redirect(start_response,
                                  f"{back}&as={user_id}&err={quote(str(exc))}")

    def _dispatch(self, form, photos, user_id, deal_id, back):
        svc = self.svc
        act = form.get("action")
        if act == "create_deal":
            amount = int(round(float(form["amount"]) * 100))
            new_id = svc.create_deal(
                user_id, form["title"], form["description"], amount,
                int(form["buyer_id"]), int(form["seller_id"]),
                inspector_id=int(form["inspector_id"]),
                window_minutes=int(form.get("window") or 30))
            return "交易单已生成", f"/deal?id={new_id}"
        deal_id = int(deal_id)
        if act == "pay":
            svc.pay(deal_id, user_id)
            return "付款已发起，等待支付回执", back
        if act == "deliver_receipt":
            result = svc.process_receipt(form["receipt_id"])
            return ("回执已处理，资金进入托管" if result == "processed"
                    else "重复回执已忽略（幂等）"), back
        if act == "sweep":
            marked = svc.sweep_payment_timeouts()
            return f"扫描完成，{len(marked)} 笔交易标记为回执延迟", back
        if act == "cancel":
            svc.cancel(deal_id, user_id)
            return "交易已撤销", back
        if act == "deliver":
            svc.deliver(deal_id, user_id, form["serial"], form["appearance"],
                        form["accessories"], photos=photos)
            return "已交付，验货窗口开启", back
        if act == "confirm":
            svc.confirm(deal_id, user_id)
            return "已确认验货一致", back
        if act == "reject":
            svc.reject_and_dispute(deal_id, user_id, form["reason"],
                                   text=form.get("text", ""), photos=photos)
            return "已拒收，资金冻结，争议已开启", f"/dispute?id={deal_id}"
        if act == "request_extension":
            svc.request_extension(deal_id, user_id, int(form["minutes"]))
            return "延期申请已提交，等待卖家审批", back
        if act == "decide_extension":
            svc.decide_extension(deal_id, user_id, int(form["extension_id"]),
                                 form["decision"] == "approve")
            return "延期申请已处理", back
        if act == "claim_no_show":
            svc.claim_no_show(deal_id, user_id)
            return "已申诉买家逾期，资金冻结", f"/dispute?id={deal_id}"
        if act == "evidence":
            svc.submit_evidence(int(form["dispute_id"]), user_id,
                                text=form.get("text", ""), photos=photos,
                                conclusion=form.get("conclusion") or None)
            return "证据已提交", f"/dispute?id={deal_id}"
        if act == "assign_arbitrator":
            svc.assign_arbitrator(int(form["dispute_id"]), user_id,
                                  int(form["arbitrator_id"]))
            return "仲裁员已指派", f"/dispute?id={deal_id}"
        if act == "rule":
            svc.rule(int(form["dispute_id"]), user_id, form["verdict"],
                     note=form.get("note", ""))
            return "裁决已生效，资金已结算", f"/dispute?id={deal_id}"
        if act == "adjust_amount":
            svc.adjust_escrow_amount(user_id, deal_id,
                                     int(round(float(form["amount"]) * 100)))
            return "托管金额已调整", back
        raise d.DomainError(f"未知操作 {act}")


COUNTDOWN_JS = """<script>
(function(){
  var el = document.getElementById("countdown");
  if(!el || !el.dataset.deadline) return;
  var dl = new Date(el.dataset.deadline).getTime();
  function tick(){
    var ms = dl - Date.now();
    if(ms <= 0){ el.textContent = "已逾期"; return; }
    var s = Math.floor(ms/1000), m = Math.floor(s/60), ss = s%60;
    el.textContent = m + " 分 " + (ss<10?"0":"") + ss + " 秒";
    setTimeout(tick, 1000);
  }
  tick();
})();
</script>"""
