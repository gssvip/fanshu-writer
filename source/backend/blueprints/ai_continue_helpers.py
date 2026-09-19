"""【正文滚动创作域·纯辅助函数】（自 blueprints/ai_continue_bp.py 拆出，单文件行数门禁）。

包含正文滚动创作域的 11 个纯辅助函数：章节计划 / 一致性校验 / 指纹依赖 /
上下文构建 / SSE 心跳 / 去AI规则块 / 章节标题解析与评分等。这些函数体引用
app.py 命名空间中的模型/helper（db、Book、Chapter、PromptContextCache、
build_auth_headers、app.logger …），采用与 ai_continue_bp 相同的
init(app_module=…) 命名空间注入机制，避免逐个延迟导入与循环依赖。
"""
from __future__ import annotations


def _inject_from(app_module):
    """把 app_module 的全局命名空间（非 dunder）注入本模块 globals()。"""
    for _n, _v in vars(app_module).items():
        if not _n.startswith('__'):
            globals()[_n] = _v


def init(app_module=None, **deps):
    """由 ai_continue_bp.init() 级联调用：注入 app.py 全局命名空间，供函数体引用解析。"""
    if app_module is not None:
        _inject_from(app_module)
    if deps:
        globals().update(deps)


# ==== 纯辅助函数（自 app.py 原样迁出，见 blueprints/ai_continue_bp.py 历史） ====

def _generate_chapter_plan(book_id, bb, current_chapter_num, vol_chapter, vol_index,
                           memory_section, foreshadowing_section, skill_pack_ids,
                           api_key, base_url, model, max_tokens=600):
    """章节计划前置（chapter_plan Agent）：在写正文前生成 200 字以内的本章三段式计划。
    【P0弊端5修复】注入当前卷纲的 nodes 列表，让章纲对齐卷纲节点节奏。
    返回计划文本，失败时返回空串（不阻塞正文生成）。"""
    # 【三类无污染】chapter_plan 前置规划属于构思阶段：只注入构思类（master）技能包
    plan_skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['chapter_plan'], mode='single')
    vol_label = f'第{vol_index}卷“{vol_chapter.title}”' if vol_chapter else '当前卷'

    # ===== 【P0弊端5修复】提取当前卷纲 nodes，定位本章对应的节点 =====
    volume_nodes_section = ''
    current_node_hint = ''
    if vol_chapter:
        try:
            if bb.timeline and bb.timeline.strip().startswith('['):
                arr = json.loads(bb.timeline)
                if isinstance(arr, list):
                    for v in arr:
                        if not isinstance(v, dict):
                            continue
                        v_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', '')))
                        if str(v_idx) == str(vol_index):
                            nodes = v.get('nodes') or []
                            if isinstance(nodes, list) and nodes:
                                # 列出本卷所有节点
                                nodes_lines = []
                                for n in nodes:
                                    if not isinstance(n, dict):
                                        continue
                                    n_title = n.get('title', '')
                                    n_chapters = str(n.get('chapters', ''))
                                    n_type = n.get('type', 'M')
                                    n_summary = n.get('summary', '')
                                    nodes_lines.append(f'  · [{n_type}] {n_chapters}章 {n_title}：{n_summary}')
                                if nodes_lines:
                                    volume_nodes_section = f"""【本卷情节节点】（章纲必须对齐到本章所属节点）
{chr(10).join(nodes_lines)}"""
                                    # 定位本章对应的节点
                                    for n in nodes:
                                        if not isinstance(n, dict):
                                            continue
                                        ch_range = str(n.get('chapters', ''))
                                        nums = re.findall(r'\d+', ch_range)
                                        if len(nums) >= 2:
                                            start_n, end_n = int(nums[0]), int(nums[-1])
                                            if start_n <= current_chapter_num <= end_n:
                                                current_node_hint = f"""【本章所属节点】第{current_chapter_num}章对应节点“{n.get('title','')}”（{ch_range}章，类型{n.get('type','M')}）：{n.get('summary','')}
章纲必须围绕此节点的核心事件展开，不得偏离到其他节点。"""
                                                break
                                        elif len(nums) == 1:
                                            if int(nums[0]) == current_chapter_num:
                                                current_node_hint = f"""【本章所属节点】第{current_chapter_num}章对应节点“{n.get('title','')}”（类型{n.get('type','M')}）：{n.get('summary','')}"""
                                                break
                            break
                # 也提取上一卷卷尾钩子作为衔接提示
                if vol_index > 1:
                    for v in arr:
                        if not isinstance(v, dict):
                            continue
                        v_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', '')))
                        if str(v_idx) == str(vol_index - 1):
                            prev_hook = v.get('ending_hook') or v.get('ending') or v.get('climax') or ''
                            if prev_hook:
                                current_node_hint = f'【上一卷卷尾钩子】（本卷开头需承接）：{prev_hook}\n' + current_node_hint
                            break
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    plan_system = f"""你是小说章节策划师。为第 {current_chapter_num} 章生成一份简洁的章节计划（200字以内）。

当前所在卷：{vol_label}

{memory_section[:1500]}

{foreshadowing_section[:800] if foreshadowing_section else ''}

{volume_nodes_section}

{current_node_hint}

{plan_skill_note}

【输出格式】严格按以下五段式输出，每段不超过70字：
1. 本章核心冲突（只许1个，写清楚谁与谁、因什么事对立）：
2. 主角本章目标与主动行为（主角想要什么、主动做成的1-2件事——不许全程被动挨打）：
3. 冲突闭环方式（本章内解决，或显式挂起说明未完待续——禁止冲突凭空消失无下文）：
4. 信息增量点（本章让读者新知道的事：设定/关系/危机，至少1条）：
5. 章尾钩子设计：

【要求】
- 必须承接前文，不可矛盾
- 若有"待回收伏笔清单"，本章应考虑回收其中1条
- 【节点对齐铁律】若存在【本章所属节点】，章纲必须围绕该节点核心事件展开，不得偏离
- 【卷间衔接】若为卷首章节，必须承接上一卷卷尾钩子
- 【事件限额】本章关键事件≤3个且必须因果衔接（下一事件由上一事件引起，禁止并列堆砌无关事件）
- 只输出计划，不要解释"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers=build_auth_headers(api_key),
            json={'model': model, 'messages': [{'role': 'system', 'content': plan_system},
                                                {'role': 'user', 'content': f'请为第 {current_chapter_num} 章生成计划'}],
                  'temperature': 0.5, 'max_tokens': max_tokens},
            timeout=60)
        result = resp.json()
        plan = result['choices'][0]['message']['content'].strip()
        if plan and len(plan) > 20:
            return plan
    except Exception:
        pass
    return ''

def _consistency_check(book_id, bb, draft_content, current_chapter_num,
                       api_key, base_url, model, max_tokens=1200, chapter_plan=''):
    """一致性检查 Agent：检查正文是否违反 key_rules/人设，并比对 chapter_plan 是否被执行。
    【P1扩展】新增 chapter_plan 比对，检测剧情偏离。
    【防遗忘回注】注入最近防遗忘检查诊断，使章后检查能对照历史诊断结果，避免重复犯错。
    【P1补全】检查覆盖面扩展到全维度：worldbuilding/timeline/foreshadowing/inventory/locations，
    按 bible 权威分级分层注入，避免地点矛盾/物资凭空获得/世界观违规等漏检。
    返回 (passed, issues_text)。失败时返回 (True, '')（不阻塞）。"""
    if not draft_content or len(draft_content) < 100:
        return True, ''
    key_rules = (bb.key_rules or '')[:800]
    chars = (bb.character_profiles or '')[:800]
    worldbuilding = (bb.worldbuilding or '')[:600]
    timeline = (bb.timeline or '')[:600]
    foreshadowing = (bb.foreshadowing or '')[:500]
    inventory = (bb.inventory or '')[:500]
    locations = (bb.locations or '')[:500]
    af_alerts = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
    if not any([key_rules, chars, worldbuilding, timeline, foreshadowing, inventory, locations,
                chapter_plan, af_alerts]):
        return True, ''

    # 构建 chapter_plan 比对段（若有）
    plan_check_section = ''
    if chapter_plan and chapter_plan.strip():
        plan_check_section = f"""

