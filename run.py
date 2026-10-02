#!/usr/bin/env python3
"""一条命令启动：初始化数据库 → 生成样例（含拒收与延期）→ 启动 Web。

用法：
    python3 run.py                 # 启动服务（默认 127.0.0.1:8000）
    python3 run.py --port 9000     # 指定端口
    python3 run.py --seed-only     # 只初始化样例数据，不启动服务
    python3 -m unittest discover -s tests -v   # 运行测试
"""
import argparse
import os
from wsgiref.simple_server import make_server

from escrow import domain as d
from escrow.db import DB
from escrow.payments import MockPaymentAdapter
from escrow.service import EscrowService
from escrow.web import App
from seed import SAMPLE_NOTES, seed_if_empty


def main():
    parser = argparse.ArgumentParser(description="当面验货托管交易平台")
    parser.add_argument("--db", default="data/escrow.db", help="SQLite 数据库路径")
    parser.add_argument("--photos", default="data/photos", help="虚构照片目录")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed-only", action="store_true", help="只生成样例数据")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) or ".", exist_ok=True)
    os.makedirs(args.photos, exist_ok=True)
    db = DB(args.db)
    db.init_schema()
    if seed_if_empty(db, args.photos):
        print("已生成样例数据：")
        for code, note in SAMPLE_NOTES.items():
            print(f"  · {code}  {note}")
    svc = EscrowService(db, MockPaymentAdapter(db))

    print("\n交易单一览：")
    for deal in svc.list_deals():
        print(f"  {deal['code']}  {deal['title']:<18} "
              f"{d.STATE_LABELS[deal['state']]}  托管 {deal['amount_cents'] / 100:,.2f} 元")
    report = svc.conservation_report()
    print(f"资金守恒校验：{'通过' if report['ok'] else '异常 ' + str(report['problems'])}")

    if args.seed_only:
        return
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\n服务已启动：{url}  （默认视角：买家小陈，可在页面右上角切换角色）")
    print("拒收样例见 D-1002 争议页，延期样例见 D-1003 交易页。Ctrl+C 停止。")
    make_server("127.0.0.1", args.port, App(db, svc, args.photos)).serve_forever()


if __name__ == "__main__":
    main()
