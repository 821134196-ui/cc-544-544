"""当面交易托管（escrow）领域包。

模块：
  db      SQLite 连接与 schema
  clock   可注入时钟（测试用假时钟）
  payments 模拟支付适配器（钱包、延迟回执、幂等）
  photos  本地虚构照片（无依赖 PNG 生成）
  service 领域服务：约定、托管流水、验货清单、争议、仲裁、资金释放
  web     http.server 页面
  demo    种子数据与启动
"""

__version__ = "1.0.0"