【本章计划】（正文必须严格遵循此计划）
{chapter_plan[:600]}

【计划执行检查】除一致性外，额外检查正文是否实现了本章计划的核心冲突、关键场景、章尾钩子。若正文明显偏离计划（如跳过关键场景、改写核心冲突、丢失章尾钩子），记为问题。"""

    # 防遗忘诊断对照段（若有）
    af_check_section = ''
    if af_alerts:
        af_check_section = f"""

【历史防遗忘诊断】（最近检查发现的问题，检查正文是否重犯了类似错误）
{af_alerts}"""

    # 各维度分段（按权威分级分层注入）
    dim_sections = []
    if key_rules:
        dim_sections.append(f'【rules 层·核心规则/金手指】（绝不可违反）\n{key_rules}')
    if worldbuilding:
        dim_sections.append(f'【foundation 层·世界观设定】（世界法则不可违反）\n{worldbuilding}')
    if chars:
        dim_sections.append(f'【人物档案】（人设/关系/能力边界）\n{chars}')
    if timeline:
        dim_sections.append(f'【剧情/时间线】（事件顺序/时间推进须一致）\n{timeline}')
    if foreshadowing:
        dim_sections.append(f'【rules 层·伏笔状态】（已回收伏笔不可当未回收，待回收伏笔不可遗忘）\n{foreshadowing}')
    if inventory:
        dim_sections.append(f'【物资库】（物品持有者/数量须一致，不可凭空获得/消失）\n{inventory}')
    if locations:
        dim_sections.append(f'【地图/地点】（地点状态/归属须一致）\n{locations}')
    dim_block = '\n\n'.join(dim_sections)

    check_system = f"""你是小说一致性审查员。检查以下章节正文是否违反"项目宪法"，并比对本章计划是否被执行。
按权威分级（rules > foundation > 人物 > 剧情 > 伏笔 > 物资 > 地点）逐层检查。
只检查，不修改。返回 JSON：{{"passed": true/false, "issues": ["问题1", "问题2"]}}

【OOC 专项检测】除常规一致性外，专项检查角色是否 OOC（Out of Character）：
1. 说话语气是否符合人设档案（如沉稳角色突然话多、冷酷角色突然唠叨、粗豪角色突然文绉绉）。
2. 行为模式是否符合人设（如谨慎角色突然鲁莽、贪婪角色突然大方、孤傲角色突然讨好）。
3. 决策逻辑是否符合人设（如重情角色突然背叛、狡诈角色突然坦诚、隐忍角色突然暴走）。
4. 情绪反应是否符合人设（角色是否拥有矛盾心理/纠结/自我怀疑，还是情绪顺滑地接受了事件）。
任一不符记为 OOC 问题，归入 issues。

【项目宪法·分层】
{dim_block}{plan_check_section}{af_check_section}

【待检查正文】
{draft_content[:3000]}"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers=build_auth_headers(api_key),
            json={'model': model, 'messages': [{'role': 'system', 'content': check_system},
                                                {'role': 'user', 'content': '请检查一致性与计划执行度，返回JSON'}],
                  'temperature': 0.2, 'max_tokens': max_tokens},
            timeout=60)
        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()
        # 使用健壮解析函数替代贪婪正则
        parsed, parse_err = _extract_json_from_llm(content, expect='object')
        if parsed is None:
            app.logger.error(f"一致性检查JSON解析失败: {parse_err}, 原始内容前500字: {content[:500]}")
            return False, "一致性检查解析失败，请人工复核"
        return bool(parsed.get('passed', True)), '; '.join(parsed.get('issues', []))
    except Exception as e:
        app.logger.error(f"一致性检查执行异常: {e}")
        return False, "一致性检查执行异常，请人工复核"

