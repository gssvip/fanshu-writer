"""AI 分析蓝图 — 从 app.py 拆出（巨石外迁第 5 批：ai-analyze 域，约 1490 行）。

路由清单（11 个）：
  /api/books/<id>/ai-analyze-content          整书分析（构思/设定/大纲/世界观/人物/伏笔/地点）
  /api/books/<id>/ai-analyze-dimension       单维度深挖
  /api/books/<id>/ai-analyze-character       人物档案
  /api/books/<id>/ai-analyze-plot-volume    剧情时间线（按卷）
  /api/books/<id>/ai-analyze-character-volume   人物档案（按卷）
  /api/books/<id>/ai-analyze-inventory-volume   物品清单（按卷）
  /api/books/<id>/ai-analyze-dynamic-volume     动态设定（按卷）
  /api/books/<id>/ai-analyze-foreshadowing-volume 伏笔（按卷）
  /api/books/<id>/ai-analyze-locations-volume     地点（按卷）
  /api/books/<id>/clear-timeline             清空剧情时间线
  /api/books/<id>/ai-analyze-from-reports     从动态报告反向归因

按卷分析的 5 个 helper（_get_volume_chapters_ordered 等）留在 app.py
（被 ai-import-recognize / dynamic-reports 复用），本蓝图延迟导入。

依赖方向（无循环）：
  - 顶层仅依赖 flask + auth_utils + 标准库；
  - 模型与 helper 在每个路由函数体内延迟导入（同 general_chat.py 模式）。
"""
from __future__ import annotations

import os
import json
from json import JSONDecodeError
import time
import requests
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from auth_utils import login_required

ai_analyze_bp = Blueprint('ai_analyze', __name__)


@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-content', methods=['POST'])
@login_required
def ai_analyze_content(book_id):
    """AI分析作品内容，自动提取并填充构思、设定、大纲、世界观、人物、剧情、伏笔、地点等维度"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 获取作品所有章节内容（限制总字数避免超长）
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not chapters:
        return jsonify({'error': '作品没有章节内容，无法分析'}), 400

    # 拼接章节内容，限制总长度
    full_text = ''
    max_chars = 12000  # 约 12000 中文字符
    for ch in chapters:
        segment = f'【{ch.title}】\n{(ch.content or "")[:2000]}\n\n'
        if len(full_text) + len(segment) > max_chars:
            remaining = max_chars - len(full_text)
            if remaining > 200:
                full_text += segment[:remaining]
            break
        full_text += segment

    system_prompt = f"""你是专业的小说分析师。请分析以下小说内容，提取并归纳各维度的设定信息。
