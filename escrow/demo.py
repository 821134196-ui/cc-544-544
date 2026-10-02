"""演示数据与一键启动。

运行：python -m escrow demo

会初始化 SQLite（data/escrow.db）、虚构照片（photos/），
创建若干处于不同状态的订单（含拒收、延期样例），启动 Web 服务，
并在终端打印「拒收」与「验货延期」的逐步操作样例。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .clock import Clock
from .db import connect, init_db
from .payments import MockPaymentGateway
from .photos import make_photo
from . import service as svc
from .service import EscrowService


WINDOW_SEC = 24 * 3600  # 验货窗口 24 小时（测试中用假时钟压缩）


def seed(db_path: str = "data/escrow.db", photos_dir: str = "photos",
         reset: bool = True):
    if reset:
        for p in (db_path, db_path + "-wal", db_path + "-shm"):
            Path(p).unlink(missing_ok=True)
        shutil.rmtree(photos_dir, ignore_errors=True)

    conn = connect(db_path)
    init_db(conn)
    clock = Clock("2026-10-02T09:00:00+00:00")
    gateway = MockPaymentGateway(conn, clock)
    s = EscrowService(conn, clock, gateway, photos_dir=photos_dir)

    # 用户：买卖双方、验货人、两名仲裁员（其中一人参与过部分交易，用于演示回避）
    s.create_user("u_li", "李卖家", ["seller"])
    s.create_user("u_wang", "王买家", ["buyer"])
    s.create_user("u_zhao", "赵验货", ["inspector"])
    s.create_user("u_qian", "钱仲裁（独立）", ["arbiter"])
    s.create_user("u_sun", "孙仲裁（兼验货人）", ["inspector", "arbiter"])
    s.create_user("u_chen", "陈卖家", ["seller"])
    s.create_user("u_zhou", "周买家", ["buyer"])

    # 描述图（虚构照片）
    make_photo(photos_dir, "LISTING CAM-A17 95NEW FULLSET", (206, 212, 218))

    # ---- 订单 1：完整顺利流程，停在「验货中」，供页面演示倒计时 ----
    o1 = s.create_order(
        seller_id="u_li", buyer_id="u_wang", inspector_id="u_zhao",
        title="微单相机 CAM-A17（95新·全套）",
        description="微单相机 CAM-A17，2024 年购入，快门数约 8000，"
                    "机身 95 新无磕碰，含原装充电器、数据线、原装盒。",
        amount=420000, window_sec=WINDOW_SEC,
    )
    s.agree(o1, "u_li")
    s.agree(o1, "u_wang")
    key = s.request_escrow(o1, "u_wang")
    clock.advance(90)  # 付款回执延迟 90 秒后到达
    gateway.settle(key)
    s.poll_gateway()
    clock.advance(3600)
    s.deliver(o1, "u_li", serial_no="SN-A17-2024-0817",
              appearance="95新，边角无磕碰，屏幕无划痕",
              accessories="原装充电器、数据线、原装盒",
              seller_conclusion="conforming",
              note="现场通电自检正常",
              photo_label="DELIVERY SN-A17-2024-0817",
              photo_rgb=(206, 212, 218))
    clock.advance(2 * 3600)  # 窗口剩余约 21 小时

    # ---- 订单 2：买家拒收样例（物品与描述不符），等卖家确认退款 ----
    o2 = s.create_order(
        seller_id="u_chen", buyer_id="u_zhou", inspector_id="u_zhao",
        title="平板 TAB-S9（描述为全新未拆）",
        description="平板 TAB-S9，宣称全新未拆封，配件齐全。",
        amount=185000, window_sec=WINDOW_SEC,
    )
    s.agree(o2, "u_chen"); s.agree(o2, "u_zhou")
    key2 = s.request_escrow(o2, "u_zhou")
    gateway.settle(key2); s.poll_gateway()
    clock.advance(1800)
    s.deliver(o2, "u_chen", serial_no="SN-TABS9-5520",
              appearance="外封条完好", accessories="充电头、说明书",
              seller_conclusion="conforming",
              photo_label="DELIVERY SN-TABS9-5520",
              photo_rgb=(206, 212, 218))
    clock.advance(900)
    s.inspect(o2, "u_zhou", "reject",
              note="屏幕有明显划痕，充电口有使用痕迹，与「全新未拆」描述不符",
              photo_label="BUYER REJECT SCRATCH FOUND",
              photo_rgb=(176, 176, 176))

    # ---- 订单 3：延期样例：验货人已延期一次，仍在验货中 ----
    o3 = s.create_order(
        seller_id="u_li", buyer_id="u_zhou", inspector_id="u_zhao",
        title="键盘 KB-87（二手）",
        description="机械键盘 KB-87，茶轴，键帽微油光。",
        amount=32000, window_sec=2 * 3600,
    )
    s.agree(o3, "u_li"); s.agree(o3, "u_zhou")
    key3 = s.request_escrow(o3, "u_zhou")
    gateway.settle(key3); s.poll_gateway()
    clock.advance(600)
    s.deliver(o3, "u_li", serial_no="SN-KB87-031", appearance="键帽微油光",
              accessories="拔键器、替换键帽四颗",
              seller_conclusion="conforming",
              photo_label="DELIVERY SN-KB87-031",
              photo_rgb=(206, 212, 218))
    clock.advance(2 * 3600 - 300)  # 距截止还剩 5 分钟时
    # 顺延 2 小时，确保演示启动后仍处于有效窗口（后续种子订单还会推进全局时钟）
    s.extend_window(o3, "u_zhao", 7200, "买家路途延误，双方电话同意顺延 2 小时")
    clock.advance(20 * 60)  # 延期后窗口仍有效

    # ---- 订单 4：争议样例：双方提交相反结论，等待仲裁 ----
    o4 = s.create_order(
        seller_id="u_chen", buyer_id="u_wang", inspector_id="u_sun",
        title="耳机 HP-2（二手）",
        description="降噪耳机 HP-2，续航正常，降噪功能完好。",
        amount=68000, window_sec=WINDOW_SEC,
    )
    s.agree(o4, "u_chen"); s.agree(o4, "u_wang")
    key4 = s.request_escrow(o4, "u_wang")
    gateway.settle(key4); s.poll_gateway()
    clock.advance(1200)
    s.deliver(o4, "u_chen", serial_no="SN-HP2-778", appearance="耳罩轻微磨损",
              accessories="充电线、收纳盒",
              seller_conclusion="conforming",
              photo_label="DELIVERY SN-HP2-778",
              photo_rgb=(206, 212, 218))
    clock.advance(900)
    s.inspect(o4, "u_wang", "reject",
              note="左耳降噪时有电流声，疑似故障",
              photo_label="BUYER REJECT NOISE DEFECT",
              photo_rgb=(176, 176, 176))
    s.open_dispute(o4, "u_wang", "opposite_conclusions", "买卖结论相反")
    s.submit_evidence(o4, "u_chen", "conforming",
                      "交付前满电测试降噪 30 分钟无异常，耳罩磨损已在描述中说明",
                      photo_label="SELLER EVIDENCE CONFORMING",
                      photo_rgb=(214, 228, 216))
    s.submit_evidence(o4, "u_wang", "defective",
                      "现场录音可见左耳电流声，附现场照片与录音记录编号",
                      photo_label="BUYER EVIDENCE DEFECTIVE",
                      photo_rgb=(176, 176, 176))
    s.add_inspector_note(o4, "u_sun", "现场复现一次左耳异响，建议送检定责")

    # ---- 订单 5：已完成并放款（绿色结论） ----
    o5 = s.create_order(
        seller_id="u_li", buyer_id="u_zhou",
        title="镜头 LENS-35（二手）",
        description="35mm 定焦镜头，无霉无划，带 UV 镜。",
        amount=98000, window_sec=WINDOW_SEC,
    )
    s.agree(o5, "u_li"); s.agree(o5, "u_zhou")
    key5 = s.request_escrow(o5, "u_zhou")
    gateway.settle(key5); s.poll_gateway()
    clock.advance(600)
    s.deliver(o5, "u_li", serial_no="SN-LENS35-12", appearance="无霉无划",
              accessories="UV镜、前后盖", seller_conclusion="conforming",
              photo_label="DELIVERY SN-LENS35-12", photo_rgb=(206, 212, 218))
    clock.advance(900)
    s.inspect(o5, "u_zhou", "accept", note="镜片通透，对焦正常",
              photo_label="BUYER ACCEPT LENS OK", photo_rgb=(214, 228, 216))
    clock.advance(120)
    # 买家验收通过即买家侧确认，卖家再确认即双方一致放款
    s.confirm_release(o5, "u_li")

    conn.commit()
    return s, clock


DEMO_GUIDE = """
========================================================================
 当面交易托管 · 一键演示已就绪