def _build_continue_fingerprint_deps(book_id, bb, instruction, skill_pack_ids, target_chapter_num,
                                    prev_chapter_content, chapter_lang_styles, enable_structured_tags,
                                    skip_chapter_plan, book=None, recent_4ch_ids=None, cache_bible_version=None):
    """正文创作阶段 指纹 Key（零脏读）：只取 DB 行级稳定标识+本批次参数，不做重查询，轻量。
    任一依赖变动 → Key 自动变 → 自动 MISS 重算。"""
    if bb is None:
        bb_fields_t = (0, '', '', '', '', '', '', '', '', 0)
    else:
        # 10 个 bible 可变维度的「len+sha前16」混合指纹：变内容=指纹变（比取完整2000字轻量，又足够敏感）
        def _fp(s):
            if not s:
                return '0'
            return str(len(s)) + ':' + hashlib.sha1(str(s).encode('utf-8')).hexdigest()[:14]
        bb_fields_t = (
            str(getattr(bb, 'id', '') or ''),
            _fp(getattr(bb, 'concept', '') or ''),
            _fp(getattr(bb, 'key_rules', '') or ''),
            _fp(getattr(bb, 'worldbuilding', '') or ''),
            _fp(getattr(bb, 'character_profiles', '') or ''),
            _fp(getattr(bb, 'plot_design', '') or ''),
            _fp(getattr(bb, 'timeline', '') or ''),
            _fp(getattr(bb, 'relation_graph', '') or ''),
            _fp(getattr(bb, 'inventory', '') or ''),
            _fp(getattr(bb, 'outline_hierarchy', '') or ''),
            _fp(getattr(bb, 'foreshadowing_graph', '') or ''),
            _fp(getattr(bb, 'generated_summary', '') or ''),
            _fp(getattr(bb, 'locations', '') or ''),
            _fp(getattr(bb, 'style_guide', '') or ''),
            int((getattr(bb, 'updated_at') or 0) and int(getattr(bb, 'updated_at', datetime(2020, 1, 1)).timestamp() * 1000 if hasattr(getattr(bb, 'updated_at', None), 'timestamp') else 0) or 0),
        )
    # book 行级稳定标识（genre/style 等用户可改字段也入指纹）
    if book is None:
        book_t = None
    else:
        book_t = (
            str(book.id or ''),
            str(getattr(book, 'genre', '') or ''),
            str(getattr(book, 'book_type', '') or ''),
            str(getattr(book, 'title', '') or ''),
            str(getattr(book, 'total_volumes', 0) or 0),
            str(getattr(book, 'chapters_per_volume', 0) or 0),
            str(getattr(book, 'master_skill_ids', '') or ''),
            str(getattr(book, 'style_skill_ids', '') or ''),
        )
    # recent_4ch_ids: 最近 4 章 id + word_count（正文每写完一章，下一章的 recent_4 滚动 → Key 自然变）
    recent_ch_t = tuple(
        (str(cid), int(wc or 0)) for (cid, wc) in (recent_4ch_ids or [])
    )
    # 批次参数
    params_t = (
        target_chapter_num,
        instruction and (str(len(instruction or '')) + ':' + hashlib.sha1((instruction or '').encode('utf-8')).hexdigest()[:12]),
        prev_chapter_content and (str(len(prev_chapter_content or '')) + ':' + hashlib.sha1((prev_chapter_content or '').encode('utf-8')).hexdigest()[:12]),
        tuple(sorted([str(x) for x in (skill_pack_ids or [])])),
        tuple(sorted([str(x) for x in (chapter_lang_styles or [])])),
        bool(enable_structured_tags),
        bool(skip_chapter_plan),
        cache_bible_version,  # 预留：外部全局版本号，暂时 None 不影响
    )
    return (book_t, bb_fields_t, recent_ch_t, params_t)