严格按JSON格式输出，不要任何其他文字：
{{
  "concept": "一句话概括核心构思（30字内）",
  "key_rules": "核心设定规则：能力体系、限制、禁忌等（200字内）",
  "plot_design": "大纲：主线冲突、分卷规划、关键转折、结局走向（300字内）",
  "worldbuilding": "世界观：世界背景、力量体系、社会结构、地理概况（300字内）",
  "character_profiles": "主要人物档案：姓名、身份、性格、动机、关系（300字内）",
  "timeline": "剧情时间线：按顺序列出关键事件（200字内）",
  "foreshadowing": "伏笔线索：已发现或可能的伏笔（150字内）",
  "locations": "地点体系：三级分类（大区域/城市/场景），JSON格式",
  "generated_summary": "作品内容摘要（100字内）"
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers=build_auth_headers(api_key),
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'作品标题：{book.title}\n\n以下是作品内容：\n\n{full_text}'}
                ],
                'temperature': 0.3,
                'max_tokens': 4000,
                'response_format': {'type': 'json_object'}
            },
            timeout=180)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        analysis = json.loads(content)

        # 更新或创建 BookBible
        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)

        # 只更新非空字段，不覆盖用户已写内容
        fields = ['concept', 'key_rules', 'plot_design', 'worldbuilding',
                  'character_profiles', 'timeline', 'foreshadowing', 'locations', 'generated_summary']
        updated_fields = []
        for field in fields:
            raw_val = analysis.get(field, '')
            # AI 可能返回 dict/list 等结构化数据，统一转为字符串
            if isinstance(raw_val, (dict, list)):
                new_val = json.dumps(raw_val, ensure_ascii=False, indent=2)
            else:
                new_val = str(raw_val).strip() if raw_val else ''
            if new_val:
                existing_val = getattr(bb, field, '') or ''
                if existing_val:
                    # 已有内容则追加
                    setattr(bb, field, f'{existing_val}\n\n【AI识别】\n{new_val}')
                else:
                    setattr(bb, field, new_val)
                updated_fields.append(field)

        bb.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({
            'success': True,
            'updated_fields': updated_fields,
            'bible': bb.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-dimension', methods=['POST'])
@login_required
def ai_analyze_dimension(book_id):
    """AI分析作品内容，只识别并填充指定维度"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    dimension = data.get('dimension', '')
    if not dimension:
        return jsonify({'error': '缺少 dimension 参数'}), 400

    # 维度 → bible字段 映射
    dim_field_map = {
        'concept': 'concept',
        'settings': 'key_rules',
        'outline': 'plot_design',
        'worldview': 'worldbuilding',
        'characters': 'character_profiles',
        'plot': 'timeline',
        'foreshadowing': 'foreshadowing',
        'locations': 'locations',
    }
    field = dim_field_map.get(dimension)
    if not field:
        return jsonify({'error': f'未知维度: {dimension}'}), 400

    dim_labels = {
        'concept': '构思', 'settings': '设定', 'outline': '大纲',
        'worldview': '世界观', 'characters': '人物', 'plot': '剧情',
        'foreshadowing': '伏笔', 'locations': '地点',
    }
    dim_label = dim_labels.get(dimension, dimension)

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not chapters:
        return jsonify({'error': '作品没有章节内容，无法分析'}), 400

    full_text = ''
    max_chars = 12000
    for ch in chapters:
        segment = f'【{ch.title}】\n{(ch.content or "")[:2000]}\n\n'
        if len(full_text) + len(segment) > max_chars:
            remaining = max_chars - len(full_text)
            if remaining > 200:
                full_text += segment[:remaining]
            break
        full_text += segment

    dim_prompts = {
        'concept': '一句话概括核心构思（30字内）',
        'key_rules': '核心设定规则：能力体系、限制、禁忌等（200字内）',
        'plot_design': '大纲：主线冲突、分卷规划、关键转折、结局走向（300字内）',
        'worldbuilding': '世界观：世界背景、力量体系、社会结构、地理概况（300字内）',
        'character_profiles': '主要人物档案：姓名、身份、性格、动机、关系（300字内）',
        'timeline': '剧情时间线：按顺序列出关键事件（200字内）',
        'foreshadowing': '伏笔线索：已发现或可能的伏笔（150字内）',
        'locations': '地点体系：三级分类（大区域/城市/场景），JSON格式',
    }

    # agent 协同：读取 bible 其他维度作为已知上下文，让识别结果与已确认维度保持一致
    bb = BookBible.query.filter_by(book_id=book_id).first()
    known_ctx_parts = []
    if bb:
        dim_label_map = {'concept': '构思', 'key_rules': '设定/规则', 'worldbuilding': '世界观',
                         'character_profiles': '人物', 'plot_design': '大纲', 'timeline': '剧情',
                         'foreshadowing': '伏笔', 'locations': '地点'}
        for f, lbl in dim_label_map.items():
            v = getattr(bb, f, '') or ''
            if v.strip() and f != field:  # 排除当前维度自身
                known_ctx_parts.append(f'【{lbl}（已确认）】\n{v[:600]}')
    known_ctx = '\n\n'.join(known_ctx_parts) if known_ctx_parts else '（暂无其他维度参考）'

    system_prompt = f"""你是专业的小说分析师，正在与其他维度分析师协同工作。
请分析以下小说内容，提取并归纳“{dim_label}”维度的设定信息。

【已确认的其他维度设定】（识别结果必须与这些维度保持一致，不可矛盾）
{known_ctx}

严格按JSON格式输出，不要任何其他文字：
{{
  "{field}": "{dim_prompts.get(field, dim_label)}"
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'

        # 构建请求体（response_format 某些LLM不支持，捕获后重试）
        req_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f'作品标题：{book.title}\n\n以下是作品内容：\n\n{full_text}'}
            ],
            'temperature': 0.3,
            'max_tokens': 2000,
            'response_format': {'type': 'json_object'}
        }

        # 第一次尝试带 response_format；不支持则去掉重试
        resp = requests.post(f'{base}/chat/completions',
            headers=build_auth_headers(api_key),
            json=req_body,
            timeout=90)

        # 若返回400且与response_format相关，去掉该参数重试
        if resp.status_code == 400:
            err_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
            err_msg = str(err_body.get('error', '')).lower()
            if 'response_format' in err_msg or 'json_object' in err_msg or 'unrecognized' in err_msg:
                req_body.pop('response_format', None)
                resp = requests.post(f'{base}/chat/completions',
                    headers=build_auth_headers(api_key),
                    json=req_body,
                    timeout=90)

        if resp.status_code != 200:
            try:
                err_detail = resp.json().get('error', {}).get('message', '') or resp.text[:300]
            except Exception:
                err_detail = resp.text[:300]
            return jsonify({'error': f'AI调用失败({resp.status_code}): {err_detail}'}), 500

        result = resp.json()
        if 'choices' not in result or not result['choices']:
            return jsonify({'error': f'AI返回异常: {str(result)[:300]}'}), 500
        content = result['choices'][0]['message']['content']

        # JSON 解析容错：提取第一个 {...} 块
        try:
            analysis = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            import re as _re_json
            m = _re_json.search(r'\{[\s\S]*\}', content)
            if m:
                analysis = json.loads(m.group(0))
            else:
                return jsonify({'error': f'AI返回非JSON格式: {content[:200]}'}), 500

        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)

        raw_val = analysis.get(field, '')
        if isinstance(raw_val, (dict, list)):
            new_val = json.dumps(raw_val, ensure_ascii=False, indent=2)
        else:
            new_val = str(raw_val).strip() if raw_val else ''

        if new_val:
            existing_val = getattr(bb, field, '') or ''
            if existing_val:
                setattr(bb, field, f'{existing_val}\n\n【AI识别】\n{new_val}')
            else:
                setattr(bb, field, new_val)

        bb.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({
            'success': True,
            'dimension': dimension,
            'field': field,
            'value': getattr(bb, field, ''),
            'bible': bb.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-character', methods=['POST'])
@login_required
def ai_analyze_character(book_id):
    """AI从章节内容中识别单个角色信息，或识别全部角色列表"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    character_name = data.get('character_name', '')  # 指定角色名，为空则识别全部角色列表

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not chapters:
        return jsonify({'error': '作品没有章节内容，无法分析'}), 400

    full_text = ''
    max_chars = 12000
    for ch in chapters:
        segment = f'【{ch.title}】\n{(ch.content or "")[:2000]}\n\n'
        if len(full_text) + len(segment) > max_chars:
            remaining = max_chars - len(full_text)
            if remaining > 200:
                full_text += segment[:remaining]
            break
        full_text += segment

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    existing_chars = []
    try:
        parsed = json.loads(bb.character_profiles or '[]')
        if isinstance(parsed, list):
            existing_chars = parsed
    except:
        pass

    if character_name:
        # 识别指定角色的详细信息
        system_prompt = f"""你是专业的小说分析师。请从以下小说内容中，提取角色“{character_name}”的详细档案。
严格按JSON格式输出，不要任何其他文字：
{{
  "name": "{character_name}",
  "role": "主角/配角/反派/路人 等角色定位",
  "identity": "身份职业",
  "personality": "性格特征（2-3句）",
  "motivation": "核心动机和目标",
  "background": "背景故事（2-3句）",
  "relationships": "与其他角色的关系（如：与XX是师徒，与XX是敌对）",
  "abilities": "拥有的能力/功法/特长",
  "items": "持有的重要物品/装备"
}}"""
    else:
        # 识别全部角色列表
        existing_names = [c.get('name', '') for c in existing_chars if isinstance(c, dict)]
        existing_note = f'\n已有角色：{", ".join(existing_names)}' if existing_names else ''
        system_prompt = f"""你是专业的小说分析师。请从以下小说内容中，识别所有重要角色（出现3次以上或有台词的角色）。
{existing_note}
严格按JSON数组格式输出，不要任何其他文字：
[{{"name": "角色名", "role": "主角/配角/反派"}}]

注意：只输出数组，不要其他文字。"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers=build_auth_headers(api_key),
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'作品标题：{book.title}\n\n以下是作品内容：\n\n{full_text}'}
                ],
                'temperature': 0.3,
                'max_tokens': 2000,
                'response_format': {'type': 'json_object' if character_name else 'json_object'}
            },
            timeout=120)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        analysis = json.loads(content)

        if character_name:
            # 更新或添加单个角色
            char_data = analysis if isinstance(analysis, dict) else {}
            char_data['name'] = character_name
            found = False
            for i, c in enumerate(existing_chars):
                if isinstance(c, dict) and c.get('name') == character_name:
                    existing_chars[i] = {**c, **char_data}
                    found = True
                    break
            if not found:
                existing_chars.append(char_data)
            bb.character_profiles = json.dumps(existing_chars, ensure_ascii=False, indent=2)
        else:
            # 合并角色列表
            new_chars = analysis if isinstance(analysis, list) else (analysis.get('characters', []) if isinstance(analysis, dict) else [])
            existing_names = {c.get('name', '') for c in existing_chars if isinstance(c, dict)}
            for nc in new_chars:
                if isinstance(nc, dict) and nc.get('name', '') not in existing_names:
                    existing_chars.append(nc)
            bb.character_profiles = json.dumps(existing_chars, ensure_ascii=False, indent=2)

        bb.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({
            'success': True,
            'character': char_data if character_name else None,
            'characters': existing_chars,
            'bible': bb.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-plot-volume', methods=['POST'])
@login_required
def ai_analyze_plot_volume(book_id):
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    """AI识别指定卷的剧情大纲。
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    优化版：数据源从单一章节内容扩展为 设定+大纲+人物+规则+章节+动态文件，
    输出增加 nodes 情节节点字段，与 ai_outline_volume 输出结构一致。
    这是相互提供资料数据的过程：识别结果会回流到 timeline 供其他维度使用。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # ===== 1. 收集该卷章节内容 =====
    all_chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    volume_chapters = []
    if volume_id:
        # 优先：parent_id 关联（标准结构）
        volume_chapters = [c for c in all_chapters if not c.is_volume and c.parent_id == volume_id]
        # 回退：顺序遍历法（兼容卷紧挨子章节之前的旧数据）
        if not volume_chapters:
            collecting = False
            for ch in all_chapters:
                if ch.id == volume_id:
                    collecting = True
                    continue
                if collecting:
                    if ch.is_volume:
                        break
                    volume_chapters.append(ch)
    else:
        volume_chapters = [c for c in all_chapters if not c.is_volume]

    # 章节内容组装（优先用 summary，其次前 800 字 + 末 200 字）
    chapter_text = ''
    max_chars = 12000
    for ch in volume_chapters:
        ch_content = (ch.content or '')
        # 优先用章节摘要
        if getattr(ch, 'summary', None) and ch.summary:
            segment = f'【{ch.title}】{ch.summary[:500]}\n'
        elif len(ch_content) > 1000:
            segment = f'【{ch.title}】{ch_content[:800]}…{ch_content[-200:]}\n'
        else:
            segment = f'【{ch.title}】{ch_content}\n'
        if len(chapter_text) + len(segment) > max_chars:
            remaining = max_chars - len(chapter_text)
            if remaining > 200:
                chapter_text += segment[:remaining]
            break
        chapter_text += segment

    # ===== 2. 收集多维度上下文（相互提供资料数据） =====
    ctx_parts = []
    if bb:
        # 大纲维度（五幕式总纲）
        if bb.plot_design:
            ctx_parts.append(f'【五幕式总纲（本卷应在此弧线内）】\n{bb.plot_design[:2000]}')
        # 设定维度（世界观+规则）
        if bb.worldbuilding:
            ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:1000]}')
        if bb.key_rules:
            ctx_parts.append(f'【核心规则（金手指/能力限制，识别时不可违反）】\n{bb.key_rules[:800]}')
        # 人物及关系维度
        if bb.character_profiles:
            ctx_parts.append(f'【人物档案】\n{bb.character_profiles[:1000]}')
        # 已有该卷剧情（若有，作为参考而非覆盖）
        if bb.timeline:
            try:
                existing_vols = json.loads(bb.timeline)
                if isinstance(existing_vols, list):
                    # 找到该卷的已有数据
                    for ev in existing_vols:
                        ev_vid = str(ev.get('volume_id', ''))
                        ev_vol = str(ev.get('volume', ''))
                        if (volume_id and ev_vid == str(volume_id)) or (volume_title and ev_vol == volume_title):
                            ctx_parts.append(f'【该卷已有剧情（参考，可补充完善）】\n{json.dumps(ev, ensure_ascii=False)[:600]}')
                            break
            except (json.JSONDecodeError, ValueError):
                pass

    # ===== 3. 从动态文件补充数据 =====
    dyn_memories = DynamicMemory.query.filter_by(book_id=book_id).all()
    for dm in dyn_memories:
        if dm.category in ('narrative_engine', 'plot_progress', 'timeline') and dm.content:
            ctx_parts.append(f'【动态文件-{dm.category}】\n{dm.content[:800]}')
            break  # 只取一份，避免过多

    extra_ctx = '\n\n'.join(ctx_parts[-5:])  # 最多 5 块上下文，避免超长

    # 技能包提示
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'chapter_plan', 'tomato_outline'], mode='agent')

    if not volume_chapters and not extra_ctx:
        return jsonify({'error': '该卷没有章节内容，也没有可参考的设定'}), 400

    vol_label = volume_title or '全部章节'

    system_prompt = f"""你是番茄小说金番作者级别的剧情分析师。请综合【设定/大纲/人物/规则/章节内容/动态文件】多维度数据，识别“{vol_label}”的剧情大纲和情节节点。

【多维度上下文（相互提供资料数据）】
{extra_ctx or '（暂无设定参考，仅依据章节内容识别）'}

【识别要求】
1. 识别出的剧情必须与【五幕式总纲】中该卷的弧线一致，若有偏差在 main_plot 中标注
2. 识别人物互动时参考【人物档案】，确保角色名字和行为准确
3. 识别金手指/能力使用时参考【核心规则】，违反规则的标注为"待修正"
4. 结合【动态文件】中的叙事记录，补充章节内容未体现的关键事件和伏笔

严格按JSON格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "main_plot": "该卷主线剧情概述（100-200字，标注与总纲的偏差）",
  "core_conflict": "核心冲突",
  "emotion_driver": "情感驱动",
  "key_events": ["关键事件1", "关键事件2", "关键事件3"],
  "turning_points": ["转折点1", "转折点2"],
  "climax": "高潮场景描述",
  "ending": "该卷结尾状态/钩子",
  "foreshadowing": ["埋设的伏笔"],
  "nodes": [
    {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "爽点类型"}}
  ]
}}

