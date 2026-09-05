"""【防遗忘报告→AI智驾 修正闭环域】（自 chat_collab_bp.py 拆出，架构门禁 P2a）。

共享符号（_apply_patches_to_text, _character_profiles_to_text, _clean_patches, build_review_rules, chat_collab_bp）由 chat_collab_bp.py 末尾的
_register_split_domains() 调用 init() 注入——本模块不反向 import
chat_collab_bp，避免循环导入；路由经 register() 挂到同一个
Blueprint，URL / endpoint / methods 与拆分前的装饰器注册完全一致。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from flask import jsonify, request


def init(**deps):
    """chat_collab_bp 加载到文件末尾时调用：把共享符号注入本模块全局命名空间。"""
    globals().update(deps)


def register(bp):
    """把本域路由挂到 chat_collab_bp（endpoint 默认取函数名，与装饰器注册一致）。"""
    bp.add_url_rule('/api/ai/smart/fix-from-report', view_func=smart_fix_from_report, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/apply-fix', view_func=smart_apply_fix, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/fix-text-from-report', view_func=smart_fix_text_from_report, methods=['POST'])
    bp.add_url_rule('/api/ai/smart/apply-text-fix', view_func=smart_apply_text_fix, methods=['POST'])


# ============================================================================
# 防遗忘报告 → AI智驾 修正闭环
# 把防遗忘检查报告（violations/suggestions/pending_foreshadowing/narrative_debt）
# 打通给AI，让AI基于报告对设定维度内容生成修正方案，用户确认后落地。
# ============================================================================

# 可被修正的设定维度字段白名单（与 BookBible 维度字段一一对应）
_FIXABLE_DIM_FIELDS = {
    'concept': '核心构思', 'key_rules': '设定/规则', 'worldbuilding': '世界观',
    'character_profiles': '人物档案', 'plot_design': '大纲', 'timeline': '剧情时间线',
    'foreshadowing': '伏笔', 'locations': '地点', 'style_guide': '文风指南',
}


def smart_fix_from_report():
    """基于防遗忘检查报告生成设定修正方案（不直接落地，返回给用户确认）。

    body: { book_id, report_id?, skill_pack_ids? }
    - report_id 为空时取最近一份防遗忘报告
    返回: { plan: [{ dim, label, issues:[..], action, new_content }], report_title, report_id }
    每个 plan 项对应一个维度的修正：列出该维度涉及的诊断问题、修正动作、修正后的完整设定内容
    """
    from app import db, Book, BookBible, Chapter, AIConfig
    from llm_gateway import get_llm_config
    import app as app_module

    data = request.json or {}
    book_id = data.get('book_id')
    report_id = data.get('report_id')
    skill_pack_ids = data.get('skill_pack_ids') or []
    volume_ids = data.get('volume_ids') or []
    if not isinstance(volume_ids, list):
        volume_ids = []

    if not book_id:
        return jsonify({'error': '缺少 book_id'}), 400
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '书籍不存在'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    cfg = AIConfig.get_active()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 取指定报告（默认最近一份）
    target_report = None
    target_rec = None
    try:
        reports = json.loads(bb.anti_forget_reports) if bb.anti_forget_reports else []
    except Exception:
        reports = []
    if not isinstance(reports, list) or not reports:
        return jsonify({'error': '暂无防遗忘检查报告，请先执行检查'}), 400

    if report_id:
        for r in reports:
            if isinstance(r, dict) and r.get('id') == report_id:
                target_rec = r
                target_report = r.get('report') or {}
                break
        if not target_rec:
            return jsonify({'error': '未找到指定报告'}), 404
    else:
        # 取最近一份（按 checked_at 降序）
        def _ts(r):
            return r.get('checked_at', '') or ''
        recent = sorted([r for r in reports if isinstance(r, dict)], key=_ts, reverse=True)
        if not recent:
            return jsonify({'error': '暂无防遗忘检查报告'}), 400
        target_rec = recent[0]
        target_report = target_rec.get('report') or {}
        report_id = target_rec.get('id')

    # 构建诊断要点文本 + 各维度当前内容
    chapters = None
    if volume_ids:
        chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    diag_parts = []
    for key, label in [('violations', '一致性违规'), ('pending_foreshadowing', '待回收伏笔'),
                       ('narrative_debt', '叙事债务'), ('suggestions', '改进建议'),
                       ('character_cognition_issues', '角色认知问题')]:
        items = target_report.get(key) or []
        if not isinstance(items, list) or not items:
            continue
        # 诊断项截断：避免报告过大导致 LLM 上下文超限或响应极慢
        limit = 5 if key == 'violations' else 3
        kept_lines = []
        for it in items[:limit]:
            if isinstance(it, dict) and volume_ids and chapters:
                loc = it.get('location') or ''
                ch = _locate_chapter_by_location(loc, chapters)
                if ch and getattr(ch, 'parent_id', None) not in volume_ids:
                    continue
            if isinstance(it, dict):
                msg = it.get('desc') or it.get('message') or it.get('issue') or \
                      it.get('promise') or it.get('content') or it.get('suggestion') or str(it)
                fix = it.get('fix') or ''
                loc = it.get('location') or ''
                line = f'  - {msg[:120]}'
                if loc:
                    line += f'（位置：{loc[:60]}）'
                if fix:
                    line += f' 💡修正：{fix[:120]}'
                kept_lines.append(line)
            else:
                kept_lines.append(f'  - {str(it)[:120]}')
        if kept_lines:
            diag_parts.append(f'■ {label}（{len(items)}项）')
            diag_parts.extend(kept_lines)
    diag_text = '\n'.join(diag_parts) or '（报告无明确诊断项）'

    # 各维度当前内容（供AI参考，避免修正时凭空臆造）
    dim_now_parts = []
    for field, label in _FIXABLE_DIM_FIELDS.items():
        val = (getattr(bb, field, '') or '').strip()
        if val:
            if field == 'character_profiles' and val.startswith('['):
                try:
                    val = _character_profiles_to_text(val)
                except Exception:
                    pass
            dim_now_parts.append(f'【{label}·当前内容】\n{val[:500]}')
    dim_now_text = '\n\n'.join(dim_now_parts) or '（各维度暂无内容）'

    # 去AI/审稿阶段·专属规则（通用核心+行文规范+完整去AI手册+review技能包）
    skill_note = build_review_rules(skill_pack_ids, mode='agent')

    volume_section = ''
    if volume_ids:
        volume_section = f"\n\n【本次只处理以下卷】\n卷ID: {volume_ids}\n请只针对这些卷涉及的问题生成修正方案"

    system_prompt = f"""你是资深网文设定修正师。任务：基于防遗忘检查报告的诊断，对小说设定维度内容生成修正方案。