def _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num=None, prev_chapter_content=None, chapter_lang_styles=None, enable_structured_tags=True, skip_chapter_plan=False, skip_cache=False, *, _bypass_cache=False):
    """构建章节写作完整上下文（ai_continue / stream / batch 共用），返回含
    system_prompt/user_prompt/temperature/max_tokens/chapter_plan/api 信息。
    新增缓存机制：先命中 PromptContextCache（零脏读指纹 Key）→ 命中直接 return，
    未命中才执行下面 0–12 步所有逐维度资料读取拼 prompt 逻辑，执行完回填缓存。
    _bypass_cache 私有参数：用于 cache 命中 lambda 内递归进入真实执行逻辑，不对外暴露。"""
    # ============== CACHE FAST PATH（仅外层调用进入；内层递归 _bypass_cache=True 跳过）==============
    if not _bypass_cache:
        # 预取少量稳定标识用于指纹（绝不在这里跑 bible/章节全量读，保持快路径 <1ms）
        _cache_book = Book.query.get(book_id)  # 行级查询，<1ms；下面真执行还会再取一次但 SQLAlchemy session 缓存掉
        _cache_allch_tip = Chapter.query.filter_by(book_id=book_id, is_volume=False).with_entities(
            Chapter.id, Chapter.word_count
        ).order_by(Chapter.order_index.desc()).limit(4).all()
        _cache_recent4 = [(c[0], c[1] or 0) for c in _cache_allch_tip] if _cache_allch_tip else []
        deps = _build_continue_fingerprint_deps(
            book_id, bb, instruction, skill_pack_ids, target_chapter_num,
            prev_chapter_content, chapter_lang_styles, enable_structured_tags,
            skip_chapter_plan, book=_cache_book, recent_4ch_ids=_cache_recent4,
        )

        def _compute():
            return _build_ai_continue_context(
                book_id, bb, instruction, skill_pack_ids, target_chapter_num,
                prev_chapter_content, chapter_lang_styles, enable_structured_tags,
                skip_chapter_plan, skip_cache=skip_cache, _bypass_cache=True,
            )

        payload, cache_info = PromptContextCache.get().get_or_compute(
            'continue_ctx', book_id, deps, _compute, ttl_sec=1800, skip_cache=skip_cache,
        )
        # 在返回 dict 上挂 cache_info（上游 wrapper 加响应头时用）
        if isinstance(payload, dict):
            payload['_cache_info'] = cache_info
        return payload

    # ============== 以下是真正装配逻辑（以前代码一字不动，仅函数签名扩展了 2 个 kwarg）==============
    book = Book.query.get(book_id)
    config = AIConfig.get_active()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    # 识别/检查类任务（章节计划、一致性检查）用识别模型，为空时回退主模型
    recognition_model = config.get_model_for_task('recognition') if config else model
    if not base_url.endswith('/v1'):
        base_url = base_url.rstrip('/') + '/v1'

    # ===== 0. 章号计算（#9：max(order_index)+1 兜底；前端传入优先，处理"已生成未保存"场景）=====
    all_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if target_chapter_num and isinstance(target_chapter_num, int) and target_chapter_num > 0:
        current_chapter_num = target_chapter_num
    else:
        if all_chapters:
            current_chapter_num = max(c.order_index for c in all_chapters) + 1
        else:
            current_chapter_num = 1
    last_chapter = all_chapters[-1] if all_chapters else None

    # ===== 1. 识别当前卷（#1+#3）=====
    vol_chapter, vol_index = _identify_current_volume(book_id, current_chapter_num)
    # 统计当前卷已有章节数（用于 temperature 计算）
    chapters_in_vol = 0
    if vol_chapter:
        next_vols = Chapter.query.filter(
            Chapter.book_id == book_id,
            Chapter.is_volume == True,
            Chapter.order_index > vol_chapter.order_index
        ).order_by(Chapter.order_index).all()
        upper_bound = next_vols[0].order_index if next_vols else 999999
        chapters_in_vol = Chapter.query.filter(
            Chapter.book_id == book_id,
            Chapter.is_volume == False,
            Chapter.order_index >= vol_chapter.order_index,
            Chapter.order_index < upper_bound
        ).count()

    # ===== 2. 分层 bible 上下文（#5：相关性筛选 + #14：预算管理）=====
    # 提取最近4章出场角色，用于 character_profiles 相关性筛选
    recent_for_chars = all_chapters[-4:] if all_chapters else []
    appearing_chars = _extract_appearing_characters(recent_for_chars, bb)
    filtered_bible = _filter_bible_by_relevance(bb, appearing_chars)

    bible_sections = []
    # ===== 【P0b·正文瘦身】全量设定注入已删除（管道式信息流，与用户确认一致）=====
    # 删除项：大纲 plot_design / 世界观 worldbuilding / 核心规则 key_rules / 构思 concept /
    #         物资库 inventory / 地图 locations / 关系图谱 relation_graph
    # 原因：上游设定已在维度生成阶段被下游转述进【卷纲·情节节点】与【动态文件】，
    #       正文阶段再全量注入只会与转述冲突 + 浪费 token；需要专有名词细节时
    #       由下方【按需检索通道】(P1) 倒排命中精准捞片段（总预算600字）。
    # 【剧情·卷纲规划】情节节点通道：前一卷+本卷+后一卷三卷注入（含情节节点）
    adjacent_outlines = _get_adjacent_volumes_outline(book_id, vol_index)
    if adjacent_outlines:
        bible_sections.append((adjacent_outlines.split('\n')[0], '\n'.join(adjacent_outlines.split('\n')[1:]).strip(), 3))
    # 【人物】出场人物（相关性筛选后）
    if filtered_bible.get('character_profiles'):
        bible_sections.append(('【人物档案】（保持人设一致）', filtered_bible['character_profiles'], 3))
    # 文风指南
    if filtered_bible.get('style_guide'):
        bible_sections.append(('【文风指南】', filtered_bible['style_guide'], 1))

    # 按卷注入维度数据（#1）——本卷人物/伏笔/地点/动态
    vol_dim_sections = []
    _inject_volume_dimensions(bb, vol_chapter, vol_index, vol_dim_sections)
    for s in vol_dim_sections:
        first_line = s.split('\n')[0]
        body = '\n'.join(s.split('\n')[1:]).strip()
        bible_sections.append((first_line, body, 2))

    # 卷目标对齐已由上方 _get_adjacent_volumes_outline 三卷注入覆盖（本卷部分更详细），
    # 此处不再单独注入当前卷卷纲，避免重复

    # 预算管理：bible设定总预算5000字（前4章正文独立注入，不挤占此预算）
    bible_context = _apply_budget_management(bible_sections, total_budget=5000) if bible_sections else (bb.generated_summary or '')[:2000]

    # ===== 3. 分层滚动记忆（P1重构：前4章完整正文 + 最近10份动态报告）=====
    # 最近10份动态报告（每份≤800字），覆盖更长剧情跨度
    relevant_reports = _collect_relevant_reports(book_id, current_chapter_num, window=100, max_reports=10, per_report_limit=800)
    # 百万字长线记忆：早期卷的按卷聚合摘要（补充 window=100 的盲区，避免前200章剧情遗忘）
    historical_digests = _collect_historical_volume_digest(book_id, current_chapter_num, max_volumes=4, per_volume_limit=400)
    # 前4章完整正文（不截断，保证剧情连贯性）
    recent_chapters_full = all_chapters[-4:] if all_chapters else []

    if relevant_reports:
        report_context = '\n\n'.join([f'【{r["title"]}（{r["chapter_start"]}-{r["chapter_end"]}章）】\n{r["content"]}' for r in relevant_reports])
    else:
        report_context = '（暂无动态报告）'

    # 历史卷摘要段（若有）：百万字时让 AI 看到早期卷的剧情概要
    historical_section = ''
    if historical_digests:
        hd_lines = [f'- {d["volume"]}（{d["chapter_range"]}）：{d["digest"]}' for d in historical_digests]
        historical_section = f"""
【早期卷剧情摘要】（百万字长线记忆，避免遗忘前几卷的关键剧情）：
{chr(10).join(hd_lines)}
"""

    if recent_chapters_full:
        chapters_text = '\n\n'.join([f'【第{c.order_index}章 {c.title or ""}】\n{c.content or ""}' for c in recent_chapters_full])
    else:
        chapters_text = '（开篇第一章，无前文）'

    # 修复上下文脱节：前端传入"上一章已生成未保存"内容时追加注入（截断2400字），避免剧情断档
    if prev_chapter_content and prev_chapter_content.strip():
        prev_trimmed = prev_chapter_content.strip()[:2400]
        prev_ch_num = current_chapter_num - 1
        prev_tag = '【上一章（已生成未保存，必须严格承接此章剧情）】'
        chapters_text = (chapters_text + '\n\n' if chapters_text and chapters_text != '（开篇第一章，无前文）' else '') + f'{prev_tag}\n【第{prev_ch_num}章】\n{prev_trimmed}'

    # ===== 【P1·按需检索通道】专有名词倒排匹配，从全量设定维度精准捞片段 =====
    # 查询 = 情节节点三卷卷纲 + 前4章正文（含未保存上一章）+ 出场人物 + 作者指令；
    # 索引 = 大纲/世界观/核心规则/构思/物资库/地图/关系图谱的分块词条（【】标题/行首字段名/人物名）；
    # 命中才注入：每词条≤200字，总预算600字（用到才给，替代 P0b 删除的全量注入）。
    try:
        _ondemand_query = '\n'.join(filter(None, [adjacent_outlines, chapters_text, instruction]))
        _ondemand_snippets = _build_ondemand_bible_snippets(bb, _ondemand_query, appearing_chars=appearing_chars)
    except Exception:
        _ondemand_snippets = ''
    ondemand_block = ''
    if _ondemand_snippets:
        ondemand_block = f"""
【按需检索·设定片段】（根据本章情节节点/前文命中的专有名词，从设定维度精准捞取的片段，细节以此为准）：
{_ondemand_snippets}"""

    # 轻量RAG：基于当前章出场角色召回相关历史章节摘要，补充前4章窗口盲区
    # 让 AI 看到涉及角色在更早章节的经历，避免"角色历史行为遗忘"
    recalled_chapters = _recall_related_chapters(book_id, appearing_chars, current_chapter_num, max_chapters=6)
    recall_section = ''
    if recalled_chapters:
        rc_lines = [f'- 第{rc["chapter_num"]}章《{rc["title"]}》：{rc["summary"]}' for rc in recalled_chapters]
        recall_section = f"""
【相关历史章节召回】（基于本章出场角色智能召回，补充前4章窗口盲区，角色历史经历须与此一致）：
{chr(10).join(rc_lines)}
"""
    # 语义检索召回（embedding 优先，自动降级 TF-IDF）；字符召回不足 6 条时启用
    if len(recalled_chapters) < 6:
        try:
            from semantic_retriever import recall_semantic_chapters
            # 构造 query：出场角色 + 当前章 content_focus
            semantic_query = ' '.join(appearing_chars) if appearing_chars else ''
            if hasattr(bb, 'outline_hierarchy') and bb.outline_hierarchy:
                try:
                    hier = json.loads(bb.outline_hierarchy)
                    for ch_plan in hier.get('chapters', []):
                        if ch_plan.get('chapter_num') == current_chapter_num:
                            semantic_query += ' ' + (ch_plan.get('content_focus') or '')
                            break
                except Exception:
                    pass
            if semantic_query:
                def _provider():
                    # P2 升级：给 semantic retriever 传 content（整章 chunk 化做语义向量），
                    # 不传 content 会退化为旧 summary-300 字模式。
                    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).filter(
                        Chapter.order_index < current_chapter_num
                    ).order_by(Chapter.order_index.desc()).limit(300).all()
                    out = []
                    for c in chs:
                        out.append({
                            'chapter_num': c.order_index,
                            'title': c.title or '',
                            'summary': (getattr(c, 'summary', '') or ''),
                            'content': (getattr(c, 'content', '') or ''),
                        })
                    return out
                semantic_results = recall_semantic_chapters(
                    book_id, semantic_query, current_chapter_num,
                    exclude_recent=4, max_chapters=6 - len(recalled_chapters),
                    chapters_provider=_provider,
                )
                # 去重：排除已召回的章号
                existing_nums = {rc['chapter_num'] for rc in recalled_chapters}
                for sr in semantic_results:
                    if sr['chapter_num'] not in existing_nums:
                        recalled_chapters.append(sr)
                        existing_nums.add(sr['chapter_num'])
                # 重新渲染 recall_section
                if recalled_chapters:
                    rc_lines = [f'- 第{rc["chapter_num"]}章《{rc["title"]}》：{rc["summary"]}' for rc in recalled_chapters]
                    recall_section = f"""
【相关历史章节召回】（基于本章出场角色+语义检索召回，补充前4章窗口盲区，角色历史经历须与此一致）：
{chr(10).join(rc_lines)}
"""
        except Exception:
            pass  # 语义检索失败不影响主流程

    memory_section = f"""【前文动态报告】（最近10份动态文件摘要，防长线遗忘）：
{report_context}{historical_section}{recall_section}

【最近4章完整正文】（即时层，剧情衔接依据，必须严格保持连贯）：
{chapters_text}"""

    # P2-9：DynamicMemory 5文件版精简摘要注入（细粒度状态回流章节生成）
    # 5 文件版提供比 DynamicReport 更精细的角色生态/能力世界/伏笔追踪状态，
    # 每文件截取前 400 字，避免与动态报告内容重复堆叠
    dynamic_memory_section = ''
    try:
        dm = DynamicMemory.query.filter_by(book_id=book_id).first()
        if dm:
            dm_parts = []
            _dm_fields = [
                ('narrative_engine', '叙事引擎·节奏/张力状态'),
                ('foreshadowing_tracker', '伏笔追踪·状态快照'),
                ('character_ecosystem', '角色生态·关系/状态'),
                ('ability_world', '能力/世界·境界/势力'),
                ('health_dashboard', '健康度·债务预警'),
            ]
            for field_key, field_label in _dm_fields:
                raw = getattr(dm, field_key, '') or ''
                if raw and raw.strip() and raw.strip() != '{}':
                    # 尝试提取关键信息（若为 JSON 则压缩空白）
                    snippet = raw.strip()[:400]
                    dm_parts.append(f'- {field_label}：{snippet}')
            if dm_parts:
                dynamic_memory_section = f"""
【细粒度状态快照】（DynamicMemory 5文件版，比动态报告更精细的角色/能力/伏笔状态，须与此一致）：
{chr(10).join(dm_parts)}
"""
    except Exception:
        pass

    # ===== 4. 伏笔防遗忘（#2：按到期紧迫度排序，扩展到Top 25）=====
    # 百万字长线：top_n 提至 25，且紧迫度算法已对"无目标但沉淀已久"的伏笔加权，避免被挤出
    pending_fs = _sort_foreshadowings_by_urgency(bb, vol_chapter, current_chapter_num, top_n=25)
    foreshadowing_section = ''
    if pending_fs:
        fs_lines = []
        for urgency, planted, target, desc in pending_fs:
            target_hint = f'（计划回收于第{target}章）' if target else '（无明确回收点）'
            fs_lines.append(f'- {desc}{target_hint}')
        pending_text = '\n'.join(fs_lines)
        foreshadowing_section = f"""【待回收伏笔清单】（按到期紧迫度排序，本章节应考虑回收其中1-2条，避免遗忘；若无合适时机可暂缓，但不可永久遗忘）
{pending_text}"""

    # P0-2 增强：从伏笔 DAG 注入本章专属伏笔任务（应埋/应收），覆盖更精准
    if get_hooks_for_chapter and bb.foreshadowing_graph:
        try:
            graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
            from foreshadowing_manager import build_hooks_prompt_section
            dag_hooks = build_hooks_prompt_section(graph, current_chapter_num)
            if dag_hooks:
                foreshadowing_section = (foreshadowing_section + '\n\n' + dag_hooks).strip()
        except Exception:
            pass  # DAG 解析失败退回原文本伏笔清单

    # ===== 4.5 防遗忘检查报告回注（让 AI 自动规避已诊断出的问题）=====
    af_alerts = _collect_anti_forget_alerts(bb, max_reports=3, max_alerts=12)
    af_section = ''
    if af_alerts:
        af_section = f"""【防遗忘检查诊断】（最近检查发现的问题，本次写作必须主动规避/修正）
{af_alerts}"""

    # P1-4 + P1-5：从四级大纲取本章戏剧位置，注入节拍模板
    beat_section = ''
    _beat_from_hierarchy = False
    if build_dramatic_position_prompt and build_beat_prompt and bb.outline_hierarchy:
        try:
            hierarchy = json.loads(bb.outline_hierarchy)
            # 戏剧位置上下文
            dp_prompt = build_dramatic_position_prompt(hierarchy, current_chapter_num)
            # 节拍模板
            from outline_hierarchy_builder import get_dramatic_context
            ctx_dp = get_dramatic_context(hierarchy, current_chapter_num)
            position = ctx_dp.get('dramatic_position', '') if ctx_dp else ''
            beats_prompt = build_beat_prompt(position, word_count=2500) if position else ''
            if dp_prompt or beats_prompt:
                beat_section = (dp_prompt + '\n\n' + beats_prompt).strip()
                _beat_from_hierarchy = True
        except Exception:
            pass  # 大纲/节拍加载失败退回无节拍模式

    # 【兜底】outline_hierarchy 缺失/查不到本章位置时按"章在卷内进度"推断位置注入节拍，
    # 避免正文零结构约束裸奔（"东拼西凑没起承转合"的根因之一：timeline 未生成时 beat 完全不注入）。
    # 推断规则：卷内前15%→起，15%-60%→承，60%-85%→转，末15%→合（卷结构也缺失时按章号1→起/其余→承）
    if not _beat_from_hierarchy and build_beat_prompt:
        _fallback_position = '起' if current_chapter_num <= 1 else '承'
        try:
            if vol_chapter is not None and current_chapter_num > 1:
                _cpv = _get_chapters_per_volume(bb, book) or 50
                _offset = max(0, current_chapter_num - int(vol_chapter.order_index or 1))
                _ratio = _offset / max(1, int(_cpv))
                if _ratio < 0.15:
                    _fallback_position = '起'
                elif _ratio < 0.6:
                    _fallback_position = '承'
                elif _ratio < 0.85:
                    _fallback_position = '转'
                else:
                    _fallback_position = '合'
            _fb_beats = build_beat_prompt(_fallback_position, word_count=2500)
            if _fb_beats:
                beat_section = (beat_section + '\n\n' if beat_section else '') + f"""【章内节拍模板】（四级大纲缺失，按第{current_chapter_num}章推断戏剧位置「{_fallback_position}」注入，必须严格遵循）
{_fb_beats}"""
        except Exception:
            pass

    # ===== 黄金开局公式（第1-3章：番茄开局留存窗口，违规=不合格）=====
    golden_section = ''
    if current_chapter_num <= 3:
        golden_section = """【黄金开局公式·第1-3章专属（开局留存窗口，违规=不合格章节）】
- 绝境代入（占全章15%-20%）：主角身处具体绝境（挨打/羞辱/濒死/被夺），压迫感落在身体细节上；穿越者必须写错愕与身体记忆错位的适应过程，禁止无缝进入战斗模式
- 金手指到账必须带代价感（疼/寿命/反噬/不可逆损失），且当章兑现一次代价
- 首次主动使用：主角当章必须主动用金手指做成一件小事（小翻身），不许只"知道有"而不"用"
- 格局钩子：章尾钩子指向更大的世界（更大的敌人/更大的利益/更大的秘密）
- 配速红线：本章世界观只给读者"当章用得上"的1条，其余留待后续展开；开局堆设定=劝退"""

    # ===== 5. 章节计划前置（#4：chapter_plan Agent）=====
    # 章节计划属"读数据→提炼→注入"的识别类任务，用识别模型（便宜快），正文生成本身仍用主模型
    # skip_chapter_plan=True 时跳过（批处理流式模式用，避免同步 LLM 调用阻塞导致心跳中断）
    chapter_plan = ''
    if not skip_chapter_plan:
        chapter_plan = _generate_chapter_plan(
            book_id, bb, current_chapter_num, vol_chapter, vol_index,
            memory_section, foreshadowing_section, skill_pack_ids,
            api_key, base_url, recognition_model, max_tokens=600
        )
    plan_section = f'【本章计划】（由 chapter_plan Agent 生成，请严格遵循）\n{chapter_plan}' if chapter_plan else ''

    # ===== 6. 技能包提示词 =====
    # 【三类无污染】正文生成阶段：只注入文风类（style）技能包，不注入构思/审查类
    # 【fix1】请求体 skill_pack_ids 为空时自动回退读 book.*_skill_ids，避免勾了文风没生效
    from skill_pack_runtime import resolve_active_style_ids
    _active_style_ids = resolve_active_style_ids(skill_pack_ids, book)
    # 【fix2】传 book_genre 让 genre_target 题材匹配生效；不匹配时 WARNING 日志
    skill_note = _get_skill_prompts_by_category(
        _active_style_ids, 'style',
        book_genre=getattr(book, 'genre', None) if book else None,
    )

    # ===== 7. 智能默认指令（#11）=====
    smart_instruction = _build_smart_instruction(instruction, last_chapter, current_chapter_num)

    # ===== 8. 组装 system_prompt（bible设定5000字预算 + 记忆独立段不挤占；卷数+流派+本章文风为核心依据）=====
    core_params_block = _build_core_params_block(bb, book)
    chapter_lang_style_block = _build_chapter_lang_style_prompt(chapter_lang_styles)
    system_prompt = f"""你是番茄小说金番作者级别的写手，正在协作写一本小说，当前准备写第 {current_chapter_num} 章。

{core_params_block}
{chapter_lang_style_block}

【设定权威分级·冲突仲裁规则】（P0-3·正文瘦身版）
当下方各层设定发生冲突时，按权威层级从高到低取信：
- plot 层（最高）：卷纲与情节节点（前中后三卷规划）—— 当前卷剧情框架
- rules 层：伏笔任务清单（本章应埋/应收）—— 不可遗漏的铁律
- runtime 层：动态文件/本卷维度数据 —— 当前卷的运行时状态
- memory 层（最低）：前4章正文 —— 即时剧情衔接依据
若情节节点与本卷动态文件冲突，以情节节点为准；若动态文件与前4章正文冲突，以动态文件为准。

【项目宪法 - 已确认设定】（必须严格遵守，不可矛盾。包含：卷纲情节节点/出场人物/本卷维度数据/文风等）
{bible_context[:5000]}
{ondemand_block}

{memory_section}
{dynamic_memory_section}

{foreshadowing_section}

{af_section}

{beat_section}

{plan_section}

{golden_section}

【写作要求】
1. 严格遵循项目宪法中的设定，不可违反
2. 保持前后人物性格、关系、能力一致；严格衔接【最近4章完整正文】剧情走向
3. 延续现有文风和叙事节奏
4. 【字数绝对铁律】每章正文必须 2400 字 ±100（2300-2500 字区间，含标点）。低于2300字=扩展；超过2500字=删减。不可违反，优先级最高。
5. 主动回收"待回收伏笔清单"中的伏笔（若有）
6. 三明治结构：苦→甜→爽→钩子
7. 章尾必留钩子，七种类型不重复
8. 若存在【本章计划】，必须严格按计划展开
9. 【剧情连贯铁律】严格承接前4章结尾场景与悬念，不得凭空开新场景；人物位置/状态/对话一致
10.【剧情结构硬卡·起承转合（防东拼西凑，违规=不合格章节）】
  10.1 事件限额与因果链：本章关键事件≤3个，且必须因果衔接——下一事件由上一事件的后果直接引起，禁止无关事件并列堆砌、禁止事件被外力打断后就再无下文
  10.2 冲突闭环：本章核心冲突必须闭环——要么解决，要么显式挂起（主角明确知道没完、敌人明确留下威胁）；被新事件打断的冲突必须交代去向
  10.3 信息增量：每个大段落必须推进 信息/关系/危机 之一，禁止空转对话和事件播报；设定靠遭遇带出，禁止对白 lecture 灌世界观
  10.4 爽点公式：高潮爆发段必须写足连环反应——主角体感1句+至少2个视角的围观反差（反派嚣张→狼狈对照/强者动容/围观哗然）；打脸之后必须留≥2拍余震再收章，禁止爆发完直接跳收尾
  10.5 呼吸节奏：每2个冲突波之间留半段闲笔（环境/小动作/一句废话），全程紧绷无喘息=节奏灾难
10.6【节奏温度·15% 喘息段铁律（tension_score>90 自动判不合格，必须补 1–2 段闲笔再输出）】
    一章里至少要有 15% 的 Band1/Band2 喘息段（日常细节 / 景物锚 / 一句碎嘴对白 / 人物小动作细节），不能全程 Band4/5 紧绷。
11.【防遗忘规避】若有【防遗忘检查诊断】，主动规避已列违规并优先回收已诊断伏笔/叙事债务
12.【OOC专项】角色不得突然性格大变。

【文风铁律 · 最高权威（任一条违规即为不合格章节，必须重写）】
{skill_note}
（文风包红线与文末《创作总则》《正文写作规范》《去AI味与行文消杀》共同生效：
段落结构、句号句长、摄像机词、禁词黑名单、禁用句式、被字句与X地副词等硬指标
全部以上述三份规范为唯一标准，此处不再重复列出。词频与句式命中由平台后置校验器统一扫描。）"""

    if enable_structured_tags and build_pre_write_check_prompt:
        system_prompt += build_pre_write_check_prompt(current_chapter_num, bb)

    # P1-6：在 system_prompt 末尾追加 CHANGES 输出模板（要求 LLM 输出结构化状态变更）
    if enable_structured_tags and build_changes_prompt_template:
        system_prompt += build_changes_prompt_template()

    # 标题自动生成：要求 LLM 在正文末尾输出 JSON 标题，供后端解析回填章节标题
    system_prompt += '\n\n【标题输出】在正文最末尾另起一行，输出一个 JSON 对象，格式：{"title": "标题文本"}。标题 8-16 字，概括本章核心冲突或转折，不使用"第X章"前缀，需贴合正文内容，避免剧透关键悬念。只输出 JSON，不要输出其他内容。'

    # ===== 【标准文风铁律】统一注入（三种创作模式共用此 context，确保输出风格一致）=====
    # 以 chat_collab_bp 三大常量（GENERAL_CORE_RULES 创作总则 / WRITING_STYLE_RULES 正文写作规范）
    # 为唯一事实源，通过 build_writing_rules 连同已选的文风技能包一并注入正文阶段；
    # 技能包提示词和章节语言风格仅做题材向微调，不得覆盖核心长短句比例与禁词清单。
    try:
        from blueprints.chat_collab_bp import build_writing_rules as _bp_build_writing_rules
        _writing_core = _bp_build_writing_rules(book=book, skill_pack_ids=skill_pack_ids, mode='agent')
    except Exception:
        _writing_core = ''
    if _writing_core:
        system_prompt += '\n\n' + _writing_core

    # ===== 用户采纳的"系统学习与优化建议"补丁：统一追加到章节生成 system prompt 末尾 =====
    # （正文生成 / 流式 / 批量生成 都共享 _build_ai_continue_context，在此一处注入全局生效）
    if bb:
        try:
            from meta_optimizer import build_active_patch_text
            _pp = build_active_patch_text(bb)
            if _pp:
                system_prompt += '\n\n' + _pp
        except Exception:
            pass

    # ===== 9. 动态 temperature（#10）=====
    temperature = _compute_dynamic_temperature(current_chapter_num, vol_chapter, vol_index, chapters_in_vol)

    # ===== 10. 组装 user_prompt（第6142行返回引用，必须在此定义）=====
    user_prompt = instruction or f'请写第 {current_chapter_num} 章正文，严格遵循上方设定与计划。'

    # ===== 【fix4】激活文风包自证清单：返回时带 activated_skill_packs，前端可拉取自证
    from skill_pack_runtime import build_activated_skill_pack_manifest
    _activated_sp_names = build_activated_skill_pack_manifest(_active_style_ids)

    return {
        'system_prompt': system_prompt,
        'user_prompt': user_prompt,
        'temperature': temperature,
        # 【字数铁律】给足输出空间不物理截断；含 PRE_WRITE_CHECK 13行表 + CHANGES JSON 12字段。
        # 【max_tokens 按模型能力"不限"】给足 131072（与智驾 _DIM_MAX_TOKENS 同策略，旧值
        # 12000 ≈ 6000 中文字对思考型模型仍可能截断正文），交由已知/自学习输出上限钳制：
        # glm-5.3→128000、deepseek-chat→8192…防下游直连 requests.post 的调用
        # （去AI味/字数修正/审校等）越界 400
        'max_tokens': min(131072, get_output_limit(base_url, model) or 131072),
        'chapter_plan': chapter_plan,
        'current_chapter_num': current_chapter_num,
        'vol_chapter': vol_chapter,
        'vol_index': vol_index,
        'api_key': api_key,
        'base_url': base_url,
        'model': model,
        'recognition_model': recognition_model,  # 识别/检查类任务用
        'activated_skill_packs': _activated_sp_names,  # 自证用：本次正文生成实际注入的文风包
    }