【情节节点识别要求】
- 每卷识别 5-8 个情节节点
- 章型：M主线/C角色/W世界观/D日常/F伏笔
- 章型配额参考：M主线50%/C角色10%/W世界观10%/D日常20%/F伏笔10%
- 节点章节范围不重叠，覆盖整卷
- 小故事闭环：新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子（5-8章）

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n\n以下是该卷章节内容：\n\n{chapter_text or "（无章节内容，请根据设定推断）"}'

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'

        # 构建请求体（response_format 某些LLM不支持，捕获后重试）
        req_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': 0.3,
            'max_tokens': 4000,
            'response_format': {'type': 'json_object'}
        }

        resp = requests.post(f'{base}/chat/completions',
            headers=build_auth_headers(api_key),
            json=req_body,
            timeout=90)

        # 若返回400且与response_format相关，去掉该参数重试
        if resp.status_code == 400:
            err_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
            err_msg = str(err_body.get('error', '')).lower()
            if 'response_format' in err_msg or 'json_object' in err_msg or 'unrecognized' in err_msg:
                req_body.pop('response_format', None)
                resp = requests.post(f'{base}/chat/completions',
                    headers=build_auth_headers(api_key),
                    json=req_body,
                    timeout=90)

        if resp.status_code != 200:
            try:
                err_detail = resp.json().get('error', {}).get('message', '') or resp.text[:300]
            except Exception:
                err_detail = resp.text[:300]
            return jsonify({'error': f'AI调用失败({resp.status_code}): {err_detail}'}), 500

        result = resp.json()
        if 'choices' not in result or not result['choices']:
            return jsonify({'error': f'AI返回异常: {str(result)[:300]}'}), 500
        content = result['choices'][0]['message']['content']

        # JSON 解析容错：使用健壮解析函数提取对象
        analysis, parse_err = _extract_json_from_llm(content, expect='object')
        if analysis is None:
            return jsonify({'error': f'AI返回非JSON格式: {content[:200]}', 'parse_error': parse_err}), 500

        # 存储到 timeline 字段（深度合并：保留人工编辑字段，更新 AI 识别字段）
        if not bb:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)

        volumes_data = []
        try:
            parsed = json.loads(bb.timeline or '[]')
            if isinstance(parsed, list):
                volumes_data = parsed
        except:
            pass

        # 补全 volume_id 和 volume
        vol_data = analysis
        if volume_id:
            vol_data['volume_id'] = volume_id
        vol_data['volume'] = vol_label
        # 补全 volume_index
        if 'volume_index' not in vol_data:
            vol_data['volume_index'] = _extract_volume_index(vol_label) or (len(volumes_data) + 1)

        # 深度合并：找到已有卷，保留人工编辑的 nodes（如果新数据没有 nodes），其他字段用新数据
        found_idx = -1
        for i, v in enumerate(volumes_data):
            if not isinstance(v, dict):
                continue
            ev_vid = str(v.get('volume_id', ''))
            ev_vol = str(v.get('volume', ''))
            if (volume_id and ev_vid == str(volume_id)) or (volume_title and ev_vol == volume_title):
                found_idx = i
                break
        if found_idx >= 0:
            existing = volumes_data[found_idx]
            # 保留人工编辑的 nodes（新数据 nodes 为空或缺失时）
            if not vol_data.get('nodes') and existing.get('nodes'):
                vol_data['nodes'] = existing['nodes']
            # 保留人工编辑的字段（raw_text 等）
            for k in ('raw_text',):
                if k in existing and k not in vol_data:
                    vol_data[k] = existing[k]
            volumes_data[found_idx] = {**existing, **vol_data}
        else:
            volumes_data.append(vol_data)

        # 按 volume_index 排序
        volumes_data.sort(key=lambda v: int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0))

        bb.timeline = json.dumps(volumes_data, ensure_ascii=False, indent=2)
        bb.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()

        return jsonify({
            'success': True,
            'volume_data': vol_data,
            'volumes': volumes_data,
            'bible': bb.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-character-volume', methods=['POST'])
@login_required
def ai_analyze_character_volume(book_id):
    """AI识别指定卷的人物档案。按卷分析章节内容，识别人物并写入 character_volumes。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别人物。请先填写设定、大纲或剧情维度。'}), 400

    # 上下文：全局人物档案 + 设定 + 该卷剧情
    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【全局人物档案（参考，避免重复识别）】\n{bb.character_profiles[:1000]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:600]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:600]}')
    # 该卷剧情
    if bb.timeline:
        try:
            vols = json.loads(bb.timeline)
            if isinstance(vols, list):
                for v in vols:
                    if isinstance(v, dict) and (str(v.get('volume_id', '')) == str(volume_id) or v.get('volume') == volume_title):
                        ctx_parts.append(f'【该卷剧情（参考）】\n{(v.get("main_plot") or "")[:500]}')
                        break
        except (json.JSONDecodeError, ValueError):
            pass
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['character_cognition', 'tomato_character'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说分析师。请从以下“{vol_label}”的章节内容中，识别本卷出现的所有重要角色（出现2次以上或有台词的角色）。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "characters": [
    {{
      "name": "角色名",
      "role": "主角/配角/反派/路人",
      "identity": "身份职业",
      "personality": "性格特征（1-2句）",
      "motivation": "本卷中的动机",
      "relationships": "本卷中与其他角色的关系",
      "abilities": "本卷中使用的能力/功法",
      "items": "本卷中持有的重要物品",
      "arc": "本卷中的角色弧线/变化"
    }}
  ]
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3000, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_cv
        m = _re_cv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    chars = analysis.get('characters', []) if isinstance(analysis, dict) else []

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'characters': chars,
    }
    data_list = _upsert_volume_entry(bb, 'character_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'character_volumes': data_list,
        'bible': bb.to_dict()
    })

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-inventory-volume', methods=['POST'])
@login_required
def ai_analyze_inventory_volume(book_id):
    """AI识别指定卷的物资库。按卷分析章节内容，识别势力/角色拥有的物品、功法、法宝、境界等。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别物资。请先填写设定、大纲或剧情维度。'}), 400

    # 上下文
    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【人物档案（识别持有者）】\n{bb.character_profiles[:1000]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则（能力体系/境界划分）】\n{bb.key_rules[:800]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定（势力格局）】\n{bb.worldbuilding[:600]}')
    if bb.timeline:
        try:
            vols = json.loads(bb.timeline)
            if isinstance(vols, list):
                for v in vols:
                    if isinstance(v, dict) and (str(v.get('volume_id', '')) == str(volume_id) or v.get('volume') == volume_title):
                        ctx_parts.append(f'【该卷剧情（参考）】\n{(v.get("main_plot") or "")[:500]}')
                        break
        except (json.JSONDecodeError, ValueError):
            pass
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'tomato_setting'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说世界观分析师。请从以下“{vol_label}”的章节内容中，识别本卷出现的所有势力及角色拥有的物资。
物资类型包括：物品、功法、法宝、境界、灵宠、领地、资源等。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "items": [
    {{
      "owner": "持有者（角色名/势力名）",
      "owner_type": "角色/势力",
      "name": "物资名称",
      "category": "物品/功法/法宝/境界/灵宠/领地/资源/其他",
      "description": "描述（来源、能力、效果）",
      "status": "获得/持有/失去/消耗",
      "chapter": "首次出现章节"
    }}
  ],
  "realms": [
    {{
      "character": "角色名",
      "realm": "当前境界",
      "progress": "修炼进度/突破节点"
    }}
  ]
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3000, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_iv
        m = _re_iv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    items = analysis.get('items', []) if isinstance(analysis, dict) else []
    realms = analysis.get('realms', []) if isinstance(analysis, dict) else []

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'items': items,
        'realms': realms,
    }
    data_list = _upsert_volume_entry(bb, 'inventory', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'inventory': data_list,
        'bible': bb.to_dict()
    })

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-dynamic-volume', methods=['POST'])
@login_required
def ai_analyze_dynamic_volume(book_id):
    """AI识别指定卷的动态文件分类。按卷汇总章节内容，生成该卷的动态摘要（人物/事件/时间/地点/势力/伏笔/境界/关系）。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别动态文件。请先填写设定、大纲或剧情维度。'}), 400

    # 收集该卷区间内的已有动态报告（5章一份）
    dyn_reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
    relevant_reports = []
    # 计算该卷的起止章号（与 _collect_volume_chapters 一致：parent_id 优先，回退顺序遍历）
    all_chs = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    if volume_id:
        vol_chs_in_order = _get_volume_chapters_ordered(book_id, volume_id)
        # 计算该卷章节在全章节序列中的序号（1-based）
        non_vol_chs = [c for c in all_chs if not c.is_volume]
        vol_ch_idx = [non_vol_chs.index(c) for c in vol_chs_in_order if c in non_vol_chs]
    else:
        vol_ch_idx = list(range(len([c for c in all_chs if not c.is_volume])))

    if vol_ch_idx:
        # 章节序号从1开始
        ch_start = vol_ch_idx[0] + 1
        ch_end = vol_ch_idx[-1] + 1
        for r in dyn_reports:
            if r.chapter_end >= ch_start and r.chapter_start <= ch_end:
                relevant_reports.append(r)

    reports_text = '\n\n'.join([f'【{r.title}】\n{(r.content or "")[:500]}' for r in relevant_reports]) if relevant_reports else '（无已生成报告）'

    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【人物档案】\n{bb.character_profiles[:600]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:600]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'narrative_debt', 'foreshadow_register'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说防遗忘系统分析师。请从以下“{vol_label}”的章节内容及已有动态报告中，生成本卷的动态分类摘要。

【已有动态报告（5章一份）】
{reports_text}

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "characters": "本卷登场人物及状态变化（200字内）",
  "events": "本卷关键事件脉络（200字内）",
  "timeline": "本卷时间线要点（150字内）",
  "locations": "本卷涉及地点（100字内）",
  "factions": "本卷势力动态（100字内）",
  "foreshadowing": "本卷埋设/回收的伏笔（150字内）",
  "realms": "本卷境界/能力变化（100字内）",
  "relationships": "本卷人物关系变化（100字内）",
  "summary": "本卷综合动态摘要（300字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无章节内容，依据报告生成）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    analysis, parse_err = _extract_json_from_llm(content, expect='object')
    if analysis is None:
        return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300], 'parse_error': parse_err}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'dynamic_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'dynamic_volumes': data_list,
        'bible': bb.to_dict()
    })

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-foreshadowing-volume', methods=['POST'])
@login_required
def ai_analyze_foreshadowing_volume(book_id):
    """AI识别指定卷的伏笔。按卷分析章节内容，识别本卷埋设/回收的伏笔并写入 foreshadowing_volumes。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别伏笔。请先填写设定、大纲或剧情维度。'}), 400

    ctx_parts = []
    if bb.foreshadowing:
        ctx_parts.append(f'【全局伏笔档案（参考，避免重复）】\n{bb.foreshadowing[:800]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:400]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['foreshadow_register', 'narrative_debt'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说伏笔分析师。请从以下“{vol_label}”的章节内容中，识别本卷埋设的伏笔、回收的伏笔、以及尚未回收的悬念。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "planted": [
    {{"content": "伏笔内容", "chapter": "埋设章节/位置", "purpose": "埋设目的", "status": "待回收"}}
  ],
  "resolved": [
    {{"content": "伏笔内容", "planted_at": "埋设位置", "resolved_at": "回收位置", "effect": "回收效果"}}
  ],
  "pending": [
    {{"content": "未回收悬念", "planted_at": "埋设位置", "importance": "高/中/低"}}
  ],
  "summary": "本卷伏笔综述（150字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_fv
        m = _re_fv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'foreshadowing_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'foreshadowing_volumes': data_list,
        'bible': bb.to_dict()
    })

