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

        # 卷数校验（tv=1 时允许模型自定 N 卷，不强校验数量一致）
        if self.tv > 1 and len(vols) != self.tv:
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
            n = len(indices)
            expected = list(range(1, n + 1))
            if indices != expected:
                issues.append(ValidationIssue(
                    'VOL_INDEX_GAP', 'warn',
                    f'卷号不连续：{indices}，应为 {expected}',
                    auto_fix='卷号从 1 开始连续递增'
                ))

        # 章号溢出校验（仅 nodes 和已落章节的节点）
        for v in vols:
            if not isinstance(v, dict):
                continue
            vol_idx = v.get('volume_index') or v.get('volume_id') or '?'
            for n in v.get('nodes', []) or []:
                if not isinstance(n, dict):
                    continue
                ch = str(n.get('chapters', ''))
                nums = re.findall(r'\d+', ch)
                if not nums:
                    continue
                end_n = int(nums[-1])
                start_n = int(nums[0])
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

        # 新结构校验：main_events 必须有且不含 chapters 字段，且 estimated_chapters 合计 = cpv
        for v in vols:
            if not isinstance(v, dict):
                continue
            vlabel = f'卷 {v.get("volume_index") or v.get("volume_id") or "?"}'
            # 卷级 6 要素（warn 级，缺字段提示但不强制失败）
            for f in ('characters', 'timeline_anchor', 'location', 'realm_change', 'age_change'):
                if not str(v.get(f) or '').strip():
                    issues.append(ValidationIssue(
                        'VOL_6TUPLE_MISSING', 'warn',
                        f'{vlabel} 缺少卷级 6 要素字段 {f}'
                    ))
            main_events = v.get('main_events')
            if not isinstance(main_events, list) or len(main_events) == 0:
                # 兼容旧结构：如果有 nodes 也放行，但优先提示缺 main_events
                if not isinstance(v.get('nodes'), list) or len(v.get('nodes')) == 0:
                    issues.append(ValidationIssue(
                        'MAIN_EVENTS_MISSING', 'error',
                        f'{vlabel} 缺少 main_events（8-12 个主要剧情事件）',
                        auto_fix='每卷必须提供 8-12 个 main_event，不要写空数组'
                    ))
                continue
            if len(main_events) < 8:
                issues.append(ValidationIssue(
                    'MAIN_EVENTS_TOO_FEW', 'warn',
                    f'{vlabel} main_events 只有 {len(main_events)} 个，建议 8-12 个',
                    auto_fix='补足到至少 8 个主要剧情事件'
                ))
            if len(main_events) > 12:
                issues.append(ValidationIssue(
                    'MAIN_EVENTS_TOO_MANY', 'warn',
                    f'{vlabel} main_events 有 {len(main_events)} 个，建议不超过 12 个',
                    auto_fix='合并/精简到最多 12 个主要剧情事件'
                ))
            total_est = 0
            for i, ev in enumerate(main_events):
                if not isinstance(ev, dict):
                    continue
                evlabel = f'{vlabel} 事件 {ev.get("index") or (i+1)}'
                if ev.get('chapters') is not None and str(ev.get('chapters', '')).strip():
                    issues.append(ValidationIssue(
                        'MAIN_EVENT_CHAPTERS_FORBIDDEN', 'error',
                        f'{evlabel} 出现了 chapters 字段，事件层禁止套章节（留空/不写）',
                        auto_fix='删除 main_event.chapters，改为 estimated_chapters 整数；chapters 只在节点设计阶段生成 nodes 时使用'
                    ))
                ec = ev.get('estimated_chapters')
                try:
                    ec_int = int(ec) if ec is not None else 0
                    if ec_int < 0:
                        ec_int = 0
                except (TypeError, ValueError):
                    ec_int = 0
                total_est += ec_int
                if ec_int <= 0:
                    issues.append(ValidationIssue(
                        'EST_CHAPTERS_MISSING', 'warn',
                        f'{evlabel} 缺少或非法 estimated_chapters（应是正整数，该事件预计支撑多少章正文）'
                    ))
                # 事件级 6 要素（缺一 warn）
                for f in ('characters', 'events', 'time', 'location', 'realm_change', 'age_change'):
                    if not str(ev.get(f) or '').strip():
                        issues.append(ValidationIssue(
                            'EVENT_6TUPLE_MISSING', 'warn',
                            f'{evlabel} 缺少 6 要素字段 {f}'
                        ))
            # 密度自检：合计 estimated_chapters ≈ cpv（允许±1误差，但尽量严格）
            if total_est > 0 and abs(total_est - self.cpv) > max(1, int(round(self.cpv * 0.05))):
                issues.append(ValidationIssue(
                    'EST_CHAPTERS_SUM_MISMATCH', 'error',
                    f'{vlabel} main_events.estimated_chapters 合计 {total_est}，与本卷 {self.cpv} 章不匹配（误差允许±{max(1, int(round(self.cpv * 0.05)))}）',
                    auto_fix=f'调整每个事件的 estimated_chapters，使总和刚好 {self.cpv}'
                ))
            # nodes 若已存在则允许，但首次应为空（非强制，仅提示）
            nds = v.get('nodes')
            if isinstance(nds, list) and len(nds) > 0 and total_est > 0:
                pass  # 节点阶段产物，通过

        # 必填字段（保留向后兼容：volume_index + main_plot/summary 至少一个）
        for v in vols:
            if not isinstance(v, dict):
                continue
            if v.get('volume_index') is None and v.get('volume_id') is None:
                issues.append(ValidationIssue(
                    'FIELD_MISSING', 'warn',
                    f'卷 {v.get("volume_id") or v.get("volume_index") or "?"} 缺少字段 volume_index/volume_id'
                ))
            has_summary = str(v.get('summary') or '').strip()
            has_main_plot = str(v.get('main_plot') or '').strip()
            if not has_summary and not has_main_plot:
                issues.append(ValidationIssue(
                    'FIELD_MISSING', 'warn',
                    f'卷 {v.get("volume_id") or v.get("volume_index") or "?"} 缺少 summary/main_plot（至少要有一个总体剧情概要）'
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
