"""SSE 保活流式工具：LLM 流式调用期间，主 generator 定时发心跳注释帧。

解决思考型模型（GLM-4.7 / DeepSeek-R1 等）在推理期后端零输出被 Render 30s idle
掐断的问题。根因有两段 chat_stream 自身是同步阻塞、无法 yield 任何东西的空窗：
  1. requests.post(stream=True) 首次响应前的阻塞期（prefill 常 >30s）
  2. iter_content 读流时两个 token 之间的 >30s 空窗

方案：把整段 chat_stream 丢后台线程，主 generator 用 Queue.get(timeout=interval)
消费，超时即发 1 帧心跳，与 LLM 状态无关，彻底覆盖上述全部阻塞场景。
独立成模块以避免 chat_collab_bp.py 巨石继续增长（架构门禁约束）。
"""
from __future__ import annotations

import threading
from queue import Empty, Queue

# SSE 心跳注释帧（冒号开头 = SSE 协议注释，前端 parseSSE 直接跳过）
SSE_HEARTBEAT_COMMENT = ': ping-heartbeat-keepalive\n\n'
# 每 10 秒发 1 帧心跳，远小于 Render 30s idle 阈值，留 20s 余量
SSE_HB_INTERVAL_SEC = 10


def gw_stream_with_hb(gw, msgs, **kw):
    """在后台线程跑 gw.chat_stream，主 generator 按 SSE_HB_INTERVAL_SEC 发心跳。

    思考帧（REASONING_HB 哨兵）同样转成心跳，不混入正文；worker 异常会在主 generator
    重新抛出，由上层 SSE 的 try/except 转成 error 帧。
    """
    import logging
    from llm_gateway import REASONING_HB

    logger = logging.getLogger("sse_keepalive")
    q: Queue = Queue()

    def _worker():
        logger.info("[gw_stream_with_hb] worker start")
        try:
            for chunk in gw.chat_stream(msgs, yield_reasoning_heartbeat=True, **kw):
                q.put(("chunk", chunk))
            logger.info("[gw_stream_with_hb] worker done (stream exhausted)")
            q.put(("done", None))
        except Exception as e:  # noqa: BLE001 在调用处重新抛出
            logger.error("[gw_stream_with_hb] worker error: %s", e)
            q.put(("error", e))

    threading.Thread(target=_worker, daemon=True).start()
    hb_count = 0
    while True:
        try:
            kind, payload = q.get(timeout=SSE_HB_INTERVAL_SEC)
        except Empty:
            hb_count += 1
            logger.info("[gw_stream_with_hb] heartbeat #%d (LLM still silent)", hb_count)
            yield SSE_HEARTBEAT_COMMENT  # 10s 内 LLM 无输出 → 心跳占住连接
            continue
        if kind == "chunk":
            yield SSE_HEARTBEAT_COMMENT if payload == REASONING_HB else payload
        elif kind == "error":
            raise payload
        else:  # done
            logger.info("[gw_stream_with_hb] finished. total heartbeats=%d", hb_count)
            return