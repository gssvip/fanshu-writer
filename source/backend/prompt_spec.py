"""PromptSpec：prompt 声明式定义 + 编译器 + golden 断言。

借鉴司命 siming-ai 的 PromptCompiler 设计，解决番茄项目"prompt 全是
Python f-string，改一个字只能上线试"的问题：

  - prompt 是 Markdown + YAML front matter，元数据声明 inputs/budget/golden_cases
  - PromptCompiler 在 CI 里拒绝：未知占位符 / 预算溢出 / golden 断言失败
  - prompt 改动不调模型就能验

PromptSpec 文件格式（.md）:
    ---
    name: chapter_writer
    description: 章节正文生成 prompt
    inputs: [system_prompt, user_prompt, chapter_num, word_budget]
    output_format: narrative_text
    budget:
      max_tokens: 4096
      temperature: 0.7
    golden_cases:
      - input: {chapter_num: 1, word_budget: 2400}
        assert_contains: ["第", "章"]
        assert_min_length: 2000
    ---
    # 章节正文生成

    你是专业网文作者。请根据以下信息写第 {{chapter_num}} 章。

    {{system_prompt}}

    {{user_prompt}}

    字数要求：{{word_budget}} 字。

使用方式：
    from prompt_spec import load_prompt_spec, PromptCompiler
    spec = load_prompt_spec("prompt_specs/chapter_writer.md")
    rendered = spec.render(chapter_num=5, word_budget=2400,
                           system_prompt="...", user_prompt="...")
    compiler = PromptCompiler()
    issues = compiler.validate(spec)  # CI 中调用
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class GoldenCase:
    """golden 断言用例。"""
    input: dict = field(default_factory=dict)
    assert_contains: list[str] = field(default_factory=list)
    assert_min_length: int = 0
    assert_max_length: int = 999999


@dataclass
class PromptSpec:
    """prompt 声明式定义。

    从 Markdown + YAML front matter 文件加载。
    """
    name: str
    description: str = ""
    template: str = ""                    # body 部分（含 {{占位符}}）
    inputs: list[str] = field(default_factory=list)
    output_format: str = "text"
    max_tokens: int = 4096
    temperature: float = 0.7
    golden_cases: list[GoldenCase] = field(default_factory=list)
    source_path: str = ""

    def render(self, **kwargs) -> str:
        """渲染模板，替换 {{key}} 占位符。

        未提供的占位符保留原样（不报错，编译器会校验）。
        """
        result = self.template
        for key, value in kwargs.items():
            placeholder = "{{" + key + "}}"
            result = result.replace(placeholder, str(value))
        return result

    def get_placeholders(self) -> list[str]:
        """提取模板中所有 {{占位符}} 名称。"""
        return list(set(re.findall(r"\{\{(\w+)\}\}", self.template)))


class PromptCompiler:
    """PromptSpec 编译器/校验器。

    在 CI 中调用 validate() 检查：
      - 未知占位符（模板有但 inputs 未声明）
      - 未使用输入（inputs 声明了但模板没用）
      - 预算合理性（max_tokens 不应为 0 或过大）
      - golden 断言（用 golden_cases 的 input 渲染后检查）
    """

    MAX_REASONABLE_TOKENS = 32768

    def validate(self, spec: PromptSpec) -> list[str]:
        """返回问题列表，空列表表示通过。"""
        issues: list[str] = []

        # 1. 占位符一致性
        placeholders = set(spec.get_placeholders())
        declared_inputs = set(spec.inputs)

        unknown = placeholders - declared_inputs
        if unknown:
            issues.append(
                f"[{spec.name}] 模板使用未声明的占位符: {sorted(unknown)}"
            )

        unused = declared_inputs - placeholders
        if unused:
            issues.append(
                f"[{spec.name}] inputs 声明了但模板未使用: {sorted(unused)}"
            )

        # 2. 预算合理性
        if spec.max_tokens <= 0:
            issues.append(f"[{spec.name}] max_tokens 不应 <= 0")
        elif spec.max_tokens > self.MAX_REASONABLE_TOKENS:
            issues.append(
                f"[{spec.name}] max_tokens={spec.max_tokens} 过大，"
                f"建议 <= {self.MAX_REASONABLE_TOKENS}"
            )

        # 3. 模板非空
        if not spec.template.strip():
            issues.append(f"[{spec.name}] 模板内容为空")

        # 4. golden 断言
        for i, case in enumerate(spec.golden_cases):
            try:
                rendered = spec.render(**case.input)
                # 检查未渲染的占位符
                unrendered = re.findall(r"\{\{\w+\}\}", rendered)
                if unrendered:
                    issues.append(
                        f"[{spec.name}] golden case {i} 有未渲染占位符: {unrendered[:3]}"
                    )
                # 检查包含断言
                for text in case.assert_contains:
                    if text not in rendered:
                        issues.append(
                            f"[{spec.name}] golden case {i} 断言失败: "
                            f"渲染结果未包含 '{text}'"
                        )
                # 检查长度断言
                if len(rendered) < case.assert_min_length:
                    issues.append(
                        f"[{spec.name}] golden case {i} 长度 {len(rendered)} "
                        f"< 期望最小 {case.assert_min_length}"
                    )
            except Exception as e:
                issues.append(f"[{spec.name}] golden case {i} 执行异常: {str(e)[:100]}")

        return issues


def load_prompt_spec(path: str | Path) -> PromptSpec:
    """从 Markdown + YAML front matter 文件加载 PromptSpec。

    文件格式：
        ---
        name: xxx
        inputs: [a, b]
        ---
        模板内容...
    """
    path = Path(path)
    content = path.read_text(encoding="utf-8")

    # 解析 YAML front matter
    front_matter = {}
    body = content
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2)
        if yaml:
            front_matter = yaml.safe_load(fm_text) or {}
        else:
            # 无 PyYAML 时的简易解析
            for line in fm_text.split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    k = k.strip()
                    v = v.strip()
                    if v.startswith("[") and v.endswith("]"):
                        front_matter[k] = [
                            x.strip().strip("'\"")
                            for x in v[1:-1].split(",") if x.strip()
                        ]
                    else:
                        front_matter[k] = v.strip("'\"")

    # 解析 golden_cases
    golden_cases = []
    for gc_data in front_matter.get("golden_cases", []):
        if isinstance(gc_data, dict):
            golden_cases.append(GoldenCase(
                input=gc_data.get("input", {}),
                assert_contains=gc_data.get("assert_contains", []),
                assert_min_length=gc_data.get("assert_min_length", 0),
                assert_max_length=gc_data.get("assert_max_length", 999999),
            ))

    budget = front_matter.get("budget", {}) or {}
    return PromptSpec(
        name=front_matter.get("name", path.stem),
        description=front_matter.get("description", ""),
        template=body.strip(),
        inputs=front_matter.get("inputs", []),
        output_format=front_matter.get("output_format", "text"),
        max_tokens=budget.get("max_tokens", 4096) if isinstance(budget, dict) else 4096,
        temperature=budget.get("temperature", 0.7) if isinstance(budget, dict) else 0.7,
        golden_cases=golden_cases,
        source_path=str(path),
    )


def load_all_specs(specs_dir: str | Path = "prompt_specs") -> list[PromptSpec]:
    """加载目录下所有 .md prompt spec 文件。"""
    specs_dir = Path(specs_dir)
    if not specs_dir.exists():
        return []
    specs = []
    for md_file in specs_dir.rglob("*.md"):
        try:
            specs.append(load_prompt_spec(md_file))
        except Exception:
            continue
    return specs


def validate_all_specs(specs_dir: str | Path = "prompt_specs") -> list[str]:
    """校验目录下所有 prompt spec，返回所有问题列表（CI 用）。"""
    compiler = PromptCompiler()
    all_issues = []
    for spec in load_all_specs(specs_dir):
        issues = compiler.validate(spec)
        all_issues.extend(issues)
    return all_issues