def _stream_llm_chunks_with_heartbeat(resp, chapter_num, last_heartbeat, heartbeat_interval=5):
    """后台线程读取 LLM 流式响应，主生成器从队列消费，无数据时 yield 心跳。

    修复 network error 根因：resp.iter_lines() 是阻塞调用，原实现把心跳检查放在
    for 循环体内，LLM 一旦长时间不输出（首 token 延迟/思考阶段/网络抖动），
    循环体不执行 → 心跳无法推送 → Render 代理判定空闲超时 → 断连 network error。

    本函数用独立线程读取流，主流程 queue.get(timeout=3) 非阻塞消费：
    - 收到数据 → 处理 delta，重置心跳计时
    - 3s 无数据 → 检查是否到 5s 心跳间隔，是则 yield 心跳

    yield 元素：('chunk', delta_str) 或 ('heartbeat',) 或 ('done',)
    异常通过 ('error', exception) 返回。"""
    import threading
    import queue as _queue
    import time as _t

    chunk_q = _queue.Queue()
    reader_err = [None]

    def _reader():
        try:
            for line in resp.iter_lines():
                if line:
                    chunk_q.put(line.decode('utf-8', errors='replace'))
        except Exception as e:
            reader_err[0] = e
        finally:
            chunk_q.put(None)  # sentinel 表示流结束

    reader_t = threading.Thread(target=_reader, daemon=True)
    reader_t.start()

    last_hb = last_heartbeat
    while True:
        try:
            item = chunk_q.get(timeout=3)
        except _queue.Empty:
            now = _t.time()
            if now - last_hb >= heartbeat_interval:
                yield ('heartbeat',)
                last_hb = now
            continue

        if item is None:
            if reader_err[0]:
                yield ('error', reader_err[0])
            yield ('done',)
            return

        if item.startswith('data: '):
            chunk = item[6:]
            if chunk == '[DONE]':
                yield ('done',)
                return
            try:
                chunk_data = json.loads(chunk)
                delta = chunk_data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                if delta:
                    yield ('chunk', delta)
                    last_hb = _t.time()
            except Exception:
                pass

