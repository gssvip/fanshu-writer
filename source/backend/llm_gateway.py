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


def _iter_sse_events(resp):
    """健壮解析 OpenAI 兼容 SSE 流，逐个 yield (kind, value)。

    kind ∈ {'delta', 'reasoning', 'message'}：
      - 'delta'      正文片段（标准流式 delta.content）
      - 'reasoning'  思考片段（delta.reasoning_content，GLM-4.7/R1 等）
      - 'message'    服务端忽略 stream 返回非流式 message.content（一次性完整正文）
    兼容差异：data: 有无空格、多行 data 拼接、\\r\\n 换行、[DONE] 结束。
    """
    buffer = b""
    for raw in resp.iter_content(chunk_size=1024):
        if not raw:
            continue
        buffer += raw
        while True:
            idx = buffer.find(b"\n\n")
            if idx == -1:
                break
            blob = buffer[:idx]
            buffer = buffer[idx + 2:]
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
            "max_tokens": max_tokens,
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
        """
        url = f"{self.base_url}/chat/completions"
        headers = build_auth_headers(self.api_key)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        payload.update(extra)

        got_content = False
        got_reasoning = False  # 思考型模型（GLM-4.7/R1 等）先输出 reasoning_content 再输出 content
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout, stream=True)
            # 【空回复根因修复】非 200 状态码必须显式抛错：
            # 旧实现直接 iter_lines 遍历错误页（无 data: 帧），流静默结束 → 调用方拿到空内容还以为成功
            if resp.status_code != 200:
                body_text = ''
                try:
                    body_text = resp.text[:300]
                except Exception:
                    pass
                fc = _classify_error(None, resp.status_code, None)
                try:
                    err_body = resp.json()
                    if isinstance(err_body, dict) and isinstance(err_body.get("error"), dict):
                        body_text = (err_body["error"].get("message") or body_text)[:300]
                except Exception:
                    pass
                raise LLMError(
                    f"LLM 流式调用失败（HTTP {resp.status_code}）：{body_text or '服务返回错误'}",
                    fc if fc != FailureClass.NONE else FailureClass.UNAVAILABLE,
                )
            for kind, value in _iter_sse_events(resp):
                if kind == "message":
                    # 服务端忽略 stream 参数返回非流式 message → 一次性 yield 完整正文
                    got_content = True
                    yield value
                elif kind == "delta":
                    got_content = True
                    yield value
                elif kind == "reasoning":
                    got_reasoning = True
                    if yield_reasoning_heartbeat:
                        yield REASONING_HB  # 上层据此发 SSE 心跳，防推理期代理 idle 掐断
            # 【空回复根因修复】流走完但一个内容帧都没有 → 先非流式兜底再报错：
            # 很多聚合/中转服务测试连接(stream=False)正常，但 stream=True 支持有缺陷，
            # 此时补一次非流式请求仍能拿到正文，避免"测试成功、智驾空回复"的假故障。
            if not got_content:
                if got_reasoning:
                    # 思考型模型：思考产出正常但正文为空 → max_tokens 被思考耗尽（属截断，重试加大 max_tokens 有意义）
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
                raise LLMError(
                    f"LLM 流式返回空内容（HTTP 200 但无任何 content 帧，model={self.model}，"
                    f"已尝试非流式兜底仍为空。可能原因：max_tokens 过小/模型拒答/供应商网关异常）",
                    FailureClass.EMPTY_RESPONSE,
                )
        except requests.exceptions.Timeout:
            raise LLMError(f"LLM 流式调用超时（{self.timeout}秒）", FailureClass.TIMEOUT)
        except requests.exceptions.ConnectionError as e:
            raise LLMError(f"LLM 服务不可达：{str(e)[:100]}", FailureClass.UNAVAILABLE)


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
