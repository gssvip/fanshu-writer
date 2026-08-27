"""
章节审计-修订闭环（P2-8）
draft → 后写校验 → （不通过）修订 → 再校验，best snapshot 持久化。
避免越改越糟：每次修订后对比分数，取最高分版本。

参考：InkOS run_chapter_review_cycle + best snapshot
设计原则：
  - PASS_SCORE=85，达到则通过
  - 最大修订 2 轮，避免死循环
  - 每轮提升 <3 分则停止（边际收益过低）
  - best snapshot 持久化到 chapter.review_snapshots
"""
from typing import Dict, Optional, Tuple
import json


PASS_SCORE = 85  # 通过分数阈值
MAX_ROUNDS = 2   # 最大修订轮数
MIN_IMPROVEMENT = 3  # 最小提升分数，低于此则停止


def run_review_cycle(
    draft_content: str,
    validate_fn,
    revise_fn,
    llm_call_fn,
) -> Dict:
    """执行审计-修订闭环。
    validate_fn(content) -> ValidationResult
    revise_fn(content, validation, llm_call_fn) -> revised_content
    llm_call_fn(sys_prompt, user_prompt) -> str
    返回 {final_content, best_score, rounds, history, passed}"""
    history = []
    best_content = draft_content
    best_score = 0
    current_content = draft_content

    for round_num in range(1, MAX_ROUNDS + 1):
        # 1. 校验当前内容
        validation = validate_fn(current_content)
        score = validation.score if validation else 100
        issues = validation.to_dict() if validation else {}

        history.append({
            'round': round_num,
            'score': score,
            'issue_count': len(validation.issues) if validation else 0,
            'critical_count': sum(1 for i in (validation.issues or []) if i.severity == 'critical') if validation else 0,
        })

        # 更新 best
        if score > best_score:
            best_score = score
            best_content = current_content

        # 2. 通过判断
        if score >= PASS_SCORE:
            return {
                'final_content': best_content,
                'best_score': best_score,
                'rounds': round_num,
                'history': history,
                'passed': True,
                'reason': f'第{round_num}轮达到通过分数 {PASS_SCORE}',
            }

        # 3. 最后一轮不再修订
        if round_num >= MAX_ROUNDS:
            break

        # 4. 修订
        try:
            revised = revise_fn(current_content, validation, llm_call_fn)
            if not revised or len(revised) < 100:
                # 修订失败，停止
                history[-1]['note'] = '修订返回异常，停止'
                break
            current_content = revised
        except Exception as e:
            history[-1]['note'] = f'修订异常：{str(e)[:100]}'
            break

        # 5. 边际收益判断（下一轮校验后对比）
        if round_num > 1:
            prev_score = history[-2]['score']
            if score - prev_score < MIN_IMPROVEMENT:
                history[-1]['note'] = f'提升仅 {score - prev_score} 分，低于阈值 {MIN_IMPROVEMENT}，停止'
                break

    return {
        'final_content': best_content,
        'best_score': best_score,
        'rounds': len(history),
        'history': history,
        'passed': best_score >= PASS_SCORE,
        'reason': f'达到最大轮数，取最高分版本（{best_score}分）',
    }