def _run_blocking_with_heartbeat(func, heartbeat_msg, heartbeat_interval=5):
    """在后台线程运行阻塞函数 func，主生成器 yield 心跳保持 SSE 连接活跃。

    用于包装非流式的 LLM 调用（如去AI味、动态报告生成）——这些调用阻塞数十秒，
    期间无法 yield 任何数据，Render 代理空闲超时会断连。

    用法（在 SSE 生成器内）：
        result = yield from _run_blocking_with_heartbeat(
            lambda: requests.post(...), '正在去AI味审校...')

    注意：后台线程会自动 push Flask app context，确保 func 内的 DB 操作
    (db.session / Model.query) 不会因 'Working outside of application context' 报错。
    """
    import threading
    import queue as _queue
    import time as _t

    result_q = _queue.Queue()
    _app = app  # 捕获 app 引用，后台线程需 push app context 才能 DB 操作

    def _runner():
        try:
            with _app.app_context():
                result_q.put(('ok', func()))
        except Exception as e:
            result_q.put(('err', e))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    last_hb = _t.time()
    while True:
        try:
            status, val = result_q.get(timeout=3)
            if status == 'err':
                raise val
            return val
        except _queue.Empty:
            now = _t.time()
            if now - last_hb >= heartbeat_interval:
                yield heartbeat_msg
                last_hb = now

