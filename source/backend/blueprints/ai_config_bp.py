"""AI 配置管理 Blueprint：支持最多 10 个配置，可切换激活。

将 /api/ai/config 与 /api/ai/configs 系列 CRUD 从 app.py 拆出，
避免 app.py 巨石膨胀。所有 LLM 调用仍通过 AIConfig.get_active() 取激活配置。

接口：
  GET    /api/ai/config            返回当前激活配置（兼容旧接口）
  PUT    /api/ai/config            更新当前激活配置（兼容旧接口）
  GET    /api/ai/configs           列出全部配置（最多 MAX_CONFIGS 个）
  POST   /api/ai/configs           新增配置（新增的自动激活，超 10 个返回 400）
  PUT    /api/ai/configs/<id>/activate   切换激活
  DELETE /api/ai/configs/<id>      删除配置（删除激活配置时自动激活剩下首条）
"""
from flask import Blueprint, jsonify, request

ai_config_bp = Blueprint('ai_config', __name__)

# 最多保留 10 个配置
MAX_CONFIGS = 10


@ai_config_bp.route('/api/ai/config', methods=['GET'])
def get_ai_config():
    """返回当前激活配置（兼容旧接口）。"""
    from app import AIConfig
    return jsonify(AIConfig.get_active().to_dict())


@ai_config_bp.route('/api/ai/config', methods=['PUT'])
def update_ai_config():
    """更新当前激活配置（兼容旧接口）。

    api_key 为 '***' 或空时保留原值，避免掩码覆盖真实密钥。
    【智谱 GLM 404 修复】落库前先把 base_url 归一化（智谱 v4 不补/v1等），
    这样 DB 存的就是正确干净的，即使有别的路径绕过 get_llm_config 也不容易坏。
    """
    from app import db, AIConfig
    from llm_gateway import _normalize_llm_base_url
    data = request.json or {}
    cfg = AIConfig.get_active()
    for field in ['name', 'provider', 'recognition_model',
                  'temperature', 'max_tokens']:
        if field in data:
            setattr(cfg, field, data[field])
    # model 参与 base_url 归一化识别（智谱 glm* → v4 分支），先取出来
    new_model = data.get('model', cfg.model)
    if 'model' in data:
        cfg.model = data['model']
    if 'base_url' in data:
        raw = data['base_url'] or ''
        # 存 DB 时就存归一化后的正确路径，避免 DB 残留坏值 /v4/v1
        cfg.base_url = _normalize_llm_base_url(raw, new_model)
    if 'api_key' in data and data['api_key'] and data['api_key'] != '***':
        cfg.api_key = data['api_key']
    db.session.commit()
    return jsonify(cfg.to_dict())


@ai_config_bp.route('/api/ai/configs', methods=['GET'])
def list_ai_configs():
    """列出全部配置，激活的排第一。"""
    from app import db, AIConfig
    all_cfgs = AIConfig.query.order_by(AIConfig.is_active.desc(), AIConfig.id.asc()).all()
    # 兼容旧库：没有任何 active 标记时，自动激活首条
    if all_cfgs and not any(c.is_active for c in all_cfgs):
        all_cfgs[0].is_active = True
        db.session.commit()
    return jsonify({'configs': [c.to_dict() for c in all_cfgs], 'max': MAX_CONFIGS})


@ai_config_bp.route('/api/ai/configs', methods=['POST'])
def create_ai_config():
    """新增一个配置（最多 MAX_CONFIGS 个）。新增的配置自动激活。

    【智谱 GLM 404 修复】落库前 base_url 归一化。
    """
    from app import db, AIConfig
    from llm_gateway import _normalize_llm_base_url
    if AIConfig.query.count() >= MAX_CONFIGS:
        return jsonify({'error': f'最多 {MAX_CONFIGS} 个配置，请先删除一个'}), 400
    data = request.json or {}
    model = data.get('model', '')
    raw_base = data.get('base_url', '') or ''
    # 同提供商加模型：前端拿到的是掩码 '***'，api_key 为空/掩码时自动继承当前激活配置的密钥
    api_key = data.get('api_key', '') or ''
    if not api_key or api_key == '***':
        act = AIConfig.query.filter_by(is_active=True).first()
        api_key = act.api_key if act else ''
    cfg = AIConfig(
        name=data.get('name') or f'配置 {AIConfig.query.count() + 1}',
        provider=data.get('provider', 'custom'),
        model=model,
        recognition_model=data.get('recognition_model', ''),
        api_key=api_key,
        base_url=_normalize_llm_base_url(raw_base, model),
        temperature=data.get('temperature', 0.7),
        max_tokens=data.get('max_tokens', 4096),
        is_active=True,
    )
    # 新增配置自动激活，其他全部取消激活
    AIConfig.query.filter_by(is_active=True).update({'is_active': False})
    db.session.add(cfg)
    db.session.commit()
    return jsonify(cfg.to_dict()), 201


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
