"""AI 配置管理 Blueprint：支持最多 3 个配置，可切换激活。

将 /api/ai/config 与 /api/ai/configs 系列 CRUD 从 app.py 拆出，
避免 app.py 巨石膨胀。所有 LLM 调用仍通过 AIConfig.get_active() 取激活配置。

接口：
  GET    /api/ai/config            返回当前激活配置（兼容旧接口）
  PUT    /api/ai/config            更新当前激活配置（兼容旧接口）
  GET    /api/ai/configs           列出全部配置（最多 MAX_CONFIGS 个）
  POST   /api/ai/configs           新增配置（新增的自动激活，超 3 个返回 400）
  PUT    /api/ai/configs/<id>/activate   切换激活
  DELETE /api/ai/configs/<id>      删除配置（删除激活配置时自动激活剩下首条）
"""
from flask import Blueprint, jsonify, request

ai_config_bp = Blueprint('ai_config', __name__)

# 最多保留 3 个配置
MAX_CONFIGS = 3


@ai_config_bp.route('/api/ai/config', methods=['GET'])
def get_ai_config():
    """返回当前激活配置（兼容旧接口）。"""
    from app import AIConfig
    return jsonify(AIConfig.get_active().to_dict())


@ai_config_bp.route('/api/ai/config', methods=['PUT'])
def update_ai_config():
    """更新当前激活配置（兼容旧接口）。

    api_key 为 '***' 或空时保留原值，避免掩码覆盖真实密钥。
    """
    from app import db, AIConfig
    data = request.json or {}
    cfg = AIConfig.get_active()
    for field in ['name', 'provider', 'model', 'recognition_model',
                  'base_url', 'temperature', 'max_tokens']:
        if field in data:
            setattr(cfg, field, data[field])
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
    """新增一个配置（最多 MAX_CONFIGS 个）。新增的配置自动激活。"""
    from app import db, AIConfig
    if AIConfig.query.count() >= MAX_CONFIGS:
        return jsonify({'error': f'最多 {MAX_CONFIGS} 个配置，请先删除一个'}), 400
    data = request.json or {}
    cfg = AIConfig(
        name=data.get('name') or f'配置 {AIConfig.query.count() + 1}',
        provider=data.get('provider', 'custom'),
        model=data.get('model', ''),
        recognition_model=data.get('recognition_model', ''),
        api_key=data.get('api_key', ''),
        base_url=data.get('base_url', ''),
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