def _build_deai_rules_block(skill_pack_ids, book):
    """统一构建去AI味审校规则块（单章 ai_continue 与批 ai_continue_batch_stream 共用）。

    消除两处 7 行重复；同时解决"去AI规则构建失败/为空时静默跳过"的隐患：
    - build_review_rules 内部已保证 parts 含 DEAI_ONLY_RULES（核心去AI表），技能包/import 异常只丢技能包；
    - 此处再兜底：即便 build_review_rules 也抛异常或返回空串，也强制回退注入核心 DEAI_ONLY_RULES，
      保证去AI审校环节永不因构建问题而静默缺失。
    返回 (rules_block, status)：status = 'ok' / 'fallback'。
    """
    from blueprints.chat_collab_bp import build_review_rules, DEAI_ONLY_RULES
    status = 'ok'
    try:
        block = build_review_rules(
            skill_pack_ids, mode='agent',
            prompt_keys_filter=['tomato_deai', 'de_ai_flavor', 'polish'], book=book)
        if not block:
            raise RuntimeError('build_review_rules 返回空串')
    except Exception as _deai_e:
        status = 'fallback'
        app.logger.error(f'去AI味规则构建异常，已回退注入核心去AI表: {_deai_e}')
        print(f'[去AI] 规则构建异常，已回退注入核心去AI表: {_deai_e}', file=sys.stderr)
        block = DEAI_ONLY_RULES
    return block, status