@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-locations-volume', methods=['POST'])
@login_required
def ai_analyze_locations_volume(book_id):
    """AI识别指定卷的地点/地图。按卷分析章节内容，识别本卷涉及的地点并写入 locations_volumes。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别地点。请先填写设定、大纲或剧情维度。'}), 400

    ctx_parts = []
    if bb.locations:
        ctx_parts.append(f'【全局地点档案（参考）】\n{bb.locations[:800]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:500]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    # P2-10: 'world_setting' 是幽灵key，替换为 'tomato_setting'
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'tomato_setting'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说地图分析师。请从以下“{vol_label}”的章节内容中，识别本卷涉及的所有地点、场景、地理信息。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "locations": [
    {{"name": "地点名", "type": "城市/山脉/秘境/建筑/其它", "description": "地点描述", "events": "该地点发生的重要事件", "importance": "高/中/低"}}
  ],
  "regions": [
    {{"name": "区域名", "scope": "范围描述", "feature": "区域特征"}}
  ],
  "summary": "本卷地理概况（150字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_lv
        m = _re_lv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'locations_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'locations_volumes': data_list,
        'bible': bb.to_dict()
    })

@ai_analyze_bp.route('/api/books/<book_id>/clear-timeline', methods=['POST'])
@login_required
def clear_timeline(book_id):
    """一键清空剧情分卷大纲（timeline 字段），不影响章节表。"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
    bb.timeline = ''
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'success': True, 'bible': bb.to_dict()})
@ai_analyze_bp.route('/api/books/<book_id>/ai-analyze-from-reports', methods=['POST'])
@login_required
def ai_analyze_from_reports(book_id):
    """从动态文件报告提取维度信息（地图/关系图谱/地点图谱/境界图谱等），节省token"""
    from app import (db, Book, BookBible, Chapter, DynamicMemory, DynamicReport,
                 AIConfig, _call_llm, _extract_json_from_llm, _extract_volume_index,
                 _get_skill_prompts_by_category, _get_volume_chapters_ordered,
                 _collect_volume_chapters, _collect_dimension_source,
                 _get_volume_list, _upsert_volume_entry)  # 请求期延迟导入，避免循环依赖
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    dimension = data.get('dimension', '')
    if not dimension:
        return jsonify({'error': '缺少 dimension 参数'}), 400

    # 维度 → bible字段 映射
    dim_field_map = {
        'locations': 'locations',
        'relationGraph': 'relation_graph',
        'locationGraph': 'locations',
        'realmGraph': 'worldbuilding',
    }
    field = dim_field_map.get(dimension)
    if not field:
        return jsonify({'error': f'不支持的维度: {dimension}'}), 400

    dim_labels = {
        'locations': '地点/地图',
        'relationGraph': '人物关系',
        'locationGraph': '地点关系',
        'realmGraph': '境界体系',
    }
    dim_label = dim_labels.get(dimension, dimension)

    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 获取已有的bible字段内容
    bible = BookBible.query.filter_by(book_id=book_id).first()
    existing_value = getattr(bible, field, '') if bible else ''

    # 按维度组装数据源（用户要求：不同图谱从不同维度读取数据供AI识别）
    # 1. 关系图谱：从“人物及关系”+“剧情”维度读取，再补充动态文件
    # 2. 地点图谱：首先从“设定”+“大纲”维度读取，再从动态文件补充
    # 3. 境界图谱：首先从“设定”+“大纲”维度读取，再从动态文件补充
    # 4. 地图(locations)：保持动态文件优先，回退章节内容

    def _bible_val(attr):
        return getattr(bible, attr, '') if bible else ''

    # 动态文件报告（补充数据源）
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start
    ).all()
    dynamic_text = '\n\n'.join([f'【{r.title}】\n{r.content}' for r in reports if r.content]) if reports else ''

    # 章节内容（最终回退）
    chapter_text = ''
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if chapters:
        max_chars = 8000
        for ch in chapters:
            segment = f'【{ch.title}】\n{(ch.content or "")[:800]}\n\n'
            if len(chapter_text) + len(segment) > max_chars:
                break
            chapter_text += segment

    source_parts = []  # [(标签, 内容)]
    if dimension == 'relationGraph':
        # 关系图谱：人物及关系 + 剧情 + 动态文件
        cp = _bible_val('character_profiles')
        tl = _bible_val('timeline')
        if cp.strip():
            source_parts.append(('人物及关系维度', cp[:3000]))
        if tl.strip():
            source_parts.append(('剧情维度', tl[:3000]))
        if dynamic_text.strip():
            source_parts.append(('动态文件补充', dynamic_text[:3000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容补充', chapter_text[:2000]))
    elif dimension in ('locationGraph', 'realmGraph'):
        # 地点图谱/境界图谱：设定 + 大纲 + 动态文件
        wb = _bible_val('worldbuilding')
        kr = _bible_val('key_rules')
        pd = _bible_val('plot_design')
        if wb.strip():
            source_parts.append(('设定维度(世界观)', wb[:3000]))
        if kr.strip():
            source_parts.append(('设定维度(核心规则)', kr[:2000]))
        if pd.strip():
            source_parts.append(('大纲维度', pd[:3000]))
        if dynamic_text.strip():
            source_parts.append(('动态文件补充', dynamic_text[:3000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容补充', chapter_text[:2000]))
    else:
        # locations 及其他：动态文件优先，回退章节内容
        if dynamic_text.strip():
            source_parts.append(('动态文件报告', dynamic_text[:8000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容', chapter_text[:8000]))
        else:
            # 没有任何数据源时，尝试从设定/大纲补充
            for attr, label in [('worldbuilding', '世界观设定'), ('key_rules', '核心规则'), ('plot_design', '大纲'), ('character_profiles', '人物及关系'), ('timeline', '剧情')]:
                v = _bible_val(attr)
                if v.strip():
                    source_parts.append((label, v[:3000]))

    if not source_parts:
        return jsonify({'error': '没有可用的数据源，请先在相关维度填写内容或生成动态文件'}), 400

    source_type = '、'.join([p[0] for p in source_parts])
    source_text = '\n\n'.join([f'--- {label} ---\n{content}' for label, content in source_parts])

    # 不同维度的提取提示
    dim_prompts = {
        'locations': """请从以下内容中提取所有地点信息，按三级分类整理（大区域/城市/场景）。
输出JSON格式：
{"locations": [{"name":"大区域名","desc":"描述","children":[{"name":"城市名","desc":"描述","children":[{"name":"场景名","desc":"描述"}]}]}]}
如果没有明确地点信息，输出空数组。""",

        'relationGraph': """请从以下内容中提取所有人物及其关系，整理为关系图谱数据。
重要规则：
- 只提取真实的人物姓名作为节点，绝对不要把"关系"、"好友"、"敌人"、"师徒"等关系类型词当作人物节点。
- 每个人物用姓名开头，关系单独列为"关系: A与B-关系类型"。

输出JSON格式：
{"relation_graph": "人物1: 姓名|身份|性格|动机\\n人物2: 姓名|身份|性格|动机\\n关系: A与B-关系类型"}
如果没有人物信息，输出空字符串。""",

        'locationGraph': """请从以下内容中提取地点之间的关联关系。
输出JSON格式：
{"locations": [{"name":"大区域名","desc":"描述","children":[{"name":"城市名","desc":"描述","children":[{"name":"场景名","desc":"描述"}]}]}]}
按层级整理地点体系。""",

        'realmGraph': """请从以下内容中提取境界/等级/实力体系信息。
输出JSON格式：
{"worldbuilding": "境界体系:\\n第一级: xxx\\n第二级: xxx\\n...\\n能力规则: xxx"}
如果没有境界信息，输出空字符串。""",

        'worldbuilding': """请从以下内容中提取世界观设定，包括境界体系、力量规则、社会结构等。
输出纯文本格式，200-400字。""",
        'character_profiles': """请从以下内容中提取人物档案和关系。
输出纯文本格式，每人一行：姓名|身份|性格|动机|关系。""",
    }

    prompt = dim_prompts.get(dimension, dim_prompts.get(field, f'提取{dim_label}信息'))

    system_prompt = f"""你是专业的小说分析师。请从以下{source_type}中提取“{dim_label}”维度的信息。

已有内容（供参考，在基础上补充而非完全重写）：
{existing_value[:500] if existing_value else '（空）'}

{prompt}"""

    user_content = f"""作品：{book.title}

{source_type}：
{source_text}

请提取{dim_label}信息："""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'

        use_json = dimension in ('locations', 'locationGraph')
        req_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content}
            ],
            'temperature': 0.3,
            'max_tokens': 2000,
        }
        if use_json:
            req_body['response_format'] = {'type': 'json_object'}

        resp = requests.post(f'{base}/chat/completions',
            headers=build_auth_headers(api_key),
            json=req_body,
            timeout=120)

        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()

        # 尝试解析JSON提取字段值
        extracted_value = content
        try:
            parsed = json.loads(content)
            if field in parsed:
                extracted_value = parsed[field] if isinstance(parsed[field], str) else json.dumps(parsed[field], ensure_ascii=False, indent=2)
            elif isinstance(parsed, dict) and len(parsed) == 1:
                # 单字段JSON
                val = list(parsed.values())[0]
                extracted_value = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, KeyError):
            pass  # 保留原始文本

        # 保存到bible
        if not bible:
            bible = BookBible(book_id=book_id)
            db.session.add(bible)
        setattr(bible, field, extracted_value)
        db.session.commit()

        return jsonify({
            'success': True,
            'dimension': dimension,
            'field': field,
            'value': extracted_value,
            'bible': bible.to_dict(),
            'source': source_type,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

