# -*- coding: utf-8 -*-
"""生成后自检闭环（Post-Generation Validation）

校验 AI 输出是否符合核心铁律：
- timeline 维度必须是 JSON 数组，卷数严格等于总卷数，卷号连续，章号不超上限
- plot_design 维度检测五幕/卷数是否与铁律一致
- character_profiles 维度禁出现 JSON 符号

接入点：smart_generate / smart_batch / smart_dim_edit 在 LLM 输出完成后调用 validate，
error 级问题自动重试一次（带错误反馈），warn 级问题仅提示不阻断。
"""
from dataclasses import dataclass, asdict
from typing import Optional, List
import json
import re


@dataclass
class ValidationIssue:
    code: str           # 问题代码，如 'VOL_COUNT_MISMATCH' / 'JSON_INVALID'
    severity: str       # 'error' 阻断级 / 'warn' 提示级
    message: str        # 人类可读描述
    auto_fix: str = ''  # 可自动修复的提示文案（用于重试 prompt）


class PostGenValidator:
    """生成后自检：校验 AI 输出是否符合核心铁律"""

    def __init__(self, total_volumes: int, chapters_per_volume: int, max_retries: int = 1):
        self.tv = max(1, int(total_volumes or 1))
        self.cpv = max(1, int(chapters_per_volume or 50))
        self.max_chapters = self.tv * self.cpv
        self.max_retries = max(0, int(max_retries))

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def validate(self, dim_key: str, content: str, raw_length_hint: int = 0) -> List[ValidationIssue]:
        """返回校验问题列表，空列表表示通过
        raw_length_hint：SSE 流里原始拼接内容长度（仅在清理后 content 为空时用于提示用户"其实有内容但被清理/模型拒答空"，
        帮助判断 EMPTY_OUTPUT 究竟是模型完全没吐字，还是 fence+清理把内容清干净了。）
        """
        if not content or not content.strip():
            msg = '生成内容为空'
            if raw_length_hint and raw_length_hint > 0:
                if raw_length_hint <= 20:
                    msg = f'生成内容为空（仅 {raw_length_hint} 字符，模型大概率未输出有效内容），请重试或补充需求后重试'
                else:
                    msg = f'生成内容为空（模型实际返回了 {raw_length_hint} 字符，但清理后为空，已自动重试并放宽清理/参数；若仍空请补充更具体的生成要求）'
            return [ValidationIssue('EMPTY_OUTPUT', 'error', msg)]
        handlers = {
            'timeline': self._validate_timeline,
            'plot_design': self._validate_outline,
            'character_profiles': self._validate_character,
        }
        handler = handlers.get(dim_key)
        if not handler:
            return []  # 未配置校验规则的维度直接放行
        return handler(content.strip())

    def should_retry(self, issues: List[ValidationIssue]) -> bool:
        """是否有 error 级问题且还有重试次数"""
        return any(i.severity == 'error' for i in issues) and self.max_retries > 0

    def build_retry_hint(self, issues: List[ValidationIssue]) -> str:
        """根据问题列表生成重试提示文案"""
        errs = [i for i in issues if i.severity == 'error']
        if not errs:
            return ''
        lines = ['上一版存在以下问题，请严格按铁律重新生成：']
        for i, e in enumerate(errs, 1):
            lines.append(f'{i}. [{e.code}] {e.message}')
            if e.auto_fix:
                lines.append(f'   修复建议：{e.auto_fix}')
        return '\n'.join(lines)

    def to_meta(self, issues: List[ValidationIssue]) -> list:
        """转为可下发给前端的 meta 结构"""
        return [asdict(i) for i in issues]

    # ------------------------------------------------------------------
    # 维度专属校验
    # ------------------------------------------------------------------
    def _validate_timeline(self, content: str) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        # 剥离代码块包裹
        raw = content
        fence = re.match(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
        if fence:
            raw = fence.group(1).strip()

        try:
            vols = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            return [ValidationIssue(
                'JSON_INVALID', 'error',
                f'timeline 应为 JSON 数组，解析失败：{e}',
                auto_fix='直接输出 JSON 数组，不要包裹 Markdown 代码块，不要解释性文字'
            )]

        if not isinstance(vols, list):
            # 可能是 {volumes: [...]} 等包装
            if isinstance(vols, dict):
                for k in ['volumes', 'data', 'result', 'items', 'list']:
                    if isinstance(vols.get(k), list):
                        vols = vols[k]
                        break
            if not isinstance(vols, list):
                return [ValidationIssue(
                    'JSON_INVALID', 'error',
                    f'timeline 应为 JSON 数组，实际类型 {type(vols).__name__}',
                    auto_fix='输出顶层数组 [ ... ]，不要包在对象里'
                )]

        if not vols:
            return [ValidationIssue(
                'VOL_EMPTY', 'error',
                f'卷列表为空，铁律要求 {self.tv} 卷',
                auto_fix=f'生成 {self.tv} 卷的 JSON 数组'
            )]

        # 卷数校验
        if len(vols) != self.tv:
            issues.append(ValidationIssue(
                'VOL_COUNT_MISMATCH', 'error',
                f'铁律要求 {self.tv} 卷，实际 {len(vols)} 卷',
                auto_fix=f'严格生成 {self.tv} 卷，不多不少'
            ))

        # 卷号连续性校验
        indices = []
        for v in vols:
            if not isinstance(v, dict):
                continue
            idx = v.get('volume_index')
            if idx is None:
                idx = v.get('volume_id')
            try:
                indices.append(int(idx))
            except (TypeError, ValueError):
                pass
        if indices:
            indices.sort()
            expected = list(range(1, self.tv + 1))
            if indices != expected:
                issues.append(ValidationIssue(
                    'VOL_INDEX_GAP', 'warn',
                    f'卷号不连续：{indices}，应为 {expected[:len(indices)]}',
                    auto_fix='卷号从 1 开始连续递增'
                ))

        # 章号溢出校验
        for v in vols:
            if not isinstance(v, dict):
                continue
            vol_idx = v.get('volume_index') or v.get('volume_id') or '?'
            for n in v.get('nodes', []) or []:
                if not isinstance(n, dict):
                    continue
                ch = str(n.get('chapters', ''))
                m = re.match(r'(\d+)\s*-\s*(\d+)', ch)
                if m:
                    start_n, end_n = int(m.group(1)), int(m.group(2))
                    if end_n > self.max_chapters:
                        issues.append(ValidationIssue(
                            'CH_NUM_OVERFLOW', 'error',
                            f'卷 {vol_idx} 节点章号 {ch} 超过上限 {self.max_chapters}',
                            auto_fix=f'章号上限 {self.max_chapters}'
                        ))
                    if start_n < 1:
                        issues.append(ValidationIssue(
                            'CH_NUM_UNDERFLOW', 'error',
                            f'卷 {vol_idx} 节点章号 {ch} 起始小于 1'
                        ))

        # 必填字段校验
        required_fields = ['volume_index', 'main_plot']
        for v in vols:
            if not isinstance(v, dict):
                continue
            for f in required_fields:
                val = v.get(f)
                if val is None or (isinstance(val, str) and not val.strip()):
                    issues.append(ValidationIssue(
                        'FIELD_MISSING', 'warn',
                        f'卷 {v.get("volume_id") or v.get("volume_index") or "?"} 缺少字段 {f}'
                    ))
        return issues

    def _validate_outline(self, content: str) -> List[ValidationIssue]:
        """五幕总纲：检测是否提及 N 幕/卷（与 tv 对齐）"""
        issues: List[ValidationIssue] = []
        # 统计"第X幕"或"第X卷"出现次数
        matches = re.findall(r'第\s*([一二三四五六七八九十百零壹貳贰叁肆伍陆陸柒捌玖拾\d]+)\s*[幕卷]', content)
        if matches:
            from app import _extract_volume_index
            nums = set()
            for m in matches:
                n = _extract_volume_index(m)
                if n and 1 <= n <= 100:
                    nums.add(n)
            if nums and len(nums) != self.tv:
                issues.append(ValidationIssue(
                    'ACT_COUNT_MISMATCH', 'warn',
                    f'总纲提及 {len(nums)} 幕/卷（{sorted(nums)}），铁律要求 {self.tv} 卷',
                    auto_fix=f'严格按 {self.tv} 幕/卷规划'
                ))
        return issues

    def _validate_character(self, content: str) -> List[ValidationIssue]:
        """人物维度：禁 JSON 符号，要求纯中文分行"""
        issues: List[ValidationIssue] = []
        # 检测 JSON 结构符号（允许引号出现在对话中，但 [ ] { } 同时出现视为 JSON）
        if re.search(r'[\[\]{}]', content) and re.search(r'[:]\s*["\u4e00-\u9fff]', content):
            issues.append(ValidationIssue(
                'CHAR_JSON_LEAK', 'error',
                '人物维度出现 JSON 结构符号，应纯中文分行',
                auto_fix='用“姓名：xxx\\n身份：xxx”分行输出，禁用 [ ] { } " : 等符号'
            ))
        return issues
