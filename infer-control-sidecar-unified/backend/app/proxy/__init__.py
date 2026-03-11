# -----------------------------------------------------------------------------
# 文件: proxy/__init__.py
# 用途: 代理服务包的公开导出接口定义。
#
# 本包实现了 sidecar 的 API 代理层和健康检查层，主要模块：
#
#   - gateway.py        —— 主业务代理（FastAPI 应用），转发 OpenAI 兼容请求到后端引擎
#                          支持流式/非流式转发、自动重试、排队控制、观测头注入
#   - health.py         —— 健康状态机核心，持续探测后端 /health + PID 存活
#   - health_service.py —— 独立健康服务（运行在 health 端口），供 K8s 探针使用
#   - http_client.py    —— 异步 HTTP 客户端配置（HTTP/2、连接池、keepalive）
#   - queueing.py       —— 双闸门 FIFO 排队控制器（Gate-0/Gate-1 + 软队列）
#   - settings.py       —— proxy 运行时配置（端口、刷包策略、并发限制等）
#   - tags.py           —— 标签常量和辅助函数（URL 构造、日志格式化、JSON 校验）
#   - speaker_logging.py—— 多 worker 日志控制（只让部分 worker 输出 INFO 级别日志）
#
# __all__ 中列出的模块名需保持稳定，uvicorn 通过
# "app.proxy.gateway:app" 和 "app.proxy.health_service:app" 引用 FastAPI 应用实例。
#
# Sidecar 契约:
#   - 导出名称保持稳定，避免破坏 uvicorn 应用引用路径
#   - 导入时不产生运行时副作用
# -----------------------------------------------------------------------------
"""代理服务包。

包含 API 代理网关、健康检查服务、排队控制、HTTP 客户端以及相关工具函数。
"""
__all__ = [
    "gateway", "http_client", "queueing",
    "settings", "tags"
]