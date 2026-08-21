"""LLM Gateway：统一 LLM 调用入口 + 错误分类 + 智能重试。

借鉴司命 siming-ai 的 ModelResult + FailureClass 设计，解决番茄项目 25 处
requests.post 直连 LLM 的重复问题：

  - 统一入口：LLMGateway.chat() 一次调用，返回标准化 ModelResult
  - 错误分类：FailureClass 区分 auth/quota/timeout/unavailable/empty/format
  - 智能重试：auth 错误不重试，timeout/unavailable 自动重试（默认 2 次）
  - 空内容检测：自动检测 LLM 返回空内容并标记 empty_response

使用方式：
    from llm_gateway import LLMGateway, ModelResult
    gw = LLMGateway(base_url, api_key, model)
    result = gw.chat(messages=[...], temperature=0.7, max_tokens=4096)
    if result.ok:
        content = result.content
    else:
        raise LLMError(result.error, result.failure_class)
"""
from __future__ import annotations

import enum
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests


class FailureClass(enum.Enum):
    """LLM 调用失败分类，决定是否重试。"""
    NONE = "none"                # 成功
    AUTHENTICATION = "auth"      # API key 无效，不重试
    QUOTA = "quota"              # 配额耗尽，不重试
    TIMEOUT = "timeout"          # 超时，可重试
    UNAVAILABLE = "unavailable"  # 服务不可用，可重试
    EMPTY_RESPONSE = "empty"     # 返回空内容，可重试
    FORMAT_ERROR = "format"      # 返回格式异常，可重试
    CANCELLED = "cancelled"      # 用户取消，不重试
    UNKNOWN = "unknown"          # 未知错误，可重试一次


# 可重试的失败类型
_RETRYABLE = {FailureClass.TIMEOUT, FailureClass.UNAVAILABLE, FailureClass.EMPTY_RESPONSE,
              FailureClass.FORMAT_ERROR, FailureClass.UNKNOWN}


# 思考帧心跳哨兵：正文中不可能出现的控制字符序列（\x00 不可打印）。
# chat_stream(yield_reasoning_heartbeat=True) 时，thinking 帧到达即 yield 此哨兵，
# 上层据此向客户端发 SSE 注释心跳帧（': ping'），防止推理期间代理层 30s idle 掐断连接。
REASONING_HB = "\x00\x00reasoning-heartbeat\x00\x00"


def build_auth_headers(api_key: str, content_type: bool = True) -> dict:
    """构造 LLM 请求认证头。

    同时下发 Authorization: Bearer 与 x-api-key 两种认证头，兼容：
    - 标准 OpenAI 兼容服务（deepseek/qwen/glm 等认 Authorization，忽略 x-api-key）
    - OpenCode Zen 免费模型端点（认 x-api-key）
    对任一 provider 均无副作用：HTTP 服务通常忽略未识别的 header。
    """
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


class LLMError(Exception):
    """LLM 调用异常，携带 failure_class。"""
    def __init__(self, message: str, failure_class: FailureClass = FailureClass.UNKNOWN):
        super().__init__(message)
        self.failure_class = failure_class


@dataclass
class ModelResult:
    """LLM 调用标准化返回。"""
    content: str = ""
    finish_reason: str = ""
    raw: dict = field(default_factory=dict)
    error: str = ""
    failure_class: FailureClass = FailureClass.NONE
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.failure_class == FailureClass.NONE and bool(self.content and self.content.strip())

    @property
    def is_empty(self) -> bool:
        """有响应但内容为空（区别于网络错误）。"""
        return self.failure_class == FailureClass.EMPTY_RESPONSE