【防遗忘检查报告诊断要点】
{diag_text}{volume_section}

【各维度当前内容】
{dim_now_text}
{chr(10) + chr(10) + '【技能包指引】' + chr(10) + skill_note if skill_note else ''}

【你的任务】—— 重点是【精准局部修正】，绝非整字段重写
针对报告诊断出的问题，逐维度生成“修正方案”。每个维度一个修正项，包含：
1. issues：该维度涉及的诊断问题（从上方诊断中归纳）
2. action：一句话说明怎么改（如：补全主角境界突破条件、修正时间线倒流）
3. patches：精准局部改动列表（核心）。每个元素 {original, rewritten}：
   - original：要在该维度当前内容中“找到的那段原文”的精确抄录（一字不差，取自上方【当前内容】截断部分或可按该维度常见格式构造；多取一点上下文避免歧义）
   - rewritten：这段原文修改后的新内容
   - 只列真正需要改动的片段；没有问题的部分一律不要出现在 patches 里，落地时这些部分会原样保留
4. new_content：（可省略）修正后该维度的完整内容；仅当你无法用 patches 精确表达时才给出，落地时若提供了能命中原文的 patches 会优先用 patches，new_content 只作兜底整字段覆盖

【伏笔/人物/地点/时间线等“列表式”维度特别注意】务必逐条给出 original→rewritten，原样保留未改动条目，绝不能只输出概要导致其余条目丢失。

