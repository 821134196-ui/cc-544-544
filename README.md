# 当面交易 · 托管平台（Escrow）

买家与卖家当面验货的 C2C 托管交易演示系统。**纯 Python 标准库实现，零第三方依赖**：
SQLite 存订单与账务事件，本地生成虚构照片，模拟支付适配器，`http.server` 提供页面。

## 一条命令启动

```bash
python3 -m escrow demo
```

自动重置并播种演示数据（`data/escrow.db`、`photos/`），启动
`http://127.0.0.1:8000` 并打开浏览器。终端会打印**拒收**与**验货延期**两个样例的操作步骤。

其他命令：

```bash
python3 -m escrow serve          # 不清空数据，直接启动
python3 -m escrow test           # 运行全部测试
python3 -m escrow help
```

## 业务流程与状态

```
pending_agreement 待约定
  → agreed 已约定待付款
  → escrow_pending 付款回执延迟中（模拟网关，回执未到）
  → escrowed 托管已冻结
  → delivered 验货中（卖家已填序列号/外观/配件，验货窗口计时）
      ├─ inspection_passed 验收通过·待确认  ─双方确认→ released 已放款
      ├─ inspection_rejected 买家拒收      ─双方确认→ refunded 已退款
      ├─ buyer_no_show 买家逾期不到
      └─ disputed 争议中（资金保持冻结）─仲裁→ arbitrated_release / arbitrated_refund
  （agreed 之前卖家可）→ cancelled 卖家已撤销
```

题面要求的每种情形都有独立状态：物品与描述不符（`inspection_rejected` /
`item_mismatch` 争议）、买家逾期不到（`buyer_no_show`）、卖家撤销（`cancelled`）、
付款回执延迟（`escrow_pending`）、验货后发现隐藏故障（`hidden_defect` 争议）、
双方相反结论（`opposite_conclusions` 争议，证据并列）。

## 关键约束如何保证

| 约束 | 实现 |
|---|---|
| 付款/退款/放款互斥 | 每单至多一笔 `hold`、一笔终态流，由部分唯一索引 + 状态机 + 幂等键三重保证 |
| 重复回执不形成两笔资金流 | 网关回执号唯一、hold 幂等键 `hold:<order>`；回调重放只返回成功不新增分录 |
| 资金守恒 | 标准复式记账（借=贷），`Σ 所有账户净值 ≡ 0`；首页实时显示 |
| 验货时限 | 交付时重设截止时间，窗口内才能验收；仅该单验货人可在窗口内延期 |
| 验货人不能改托管金额 | 验货人角色无任何资金动作权限（付款/放款/退款/仲裁均 403） |
| 仲裁回避 | 仲裁员若为该单买家/卖家/验货人，裁决直接拒绝 |
| 争议冻结 | `disputed` 状态下一切资金动作被拒，裁决作出才划转 |

## 页面

- **交易页 `/order/{id}`**：顶部 sticky 固定条常驻显示**托管金额、剩余验货时间（秒级倒计时）、
  双方确认状态**；下方为验货清单、买家结论、托管流水与事件时间线。
- **争议页 `/dispute/{id}`**：**商品描述与现场照片左右并排**，双方相反结论证据并列，
  仲裁表单对非仲裁员/参与人拒绝。
- 配色仅白、深灰与有限绿色——绿色只用于「一致 / 通过 / 已放款」结论提示。

右上角可切换当前操作人（买家/卖家/验货人/仲裁员）以体验权限隔离。

## 代码结构

```
escrow/
  db.py        SQLite schema（订单、清单、证据、仲裁、资金流、复式账、事件流）
  clock.py     可注入时钟（测试假时钟；demo 种子快进后 go_live 转真实时间）
  payments.py  模拟支付网关：queued→paid、延迟回执、确定性回执号、重放幂等
  photos.py    无依赖手写 PNG：标签 + 色块 + 准星的虚构现场照片
  service.py   领域服务：状态机、托管账务、验货时限、争议仲裁、权限
  web.py       页面与表单动作
  demo.py      5 个不同状态的种子订单（含拒收/延期/争议/已放款）
tests/
  test_escrow.py  资金守恒、回执重放、验货时限、权限隔离（19 例）
  test_web.py     页面固定栏、争议并排、HTTP 动作与权限回显（4 例）
```

## 测试

```bash
python3 -m escrow test
```

覆盖：hold/放款/退款/两种仲裁后的守恒与冻结额、同一回执重复回调只入一次账、
窗口内/外验收与延期、逾期不到、验货人越权触碰资金、非指派验货人延期、
仲裁员利益回避、终态资金流互斥等。