def _classify_error(exc: Exception, status_code: int = 0, body: dict | None = None) -> FailureClass:
    """根据异常/状态码/响应体分类失败类型。"""
    if isinstance(exc, requests.exceptions.Timeout):
        return FailureClass.TIMEOUT
    if isinstance(exc, requests.exceptions.ConnectionError):
        return FailureClass.UNAVAILABLE
    if status_code in (401, 403):
        return FailureClass.AUTHENTICATION
    if status_code == 429:
        return FailureClass.QUOTA
    if status_code >= 500:
        return FailureClass.UNAVAILABLE
    # 检查响应体中的 error 字段
    if body and isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = (err.get("message") or "").lower()
            if "api key" in msg or "unauthorized" in msg or "authentication" in msg:
                return FailureClass.AUTHENTICATION
            if "quota" in msg or "rate limit" in msg or "insufficient" in msg:
                return FailureClass.QUOTA
    return FailureClass.UNKNOWN


def _extract_content(body: dict) -> tuple[str, str, FailureClass]:
    """从 OpenAI 兼容响应体提取 (content, finish_reason, failure_class)。"""
    try:
        # 检查 API 层错误
        if body.get("error"):
            err = body["error"]
            fc = _classify_error(None, body.get("status_code", 0), body)
            return "", "", fc
    except Exception:
        pass

    try:
        choice = body["choices"][0]
        content = (choice.get("message", {}).get("content") or "").strip()
        finish_reason = choice.get("finish_reason", "")
        if not content:
            if finish_reason == "length":
                return "", finish_reason, FailureClass.FORMAT_ERROR
            return "", finish_reason, FailureClass.EMPTY_RESPONSE
        return content, finish_reason, FailureClass.NONE
    except (KeyError, IndexError, TypeError) as e:
        return "", "", FailureClass.FORMAT_ERROR


# SSE 事件分隔符：匹配 \n\n / \r\n\r\n / \r\r 等任意换行组合的空行。
# 用正则而非 find(b"\n\n")：很多服务（智谱 GLM、部分中转）用 \r\n\r\n 分隔，
# 若不统一处理会永远切不出事件 → 全部数据堆到流结束一次性解析失败 → 空内容。
_SSE_BOUNDARY = re.compile(rb"\r?\n\r?\n")


def _iter_sse_events(resp):
    """健壮解析 OpenAI 兼容 SSE 流，逐个 yield (kind, value)。

    kind ∈ {'delta', 'reasoning', 'message'}：
      - 'delta'      正文片段（标准流式 delta.content）
      - 'reasoning'  思考片段（delta.reasoning_content，GLM-4.7/R1 等）
      - 'message'    服务端忽略 stream 返回非流式 message.content（一次性完整正文）
    兼容差异：data: 有无空格、多行 data 拼接、\n 与 \r\n 换行、[DONE] 结束。
    """
    buffer = b""
    for raw in resp.iter_content(chunk_size=1024):
        if not raw:
            continue
        buffer += raw
        # 用正则按空行切事件，最后一段是未完成的（可能留了半个分隔符），保留在 buffer
        parts = _SSE_BOUNDARY.split(buffer)
        buffer = parts.pop()
        for blob in parts:
            if blob.strip():
                yield from _parse_sse_event(blob)
    if buffer.strip():
        yield from _parse_sse_event(buffer)


def _parse_sse_event(blob: bytes):
    """解析单个 SSE 事件块，提取 data 行并 JSON 解析内容字段。"""
    text = blob.decode("utf-8", errors="ignore")
    data_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        payload = "\n".join(data_lines).strip()
    else:
        # 无 data: 前缀 → 服务端忽略 stream 返回的裸 JSON 响应体（非 SSE 格式）
        payload = text.strip()
    if not payload or payload == "[DONE]":
        return
    try:
        chunk = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return
    choices = chunk.get("choices") or []
    if not choices:
        # DeepSeek/某些服务把内容放在顶层 content 字段
        if chunk.get("content"):
            yield ("delta", chunk["content"])
        return
    choice = choices[0] or {}
    # 非流式格式：服务端忽略 stream 参数，返回 message 而非 delta
    if "message" in choice and choice["message"]:
        msg_content = (choice["message"] or {}).get("content")
        if msg_content:
            yield ("message", msg_content)
        return
    delta = choice.get("delta") or {}
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    if content:
        yield ("delta", content)
    elif reasoning:
        yield ("reasoning", reasoning)