【输出格式铁律】严格输出 JSON 数组（不要 markdown 代码块、不要任何解释文字），结构：
[
  {{
    "dim": "维度字段名（concept/key_rules/worldbuilding/character_profiles/plot_design/timeline/foreshadowing/locations/style_guide 之一）",
    "label": "维度中文名",
    "issues": ["该维度涉及的诊断问题1", "问题2"],
    "action": "修正动作说明",
    "patches": [
      {{"original": "当前内容中的一段原文", "rewritten": "修改后的对应内容"}}
    ],
    "new_content": "（可选）修正后完整内容"
  }}
]
只输出确实需要修正的维度，没有问题的维度不要输出。最多输出6个维度。"""

    try:
        base_url, api_key, model = get_llm_config(app_module)
    except Exception as e:
        return jsonify({'error': f'AI 配置异常：{e}'}), 400

    from app import _call_llm
    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': '请基于报告诊断生成设定修正方案'}],
        max_tokens=0, temperature=0.5, task_type='creation'
    )
    if err:
        return jsonify({'error': f'生成修正方案失败：{err}'}), 500

    # 解析 JSON 数组
    plan = []
    try:
        import re as _re_fix
        m = _re_fix.search(r'\[[\s\S]*\]', content or '')
        if m:
            plan = json.loads(m.group())
    except (json.JSONDecodeError, ValueError):
        pass
    # 兜底：若解析失败，把整段作为单项返回
    if not isinstance(plan, list) or not plan:
        plan = [{'dim': 'foreshadowing', 'label': '伏笔',
                 'issues': ['AI 输出未按 JSON 格式，请查看 new_content 原文'],
                 'action': '请人工核对', 'new_content': content or ''}]

    # 清洗：只保留白名单维度，字段补全
    cleaned = []
    for p in plan:
        if not isinstance(p, dict):
            continue
        dim = (p.get('dim') or '').strip()
        if dim not in _FIXABLE_DIM_FIELDS:
            continue
        item = {
            'dim': dim,
            'label': p.get('label') or _FIXABLE_DIM_FIELDS[dim],
            'issues': p.get('issues') if isinstance(p.get('issues'), list) else ([str(p.get('issues'))] if p.get('issues') else []),
            'action': (p.get('action') or '').strip(),
            'new_content': (p.get('new_content') or '').strip(),
            'patches': _clean_patches(p.get('patches')),
        }
        if item['new_content'] or item['patches']:
            cleaned.append(item)

    return jsonify({
        'plan': cleaned,
        'report_title': target_rec.get('title', '防遗忘检查报告'),
        'report_id': report_id,
    })


def smart_apply_fix():
    """应用用户确认的修正方案到对应 bible 维度字段（落地）。

    body: { book_id, fixes: [{ dim, new_content }] }
    仅写入用户勾选的维度，未勾选的不动。
    返回: { ok, applied: [{ dim, label }] }
    """
    from app import db, BookBible
    data = request.json or {}
    book_id = data.get('book_id')
    fixes = data.get('fixes') or []

    if not book_id or not isinstance(fixes, list) or not fixes:
        return jsonify({'error': '参数无效：需要 book_id 和非空 fixes'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    applied = []
    for f in fixes:
        if not isinstance(f, dict):
            continue
        dim = (f.get('dim') or '').strip()
        new_content = (f.get('new_content') or '').strip()
        if dim not in _FIXABLE_DIM_FIELDS:
            continue
        patches = _clean_patches(f.get('patches'))
        cur = (getattr(bb, dim, '') or '').strip()
        # 【精准局部落地】优先用 patches 逐项命中替换：只改被修改的部分，
        # 其余未改动内容 100% 原样保留，绝不再一股脑整字段覆盖导致内容不全。
        if patches and cur:
            patched, applied_count = _apply_patches_to_text(cur, patches)
            # 有局部命中 → 用局部替换结果；一条都没命中且无 new_content → 放弃（不破坏原内容）
            if applied_count > 0:
                final = patched
            elif new_content:
                final = new_content
            else:
                continue
        else:
            # 无补丁：沿用旧的整字段覆盖语义（向后兼容）
            if not new_content:
                continue
            final = new_content
        setattr(bb, dim, final)
        applied.append({'dim': dim, 'label': _FIXABLE_DIM_FIELDS[dim]})

    if applied:
        bb.updated_at = datetime.now(timezone.utc)
        db.session.commit()

    return jsonify({'ok': True, 'applied': applied})


# ============================================================================
# 第三阶段：基于防遗忘报告自动定位章节段落并生成正文改写补丁
# ============================================================================

def _locate_chapter_by_location(location: str, chapters: list):
    """根据违规位置字符串定位章节。支持“第N章”或标题匹配。"""
    if not location:
        return None
    # 优先匹配“第N章”
    nums = re.findall(r'第\s*(\d+)\s*章', str(location))
    if nums:
        idx = int(nums[0]) - 1
        non_vol = [c for c in chapters if not getattr(c, 'is_volume', False)]
        if 0 <= idx < len(non_vol):
            return non_vol[idx]
    #  fallback：匹配章节标题
    for c in chapters:
        title = getattr(c, 'title', '') or ''
        if title and title in str(location):
            return c
    return None


def _extract_json_from_llm_text(text: str):
    """从 LLM 返回的文本中提取 JSON 对象（兼容代码块和普通文本）。"""
    if not text:
        return None
    # 优先尝试去掉 markdown 代码块
    fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        candidate = text.strip()
    # 取第一个 { 到最后一个 } 之间的内容
    m = re.search(r'\{[\s\S]*\}', candidate)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        # 尝试修复尾部多余逗号
        try:
            fixed = re.sub(r',\s*([}\]])', r'\1', m.group())
            return json.loads(fixed)
        except Exception:
            return None


def smart_fix_text_from_report():
    """基于防遗忘检查报告，定位具体章节段落并生成正文改写补丁。

    body: { book_id, report_id?, skill_pack_ids? }
    返回: { fixes: [{ chapter_id, chapter_title, paragraph_index, original, rewritten, reason, violation_desc }] }
    """
    try:
        from app import db, Book, BookBible, Chapter, AIConfig, _call_llm

        data = request.json or {}
        book_id = data.get('book_id')
        report_id = data.get('report_id')
        skill_pack_ids = data.get('skill_pack_ids') or []
        volume_ids = data.get('volume_ids') or []
        if not isinstance(volume_ids, list):
            volume_ids = []

        if not book_id:
            return jsonify({'error': '缺少 book_id'}), 400
        book = Book.query.get(book_id)
        if not book:
            return jsonify({'error': '书籍不存在'}), 404
        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            return jsonify({'error': '请先创建设定'}), 400

        cfg = AIConfig.get_active()
        if not cfg or not cfg.api_key:
            return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

        try:
            reports = json.loads(bb.anti_forget_reports) if bb.anti_forget_reports else []
        except Exception:
            reports = []
        if not isinstance(reports, list) or not reports:
            return jsonify({'error': '暂无防遗忘检查报告'}), 400

        target_rec = None
        target_report = {}
        if report_id:
            for r in reports:
                if isinstance(r, dict) and r.get('id') == report_id:
                    target_rec = r
                    target_report = r.get('report') or {}
                    break
            if not target_rec:
                return jsonify({'error': '未找到指定报告'}), 404
        else:
            recent = sorted([r for r in reports if isinstance(r, dict)], key=lambda x: x.get('checked_at', ''), reverse=True)
            if not recent:
                return jsonify({'error': '暂无防遗忘检查报告'}), 400
            target_rec = recent[0]
            target_report = target_rec.get('report') or {}
            report_id = target_rec.get('id')

        violations = target_report.get('violations') or []
        if not isinstance(violations, list):
            violations = []

        def _severity_key(v):
            sev = (v.get('severity') or '').strip()
            if sev == '严重':
                return 0
            if sev == '警告':
                return 1
            return 2

        # 只保留有 location 的 dict 违规，按严重程度排序，取前 5
        filtered = sorted(
            [v for v in violations if isinstance(v, dict) and v.get('location')],
            key=_severity_key
        )[:5]
        if not filtered:
            no_loc = [v for v in violations if isinstance(v, dict) and not v.get('location')]
            if no_loc:
                return jsonify({'fixes': [], 'empty_reason': '报告中的违规项缺少位置信息（location 字段为空），无法定位到具体章节。请重新运行防遗忘检查，确保违规项包含“第N章”或章节标题。'})
            return jsonify({'fixes': [], 'empty_reason': '报告中没有违规项，无需修正正文。'})

        chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

        # 去AI/审稿阶段·专属规则（通用核心+行文规范+完整去AI手册+review技能包）
        skill_note = build_review_rules(skill_pack_ids, mode='agent')

        cases = []
        case_chapters = []
        located_but_filtered_out = 0
        not_located = 0
        for v in filtered:
            location = v.get('location') or ''
            desc = v.get('desc') or ''
            fix_hint = v.get('fix') or ''
            severity = v.get('severity') or ''
            ch = _locate_chapter_by_location(location, chapters)
            if not ch or not ch.content:
                not_located += 1
                continue
            if volume_ids and getattr(ch, 'parent_id', None) not in volume_ids:
                located_but_filtered_out += 1
                continue
            cases.append({
                'case_index': len(cases),
                'chapter_id': ch.id,
                'chapter_title': ch.title or f'第{chapters.index(ch) + 1}章',
                'location': location,
                'severity': severity,
                'desc': desc,
                'fix_hint': fix_hint,
                'context': str(ch.content)[:1200],
            })
            case_chapters.append(ch)

        if not cases:
            if volume_ids and located_but_filtered_out > 0 and not_located == 0:
                return jsonify({'fixes': [], 'empty_reason': f'共 {located_but_filtered_out} 处违规已定位到章节，但均不在所选分卷内。请选择包含违规章节的分卷，或不限分卷重新检查。'})
            if not_located > 0 and located_but_filtered_out == 0:
                return jsonify({'fixes': [], 'empty_reason': f'共 {not_located} 处违规的位置无法匹配到已有章节（位置格式需为“第N章”或章节标题）。请检查违规位置描述，或重新运行防遗忘检查。'})
            return jsonify({'fixes': [], 'empty_reason': f'共 {len(filtered)} 处违规均无法生成可修正案例（{not_located} 处定位失败，{located_but_filtered_out} 处不在所选分卷）。请检查违规位置或重新选择分卷。'})

        case_blocks = []
        for case in cases:
            case_blocks.append(f"""【违规案例 {case['case_index']}】
