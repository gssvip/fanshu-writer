"""AI 配置管理 Blueprint：支持最多 10 个配置，可切换激活。

将 /api/ai/config 与 /api/ai/configs 系列 CRUD 从 app.py 拆出，
避免 app.py 巨石膨胀。所有 LLM 调用仍通过 AIConfig.get_active() 取激活配置。

接口：
  GET    /api/ai/config                       返回当前激活配置（兼容旧接口）
  PUT    /api/ai/config                       更新当前激活配置（支持 models 数组 + model）
  GET    /api/ai/configs                      列出全部配置（每个提供商一条）
  POST   /api/ai/configs                      新增/更新一个提供商（合并 models，复用同 provider）
  POST   /api/ai/configs/<id>/select-model    智驾设置某个 provider 的当前模型并全局激活
  PUT    /api/ai/configs/<id>/activate        切换激活
  DELETE /api/ai/configs/<id>                 删除配置（删除激活配置时自动激活剩下首条）

「每个提供商一行，内部保存选定的多个模型」。所有 LLM 调用仍通过
AIConfig.get_active() 取激活配置，用其 model 作为当前使用模型。
"""
import json

from flask import Blueprint, jsonify, request

ai_config_bp = Blueprint('ai_config', __name__)

# 最多保留 10 个配置
MAX_CONFIGS = 10


@ai_config_bp.route('/api/ai/config', methods=['GET'])
def get_ai_config():
    """返回当前激活配置（兼容旧接口）。"""
    from app import AIConfig
    return jsonify(AIConfig.get_active().to_dict())


def _merge_models(base: list, add: list) -> list:
    """合并选定模型列表，保序去重。"""
    out: list = []
    for m in (list(base or []) + list(add or [])):
        m = str(m).strip()
        if m and m not in out:
            out.append(m)
    return out


@ai_config_bp.route('/api/ai/config', methods=['PUT'])
def update_ai_config():
    """更新当前激活配置（一个提供商一条）。

    - models：用户选定的该提供商模型列表（JSON 数组）
    - model：当前使用模型；models 内但 model 不在时，自动补充/修正
    - api_key 为 '***' 或空时保留原值，避免掩码覆盖真实密钥。
    """
    from app import db, AIConfig
    from llm_gateway import _normalize_llm_base_url
    data = request.json or {}
    cfg = AIConfig.get_active()
    for field in ['name', 'provider', 'recognition_model',
                  'temperature', 'max_tokens']:
        if field in data:
            setattr(cfg, field, data[field])
    # models 数组（选定模型列表）
    if isinstance(data.get('models'), list):
        cfg.models = json.dumps([m for m in data['models'] if str(m).strip()])
    models = cfg.get_models()
    # base_url 归一化参考模型
    new_model = data.get('model', cfg.model)
    if 'model' in data:
        cfg.model = data['model']
    if 'base_url' in data:
        raw = data['base_url'] or ''
        cfg.base_url = _normalize_llm_base_url(raw, new_model)
    # 当前 model 不在选定列表里时，挂到列表首尾（避免智驾点上空模型）
    if models and new_model and new_model not in models:
        models = [new_model] + models
        cfg.models = json.dumps(models)
    if 'api_key' in data and data['api_key'] and data['api_key'] != '***':
        cfg.api_key = data['api_key']
    db.session.commit()
    return jsonify(cfg.to_dict())


@ai_config_bp.route('/api/ai/configs', methods=['GET'])
def list_ai_configs():
    """列出全部配置（每个提供商一条），激活的排第一。"""
    from app import db, AIConfig
    all_cfgs = AIConfig.query.order_by(AIConfig.is_active.desc(), AIConfig.id.asc()).all()
    # 兼容旧库：没有任何 active 标记时，自动激活首条
    if all_cfgs and not any(c.is_active for c in all_cfgs):
        all_cfgs[0].is_active = True
        db.session.commit()
    return jsonify({'configs': [c.to_dict() for c in all_cfgs], 'max': MAX_CONFIGS})


