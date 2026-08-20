"""SEED_SKILL_PACKS 序列化契约回归：杜绝裸 list/dict 打崩启动。

背景（2026-08-20 线上 P0 事故）：4 个新导文风包的 'workflow' 写成裸 Python list
（其他包均为 json.dumps 字符串）。启动时 seed_skill_packs() 同步逻辑把裸 list 直接
赋给 Text 列 → PostgreSQL psycopg2 "can't adapt type 'dict'" → 部署崩溃退出
（status 1）→ 无端口监听 → 整站宕机。

契约：SEED_SKILL_PACKS 每个条目的 workflow / stage_keys / prompts 必须是
JSON 字符串（DB 列是 Text）。本测试在 CI 直接拦下任何裸对象写法。
"""
import json

import pytest


@pytest.mark.usefixtures("app")
class TestSeedSkillPacksSerialization:
    def test_all_seed_json_fields_are_strings(self):
        """所有 seed 条目的三个 JSON 字段必须是 str（裸 list/dict 会打崩 psycopg2）。"""
        from app import SEED_SKILL_PACKS
        assert SEED_SKILL_PACKS, 'SEED_SKILL_PACKS 不应为空'
        offenders = []
        for sp in SEED_SKILL_PACKS:
            for k in ('workflow', 'stage_keys', 'prompts'):
                if not isinstance(sp.get(k), str):
                    offenders.append(f"{sp.get('name')}:{k}={type(sp[k]).__name__}")
        assert not offenders, f'seed JSON 字段必须是 str（线上can\'t adapt事故根因）：{offenders}'

    def test_all_seed_json_fields_parseable(self):
        """三个 JSON 字段必须能被 json.loads 解析（字符串但内容非法同样是脏数据）。"""
        from app import SEED_SKILL_PACKS
        for sp in SEED_SKILL_PACKS:
            for k in ('workflow', 'stage_keys', 'prompts'):
                v = sp[k]
                try:
                    json.loads(v)
                except (TypeError, ValueError) as e:
                    pytest.fail(f"{sp.get('name')}.{k} 非法 JSON: {e}")

    def test_seed_skill_packs_survives_clean_db(self, app):
        """全量 seed 在空库上跑通（INSERT 路径），所有行 JSON 字段均为合法字符串。"""
        from app import db, SkillPack, seed_skill_packs
        with app.app_context():
            seed_skill_packs()
            packs = SkillPack.query.filter_by(is_builtin=True).all()
            assert packs, 'seed 后必须存在内置技能包'
            for p in packs:
                json.loads(p.workflow_json or '[]')
                json.loads(p.stage_keys_json or '[]')
                json.loads(p.prompts_json or '{}')

    def test_normalization_defends_raw_objects(self, app):
        """防御性归一化：即使有人再写裸 list/dict，seed 也不再崩（自动序列化）。"""
        from app import SEED_SKILL_PACKS, seed_skill_packs
        victim = next(sp for sp in SEED_SKILL_PACKS if sp['name'])
        # 注入裸 list 模拟事故现场（copy 后还原，不污染全局）
        original, victim['workflow'] = victim['workflow'], [{'step': 1, 'name': 'x'}]
        try:
            with app.app_context():
                seed_skill_packs()  # 若无归一化兜底，此处抛 can't adapt / binding 错误
            assert isinstance(victim['workflow'], str)
            assert json.loads(victim['workflow'])[0]['name'] == 'x'
        finally:
            victim['workflow'] = original
