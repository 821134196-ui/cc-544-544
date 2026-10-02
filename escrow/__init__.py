"""当面验货托管交易平台。

仅依赖 Python 标准库：
- escrow.domain   状态机、争议原因、业务异常
- escrow.db       SQLite 存储（订单、账务事件、清单、证据、仲裁）
- escrow.ledger   托管流水（HOLD/RELEASE/REFUND）与资金守恒校验
- escrow.payments 模拟支付渠道适配器（回执可延迟、重放安全）
- escrow.service  业务编排（约定/托管/验货/争议/仲裁/放款）
- escrow.web      WSGI 界面（交易页、争议页）
"""