位置：{case['location']}
严重程度：{case['severity']}
问题描述：{case['desc']}
修正建议：{case['fix_hint']}
章节上下文：
{case['context']}""")
        cases_text = '\n\n'.join(case_blocks)

        skill_section = f"【技能包指引】\n{skill_note}\n" if skill_note else ''
        volume_section = ''
        if volume_ids:
            volume_section = f"【本次只处理以下卷】\n卷ID: {volume_ids}\n请只从这些卷中定位需要改写的段落。\n"

        system_prompt = f"""你是资深网文正文修正师。下面有 {len(cases)} 处违规，每处包含位置、问题描述、修正建议和对应章节上下文。
请逐条分析，只改写确实需要修正的原文段落，保持文风、剧情、语气不变，不扩写。

{skill_section}{volume_section}{cases_text}

【输出格式铁律】严格输出 JSON 对象，不要 markdown 代码块、不要解释：
{{
  "fixes": [
    {{
      "case_index": 0,
      "paragraph_index": 0,
      "original": "需要改写的原文完整段落（50-400字）",
      "rewritten": "改写后的段落",
      "reason": "一句话说明为什么改写"
    }}
  ]
}}
如果某条违规无法定位或无需改写，可以不输出对应 fix。"""

        content, err = _call_llm(
            [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': '请生成正文改写补丁'}],
            max_tokens=0, temperature=0.4, task_type='creation'
        )
        if err:
            return jsonify({'error': f'生成正文改写补丁失败：{err}'}), 500

        parsed = _extract_json_from_llm_text(content)
        if parsed is None:
            return jsonify({'fixes': [], 'empty_reason': 'AI 返回内容无法解析为有效 JSON 补丁（可能模型未按格式输出）。请重试，或在 AI 配置中换一个响应更稳定的模型。'})
        arr = parsed.get('fixes') or []
        if not isinstance(arr, list):
            return jsonify({'fixes': [], 'empty_reason': 'AI 返回的 fixes 字段格式异常（非数组）。请重试，或换一个模型。'})

        fixes = []
        dropped_no_match = 0
        for fx in arr:
            if not isinstance(fx, dict):
                continue
            case_index = fx.get('case_index')
            if not isinstance(case_index, int) or case_index < 0 or case_index >= len(cases):
                continue
            case = cases[case_index]
            ch = case_chapters[case_index]
            original = (fx.get('original') or '').strip()
            rewritten = (fx.get('rewritten') or '').strip()
            if not original or not rewritten or original == rewritten:
                continue
            if original not in str(ch.content):
                dropped_no_match += 1
                continue
            try:
                paragraph_index = int(fx.get('paragraph_index', 0))
            except Exception:
                paragraph_index = 0
            fixes.append({
                'chapter_id': case['chapter_id'],
                'chapter_title': case['chapter_title'],
                'paragraph_index': paragraph_index,
                'original': original,
                'rewritten': rewritten,
                'reason': (fx.get('reason') or '').strip() or f"修正：{case['desc']}",
                'violation_desc': case['desc'],
                'report_id': report_id,
            })

        if not fixes and dropped_no_match > 0:
            return jsonify({'fixes': [], 'empty_reason': f'AI 生成了 {len(arr)} 条补丁，但原文片段均无法在章节内容中精确匹配（{dropped_no_match} 处对不上）。可能是模型对原文的复述偏差较大，建议重试或换一个模型。'})
        return jsonify({'fixes': fixes, 'report_title': target_rec.get('title', ''), 'report_id': report_id})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'生成正文改写补丁失败：{e}'}), 500


def smart_apply_text_fix():
    """应用用户确认的正文改写补丁到对应章节。

    body: { book_id, fixes: [{ chapter_id, paragraph_index?, original, rewritten }] }
    返回: { ok, applied: [{ chapter_id, chapter_title, count }] }
    """
    from app import db, BookBible, Chapter
    data = request.json or {}
    book_id = data.get('book_id')
    fixes = data.get('fixes') or []

    if not book_id or not isinstance(fixes, list) or not fixes:
        return jsonify({'error': '参数无效：需要 book_id 和非空 fixes'}), 400
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '请先创建设定'}), 400

    # 按章节分组，同章节按 paragraph_index 降序处理，避免索引漂移
    by_chapter: dict[str, list] = {}
    for f in fixes:
        if not isinstance(f, dict):
            continue
        cid = f.get('chapter_id')
        original = (f.get('original') or '').strip()
        rewritten = (f.get('rewritten') or '').strip()
        if not cid or not original or not rewritten:
            continue
        by_chapter.setdefault(cid, []).append(f)

    applied = []
    for cid, ch_fixes in by_chapter.items():
        ch = Chapter.query.filter_by(id=cid, book_id=book_id, is_volume=False).first()
        if not ch:
            continue
        content = ch.content or ''
        # 优先按整段替换；如果原文不在内容中，尝试按 paragraph_index 替换
        count = 0
        # 先尝试直接字符串替换（original 为完整段落）
        for f in ch_fixes:
            original = f.get('original')
            rewritten = f.get('rewritten')
            if original in content:
                content = content.replace(original, rewritten, 1)
                count += 1
        # 对于未直接替换成功的，尝试按段落索引
        paragraphs = [p for p in content.split('\n\n')]
        for f in sorted(ch_fixes, key=lambda x: int(x.get('paragraph_index', 0) or 0), reverse=True):
            if f.get('original') in (ch.content or ''):
                # 已在上一步替换
                continue
            idx = int(f.get('paragraph_index', 0) or 0)
            if 0 <= idx < len(paragraphs):
                old_para = paragraphs[idx].strip()
                if old_para and old_para != f.get('rewritten'):
                    paragraphs[idx] = f.get('rewritten')
                    count += 1
        if count > 0:
            ch.content = '\n\n'.join(paragraphs)
            ch.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            applied.append({'chapter_id': cid, 'chapter_title': ch.title or '', 'count': count})

    if applied:
        bb.updated_at = datetime.now(timezone.utc)
        db.session.commit()

    return jsonify({'ok': True, 'applied': applied})
