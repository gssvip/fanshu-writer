"""
数据模型集中定义（从 app.py 外迁，用于削减巨石体积）。

- db 实例在 extensions.py（SQLAlchemy() 未绑定 app，由 app.py 统一 init_app）
- 本模块内的模型仍通过 `from app import X` 向上兼容导出给各蓝图/工具模块使用
"""
import json
import uuid
from datetime import datetime, timezone

from extensions import db


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # 会员：True = 已开通网站永久会员（¥19.9，享无限创建书等权益）
    is_vip = db.Column(db.Boolean, default=False, nullable=False, server_default=db.false())

    def to_dict(self):
        return {'id': self.id, 'username': self.username, 'email': self.email,
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'is_vip': bool(getattr(self, 'is_vip', False))}

class AuthToken(db.Model):
    __tablename__ = 'auth_tokens'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    used = db.Column(db.Boolean, default=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Book(db.Model):
    __tablename__ = 'books'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), default='')
    title = db.Column(db.String(200), nullable=False)
    author = db.Column(db.String(100), default='')
    genre = db.Column(db.String(50), default='other')
    book_type = db.Column(db.String(20), default='novel')  # novel, short_story, script
    synopsis = db.Column(db.Text, default='')
    cover_path = db.Column(db.String(500), default='')
    template_id = db.Column(db.String(36), default='')
    word_count = db.Column(db.Integer, default=0)
    chapter_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='draft')  # draft, writing, completed
    target_words = db.Column(db.Integer, default=0)
    # 总卷数（用户自定义，不设上限）：作为五幕总纲/剧情大纲生成的核心依据。
    # 0 = 未设定（创作链路按"由作者定义"处理）——严禁默认 10，否则"圆桌永远按十卷设计"
    total_volumes = db.Column(db.Integer, default=0)
    # 小说风格流派（JSON 数组字符串，最多3种叠加）：爽文流/虐文流/系统流等
    novel_styles = db.Column(db.Text, default='[]')
    # 技能包三类划分：构思类/文风类/审查类 各自独立的 ID 列表（JSON 数组字符串）
    # 三类在各创作阶段无污染隔离：构思类→大纲/规划，文风类→正文生成，审查类→去AI味/一致性
    master_skill_ids = db.Column(db.Text, default='[]')  # 构思类技能包
    style_skill_ids = db.Column(db.Text, default='[]')   # 文风类技能包（通常选1个）
    review_skill_ids = db.Column(db.Text, default='[]')  # 审查类技能包
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    metadata_json = db.Column(db.Text, default='{}')

    chapters = db.relationship('Chapter', backref='book', lazy=True, cascade='all, delete-orphan', order_by='Chapter.order_index')
    characters = db.relationship('Character', backref='book', lazy=True, cascade='all, delete-orphan')
    outlines = db.relationship('Outline', backref='book', lazy=True, cascade='all, delete-orphan', order_by='Outline.order_index')
    daily_stats = db.relationship('DailyStats', backref='book', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id, 'title': self.title, 'author': self.author,
            'genre': self.genre, 'book_type': self.book_type, 'synopsis': self.synopsis,
            'cover_path': self.cover_path, 'template_id': self.template_id,
            'word_count': self.word_count, 'chapter_count': self.chapter_count,
            'status': self.status, 'target_words': self.target_words,
            'total_volumes': self.total_volumes if self.total_volumes else 10,
            'novel_styles': json.loads(self.novel_styles or '[]'),
            'master_skill_ids': json.loads(self.master_skill_ids or '[]'),
            'style_skill_ids': json.loads(self.style_skill_ids or '[]'),
            'review_skill_ids': json.loads(self.review_skill_ids or '[]'),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'metadata': json.loads(self.metadata_json or '{}')
        }