# ============================================================================
# 模型输出上限自动适配（2026-08-21）
#
# 背景：chat_collab_bp 生成链路统一请求 max_tokens=27000 给足输出预算，但各家模型
# 输出上限不一（deepseek-chat 8192、gpt-4o 16384、claude-3.5 8192……），超限时 API
# 直接 400 拒绝 → 生成必失败。适配策略（三层）：
#   1. 已知模型表 _KNOWN_OUTPUT_LIMITS：常见模型直接钳制，省一次 400 往返
#   2. 报错自学习 _LEARNED_OUTPUT_LIMITS：解析 400 报错里的真实上限（各家文案不同：
#      OpenAI "maximum allowed value of 16384"、Anthropic "at most 8191"、
#      中文服务 "最大值为 65536"），记入进程级缓存，后续调用直接使用
#   3. 兜底：解析不出具体上限时，本次调用退到 8192（最常见档位）重试一次
# 原则：宁可表里漏记（多付一次 400 自学习往返），不可错记低值（会静默截断输出）。
# ============================================================================

_KNOWN_OUTPUT_LIMITS = {
    # DeepSeek（本项目默认配置）
    'deepseek-chat': 8192, 'deepseek-v3': 8192,
    'deepseek-reasoner': 65536, 'deepseek-r1': 65536,
    # OpenAI
    'gpt-4o': 16384, 'gpt-4o-mini': 16384,
    'gpt-4.1': 32768, 'gpt-4.1-mini': 32768, 'gpt-4.1-nano': 32768,
    'gpt-5': 128000, 'gpt-5-mini': 128000, 'gpt-5-nano': 128000,
    'o1': 100000, 'o3': 100000, 'o4-mini': 100000,
    # Anthropic
    'claude-3-5-sonnet': 8192, 'claude-3-5-haiku': 8192,
    'claude-3-7-sonnet': 64000, 'claude-sonnet-4': 64000, 'claude-opus-4': 32000,
    # Google
    'gemini-2.5-pro': 65536, 'gemini-2.5-flash': 65536, 'gemini-2.0-flash': 8192,
}

# (base_url, model) → 报错学到的输出上限（进程级缓存，重启后首跑自学习一次）
_LEARNED_OUTPUT_LIMITS: dict[tuple[str, str], int] = {}

_MAX_TOKENS_MENTION_RE = re.compile(r'max[\s_-]?tokens', re.I)
# 上限值锚点：紧随"maximum allowed value of / at most / 不能超过 / 最大值为"等措辞的数字
_OUTPUT_LIMIT_ANCHOR_RE = re.compile(
    r'(?:maximum allowed (?:value|number) of|at most|maximum of|limit (?:is|of)'
    r'|<=|must be (?:at most|no greater than)|不能超过|最多|最大值(?:为)?|上限(?:为)?)'
    r'[\s:：]*([\d][\d,]{2,9})',
    re.I,
)


def _known_output_limit(model: str) -> int:
    """查已知模型表。变体名（带日期/版本后缀，如 gpt-4o-2024-08-06）按子串匹配，
    多个命中取最大值（宁高勿低：偏高只多一次 400 自学习，偏低会静默截断）。未知返回 0。"""
    m = (model or '').lower()
    if not m:
        return 0
    if m in _KNOWN_OUTPUT_LIMITS:
        return _KNOWN_OUTPUT_LIMITS[m]
    best = 0
    for key, lim in _KNOWN_OUTPUT_LIMITS.items():
        if len(key) >= 5 and key in m:
            best = max(best, lim)
    return best