@ai_config_bp.route('/api/ai/configs', methods=['POST'])
def create_ai_config():
    """新增/更新一个提供商（一个提供商一条）。

    同一 provider 已存在时报错提示改为"拉取更新"，避免重复建行；
    新 provider 则新建并自动激活。models 为选定的模型列表。
    """
    from app import db, AIConfig
    from llm_gateway import _normalize_llm_base_url
    data = request.json or {}
    provider = str(data.get('provider') or 'custom')
    raw_base = data.get('base_url', '') or ''
    new_models = data.get('models') if isinstance(data.get('models'), list) else []
    api_key = data.get('api_key', '') or ''
    # 已存在同 provider → 复用（合并 models + 继承已有 key/地址缺省值）
    existing = AIConfig.query.filter_by(provider=provider).first()
    if existing:
        if 'api_key' in data and data['api_key'] and data['api_key'] != '***':
            api_key = data['api_key']
        else:
            api_key = existing.api_key
        cur_models = existing.get_models()
        merged = _merge_models(cur_models, new_models)
        if 'model' in data and data.get('model'):
            merged = _merge_models([data['model']], merged)
        existing.model = data.get('model', '') or existing.model
        existing.models = json.dumps(merged)
        existing.name = data.get('name') or existing.name or provider
        if data.get('base_url'):
            existing.base_url = _normalize_llm_base_url(raw_base, data.get('model') or existing.model)
        if api_key:
            existing.api_key = api_key
        AIConfig.query.filter_by(is_active=True).update({'is_active': False})
        existing.is_active = True
        db.session.commit()
        return jsonify(existing.to_dict()), 201
    if AIConfig.query.count() >= MAX_CONFIGS:
        return jsonify({'error': f'最多 {MAX_CONFIGS} 个提供商配置，请先删除一个'}), 400
    if not api_key or api_key == '***':
        act = AIConfig.query.filter_by(is_active=True).first()
        api_key = act.api_key if act else ''
    new_model = data.get('model', '') or (new_models[0] if new_models else '')
    cfg = AIConfig(
        name=data.get('name') or data.get('provider_label') or provider,
        provider=provider,
        model=new_model,
        models=json.dumps(new_models or ([new_model] if new_model else [])),
        recognition_model=data.get('recognition_model', ''),
        api_key=api_key,
        base_url=_normalize_llm_base_url(raw_base, new_model) if raw_base else data.get('base_url', ''),
        temperature=data.get('temperature', 0.7),
        max_tokens=data.get('max_tokens', 4096),
        is_active=True,
    )
    AIConfig.query.filter_by(is_active=True).update({'is_active': False})
    db.session.add(cfg)
    db.session.commit()
    return jsonify(cfg.to_dict()), 201


@ai_config_bp.route('/api/ai/configs/<cfg_id>/select-model', methods=['POST'])
def select_ai_config_model(cfg_id):
    """智驾通用切换模型：把某提供商下的某个模型设为当前，并全局激活该提供商。

    body: { "model": "deepseek-chat" }
    效果：该 provider 成为激活配置，其 model = 所选模型 → 智驾设定/正文/去AI/校审全部跟随。
    """
    from app import db, AIConfig
    cfg = AIConfig.query.get(cfg_id)
    if not cfg:
        return jsonify({'error': '配置不存在'}), 404
    data = request.json or {}
    model = str(data.get('model') or '').strip()
    if not model:
        return jsonify({'error': '模型不能为空'}), 400
    # 把所选模型补进选定列表，并设为当前使用模型
    cfg.models = json.dumps(_merge_models(cfg.get_models(), [model]))
    cfg.model = model
    AIConfig.query.filter_by(is_active=True).update({'is_active': False})
    cfg.is_active = True
    db.session.commit()
    return jsonify(cfg.to_dict())


@ai_config_bp.route('/api/ai/configs/<cfg_id>/activate', methods=['PUT'])
def activate_ai_config(cfg_id):
    """切换激活配置。"""
    from app import db, AIConfig
    cfg = AIConfig.query.get(cfg_id)
    if not cfg:
        return jsonify({'error': '配置不存在'}), 404
    AIConfig.query.filter_by(is_active=True).update({'is_active': False})
    cfg.is_active = True
    db.session.commit()
    return jsonify(cfg.to_dict())


@ai_config_bp.route('/api/ai/configs/<cfg_id>', methods=['DELETE'])
def delete_ai_config(cfg_id):
    """删除配置。若删除的是激活配置，自动激活剩下的首条。"""
    from app import db, AIConfig
    cfg = AIConfig.query.get(cfg_id)
    if not cfg:
        return jsonify({'error': '配置不存在'}), 404
    was_active = cfg.is_active
    db.session.delete(cfg)
    db.session.commit()
    if was_active:
        first = AIConfig.query.order_by(AIConfig.id.asc()).first()
        if first:
            first.is_active = True
            db.session.commit()
    return jsonify({'ok': True})