class Chapter(db.Model):
    __tablename__ = 'chapters'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False, default='')
    content = db.Column(db.Text, default='')
    order_index = db.Column(db.Integer, default=0)
    word_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='draft')
    is_volume = db.Column(db.Boolean, default=False)
    parent_id = db.Column(db.String(36), default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    notes = db.Column(db.Text, default='')
    review_snapshots = db.Column(db.Text, default='')  # P1-5：审计-修订闭环 best snapshot 历史（JSON）
    summary = db.Column(db.String(500), default='')  # 章节摘要（用于上下文构建，避免塞入完整正文）
    # M1a: 本章埋/收的伏笔索引 + 本章抽取的事件ID列表（支持跨章溯源与任务清单）
    hooks_set_json = db.Column(db.Text, default='')
    events_extracted_json = db.Column(db.Text, default='')

    versions = db.relationship('ChapterVersion', backref='chapter', lazy=True, cascade='all, delete-orphan', order_by='ChapterVersion.version_num.desc()')

    def to_dict(self, include_content=True):
        d = {
            'id': self.id, 'book_id': self.book_id, 'title': self.title,
            'order_index': self.order_index, 'word_count': self.word_count,
            'status': self.status, 'is_volume': self.is_volume, 'parent_id': self.parent_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'notes': self.notes,
            'summary': self.summary,
            'hooks_set': json.loads(self.hooks_set_json or '[]'),
            'events_extracted': json.loads(self.events_extracted_json or '[]'),
        }
        if include_content:
            d['content'] = self.content
        return d

class ChapterVersion(db.Model):
    __tablename__ = 'chapter_versions'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    chapter_id = db.Column(db.String(36), db.ForeignKey('chapters.id'), nullable=False)
    content = db.Column(db.Text, default='')
    version_num = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    summary = db.Column(db.String(200), default='')

    def to_dict(self):
        return {
            'id': self.id, 'chapter_id': self.chapter_id, 'version_num': self.version_num,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'summary': self.summary, 'content': self.content
        }

class Character(db.Model):
    __tablename__ = 'characters'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(50), default='supporting')  # protagonist, antagonist, supporting
    description = db.Column(db.Text, default='')
    appearance = db.Column(db.Text, default='')
    personality = db.Column(db.Text, default='')
    background = db.Column(db.Text, default='')
    relationships_json = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'name': self.name, 'role': self.role,
            'description': self.description, 'appearance': self.appearance,
            'personality': self.personality, 'background': self.background,
            'relationships': json.loads(self.relationships_json or '[]'),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class Outline(db.Model):
    __tablename__ = 'outlines'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False, default='')
    content = db.Column(db.Text, default='')
    order_index = db.Column(db.Integer, default=0)
    level = db.Column(db.Integer, default=0)  # 0=act, 1=chapter, 2=scene
    parent_id = db.Column(db.String(36), default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'title': self.title,
            'content': self.content, 'order_index': self.order_index, 'level': self.level,
            'parent_id': self.parent_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class DailyStats(db.Model):
    __tablename__ = 'daily_stats'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    words_written = db.Column(db.Integer, default=0)
    time_spent_minutes = db.Column(db.Integer, default=0)
    chapters_completed = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'date': self.date.isoformat(),
            'words_written': self.words_written, 'time_spent_minutes': self.time_spent_minutes,
            'chapters_completed': self.chapters_completed
        }