========================================================================

【样例 A：买家拒收 → 退款（订单 TAB-S9）】
 1. 右上角「切换操作人」选择 陈卖家
 2. 打开订单 TAB-S9（状态：买家拒收）
    - 固定顶条显示：托管金额 ¥1,850.00、双方确认状态
    - 买家已提交拒收结论与现场照片（屏幕划痕，与「全新未拆」不符）
 3. 陈卖家点击「卖家认可拒收 · 同意退款」
    → 双方确认一致，托管资金退款；流水区只出现 hold + refund，无第二笔出账
 （若卖家不认可，可改为「发起争议」，资金继续冻结，进入争议页仲裁。）

【样例 B：验货延期（订单 KB-87）】
 1. 打开订单 KB-87：验货窗口原为 2 小时，验货人赵验货在截止前 5 分钟顺延 2 小时
 2. 顶部「剩余验货时间」按秒跳动；事件流可查 window_extended 记录
 3. 切换为赵验货，可再次延期；切换为其他任何人点延期都会被拒绝（权限隔离）
 4. 窗口结束后买家再提交验收会被拒绝；卖家/验货人可「标记买家逾期不到」

【争议与仲裁（订单 HP-2）】
 - 争议页把「商品描述」与「现场照片」并排，双方相反结论证据并列
 - 仲裁员钱仲裁可裁决；孙仲裁是该单验货人，裁决会被系统要求回避
 - 裁决一旦作出立即执行放款或退款，冻结资金随之划转

【可现场验证的系统约束】
 - 付款回执延迟：新订单付款后停在「付款回执延迟中」，需轮询拉取才入账
 - 回执重放：同一回执重复回调，只确认一次（见 python -m escrow test）
 - 资金守恒：首页底部 Σ 账户净值恒为 ¥0.00
========================================================================
"""


def run_demo(host="127.0.0.1", port=8000):
    s, clock = seed()
    # 种子用虚拟时间快进；服务启动后倒计时按真实时间继续递减
    clock.go_live()
    print(DEMO_GUIDE, flush=True)
    from .web import serve
    serve("data/escrow.db", "photos", host=host, port=port,
          open_browser=True, clock=clock)
    return s