def _parse_max_tokens_limit(message: str, requested: int) -> int:
    """从 API 400/422 报错文案解析模型允许的 max_tokens 上限；解析不出返回 0。

    兼容各家文案（实测样例）：
      OpenAI:    Invalid 'max_tokens': integer exceeds the maximum allowed value of 16384
      Anthropic: max_tokens: 27000 > 8191, which is the maximum allowed number of output tokens
      旧式:       max_tokens is too large: 27000. This model supports at most 4096
      中文:       max_tokens 参数最大值为 65536 / max_tokens 不能超过 8192
    """
    if not message or not _MAX_TOKENS_MENTION_RE.search(message):
        return 0
    candidates: list[int] = []
    for m in _OUTPUT_LIMIT_ANCHOR_RE.finditer(message):
        candidates.append(int(m.group(1).replace(',', '')))
    # 兜底：报错文本里的其余整数（剔除请求值本身，限定合理区间）
    for m in re.finditer(r'\d[\d,]{2,9}', message):
        n = int(m.group(0).replace(',', ''))
        if n != requested and 512 <= n <= 1_000_000:
            candidates.append(n)
    if not candidates:
        return 0
    limit = max(candidates)
    return limit if limit < requested else 0


def get_output_limit(base_url: str, model: str) -> int:
    """模型输出上限：报错自学习缓存优先，其次已知模型表；未知返回 0（不钳制）。"""
    key = ((base_url or '').rstrip('/'), (model or '').lower())
    learned = _LEARNED_OUTPUT_LIMITS.get(key)
    if learned:
        return learned
    return _known_output_limit(model)


def _learn_output_limit(base_url: str, model: str, error_text: str, requested: int) -> int:
    """从 400 报错解析并缓存真实输出上限；返回解析到的上限（0 = 没解析到）。"""
    limit = _parse_max_tokens_limit(error_text, requested)
    if limit:
        _LEARNED_OUTPUT_LIMITS[((base_url or '').rstrip('/'), (model or '').lower())] = limit
    return limit


def _error_text(resp) -> str:
    """提取响应体里的错误文案（优先 JSON error.message，退回纯文本）。"""
    try:
        err_body = resp.json()
        if isinstance(err_body, dict):
            err = err_body.get("error")
            if isinstance(err, dict):
                return (err.get("message") or "")[:300]
    except Exception:
        pass
    try:
        return (resp.text or "")[:300]
    except Exception:
        return ""


