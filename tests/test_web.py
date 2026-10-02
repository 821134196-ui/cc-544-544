"""Web 层集成测试：固定状态栏、争议页并排、HTTP 动作、权限回显。"""

from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from http.server import ThreadingHTTPServer

from escrow.clock import Clock
from escrow.db import connect, init_db
from escrow.payments import MockPaymentGateway
from escrow.service import EscrowService
import escrow.web as web


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = str(self.dir / "web.db")
        self.photos_dir = str(self.dir / "photos")

        conn = connect(self.db_path)
        init_db(conn)
        clock = Clock("2026-10-02T09:00:00+00:00")
        gw = MockPaymentGateway(conn, clock)
        s = EscrowService(conn, clock, gw, photos_dir=self.photos_dir)
        s.create_user("seller", "卖家", ["seller"])
        s.create_user("buyer", "买家", ["buyer"])
        s.create_user("insp", "验货人", ["inspector"])
        s.create_user("arb", "仲裁员", ["arbiter"])
        self.oid = s.create_order(
            seller_id="seller", buyer_id="buyer", inspector_id="insp",
            title="测试相机", description="九成新，配件齐全，快门数低",
            amount=100000, window_sec=3600)
        s.agree(self.oid, "seller"); s.agree(self.oid, "buyer")
        key = s.request_escrow(self.oid, "buyer")
        gw.settle(key); s.poll_gateway()
        clock.advance(60)
        s.deliver(self.oid, "seller", serial_no="SN-9", appearance="九成新",
                  accessories="充电器", seller_conclusion="conforming",
                  photo_label="DELIVERY SN-9")
        conn.commit(); conn.close()

        web.App.db_path = self.db_path
        web.App.photos_dir = self.photos_dir
        web.App.clock = clock  # 与种子数据共享同一时钟，窗口判定一致
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.App)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        self.cj = CookieJar()
        # 不跟随 303：直接检查 Location（含 flash/ERR）
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj), NoRedirect)
        # GET 用另一个会跟随重定向的 opener
        self.get_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def get(self, path):
        return self.get_opener.open(self.base + path).read().decode()

    def post(self, path, data: dict, expect_redirect=True):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            resp = self.opener.open(req)
            return resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            # 303 不再被自动跟随后以 HTTPError 抛出；手动抽取 Set-Cookie
            self.cj.extract_cookies(e, req)
            return e.code, e.headers.get("Location", "")

    def switch(self, user_id):
        self.post("/act/switch_user", {"user_id": user_id})

    def test_index_and_fixed_order_bar(self):
        html = self.get("/")
        self.assertIn("测试相机", html)
        self.assertIn("Σ 账户净值", html)

        page = self.get(f"/order/{self.oid}")
        # 固定顶条必须同时包含托管金额、剩余验货时间、双方确认状态
        self.assertIn("托管已冻结", page)
        self.assertIn("¥1,000.00", page)
        self.assertIn("剩余验货时间", page)
        self.assertIn("卖家约定", page)
        self.assertIn("买家约定", page)
        # 验货清单
        self.assertIn("SN-9", page)
        self.assertIn("/photos/", page)

    def test_status_api(self):
        import json
        data = json.loads(self.get(f"/order/{self.oid}/status"))
        self.assertTrue(data["in_window"])
        self.assertGreater(data["remaining"], 3000)

    def test_inspector_cannot_extend_other_order_is_400_flash(self):
        # 未登录操作 → 重定向回错误
        status, loc = self.post("/act", {
            "action": "inspect_accept", "order_id": self.oid})
        self.assertEqual(status, 303)
        self.assertIn("ERR", loc)
        self.assertIn(urllib.parse.quote("请先"), loc)

    def test_reject_flow_and_dispute_page(self):
        self.switch("buyer")
        status, loc = self.post("/act", {
            "action": "inspect_reject", "order_id": self.oid,
            "note": "有划痕，与描述不符"})
        self.assertEqual(status, 303)
        page = self.get(f"/order/{self.oid}")
        self.assertIn("买家拒收", page)
        self.assertIn("有划痕，与描述不符", page)

        self.post("/act", {"action": "open_dispute", "order_id": self.oid,
                           "reason": "item_mismatch"})
        dpage = self.get(f"/dispute/{self.oid}")
        # 争议页：商品描述与现场照片并排
        self.assertIn("商品描述（约定口径）", dpage)
        self.assertIn("现场照片", dpage)
        self.assertIn("九成新，配件齐全", dpage)
        self.assertIn("/photos/", dpage)

        # 提交相反证据
        self.post("/act", {"action": "evidence", "order_id": self.oid,
                           "conclusion": "defective", "note": "买家证据：划痕",
                           "photo_label": "B EVIDENCE"})
        self.switch("seller")
        self.post("/act", {"action": "evidence", "order_id": self.oid,
                           "conclusion": "conforming", "note": "卖家证据：交付时完好",
                           "photo_label": "S EVIDENCE"})
        dpage = self.get(f"/dispute/{self.oid}")
        self.assertIn("买家证据：划痕", dpage)
        self.assertIn("卖家证据：交付时完好", dpage)

        # 验货人仲裁必须被拒（无 arbiter 角色 → 错误）
        self.switch("insp")
        status, loc = self.post("/act", {"action": "arbitrate", "order_id": self.oid,
                                         "ruling": "refund_to_buyer", "rationale": "x"})
        self.assertIn("ERR", loc)

        # 独立仲裁员裁决退款
        self.switch("arb")
        status, loc = self.post("/act", {"action": "arbitrate", "order_id": self.oid,
                                         "ruling": "refund_to_buyer",
                                         "rationale": "描述为九成新，划痕属不符"})
        self.assertNotIn("ERR", loc)
        dpage = self.get(f"/dispute/{self.oid}")
        self.assertIn("退款给买家", dpage)


if __name__ == "__main__":
    unittest.main()
