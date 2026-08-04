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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
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
                    max_tokens: int = 4096, **extra):
        """流式调用 LLM，yield delta content。

        兼容多种 chunk 格式（标准 OpenAI / 简化 delta / 直接 content）。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        payload.update(extra)

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout, stream=True)
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8", errors="ignore") if isinstance(line, bytes) else line
                if line_str.startswith("data: "):
                    line_str = line_str[6:]
                if line_str.strip() == "[DONE]":
                    break
                try:
                    import json
                    chunk = json.loads(line_str)
                    # 标准 OpenAI 格式
                    if "choices" in chunk and chunk["choices"]:
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    # 简化格式：直接 content 字段
                    elif chunk.get("content"):
                        yield chunk["content"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
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
        config = app_module.AIConfig.query.first()
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
