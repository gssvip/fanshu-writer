---
name: chapter_writer
description: 章节正文生成 prompt（多Agent模式核心）
inputs: [system_prompt, user_prompt, chapter_num, word_budget]
output_format: narrative_text
budget:
  max_tokens: 4096
  temperature: 0.7
golden_cases:
  - input:
      chapter_num: 1
      word_budget: 2400
      system_prompt: "你是专业网文作者"
      user_prompt: "写第一章"
    assert_contains: ["第1章"]
    assert_min_length: 50
---

你是专业网文作者，正在创作第 {{chapter_num}} 章。

【系统指令】
{{system_prompt}}

【用户要求】
{{user_prompt}}

【字数要求】
正文 {{word_budget}} 字（2300-2500区间）。
