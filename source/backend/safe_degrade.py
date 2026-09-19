# -*- coding: utf-8 -*-
"""静默降级治理（P0-3）：统一封装"可选组件"的导入与执行降级。

目标：
  - 默认：主流程不阻断，降级时打印一条 `[降级]` 日志，让故障可观测（而非静默吞掉）。
  - FANSHU_STRICT=1/true：任何降级直接抛异常，便于开发 / CI 尽早暴露缺模块或隐藏故障。

用法：
  - safe_import：替代 `try: from X import a, b ... except ImportError: xxx = None` 的导入降级。
  - 直接读取 STRICT / 调用 mark_degraded 来自定义其它降级点的可观测性。
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, List, Optional, Sequence, Union


# FANSHU_STRICT=1/true/yes 时进入严格模式：降级即抛异常，而不是静默回退。
STRICT = os.environ.get('FANSHU_STRICT', '').strip().lower() in ('1', 'true', 'yes')


def mark_degraded(component: str, reason: str):
    """记录一次降级。默认打印可观测日志；STRICT 模式下抛出异常。"""
    msg = f'[降级] {component}: {reason}'
    if STRICT:
        raise RuntimeError(f'FANSHU_STRICT=1，拒绝静默降级 -> {msg}')
    print(msg, flush=True)


def safe_import(module: str,
                names: Union[str, Sequence[str]],
                label: str = '',
                fallbacks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """尝试 `from {module} import {names...}`，失败时降级。

    - names：单个名字字符串，或名字序列（本地名与导入名一致）。
    - fallbacks：{本地名: 回退值}，导入失败时填充；其余名字置 None。
    - 成功返回 {名字: 值}；失败（非 STRICT）时记录降级并返回含回退值的字典。
    调用方再用「解包赋值」把需要的名字绑定到模块局部命名空间。
    """
    if isinstance(names, str):
        names = [names]
    names = list(names)
    result: Dict[str, Any] = {n: None for n in names}
    try:
        mod = importlib.import_module(module)
        for n in names:
            result[n] = getattr(mod, n)
    except (ImportError, AttributeError) as exc:
        # 仅对「模块缺失 / 名字不存在」降级；语法错误等真实故障仍向上抛出，避免掩盖坏代码。
        reason = f'{exc.__class__.__name__}: {exc}'
        mark_degraded(label or module, reason)
        for n, v in (fallbacks or {}).items():
            result[n] = v
    return result


def degrade(component: str, reason: str, fallback: Any = None) -> Any:
    """运行时降级点：记录可观测日志，返回 fallback。STRICT 模式下抛异常。

    用于替代代码里 `try: ... except Exception: pass`（吞异常）的运行时分支。
    """
    mark_degraded(component, reason)
    return fallback