class LLMGateway:
    """统一 LLM 调用网关。

    所有 LLM 调用应经此入口，统一错误处理与重试。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 180, max_retries: int = 2):
        self.base_url = (base_url or "").rstrip("/")
        # 兼容 base_url 不带 /v1 的情况
        if not self.base_url.endswith("/v1"):
            # 如果已经是 .../v1 就不加；如果是 .../chat/completions 的根就加
            # 实际调用时拼 /chat/completions，所以 base_url 应是 .../v1
            pass
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def _effective_max_tokens(self, max_tokens: int) -> int:
        """请求值按模型已知/已学习输出上限钳制；上限未知则原样发出（报错自适应兜底）。"""
        limit = get_output_limit(self.base_url, self.model)
        return min(max_tokens, limit) if limit else max_tokens

    def chat(self, messages: list[dict], temperature: float = 0.7,
             max_tokens: int = 4096, **extra) -> ModelResult:
        """同步调用 LLM，返回 ModelResult。

        自动重试：可重试类错误最多重试 max_retries 次。
        """
        result = ModelResult()
        url = f"{self.base_url}/chat/completions"
        headers = build_auth_headers(self.api_key)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self._effective_max_tokens(max_tokens),
        }
        payload.update(extra)

        last_error = ""
        for attempt in range(1, self.max_retries + 2):  # 1 正常 + max_retries 重试
            result.attempts = attempt
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                # 【对齐 chat_stream】非 200 状态码先分类报错（旧实现直接 resp.json()，
                # 非 JSON 错误页抛异常进 UNKNOWN 盲目重试，错误信息不可读）
                if resp.status_code != 200:
                    fc = _classify_error(None, resp.status_code, None)
                    body_text = ''
                    try:
                        err_body = resp.json()
                        if isinstance(err_body, dict):
                            err = err_body.get("error")
                            if isinstance(err, dict):
                                body_text = (err.get("message") or "")[:300]
                    except Exception:
                        body_text = (resp.text or "")[:200]
                    # 【输出上限自适应】400/422 且报错指向 max_tokens 超限 → 解析真实
                    # 上限、钳制 payload 后立即重发（解析不出具体数字则退 8192 兜底）
                    if resp.status_code in (400, 422):
                        _limit = _learn_output_limit(self.base_url, self.model,
                                                     body_text, payload["max_tokens"])
                        if _limit and _limit < payload["max_tokens"]:
                            payload["max_tokens"] = _limit
                            continue
                        if not _limit and payload["max_tokens"] > 8192:
                            payload["max_tokens"] = 8192
                            continue
                    last_error = f"LLM 调用失败（HTTP {resp.status_code}）：{body_text or '服务返回错误'}"
                    result.failure_class = fc if fc != FailureClass.NONE else FailureClass.UNAVAILABLE
                    result.error = last_error
                    result.raw = {"status_code": resp.status_code}
                    if result.failure_class not in _RETRYABLE or attempt > self.max_retries:
                        return result
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                body = resp.json()

                content, finish_reason, fc = _extract_content(body)
                if fc == FailureClass.NONE:
                    result.content = content
                    result.finish_reason = finish_reason
                    result.raw = body
                    result.failure_class = FailureClass.NONE
                    return result

                # 失败：记录错误，判断是否重试
                if body.get("error"):
                    err = body["error"]
                    last_error = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                elif fc == FailureClass.EMPTY_RESPONSE:
                    last_error = f"LLM 返回空内容（finish_reason={finish_reason}）"
                elif fc == FailureClass.FORMAT_ERROR:
                    last_error = f"LLM 输出被截断（finish_reason=length），请减小 max_tokens 或换用更长输出的模型"
                else:
                    last_error = f"LLM 返回格式异常"

                result.failure_class = fc
                result.error = last_error
                result.finish_reason = finish_reason
                result.raw = body

                if fc not in _RETRYABLE or attempt > self.max_retries:
                    return result

                # 重试前等待（指数退避）
                time.sleep(min(2 ** (attempt - 1), 4))

            except requests.exceptions.Timeout as e:
                last_error = f"LLM 调用超时（{self.timeout}秒）"
                result.failure_class = FailureClass.TIMEOUT
                result.error = last_error
                if attempt > self.max_retries:
                    return result
                time.sleep(min(2 ** (attempt - 1), 4))

            except requests.exceptions.ConnectionError as e:
                last_error = f"LLM 服务不可达：{str(e)[:100]}"
                result.failure_class = FailureClass.UNAVAILABLE
                result.error = last_error
                if attempt > self.max_retries:
                    return result
                time.sleep(min(2 ** (attempt - 1), 4))

            except Exception as e:
                last_error = f"LLM 调用失败：{str(e)[:200]}"
                result.failure_class = FailureClass.UNKNOWN
                result.error = last_error
                if attempt > self.max_retries:
                    return result
                time.sleep(min(2 ** (attempt - 1), 4))

        return result

    def chat_stream(self, messages: list[dict], temperature: float = 0.7,
                    max_tokens: int = 4096, yield_reasoning_heartbeat: bool = False, **extra):
        """流式调用 LLM，yield delta content.

        兼容多种 chunk 格式（标准 OpenAI / 简化 delta / 直接 content）。
        yield_reasoning_heartbeat=True 时，thinking（reasoning_content）帧到达即 yield
        REASONING_HB 哨兵（不混入正文），供上层转发 SSE 心跳防代理 idle 掐断。

        【流式重试规则 · 新增 2026-08-21】
        仅在"第一个正文/思考 chunk 吐出去之前"的失败允许重试（避免已吐部分内容导致正文重复）：
          - 5xx / CONNECTION / TIMEOUT / SERVICE_BUSY 类 → 指数退避最多 2 次重试（3 次机会）
          - 401 / 429 / 4xx（非 400/422）→ 不重试，直接把真实错误体摘要抛出来
          - 400/422 且报错指向 max_tokens 超限 → 解析真实上限钳制 payload 后重发
        任何抛错都带 status_code/body_text/traceId（若上游给了），不再只说"状态码:503"。
        """
        url = f"{self.base_url}/chat/completions"
        headers = build_auth_headers(self.api_key)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self._effective_max_tokens(max_tokens),
            "stream": True,
        }
        payload.update(extra)

        got_content = False
        got_reasoning = False  # 思考型模型（GLM-4.7/R1 等）先输出 reasoning_content 再输出 content
        max_attempts = self.max_retries + 1  # 1 次首发 + max_retries 重试（默认 3 次总机会）
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout, stream=True)
                # 【输出上限自适应】400/422 且报错指向 max_tokens 超限 → 解析真实上限、
                # 钳制 payload 后重发一次；解析不出具体数字则退 8192 兜底（均只重发一次）。
                if resp.status_code in (400, 422):
                    _limit = _learn_output_limit(self.base_url, self.model,
                                                 _error_text(resp), payload["max_tokens"])
                    if _limit and _limit < payload["max_tokens"]:
                        payload["max_tokens"] = _limit
                        resp = requests.post(url, headers=headers, json=payload,
                                             timeout=self.timeout, stream=True)
                    elif not _limit and payload["max_tokens"] > 8192:
                        payload["max_tokens"] = 8192
                        resp = requests.post(url, headers=headers, json=payload,
                                             timeout=self.timeout, stream=True)
                # 【非 200 先分类，再决定重试 vs 直接抛】（旧实现非 200 一律一次不重试直接抛，
                # 导致上游鉴权 SERVICE_BUSY 这种理应重试的 503 瞬间失败）
                if resp.status_code != 200:
                    body_text = ''
                    trace_id = ''
                    try:
                        body_text = resp.text[:500]
                    except Exception:
                        pass
                    try:
                        # 某些网关把 traceId 放到 header（比如阿里云/火山/豆包），抓出来方便用户
                        for h in ('x-trace-id', 'trace-id', 'X-Tt-Logid'):
                            if resp.headers.get(h):
                                trace_id = resp.headers[h]
                                break
                        # 响应体里有 traceId 字段也取
                        try:
                            import json as _json
                            _jb = _json.loads(body_text or '{}')
                            if isinstance(_jb, dict):
                                trace_id = _jb.get('traceId') or trace_id or _jb.get('trace_id') or trace_id
                        except Exception:
                            pass
                    except Exception:
                        pass
                    try:
                        err_body = resp.json()
                        if isinstance(err_body, dict) and isinstance(err_body.get("error"), dict):
                            body_text = (err_body["error"].get("message") or body_text)[:500]
                    except Exception:
                        pass
                    fc = _classify_error(None, resp.status_code, None)
                    # 401/403/429（鉴权配额耗尽）永不重试——否则循环打空 quota
                    is_no_retry = fc in (FailureClass.AUTHENTICATION, FailureClass.QUOTA) \
                        or resp.status_code in (401, 403, 429) \
                        or (400 <= resp.status_code < 500 and resp.status_code not in (400, 408, 422, 429, 401, 403, 499))
                    suffix = f"（traceId={trace_id}）" if trace_id else ""
                    err_msg = f"LLM 流式调用失败（HTTP {resp.status_code}）{suffix}：{body_text or '服务返回错误'}"
                    # 只有"还没吐出任何内容帧"才能重试——否则内容重复
                    can_retry = (not got_content) and (not got_reasoning) and attempt < max_attempts \
                        and (not is_no_retry) and (fc in _RETRYABLE or fc == FailureClass.NONE)
                    if can_retry:
                        time.sleep(min(2 ** (attempt - 1), 4))
                        continue
                    raise LLMError(err_msg,
                                   fc if fc != FailureClass.NONE else FailureClass.UNAVAILABLE)

                for kind, value in _iter_sse_events(resp):
                    if kind == "message":
                        got_content = True
                        yield value
                    elif kind == "delta":
                        got_content = True
                        yield value
                    elif kind == "reasoning":
                        got_reasoning = True
                        if yield_reasoning_heartbeat:
                            yield REASONING_HB
                # 【空回复根因修复】流走完但一个内容帧都没有 → 先非流式兜底再报错：
                if not got_content:
                    if got_reasoning:
                        raise LLMError(
                            f"思考型模型正文为空：模型思考已产出但 max_tokens={max_tokens} 被耗尽，"
                            f"无余量输出正文（model={self.model}）。请增大 max_tokens 或关闭思考模式",
                            FailureClass.FORMAT_ERROR,
                        )
                    try:
                        fb_payload = dict(payload)
                        fb_payload["stream"] = False
                        fb_resp = requests.post(url, headers=headers, json=fb_payload, timeout=self.timeout)
                        if fb_resp.status_code == 200:
                            fb_content, fb_reason, fb_fc = _extract_content(fb_resp.json())
                            if fb_content:
                                yield fb_content
                                return
                    except Exception:
                        pass
                    # 空内容（非流式兜底也空）：可重试吗？（也只在首发 attempt 允许重发）
                    if attempt < max_attempts and not got_content:
                        time.sleep(min(2 ** (attempt - 1), 4))
                        continue
                    raise LLMError(
                        f"LLM 流式返回空内容（HTTP 200 但无任何 content 帧，model={self.model}，"
                        f"已尝试非流式兜底仍为空。可能原因：max_tokens 过小/模型拒答/供应商网关异常）",
                        FailureClass.EMPTY_RESPONSE,
                    )
                # 正常走完流 + 有内容 → 返回
                return
            except requests.exceptions.Timeout as e:
                if attempt < max_attempts and (not got_content) and (not got_reasoning):
                    time.sleep(min(2 ** (attempt - 1), 4))
                    last_exc = e
                    continue
                raise LLMError(f"LLM 流式调用超时（{self.timeout}秒，attempt={attempt}/{max_attempts}）",
                               FailureClass.TIMEOUT)
            except requests.exceptions.ConnectionError as e:
                if attempt < max_attempts and (not got_content) and (not got_reasoning):
                    time.sleep(min(2 ** (attempt - 1), 4))
                    last_exc = e
                    continue
                raise LLMError(f"LLM 服务不可达：{str(e)[:100]}（attempt={attempt}/{max_attempts}）",
                               FailureClass.UNAVAILABLE)
            except LLMError:
                # 上面分类抛出来的 LLMError 已经带了正确 failure_class 和完整信息，直接抛
                raise
            except Exception as e:
                if attempt < max_attempts and (not got_content) and (not got_reasoning):
                    time.sleep(min(2 ** (attempt - 1), 4))
                    last_exc = e
                    continue
                raise LLMError(
                    f"LLM 流式调用异常：{type(e).__name__}: {str(e)[:300]}（attempt={attempt}/{max_attempts}）",
                    FailureClass.UNKNOWN,
                )
        # 所有尝试用完但被静默掉的最后保险
        if last_exc is not None:
            raise LLMError(f"LLM 流式调用重试耗尽：{type(last_exc).__name__} {str(last_exc)[:200]}",
                           FailureClass.UNAVAILABLE)


def get_llm_config(app_module=None):
    """统一获取 LLM 配置（从 AIConfig 表或环境变量）。

    返回 (base_url, api_key, model)。
    可传入 app_module 避免循环 import；不传则延迟 import。
    """
    import os
    if app_module is None:
        import app as app_module

    with app_module.app.app_context():
        # 必须取激活配置，否则多配置场景下 query.first() 会取到旧配置导致 api_key 为空
        config = app_module.AIConfig.get_active()
        api_key = config.api_key if config and config.api_key else os.environ.get("USER_LLM_API_KEY", "")
        base_url = config.base_url if config else os.environ.get("USER_LLM_BASE_URL", "https://api.deepseek.com/v1")
        model = config.model if config else os.environ.get("USER_LLM_MODEL", "deepseek-chat")
        # 确保 base_url 以 /v1 结尾
        if not base_url.rstrip("/").endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        return base_url, api_key, model


def create_gateway(app_module=None) -> LLMGateway:
    """便捷工厂：从配置创建 LLMGateway。"""
    base_url, api_key, model = get_llm_config(app_module)
    return LLMGateway(base_url, api_key, model)