class Template(db.Model):
    __tablename__ = 'templates'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    genre = db.Column(db.String(50), default='other')
    book_type = db.Column(db.String(20), default='novel')
    structure_json = db.Column(db.Text, default='[]')
    prompts_json = db.Column(db.Text, default='{}')
    is_builtin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'description': self.description,
            'genre': self.genre, 'book_type': self.book_type,
            'structure': json.loads(self.structure_json or '[]'),
            'prompts': json.loads(self.prompts_json or '{}'),
            'is_builtin': self.is_builtin,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class AISession(db.Model):
    __tablename__ = 'ai_sessions'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)  # 多用户隔离：会话归属用户
    scope = db.Column(db.String(50), default='general')  # general, character, plot, chapter
    scope_id = db.Column(db.String(36), default='')
    title = db.Column(db.String(200), default='')
    messages_json = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'scope': self.scope,
            'scope_id': self.scope_id, 'title': self.title,
            'messages': json.loads(self.messages_json or '[]'),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class AIConfig(db.Model):
    """AI 配置：一行 = 一个提供商（provider 唯一）。

    - provider / base_url / api_key：一个提供商一份（同提供商多模型共用）
    - models：JSON 数组，用户在「拉取模型」后勾选保留的该提供商模型列表
    - model：当前使用的模型（智驾各Tab取这个；为空时回退 models[0]）
    """
    __tablename__ = 'ai_config'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)  # 多用户隔离：配置归属用户（None=共享兜底）
    name = db.Column(db.String(50), default='默认配置')  # 提供商展示名（如 DeepSeek）
    is_active = db.Column(db.Boolean, default=True, index=True)  # 是否激活（同时仅一个）
    provider = db.Column(db.String(50), default='deepseek')
    model = db.Column(db.String(100), default='deepseek-chat')  # 当前使用模型
    models = db.Column(db.Text, default='[]')  # JSON: 用户选定的该提供商模型ID列表
    recognition_model = db.Column(db.String(100), default='')  # AI识别专用模型，为空时使用model
    api_key = db.Column(db.String(200), default='')
    base_url = db.Column(db.String(300), default='https://api.deepseek.com')
    temperature = db.Column(db.Float, default=0.7)
    max_tokens = db.Column(db.Integer, default=4096)

    def get_models(self):
        """解析用户选定的模型列表；空时回退单个 model。"""
        try:
            raw = json.loads(self.models or '[]')
            if isinstance(raw, list):
                raw = [str(x) for x in raw if str(x).strip()]
        except Exception:
            raw = []
        # 老数据兜底：models 为空但有 model → 视为已选一个模型
        if not raw and self.model:
            raw = [self.model]
        return raw

    def to_dict(self):
        models = self.get_models()
        return {
            'id': self.id, 'name': self.name or '默认配置', 'is_active': self.is_active,
            'provider': self.provider, 'model': self.model or (models[0] if models else ''),
            'models': models,
            'recognition_model': self.recognition_model or '',
            'api_key': '***' if self.api_key else '', 'base_url': self.base_url,
            'temperature': self.temperature, 'max_tokens': self.max_tokens,
            'has_key': bool(self.api_key)
        }

    def get_model_for_task(self, task_type='creation'):
        """根据任务类型返回对应模型：recognition或creation。"""
        if task_type == 'recognition' and self.recognition_model:
            return self.recognition_model
        if self.model:
            return self.model
        models = self.get_models()
        return models[0] if models else ''

    @classmethod
    def get_active(cls, user_id=None):
        """返回激活配置。user_id 非空时优先取该用户的激活配置；
        无用户上下文（后台任务）时回退到共享配置（user_id IS NULL），保证兼容。"""
        q = cls.query
        if user_id:
            q = q.filter((cls.user_id == user_id) | (cls.user_id.is_(None)))
        cfg = q.filter_by(is_active=True).order_by(cls.user_id.is_(None).asc()).first()
        if cfg:
            return cfg
        cfg = q.order_by(cls.user_id.is_(None).asc(), cls.id.asc()).first()
        if cfg:
            cfg.is_active = True
            db.session.commit()
            return cfg
        cfg = cls(name='默认配置', is_active=True, models='["deepseek-chat"]', user_id=user_id)
        db.session.add(cfg)
        db.session.commit()
        return cfg


    @classmethod
    def get_by_id(cls, cfg_id):
        """P1-1 会话级切模型：按ID取指定配置（找不到返回None），绝不修改全局激活。"""
        if not cfg_id: return None
        try:
            return cls.query.filter_by(id=str(cfg_id)).first()
        except Exception:
            return None


class AIUsageLog(db.Model):
    """【AI调用账本】记录每次LLM调用的场景、模型、字数、Token、成败、完整输入输出文本，用于审计与成本统计。"""
    __tablename__ = 'ai_usage_logs'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=True, index=True)
    chapter_id = db.Column(db.String(36), db.ForeignKey('chapters.id'), nullable=True)
    scene = db.Column(db.String(64), default='', index=True)      # 细分场景（自动取调用函数名，可被 scene_label 覆盖）
    task_type = db.Column(db.String(32), default='creation')      # creation / recognition
    model = db.Column(db.String(100), default='')
    prompt_chars = db.Column(db.Integer, default=0)               # 输入字数
    output_chars = db.Column(db.Integer, default=0)               # 输出字数
    # ===== 2026-09-03 新增 Tokens & 原文 =====
    input_tokens = db.Column(db.Integer, default=0, server_default=db.text('0'))   # 供应商返回 prompt_tokens
    output_tokens = db.Column(db.Integer, default=0, server_default=db.text('0'))  # 供应商返回 completion_tokens
    total_tokens = db.Column(db.Integer, default=0, server_default=db.text('0'))   # 供应商返回 total_tokens
    prompt_text = db.Column(db.Text, default='', server_default=db.text("''"))     # 完整请求文本（截断 8k）
    response_text = db.Column(db.Text, default='', server_default=db.text("''"))   # 完整响应文本（截断 8k）
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text, default='')
    duration_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'chapter_id': self.chapter_id,
            'scene': self.scene, 'task_type': self.task_type, 'model': self.model,
            'prompt_chars': self.prompt_chars, 'output_chars': self.output_chars,
            'input_tokens': int(getattr(self, 'input_tokens', 0) or 0),
            'output_tokens': int(getattr(self, 'output_tokens', 0) or 0),
            'total_tokens': int(getattr(self, 'total_tokens', 0) or 0),
            'prompt_text': getattr(self, 'prompt_text', '') or '',
            'response_text': getattr(self, 'response_text', '') or '',
            'success': self.success, 'error_message': self.error_message,
            'duration_ms': self.duration_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AppPreference(db.Model):
    __tablename__ = 'app_preferences'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default='')

    @staticmethod
    def get(key, default=None):
        pref = AppPreference.query.filter_by(key=key).first()
        return pref.value if pref else default

    @staticmethod
    def set(key, value):
        pref = AppPreference.query.filter_by(key=key).first()
        if pref:
            pref.value = value
        else:
            db.session.add(AppPreference(key=key, value=value))
        db.session.commit()

