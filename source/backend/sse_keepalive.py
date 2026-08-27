"""SSE 保活流式工具：LLM 流式调用期间，主 generator 定时发心跳注释帧。

解决思考型模型（GLM-4.7 / DeepSeek-R1 等）在推理期后端零输出被 Render 30s idle
掐断的问题。根因有两段 chat_stream 自身是同步阻塞、无法 yield 任何东西的空窗：
  1. requests.post(stream=True) 首次响应前的阻塞期（prefill 常 >30s）
  2. iter_content 读流时两个 token 之间的 >30s 空窗

方案：把整段 chat_stream 丢后台线程，主 generator 用 Queue.get(timeout=interval)
消费，超时即 yield HEARTBEAT 哨兵，与 LLM 状态无关，彻底覆盖上述全部阻塞场景。

⚠️ 契约（P0，勿违反）：本函数 yield 三种东西——
  1. HEARTBEAT 哨兵对象（不是字符串！）—— 调用方必须 yield SSE_HEARTBEAT_COMMENT
     裸注释帧并 continue，绝不能 append 进内容缓冲/包进 data: delta 帧。
     （旧版直接 yield 注释字符串，被调用方当正文包进 delta → 用户聊天窗口
      刷屏 ": ping-heartbeat-keepalive"，且污染卡片内容，实锤 P0 事故。）
  2. STREAM_RETRY 哨兵（_StreamRetry）—— 调用方必须 yield SSE meta
     (kind=stream_retry, info=chunk.info) 并 continue，绝不 append 进正文。
  3. 真实正文 chunk（str）—— 正常 append + 包 delta 帧。
独立成模块以避免 chat_collab_bp.py 巨石继续增长（架构门禁约束）。
"""
from __future__ import annotations

import threading
from queue import Empty, Queue

# SSE 心跳注释帧（冒号开头 = SSE 协议注释，前端 parseSSE 直接跳过）
SSE_HEARTBEAT_COMMENT = ': ping-heartbeat-keepalive\n\n'
# 每 10 秒发 1 帧心跳，远小于 Render 30s idle 阈值，留 20s 余量
SSE_HB_INTERVAL_SEC = 10


class _Heartbeat:
    """心跳哨兵：identity 唯一（`is HEARTBEAT` 判定），绝不混入正文内容。"""
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return '<SSE-HEARTBEAT>'


HEARTBEAT = _Heartbeat()


class _StreamRetry:
    """SSE 自动续连事件哨兵：携带 attempt/reason/continued_chars 信息，绝不混入正文。"""
    __slots__ = ('info',)
    def __init__(self, info): self.info = dict(info)
    def __repr__(self) -> str: return f'<SSE-STREAM-RETRY attempt={self.info.get("attempt")} reason={self.info.get("reason")}>'


def STREAM_RETRY(info): return _StreamRetry(info)
def _is_stream_retry(obj): return isinstance(obj, _StreamRetry)


def gw_stream_with_hb(gw, msgs, **kw):
    """在后台线程跑 gw.chat_stream，静默期 yield HEARTBEAT 哨兵。

    思考帧（REASONING_HB 哨兵）同样转成 HEARTBEAT，不混入正文；
    STREAM_RETRY 事件（\x00\x00stream-retry|JSON\x00\x00）解析成 _StreamRetry
    哨兵，不混入正文；worker 异常会在主 generator 重新抛出，由上层 SSE 的
    try/except 转成 error 帧。
    """
    from llm_gateway import REASONING_HB, parse_stream_retry_event
    q: Queue = Queue()

    def _worker():
        try:
            for chunk in gw.chat_stream(msgs, yield_reasoning_heartbeat=True, **kw):
                q.put(("chunk", chunk))
            q.put(("done", None))
        except Exception as e:  # noqa: BLE001 在调用处重新抛出
            q.put(("error", e))

    threading.Thread(target=_worker, daemon=True).start()
    while True:
        try:
            kind, payload = q.get(timeout=SSE_HB_INTERVAL_SEC)
        except Empty:
            yield HEARTBEAT  # 10s 内 LLM 无输出 → 调用方据此发裸注释心跳帧占住连接
            continue
        if kind == "chunk":
            if payload == REASONING_HB:
                yield HEARTBEAT
                continue
            # P0-6: 检测中流续连事件，解析成 STREAM_RETRY 哨兵，供调用方 yield meta 帧
            _sre_info = parse_stream_retry_event(payload)
            if _sre_info is not None:
                yield STREAM_RETRY(_sre_info)
                continue
            yield payload
        elif kind == "error":
            raise payload
        else:  # done
            return
