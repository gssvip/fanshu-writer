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