class StageContent(db.Model):
    __tablename__ = 'stage_contents'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    stage_key = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (db.UniqueConstraint('book_id', 'stage_key'),)

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'stage_key': self.stage_key,
            'content': self.content,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class BookBible(db.Model):
    __tablename__ = 'book_bible'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False, unique=True)
    worldbuilding = db.Column(db.Text, default='')
    character_profiles = db.Column(db.Text, default='')
    timeline = db.Column(db.Text, default='')
    foreshadowing = db.Column(db.Text, default='')
    style_guide = db.Column(db.Text, default='')
    key_rules = db.Column(db.Text, default='')
    locations = db.Column(db.Text, default='')
    concept = db.Column(db.Text, default='')
    plot_design = db.Column(db.Text, default='')
    generated_summary = db.Column(db.Text, default='')
    # 关系图谱专用字段（与 character_profiles 解耦，避免互相覆盖导致角色丢失）
    relation_graph = db.Column(db.Text, default='')
    # 物资库：JSON 数组，按卷存储势力/角色的物品、功法、法宝、境界等
    inventory = db.Column(db.Text, default='')
    # 人物按卷：JSON 数组，每卷的人物档案
    character_volumes = db.Column(db.Text, default='')
    # 动态文件按卷：JSON 数组，每卷的动态分类摘要
    dynamic_volumes = db.Column(db.Text, default='')
    # 伏笔按卷：JSON 数组，每卷的伏笔识别数据
    foreshadowing_volumes = db.Column(db.Text, default='')
    # 地图按卷：JSON 数组，每卷的地点识别数据
    locations_volumes = db.Column(db.Text, default='')
    # 防遗忘检查报告历史：JSON 数组，每份报告含 id/title/checked_at/scope/volume_ids/report_json
    anti_forget_reports = db.Column(db.Text, default='')
    # P0-2：伏笔 DAG（JSON，结构化伏笔图，与 foreshadowing 文本字段并存）
    # 文本字段供前端展示，DAG 供后端状态追踪/prompt注入/逾期检测用
    foreshadowing_graph = db.Column(db.Text, default='')
    # P1-4：四级大纲层级（JSON，master→arc→section→chapter，由 timeline 自动构建）
    outline_hierarchy = db.Column(db.Text, default='')
    # P1-6：章级变更日志（JSON 数组，每章的 12 类 CHANGES delta，支持重写回滚）
    chapter_changes_log = db.Column(db.Text, default='')
    # 借鉴 PlotPilot 检查点快照：每5章自动备份 BookBible+DynamicMemory 关键字段，支持回滚
    state_snapshots = db.Column(db.Text, default='')
    # M1a: 全书事件日志（时间序列），每章写作后自动抽取事件追加
    event_log_json = db.Column(db.Text, default='')
    # M1b: 实体注册表（JSON），跨维度统一追踪人/地/物/势力/技能及其别名
    entity_registry_json = db.Column(db.Text, default='')
    # M4: 失败记录库（JSON），供 Meta-LLM 分析并优化 prompt
    failure_log_json = db.Column(db.Text, default='')
    # M4b: 用户已采纳的 prompt 补丁列表（JSON 数组），每条含 id/category/patch_text/handled_bucket_key/applied_at
    # 生成任何维度/章节时自动追加到 system prompt 末尾（tail-rule 之后）
    prompt_patches_json = db.Column(db.Text, default='')
    # M4c: 忽略的失败模式 bucket（JSON 数组），被忽略的 bucket 不出现在 optimization-report 里
    ignored_failure_buckets_json = db.Column(db.Text, default='')
    # 总卷数 + 风格流派（与 Book 表同步，创作时从 bible 直接读取注入各维度）
    # 0 = 未设定，与 Book 表口径一致；严禁默认 10（历史脏数据的来源之一）
    total_volumes = db.Column(db.Integer, default=0)
    novel_styles = db.Column(db.Text, default='[]')
    last_synced_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id, 'worldbuilding': self.worldbuilding,
            'character_profiles': self.character_profiles, 'timeline': self.timeline,
            'foreshadowing': self.foreshadowing, 'style_guide': self.style_guide,
            'key_rules': self.key_rules, 'locations': self.locations,
            'concept': self.concept, 'plot_design': self.plot_design,
            'generated_summary': self.generated_summary,
            'relation_graph': self.relation_graph,
            'inventory': self.inventory or '',
            'character_volumes': self.character_volumes or '',
            'dynamic_volumes': self.dynamic_volumes or '',
            'foreshadowing_volumes': self.foreshadowing_volumes or '',
            'locations_volumes': self.locations_volumes or '',
            'anti_forget_reports': self.anti_forget_reports or '',
            # P0-2/P1-4/P1-6 新增字段
            'foreshadowing_graph': self.foreshadowing_graph or '',
            'outline_hierarchy': self.outline_hierarchy or '',
            'chapter_changes_log': self.chapter_changes_log or '',
            'state_snapshots': self.state_snapshots or '',
            'event_log': json.loads(self.event_log_json or '[]'),
            'entity_registry': json.loads(self.entity_registry_json or '{}'),
            'failure_log': json.loads(self.failure_log_json or '[]'),
            'prompt_patches': json.loads(self.prompt_patches_json or '[]'),
            'ignored_failure_buckets': json.loads(self.ignored_failure_buckets_json or '[]'),
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            # P1-2修复：补 total_volumes / novel_styles，让前端可见 bible 权威值
            'total_volumes': self.total_volumes if self.total_volumes else 10,
            'novel_styles': self.novel_styles or '[]'
        }

