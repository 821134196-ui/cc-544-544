"""样例数据：八笔交易覆盖全部状态，含拒收样例与延期样例。

所有流程都通过 EscrowService 的真实业务路径生成，
账务事件、验货清单、争议证据与仲裁记录保持一致。
"""
import os
from datetime import datetime, timedelta

from escrow import domain as d
from escrow.db import Clock
from escrow.payments import MockPaymentAdapter
from escrow.photos import write_photo
from escrow.service import EscrowService

SAMPLE_NOTES = {
    "D-1002": "拒收样例：买家拒收 → 双方结论相反 → 仲裁中",
    "D-1003": "延期样例：验货窗口已获卖家同意延长",
    "D-1004": "回执延迟样例：付款回执未按时到达",
}


def seed_if_empty(db, photos_dir):
    """数据库为空时生成样例；返回是否执行了播种。"""
    if db.one("SELECT 1 FROM deals LIMIT 1"):
        return False
    os.makedirs(photos_dir, exist_ok=True)
    base = datetime.now().replace(microsecond=0)
    clock = Clock(base - timedelta(days=2))
    svc = EscrowService(db, MockPaymentAdapter(db), now=clock)

    platform = svc.add_user("平台运营", "platform")
    buyer = svc.add_user("小陈", "buyer")
    seller = svc.add_user("老周", "seller")
    inspector = svc.add_user("阿敏", "inspector")
    arbitrator = svc.add_user("林仲裁", "arbitrator")

    # ---- 虚构照片（本地 SVG）
    def photo(name, title, subtitle=""):
        return write_photo(photos_dir, name, title, subtitle)

    photo("d1001-handoff-1.svg", "镜头交付实拍", "序列号 RF50-8842-CN")
    photo("d1002-desc-1.svg", "商品描述图：HHKB 键盘", "九成新 · 键帽齐全")
    photo("d1002-site-1.svg", "现场照片：键盘全貌", "卖家交付时拍摄")
    photo("d1002-site-2.svg", "现场照片：缺失键帽", "买家验货时拍摄")
    photo("d1003-handoff-1.svg", "显示器交付实拍", "序列号 U2723-5521")
    photo("d1005-handoff-1.svg", "耳机交付实拍", "序列号 XM5-2024-09")
    photo("d1006-fault-1.svg", "隐藏故障：背光不均", "买家回家后发现")

    def fund(deal_id):
        """付款并立即送达回执，进入托管。"""
        receipt_id = svc.pay(deal_id, buyer)
        svc.process_receipt(receipt_id)

    # ---- D-1001 验货中（倒计时进行中）
    clock.set(base - timedelta(minutes=40))
    deal1 = svc.create_deal(
        platform, "佳能 RF 50mm F1.8 镜头",
        "2024 年购入，无霉无划，含前后盖与遮光罩，箱说齐全。",
        320000, buyer, seller, inspector, window_minutes=60, code="D-1001")
    fund(deal1)
    clock.set(base - timedelta(minutes=10))
    svc.deliver(deal1, seller, "RF50-8842-CN", "镜身无划痕，镜片通透无霉",
                "前后盖、遮光罩、原盒", photos=["d1001-handoff-1.svg"])

    # ---- D-1002 拒收样例：物品与描述不符 → 双方结论相反 → 仲裁中
    clock.set(base - timedelta(hours=3))
    deal2 = svc.create_deal(
        platform, "HHKB 静电容键盘",
        "九成新，键帽齐全无打油，含原装数据线。",
        165000, buyer, seller, inspector, window_minutes=45, code="D-1002",
        photos=["d1002-desc-1.svg"])
    fund(deal2)
    clock.set(base - timedelta(hours=2, minutes=30))
    svc.deliver(deal2, seller, "HHKB-2024-117", "外观与描述一致",
                "键盘、原装数据线", photos=["d1002-site-1.svg"])
    dispute2 = svc.reject_and_dispute(
        deal2, buyer, d.REASON_ITEM_MISMATCH,
        text="缺少三枚键帽，外壳有明显打油痕迹，与描述不符",
        photos=["d1002-site-2.svg"], conclusion=d.CONCLUSION_MISMATCH)
    svc.submit_evidence(dispute2, seller,
                        text="交付时键帽齐全，买家照片拍摄角度问题",
                        photos=["d1002-site-1.svg"], conclusion=d.CONCLUSION_MATCH)
    svc.submit_evidence(dispute2, inspector,
                        text="现场核实：F 行缺失两枚键帽，外壳有使用痕迹",
                        conclusion=d.CONCLUSION_MISMATCH)
    svc.assign_arbitrator(dispute2, platform, arbitrator)

    # ---- D-1003 延期样例：验货窗口已延长
    clock.set(base - timedelta(minutes=50))
    deal3 = svc.create_deal(
        platform, "戴尔 U2723QE 显示器",
        "4K 显示器，无坏点，含 HDMI 线与底座。",
        210000, buyer, seller, inspector, window_minutes=30, code="D-1003")
    fund(deal3)
    clock.set(base - timedelta(minutes=20))
    svc.deliver(deal3, seller, "U2723-5521", "屏幕无划痕无坏点",
                "显示器、HDMI 线、底座、电源线", photos=["d1003-handoff-1.svg"])
    ext = svc.request_extension(deal3, buyer, 40)
    svc.decide_extension(deal3, seller, ext, approve=True)

    # ---- D-1004 回执延迟样例：付款已发起，回执迟迟未到
    clock.set(base - timedelta(minutes=30))
    deal4 = svc.create_deal(
        platform, "Switch OLED 游戏主机",
        "日版 OLED，含 Joy-Con 与底座，无拆修。",
        189000, buyer, seller, inspector, code="D-1004")
    svc.pay(deal4, buyer)  # 回执保持 PENDING，模拟渠道延迟
    clock.set(base)
    svc.sweep_payment_timeouts()

    # ---- D-1005 已完成：双方确认一致，已放款
    clock.set(base - timedelta(days=1))
    deal5 = svc.create_deal(
        platform, "索尼 WH-1000XM5 降噪耳机",
        "使用半年，功能正常，含收纳盒与音频线。",
        199900, buyer, seller, inspector, window_minutes=30, code="D-1005")
    fund(deal5)
    svc.deliver(deal5, seller, "XM5-2024-09", "外观轻微使用痕迹",
                "耳机、收纳盒、音频线", photos=["d1005-handoff-1.svg"])
    svc.confirm(deal5, buyer)
    svc.confirm(deal5, seller)

    # ---- D-1006 争议中：验货后发现隐藏故障
    clock.set(base - timedelta(hours=1))
    deal6 = svc.create_deal(
        platform, "iPad Air 5 64GB",
        "WiFi 版，电池健康 92%，外观完好。",
        350000, buyer, seller, inspector, window_minutes=60, code="D-1006")
    fund(deal6)
    clock.set(base - timedelta(minutes=30))
    svc.deliver(deal6, seller, "IPADAIR5-7712", "外观无磕碰",
                "主机、数据线", photos=[])
    svc.confirm(deal6, buyer)
    svc.reject_and_dispute(
        deal6, buyer, d.REASON_HIDDEN_FAULT,
        text="现场确认外观无误，回家充电后发现屏幕背光不均",
        photos=["d1006-fault-1.svg"], conclusion=d.CONCLUSION_MISMATCH)

    # ---- D-1007 托管中·待交付
    clock.set(base - timedelta(minutes=5))
    deal7 = svc.create_deal(
        platform, "马歇尔 Acton III 音箱",
        "国行在保，功能正常，含电源线。",
        220000, buyer, seller, inspector, code="D-1007")
    fund(deal7)

    # ---- D-1008 卖家撤销：已托管资金退回买家
    clock.set(base - timedelta(hours=5))
    deal8 = svc.create_deal(
        platform, "卡西欧 G-Shock 手表",
        "GW-B5600，太阳能电波表，九成新。",
        89000, buyer, seller, inspector, code="D-1008")
    fund(deal8)
    svc.cancel(deal8, seller)

    return True