def _extract_chapter_body(full_content: str) -> str:
    """从 LLM 完整输出中剥离 PRE_WRITE_CHECK、chapter_changes、【标题】标签，只保留正文。
    P1-6 启用后 LLM 会输出结构化标签，校验器只检查正文部分。"""
    import re as _re
    body = full_content
    # 剥离 <pre_write_check>...</pre_write_check>
    body = _re.sub(r'<pre_write_check>[\s\S]*?</pre_write_check>', '', body, flags=_re.IGNORECASE)
    # 剥离 <chapter_changes>...</chapter_changes>
    body = _re.sub(r'<chapter_changes>[\s\S]*?</chapter_changes>', '', body, flags=_re.IGNORECASE)
    # 剥离 【标题】... 标签行（标题自动生成产物）
    body = _re.sub(r'【标题】[^\n]*', '', body)
    return body.strip()

def _format_chapter_title(chapter_num, suggested_title):
    """统一章节标题格式：第X章 标题文本

    三种创作模式（多Agent同步 / 流式 / 连续创作流式）统一使用此函数格式化标题，
    确保所有章节标题格式一致，混用模式时不会出现格式混乱。

    规则：
    - 有 suggested_title：格式为“第{章号}章 {标题}”
    - 无 suggested_title：格式为“第{章号}章”
    - suggested_title 已含“第X章”前缀时去重，避免“第1章 第1章 xxx”
    """
    import re as _re
    prefix = f'第{chapter_num}章'
    if not suggested_title or not suggested_title.strip():
        return prefix
    title = suggested_title.strip()
    # 去除标题中可能自带的“第X章”前缀（LLM 偶尔会带上）
    title = _re.sub(r'^第[一二三四五六七八九十百零0-9]+章[\s:：]*', '', title).strip()
    if not title:
        return prefix
    return f'{prefix} {title}'

def _extract_chapter_title(full_content: str, fallback_content: str = '') -> str:
    """从 LLM 完整输出中解析标题。
    优先解析末尾的 JSON：{"title": "标题文本"}
    解析失败时用章节前 20 字自动生成标题（兜底）。
    返回标题文本（无则空串）。"""
    import re as _re
    
    # 尝试解析末尾的 JSON 块
    try:
        # 查找最后一个 JSON 对象（从后往前找）
        json_matches = list(_re.finditer(r'\{[^{}]*"title"\s*:\s*"([^"]+)"[^{}]*\}', full_content))
        if json_matches:
            # 取最后一个匹配的 JSON
            last_match = json_matches[-1]
            title = last_match.group(1).strip()
            # 去除可能的"第X章"前缀
            title = _re.sub(r'^第[一二三四五六七八九十百零0-9]+章[\s:：]*', '', title)
            if title:
                return title[:30]  # 限长 30 字
    except Exception:
        pass
    
    # 兜底：用章节前 20 字生成标题
    if fallback_content:
        # 去除空白和标点，取前 20 字
        clean = _re.sub(r'\s+', '', fallback_content)
        # 去除常见的开头标记
        clean = _re.sub(r'^[^a-zA-Z\u4e00-\u9fa5]+', '', clean)
        if clean:
            return clean[:20] + ('...' if len(clean) > 20 else '')
    
    return ''

def _calc_chapter_score(post_validate, consistency_passed, consistency_issues, gate_result, word_count,
                        chapter_plan=None):
    """章节审校校验报告：聚合 post_validate 和 consistency_issues 的结果。
    返回 dict：{has_issues: bool, issues: [{'type': str, 'severity': str, 'description': str}]}。
    不再返回 0-100 分数和等级，简化为校验问题列表。"""
    if not isinstance(post_validate, dict):
        post_validate = {}
    
    issues = []
    
    # ① AI痕迹检测（post_validate）
    pv_issues = post_validate.get('issues', []) or []
    for iss in pv_issues:
        issues.append({
            'type': 'ai_trace',
            'severity': iss.get('severity', 'warning'),
            'description': f"[{iss.get('category', '未知')}] {iss.get('pattern', '')} — {iss.get('suggestion', '')}"
        })
    
    # ② 一致性检查（consistency_issues）
    if not consistency_passed and consistency_issues:
        # consistency_issues 可能是字符串（分号分隔）或列表
        if isinstance(consistency_issues, str):
            cons_issue_list = [s.strip() for s in consistency_issues.split('；') if s.strip()]
        elif isinstance(consistency_issues, list):
            cons_issue_list = consistency_issues
        else:
            cons_issue_list = []
        
        for issue_text in cons_issue_list:
            issues.append({
                'type': 'consistency',
                'severity': 'warning',
                'description': issue_text
            })
    
    return {
        'has_issues': len(issues) > 0,
        'issues': issues
    }