class SkillPack(db.Model):
    __tablename__ = 'skill_packs'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    genre = db.Column(db.String(50), default='other')
    book_type = db.Column(db.String(20), default='short_story')
    stage_keys_json = db.Column(db.Text, default='[]')
    workflow_json = db.Column(db.Text, default='[]')
    prompts_json = db.Column(db.Text, default='{}')
    is_builtin = db.Column(db.Boolean, default=False)
    icon = db.Column(db.String(10), default='📦')
    github_source = db.Column(db.String(500), default='')  # GitHub 仓库地址，用于拉取更新
    github_synced_at = db.Column(db.DateTime, nullable=True)  # 上次同步时间
    # 技能包三类划分：master=构思类 / style=文风类 / review=审查类
    category = db.Column(db.String(20), default='master')
    # 文风类专属：题材目标（fantasy/urban/mystery/history...），构思/审查类为空
    genre_target = db.Column(db.String(50), default='')
    # 同类多包时的注入优先级（数字小的先注入），默认100
    priority = db.Column(db.Integer, default=100)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'description': self.description,
            'genre': self.genre, 'book_type': self.book_type,
            'stage_keys': json.loads(self.stage_keys_json or '[]'),
            'workflow': json.loads(self.workflow_json or '[]'),
            'prompts': json.loads(self.prompts_json or '{}'),
            'is_builtin': self.is_builtin, 'icon': self.icon,
            'github_source': self.github_source or '',
            'category': self.category or 'master',
            'genre_target': self.genre_target or '',
            'priority': self.priority if self.priority is not None else 100,
            'github_synced_at': self.github_synced_at.isoformat() if self.github_synced_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class DynamicMemory(db.Model):
    """动态文件库 - 长篇小说防遗忘系统（5文件版）"""
    __tablename__ = 'dynamic_memory'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False, unique=True)
    # 5个动态文件（JSON文本）
    narrative_engine = db.Column(db.Text, default='')       # 叙事引擎
    foreshadowing_tracker = db.Column(db.Text, default='')  # 伏笔追踪器
    character_ecosystem = db.Column(db.Text, default='')    # 角色生态系统
    ability_world = db.Column(db.Text, default='')          # 能力与世界观
    health_dashboard = db.Column(db.Text, default='')       # 健康度仪表盘
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    FILE_KEYS = ['narrative_engine', 'foreshadowing_tracker', 'character_ecosystem', 'ability_world', 'health_dashboard']

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id,
            'narrative_engine': self.narrative_engine or '',
            'foreshadowing_tracker': self.foreshadowing_tracker or '',
            'character_ecosystem': self.character_ecosystem or '',
            'ability_world': self.ability_world or '',
            'health_dashboard': self.health_dashboard or '',
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    @staticmethod
    def get_empty_template(file_key):
        """返回各文件的空模板"""
        templates = {
            'narrative_engine': json.dumps({
                'state': {
                    'book_name': '', 'current_chapter': 0, 'current_volume': 1,
                    'current_node': '', 'mc_status': '', 'recent_events': [],
                    'active_hooks': [], 'pending_foreshadowing': [], 'cost_points': 0, 'cost_threshold': 10
                },
                'timeline': [],
                'chapters': []
            }, ensure_ascii=False, indent=2),
            'foreshadowing_tracker': json.dumps({
                'foreshadowing': [],
                'scan_rules': {
                    'short_cycle': 20, 'mid_cycle': 75, 'long_cycle': 150,
                    'alert_levels': {'warning': '待回收', 'danger': '高危遗忘', 'critical': '核心悬念'}
                }
            }, ensure_ascii=False, indent=2),
            'character_ecosystem': json.dumps({
                'characters': [],
                'relationships': []
            }, ensure_ascii=False, indent=2),
            'ability_world': json.dumps({
                'ability_log': [],
                'world_facts': []
            }, ensure_ascii=False, indent=2),
            'health_dashboard': json.dumps({
                'thread_health': {
                    'main_line': {'last_chapter': 0, 'status': 'ok'},
                    'sub_line_a': {'last_chapter': 0, 'status': 'ok'},
                    'sub_line_b': {'last_chapter': 0, 'status': 'ok'},
                    'dark_line': {'last_chapter': 0, 'status': 'ok'}
                },
                'chapter_type_distribution': {},
                'foreshadowing_aging': {},
                'ai_flavor_trend': [],
                'dialogue_ratio_trend': [],
                'character_appearances': {},
                'alerts': []
            }, ensure_ascii=False, indent=2),
        }
        return templates.get(file_key, '{}')