def run_review_cycle_with_bible(polished_content, bb, post_validate, book_id, chapter_num,
                                api_key, base_url, model, extract_chapter_body_fn):
    """【优化4首步·自 app.py 抽取】P1-5 审计-修订闭环编排（原 ai_continue 内联块）。
    条件触发：后写校验有问题时；local→spot_fix（省token）/ structural→整章重写；
    best snapshot 持久化到最近已落库章节（保留最近20条）。
    返回 (polished_content, review_cycle_result)；依赖缺失/任何异常原样返回，不阻断章节生成。"""
    import requests
    from datetime import datetime, timezone
    from post_write_validator import validate_chapter, validate_chapter_with_bible
    from revise import route_revision, build_spot_fix_prompt, apply_spot_fix_patches
    from llm_gateway import build_auth_headers, get_output_limit, _normalize_llm_base_url
    base_url = _normalize_llm_base_url(base_url, model)

    review_cycle_result = None
    if not (post_validate and post_validate.get('issues') and bb):
        return polished_content, review_cycle_result
    try:
        # 1. validate_fn：复用 validate_chapter_with_bible，注入全维度 bible_ctx
        def _validate_fn(content, _bb=bb):
            body = extract_chapter_body_fn(content)
            _ctx = {
                'character_profiles': _bb.character_profiles or '',
                'chapter_changes_log': _bb.chapter_changes_log or '',
                'key_rules': _bb.key_rules or '',
                'worldbuilding': _bb.worldbuilding or '',
                'inventory': _bb.inventory or '',
                'locations': _bb.locations or '',
                'foreshadowing': _bb.foreshadowing or '',
            } if _bb else None
            return validate_chapter_with_bible(body, _ctx) if _ctx else validate_chapter(body)

        # 2. revise_fn：auto 路由，local→spot_fix / structural→整章重写
        def _revise_fn(content, validation, llm_call_fn):
            if not route_revision:
                return content
            v_dict = validation.to_dict() if validation else {}
            routing = route_revision(content, v_dict, mode='auto')
            if routing['strategy'] == 'none' or routing['strategy'] == 'rewrite':
                rewrite_sys = ("你是小说修订专家。根据校验问题整章重写，保留原剧情走向与人物对话，"
                               "只修正结构性问题（OOC/剧情偏离/伏笔遗漏/一致性异常）。"
                               "只输出修订后的完整正文。")
                issues_text = '; '.join(v_dict.get('issues', [])[:5]) if isinstance(v_dict.get('issues'), list) else str(v_dict.get('issues', ''))[:500]
                rewrite_user = f'【校验问题】\n{issues_text}\n\n【原文】\n{content}'
                return llm_call_fn(rewrite_sys, rewrite_user)
            patches = routing['patches']
            if not patches:
                return content
            sys_prompt, user_prompt = build_spot_fix_prompt(content, patches)
            llm_output = llm_call_fn(sys_prompt, user_prompt)
            return apply_spot_fix_patches(content, patches, llm_output)

        # 3. llm_call_fn：封装 requests.post
        def _llm_call_fn(sys_prompt, user_prompt):
            # 【输出上限适配】12000 会撞 8k 输出上限的模型直接 400，按已知/已学习上限钳制
            _rev_max_tok = min(12000, get_output_limit(base_url, model) or 12000)
            resp = requests.post(f'{base_url}/chat/completions',
                headers=build_auth_headers(api_key),
                json={'model': model,
                      'messages': [{'role': 'system', 'content': sys_prompt},
                                   {'role': 'user', 'content': user_prompt}],
                      'temperature': 0.3, 'max_tokens': _rev_max_tok},
                timeout=180)
            result = resp.json()
            return result['choices'][0]['message']['content'].strip()

        # 4. 执行闭环
        cycle_outcome = run_review_cycle(
            draft_content=polished_content,
            validate_fn=_validate_fn,
            revise_fn=_revise_fn,
            llm_call_fn=_llm_call_fn,
        )
        # 5. best snapshot 落地：最终内容优于初稿时替换
        if cycle_outcome.get('final_content') and cycle_outcome.get('best_score', 0) > 0:
            final_content = cycle_outcome['final_content']
            if final_content != polished_content:
                polished_content = final_content
            review_cycle_result = {
                'best_score': cycle_outcome.get('best_score'),
                'rounds': cycle_outcome.get('rounds'),
                'passed': cycle_outcome.get('passed'),
                'reason': cycle_outcome.get('reason'),
                'history': cycle_outcome.get('history'),
            }
            # best snapshot 持久化到最近已落库章节
            try:
                from app import Chapter, db
                ch_for_snapshot = Chapter.query.filter_by(
                    book_id=book_id, is_volume=False
                ).order_by(Chapter.order_index.desc()).first()
                if ch_for_snapshot:
                    snapshots = []
                    if ch_for_snapshot.review_snapshots:
                        try:
                            snapshots = json.loads(ch_for_snapshot.review_snapshots)
                            if not isinstance(snapshots, list):
                                snapshots = []
                        except Exception:
                            snapshots = []
                    snapshots.append({
                        'chapter_num': chapter_num,
                        'best_score': cycle_outcome.get('best_score'),
                        'rounds': cycle_outcome.get('rounds'),
                        'passed': cycle_outcome.get('passed'),
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                    })
                    ch_for_snapshot.review_snapshots = json.dumps(snapshots[-20:], ensure_ascii=False)
                    db.session.commit()
            except Exception:
                from app import db
                db.session.rollback()
    except Exception:
        from app import db
        db.session.rollback()
    return polished_content, review_cycle_result