class DynamicReport(db.Model):
    """动态文件报告 - 长篇小说防遗忘摘要（每5-10章自动生成）"""
    __tablename__ = 'dynamic_reports'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    book_id = db.Column(db.String(36), db.ForeignKey('books.id'), nullable=False)
    title = db.Column(db.String(200), default='')        # 如 "动态-(1-5章)"
    content = db.Column(db.Text, default='')             # 汇总报告，≤500字
    chapter_start = db.Column(db.Integer, default=0)     # 起始章号
    chapter_end = db.Column(db.Integer, default=0)       # 结束章号
    auto_generated = db.Column(db.Boolean, default=False) # 是否自动生成
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'book_id': self.book_id,
            'title': self.title, 'content': self.content,
            'chapter_start': self.chapter_start, 'chapter_end': self.chapter_end,
            'auto_generated': self.auto_generated,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

class PromptTemplate(db.Model):
    __tablename__ = 'prompt_templates'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(200), nullable=False)
    agent_id = db.Column(db.String(100), nullable=False)
    book_type = db.Column(db.String(20), default='short_story')
    genre = db.Column(db.String(50), default='other')
    content = db.Column(db.Text, default='')
    is_builtin = db.Column(db.Boolean, default=False)
    description = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'agent_id': self.agent_id,
            'book_type': self.book_type, 'genre': self.genre,
            'content': self.content, 'is_builtin': self.is_builtin,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class AppMeta(db.Model):
    """应用元数据 KV 表：记录 schema/seed 版本，支持启动快速路径。"""
    __tablename__ = 'app_meta'
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)
