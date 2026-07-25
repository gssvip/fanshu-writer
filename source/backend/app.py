import os
import json
import uuid
import shutil
import zipfile
import tempfile
import hashlib
import functools
import requests
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__, static_folder=None)
app.config['SECRET_KEY'] = 'fanshu-writer-secret-key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# 前端构建产物目录：优先使用环境变量，其次查找同级 frontend/dist 和上级 dist
FRONTEND_DIST = Path(os.environ.get('FANSHU_FRONTEND_DIST', Path(__file__).parent.parent / 'frontend' / 'dist'))

DATA_DIR = Path(os.environ.get('FANSHU_DATA_DIR', Path.home() / '.fanshu-writer'))
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATA_DIR}/fanshu.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": "*"}})
db = SQLAlchemy(app)

EXPORTS_DIR = DATA_DIR / 'exports'
EXPORTS_DIR.mkdir(exist_ok=True)
COVERS_DIR = DATA_DIR / 'covers'
COVERS_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = DATA_DIR / 'templates'
TEMPLATES_DIR.mkdir(exist_ok=True)


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(100), default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {'id': self.id, 'username': self.username, 'email': self.email,
                'created_at': self.created_at.isoformat() if self.created_at else None}


class AuthToken(db.Model):
    __tablename__ = 'auth_tokens'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


def generate_token():
    return hashlib.sha256(os.urandom(32)).hexdigest()

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            token = request.args.get('token', '')
        if not token:
            return jsonify({'error': '请先登录'}), 401
        at = AuthToken.query.filter_by(token=token).first()
        now = datetime.now(timezone.utc)
        if not at:
            return jsonify({'error': '登录已过期，请重新登录'}), 401
        exp = at.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            return jsonify({'error': '登录已过期，请重新登录'}), 401
        request.current_user_id = at.user_id
        return f(*args, **kwargs)
    return decorated

def optional_login(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        at = AuthToken.query.filter_by(token=token).first() if token else None
        now = datetime.now(timezone.utc)
        if at:
            exp = at.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            request.current_user_id = at.user_id if exp > now else None
        else:
            request.current_user_id = None
        return f(*args, **kwargs)
    return decorated


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

    versions = db.relationship('ChapterVersion', backref='chapter', lazy=True, cascade='all, delete-orphan', order_by='ChapterVersion.version_num.desc()')

    def to_dict(self, include_content=True):
        d = {
            'id': self.id, 'book_id': self.book_id, 'title': self.title,
            'order_index': self.order_index, 'word_count': self.word_count,
            'status': self.status, 'is_volume': self.is_volume, 'parent_id': self.parent_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'notes': self.notes
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
    __tablename__ = 'ai_config'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider = db.Column(db.String(50), default='deepseek')
    model = db.Column(db.String(100), default='deepseek-chat')
    recognition_model = db.Column(db.String(100), default='')  # AI识别专用模型，为空时使用model
    api_key = db.Column(db.String(200), default='')
    base_url = db.Column(db.String(300), default='https://api.deepseek.com')
    temperature = db.Column(db.Float, default=0.7)
    max_tokens = db.Column(db.Integer, default=4096)

    def to_dict(self):
        return {
            'id': self.id, 'provider': self.provider, 'model': self.model,
            'recognition_model': self.recognition_model or '',
            'api_key': '***' if self.api_key else '', 'base_url': self.base_url,
            'temperature': self.temperature, 'max_tokens': self.max_tokens,
            'has_key': bool(self.api_key)
        }

    def get_model_for_task(self, task_type='creation'):
        """根据任务类型返回对应模型：recognition或creation"""
        if task_type == 'recognition' and self.recognition_model:
            return self.recognition_model
        return self.model


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
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None
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
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'description': self.description,
            'genre': self.genre, 'book_type': self.book_type,
            'stage_keys': json.loads(self.stage_keys_json or '[]'),
            'workflow': json.loads(self.workflow_json or '[]'),
            'prompts': json.loads(self.prompts_json or '{}'),
            'is_builtin': self.is_builtin, 'icon': self.icon,
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


def count_words(text):
    """统计字数：中文按字符计数，英文按单词计数"""
    if not text:
        return 0
    import re
    # 移除空白字符
    cleaned = re.sub(r'\s+', '', text)
    # 统计中文字符数（含中文标点）
    cn_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', cleaned))
    # 统计英文单词数
    en_words = len(re.findall(r'[a-zA-Z]+', text))
    # 统计数字串
    numbers = len(re.findall(r'\d+', text))
    return cn_chars + en_words + numbers

def update_book_stats(book_id):
    book = Book.query.get(book_id)
    if not book:
        return
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    book.word_count = sum(c.word_count for c in chapters)
    book.chapter_count = len(chapters)
    book.updated_at = datetime.now(timezone.utc)
    db.session.commit()


def build_outline_tree(outlines):
    outline_map = {o.id: o.to_dict() for o in outlines}
    tree = []
    for o in outlines:
        node = outline_map[o.id]
        node['children'] = []
        if o.parent_id and o.parent_id in outline_map:
            outline_map[o.parent_id]['children'].append(node)
        elif not o.parent_id:
            tree.append(node)
    return tree


# ==== Auth API ====

@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request.json
    username = (data.get('username', '')).strip()
    password = (data.get('password', '')).strip()
    email = (data.get('email', '')).strip()
    if len(username) < 2 or len(username) > 30:
        return jsonify({'error': '用户名需2-30个字符'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4个字符'}), 400
    if not email:
        return jsonify({'error': '邮箱不能为空'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'error': '用户名已存在'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': '该邮箱已被注册'}), 409
    user = User(username=username, password_hash=generate_password_hash(password), email=email)
    db.session.add(user)
    db.session.commit()
    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.add(AuthToken(user_id=user.id, token=token, expires_at=expires))
    db.session.commit()
    return jsonify({'user': user.to_dict(), 'token': token}), 201

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    username = (data.get('username', '')).strip()
    password = (data.get('password', '')).strip()
    # 支持用户名或邮箱登录：先按邮箱查，查不到再按用户名查
    user = User.query.filter_by(email=username).first()
    if not user:
        user = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({'error': '用户名或密码错误'}), 401
    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    db.session.add(AuthToken(user_id=user.id, token=token, expires_at=expires))
    db.session.commit()
    return jsonify({'user': user.to_dict(), 'token': token})

@app.route('/api/auth/me', methods=['GET'])
@login_required
def get_me():
    user = User.query.get(request.current_user_id)
    return jsonify(user.to_dict() if user else {'error': 'User not found'})

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    AuthToken.query.filter_by(token=token).delete()
    db.session.commit()
    return jsonify({'success': True})


# ==== Books API ====

@app.route('/api/books', methods=['GET'])
@login_required
def list_books():
    books = Book.query.filter_by(user_id=request.current_user_id).order_by(Book.updated_at.desc()).all()
    return jsonify([b.to_dict() for b in books])

@app.route('/api/books/<book_id>', methods=['GET'])
def get_book(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    return jsonify(book.to_dict())

@app.route('/api/books', methods=['POST'])
@login_required
def create_book():
    data = request.json
    book = Book(
        user_id=request.current_user_id,
        title=data.get('title', '新书'),
        author=data.get('author', ''),
        genre=data.get('genre', 'other'),
        book_type=data.get('book_type', 'novel'),
        synopsis=data.get('synopsis', ''),
        template_id=data.get('template_id', ''),
        target_words=data.get('target_words', 0),
        status='draft'
    )
    db.session.add(book)
    db.session.flush()

    if data.get('template_id'):
        template = Template.query.get(data['template_id'])
        if template and template.structure_json:
            structure = json.loads(template.structure_json)
            for i, s in enumerate(structure):
                ch = Chapter(
                    book_id=book.id, title=s.get('title', f'章节{i+1}'),
                    order_index=i, is_volume=s.get('is_volume', False),
                    parent_id=s.get('parent_id', '')
                )
                db.session.add(ch)
    db.session.commit()
    return jsonify(book.to_dict()), 201

@app.route('/api/books/<book_id>', methods=['PUT'])
def update_book(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    data = request.json
    for field in ['title', 'author', 'genre', 'book_type', 'synopsis', 'status', 'target_words', 'metadata_json']:
        if field in data:
            if field == 'metadata_json' and isinstance(data[field], dict):
                setattr(book, field, json.dumps(data[field], ensure_ascii=False))
            else:
                setattr(book, field, data[field])
    db.session.commit()
    return jsonify(book.to_dict())

@app.route('/api/books/<book_id>', methods=['DELETE'])
def delete_book(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    db.session.delete(book)
    db.session.commit()
    return jsonify({'success': True})


# ==== Chapters API ====

@app.route('/api/books/<book_id>/chapters', methods=['GET'])
def list_chapters(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    return jsonify([c.to_dict(include_content=False) for c in chapters])

@app.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['GET'])
def get_chapter(book_id, chapter_id):
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    return jsonify(ch.to_dict(include_content=True))

@app.route('/api/books/<book_id>/chapters', methods=['POST'])
def create_chapter(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    data = request.json
    max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
    ch = Chapter(
        book_id=book_id, title=data.get('title', '新章节'),
        content=data.get('content', ''), order_index=data.get('order_index', max_order + 1),
        is_volume=data.get('is_volume', False), parent_id=data.get('parent_id', ''),
        notes=data.get('notes', '')
    )
    if ch.content:
        ch.word_count = count_words(ch.content)
    db.session.add(ch)
    db.session.flush()
    update_book_stats(book_id)

    # 自动检测是否需要生成动态报告（每5章触发）
    auto_report = None
    if not ch.is_volume and ch.content:
        try:
            result = _check_and_auto_generate_report(book_id)
            if result and 'report' in result:
                auto_report = result['report']
        except Exception:
            pass  # 自动生成失败不影响章节创建

    resp = ch.to_dict(include_content=True)
    if auto_report:
        resp['auto_report'] = auto_report
    return jsonify(resp), 201

@app.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['PUT'])
def update_chapter(book_id, chapter_id):
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    old_content = ch.content
    data = request.json

    has_content_change = 'content' in data and data['content'] != old_content
    if has_content_change:
        versions = ChapterVersion.query.filter_by(chapter_id=chapter_id).order_by(ChapterVersion.version_num.desc()).all()
        new_ver = (versions[0].version_num + 1) if versions else 1
        ver = ChapterVersion(chapter_id=chapter_id, content=old_content, version_num=new_ver)
        db.session.add(ver)

    for field in ['title', 'content', 'order_index', 'status', 'is_volume', 'parent_id', 'notes']:
        if field in data:
            setattr(ch, field, data[field])
    if 'content' in data:
        ch.word_count = count_words(data['content'])
    ch.updated_at = datetime.now(timezone.utc)
    db.session.flush()
    update_book_stats(book_id)

    # 自动检测是否需要生成动态报告（每5章触发，仅内容变更时）
    auto_report = None
    if has_content_change and not ch.is_volume and ch.content:
        try:
            result = _check_and_auto_generate_report(book_id)
            if result and 'report' in result:
                auto_report = result['report']
        except Exception:
            pass

    resp = ch.to_dict(include_content=True)
    if auto_report:
        resp['auto_report'] = auto_report
    return jsonify(resp)

@app.route('/api/books/<book_id>/chapters/<chapter_id>', methods=['DELETE'])
def delete_chapter(book_id, chapter_id):
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    if not ch:
        return jsonify({'error': 'Chapter not found'}), 404
    db.session.delete(ch)
    db.session.flush()
    update_book_stats(book_id)
    return jsonify({'success': True})

@app.route('/api/books/<book_id>/chapters/reorder', methods=['POST'])
def reorder_chapters(book_id):
    data = request.json
    order = data.get('order', [])
    for item in order:
        Chapter.query.filter_by(id=item['id'], book_id=book_id).update({'order_index': item['order_index']})
    db.session.commit()
    return jsonify({'success': True})


# ==== Chapter Versions ====

@app.route('/api/books/<book_id>/chapters/<chapter_id>/versions', methods=['GET'])
def list_chapter_versions(book_id, chapter_id):
    versions = ChapterVersion.query.filter_by(chapter_id=chapter_id).order_by(ChapterVersion.version_num.desc()).all()
    return jsonify([v.to_dict() for v in versions])

@app.route('/api/books/<book_id>/chapters/<chapter_id>/versions/<version_id>/restore', methods=['POST'])
def restore_chapter_version(book_id, chapter_id, version_id):
    ch = Chapter.query.filter_by(id=chapter_id, book_id=book_id).first()
    ver = ChapterVersion.query.filter_by(id=version_id, chapter_id=chapter_id).first()
    if not ch or not ver:
        return jsonify({'error': 'Not found'}), 404
    ch.content = ver.content
    ch.word_count = count_words(ver.content)
    ch.updated_at = datetime.now(timezone.utc)
    db.session.flush()
    update_book_stats(book_id)
    return jsonify(ch.to_dict(include_content=True))


# ==== Characters API ====

@app.route('/api/books/<book_id>/characters', methods=['GET'])
def list_characters(book_id):
    chars = Character.query.filter_by(book_id=book_id).all()
    return jsonify([c.to_dict() for c in chars])

@app.route('/api/books/<book_id>/characters', methods=['POST'])
def create_character(book_id):
    data = request.json
    char = Character(
        book_id=book_id, name=data.get('name', '新角色'),
        role=data.get('role', 'supporting'), description=data.get('description', ''),
        appearance=data.get('appearance', ''), personality=data.get('personality', ''),
        background=data.get('background', '')
    )
    if 'relationships' in data:
        char.relationships_json = json.dumps(data['relationships'], ensure_ascii=False)
    db.session.add(char)
    db.session.commit()
    return jsonify(char.to_dict()), 201

@app.route('/api/books/<book_id>/characters/<char_id>', methods=['PUT'])
def update_character(book_id, char_id):
    char = Character.query.filter_by(id=char_id, book_id=book_id).first()
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    data = request.json
    for field in ['name', 'role', 'description', 'appearance', 'personality', 'background']:
        if field in data:
            setattr(char, field, data[field])
    if 'relationships' in data:
        char.relationships_json = json.dumps(data['relationships'], ensure_ascii=False)
    db.session.commit()
    return jsonify(char.to_dict())

@app.route('/api/books/<book_id>/characters/<char_id>', methods=['DELETE'])
def delete_character(book_id, char_id):
    char = Character.query.filter_by(id=char_id, book_id=book_id).first()
    if not char:
        return jsonify({'error': 'Character not found'}), 404
    db.session.delete(char)
    db.session.commit()
    return jsonify({'success': True})


# ==== Outlines API ====

@app.route('/api/books/<book_id>/outlines', methods=['GET'])
def list_outlines(book_id):
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    tree = build_outline_tree(outlines)
    return jsonify({'flat': [o.to_dict() for o in outlines], 'tree': tree})

@app.route('/api/books/<book_id>/outlines', methods=['POST'])
def create_outline(book_id):
    data = request.json
    max_order = db.session.query(db.func.max(Outline.order_index)).filter_by(book_id=book_id).scalar() or -1
    outline = Outline(
        book_id=book_id, title=data.get('title', '新节点'),
        content=data.get('content', ''), order_index=data.get('order_index', max_order + 1),
        level=data.get('level', 0), parent_id=data.get('parent_id', '')
    )
    db.session.add(outline)
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'item': outline.to_dict(), 'tree': build_outline_tree(outlines)}), 201

@app.route('/api/books/<book_id>/outlines/<outline_id>', methods=['PUT'])
def update_outline(book_id, outline_id):
    outline = Outline.query.filter_by(id=outline_id, book_id=book_id).first()
    if not outline:
        return jsonify({'error': 'Outline not found'}), 404
    data = request.json
    for field in ['title', 'content', 'order_index', 'level', 'parent_id']:
        if field in data:
            setattr(outline, field, data[field])
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'item': outline.to_dict(), 'tree': build_outline_tree(outlines)})

@app.route('/api/books/<book_id>/outlines/<outline_id>', methods=['DELETE'])
def delete_outline(book_id, outline_id):
    outline = Outline.query.filter_by(id=outline_id, book_id=book_id).first()
    if not outline:
        return jsonify({'error': 'Outline not found'}), 404
    db.session.delete(outline)
    db.session.commit()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()
    return jsonify({'tree': build_outline_tree(outlines)})


# ==== Stats API ====

@app.route('/api/books/<book_id>/stats', methods=['GET'])
def get_book_stats(book_id):
    stats = DailyStats.query.filter_by(book_id=book_id).order_by(DailyStats.date).all()
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    return jsonify({
        'daily': [s.to_dict() for s in stats],
        'chapters': [{'title': c.title, 'word_count': c.word_count, 'date': c.created_at.isoformat() if c.created_at else None} for c in chapters]
    })

@app.route('/api/books/<book_id>/stats', methods=['POST'])
def add_daily_stats(book_id):
    data = request.json
    today = datetime.now(timezone.utc).date()
    stat = DailyStats.query.filter_by(book_id=book_id, date=today).first()
    if stat:
        stat.words_written += data.get('words_written', 0)
        stat.time_spent_minutes += data.get('time_spent_minutes', 0)
        stat.chapters_completed += data.get('chapters_completed', 0)
        prev_words = stat.words_written
    else:
        stat = DailyStats(
            book_id=book_id, date=today,
            words_written=data.get('words_written', 0),
            time_spent_minutes=data.get('time_spent_minutes', 0),
            chapters_completed=data.get('chapters_completed', 0)
        )
        db.session.add(stat)
        prev_words = 0
    db.session.commit()
    return jsonify(stat.to_dict())


# ==== Templates API ====

def seed_builtin_templates():
    if Template.query.filter_by(is_builtin=True).first():
        return
    builtins = [
        {
            'name': '经典三幕式', 'description': '适合长篇小说，采用开端-发展-结局三幕结构', 'genre': 'other', 'book_type': 'novel',
            'structure': [
                {'title': '第一幕：开端', 'is_volume': True, 'parent_id': ''},
                {'title': '第一章 平凡世界', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二章 冒险召唤', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第三章 拒绝召唤', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二幕：发展', 'is_volume': True, 'parent_id': ''},
                {'title': '第四章 跨越门槛', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第五章 试炼之路', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第六章 中点转折', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第七章 危机降临', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第三幕：结局', 'is_volume': True, 'parent_id': ''},
                {'title': '第八章 高潮对决', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '第九章 回归之路', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '第十章 新的平衡', 'is_volume': False, 'parent_id': 'v3'},
            ]
        },
        {
            'name': '短篇小说模板', 'description': '简洁的四段式结构，适合万字以内短篇', 'genre': 'other', 'book_type': 'short_story',
            'structure': [
                {'title': '开篇：引入', 'is_volume': False},
                {'title': '发展：冲突', 'is_volume': False},
                {'title': '高潮：转折', 'is_volume': False},
                {'title': '结尾：余韵', 'is_volume': False},
            ]
        },
        {
            'name': '都市言情模板', 'description': '现代都市爱情故事专用模板', 'genre': 'romance', 'book_type': 'novel',
            'structure': [
                {'title': '第一卷：相遇', 'is_volume': True, 'parent_id': ''},
                {'title': '第一章 意外邂逅', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二章 命运交错', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第三章 心动暗生', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二卷：相知', 'is_volume': True, 'parent_id': ''},
                {'title': '第四章 甜蜜日常', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第五章 误会波折', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第六章 真心考验', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第三卷：相守', 'is_volume': True, 'parent_id': ''},
                {'title': '第七章 破镜重圆', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '第八章 携手未来', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '终章 此生不换', 'is_volume': False, 'parent_id': 'v3'},
            ]
        },
        {
            'name': '玄幻修仙模板', 'description': '玄幻修真小说的标准成长进阶结构', 'genre': 'fantasy', 'book_type': 'novel',
            'structure': [
                {'title': '第一卷：凡尘', 'is_volume': True, 'parent_id': ''},
                {'title': '第一章 少年崛起', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二章 初入宗门', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第三章 秘境试炼', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二卷：问道', 'is_volume': True, 'parent_id': ''},
                {'title': '第四章 宗门大比', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第五章 天劫淬体', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第六章 纵横四海', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第三卷：飞升', 'is_volume': True, 'parent_id': ''},
                {'title': '第七章 宿命之战', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '第八章 问道长生', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '终章 飞升仙界', 'is_volume': False, 'parent_id': 'v3'},
            ]
        },
        {
            'name': '悬疑推理模板', 'description': '层层递进的悬疑推理小说结构', 'genre': 'mystery', 'book_type': 'novel',
            'structure': [
                {'title': '第一卷：迷雾', 'is_volume': True, 'parent_id': ''},
                {'title': '第一章 案件发生', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二章 线索初现', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第三章 误入歧途', 'is_volume': False, 'parent_id': 'v1'},
                {'title': '第二卷：追逐', 'is_volume': True, 'parent_id': ''},
                {'title': '第四章 新线索', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第五章 逼近真相', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第六章 惊天逆转', 'is_volume': False, 'parent_id': 'v2'},
                {'title': '第三卷：真相', 'is_volume': True, 'parent_id': ''},
                {'title': '第七章 凶手现身', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '第八章 真相大白', 'is_volume': False, 'parent_id': 'v3'},
                {'title': '尾声 尘埃落定', 'is_volume': False, 'parent_id': 'v3'},
            ]
        },
    ]
    for t in builtins:
        s = t['structure']
        for i, item in enumerate(s):
            item['order_index'] = i
        template = Template(
            name=t['name'], description=t['description'], genre=t['genre'],
            book_type=t['book_type'], structure_json=json.dumps(s, ensure_ascii=False),
            is_builtin=True
        )
        db.session.add(template)
    db.session.commit()

@app.route('/api/templates', methods=['GET'])
def list_templates():
    templates = Template.query.order_by(Template.is_builtin.desc(), Template.created_at.desc()).all()
    return jsonify([t.to_dict() for t in templates])

@app.route('/api/templates', methods=['POST'])
def create_template():
    data = request.json
    template = Template(
        name=data.get('name', '自定义模板'), description=data.get('description', ''),
        genre=data.get('genre', 'other'), book_type=data.get('book_type', 'novel'),
        structure_json=json.dumps(data.get('structure', []), ensure_ascii=False),
        prompts_json=json.dumps(data.get('prompts', {}), ensure_ascii=False)
    )
    db.session.add(template)
    db.session.commit()
    return jsonify(template.to_dict()), 201


# ==== AI API ====

@app.route('/api/ai/config', methods=['GET'])
def get_ai_config():
    cfg = AIConfig.query.first()
    if not cfg:
        cfg = AIConfig()
        db.session.add(cfg)
        db.session.commit()
    return jsonify(cfg.to_dict())

@app.route('/api/ai/config', methods=['PUT'])
def update_ai_config():
    data = request.json
    cfg = AIConfig.query.first()
    if not cfg:
        cfg = AIConfig()
        db.session.add(cfg)
    for field in ['provider', 'model', 'recognition_model', 'base_url', 'temperature', 'max_tokens']:
        if field in data:
            setattr(cfg, field, data[field])
    if 'api_key' in data and data['api_key'] and data['api_key'] != '***':
        cfg.api_key = data['api_key']
    db.session.commit()
    return jsonify(cfg.to_dict())


def _do_fetch_models(base_url, api_key):
    """实际拉取模型列表的内部函数，供多个接口复用"""
    import requests as req
    base = base_url.rstrip('/')
    # 如果地址已经以 /v1 结尾就不重复添加
    if not base.endswith('/v1'):
        base += '/v1'
    resp = req.get(
        f"{base}/models",
        headers={'Authorization': f'Bearer {api_key}'},
        timeout=15
    )
    if resp.status_code != 200:
        return None, f'请求失败 (HTTP {resp.status_code})：{resp.text[:200]}', 400
    result = resp.json()
    models = []
    for m in result.get('data', []):
        mid = m.get('id', '')
        if mid:
            models.append({
                'id': mid,
                'owned_by': m.get('owned_by', ''),
            })
    models.sort(key=lambda x: x['id'])
    return models, None, 200


def _do_test_connection(base_url, api_key, model):
    """实际测试连接的内部函数，供多个接口复用"""
    import requests as req
    base = base_url.rstrip('/')
    if not base.endswith('/v1'):
        base += '/v1'
    resp = req.post(
        f"{base}/chat/completions",
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={
            'model': model,
            'messages': [{'role': 'user', 'content': '你好，请回复"连接成功"四个字。'}],
            'max_tokens': 20,
            'temperature': 0.1,
            'stream': False
        },
        timeout=30
    )
    if resp.status_code != 200:
        try:
            err_data = resp.json()
            err_msg = err_data.get('error', {}).get('message', '') or str(err_data)
        except Exception:
            err_msg = resp.text[:300]
        return None, f'连接失败 (HTTP {resp.status_code})：{err_msg}', 400
    result = resp.json()
    if 'choices' in result and len(result['choices']) > 0:
        reply = result['choices'][0]['message']['content']
        usage = result.get('usage', {})
        return {
            'success': True,
            'reply': reply,
            'model': result.get('model', model),
            'usage': usage
        }, None, 200
    else:
        return None, f'返回数据异常：{str(result)[:200]}', 400


@app.route('/api/ai/models', methods=['POST'])
def fetch_ai_models():
    """根据 base_url 和 api_key 拉取可用模型列表（OpenAI 兼容的 /v1/models 接口）"""
    data = request.json or {}
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()

    # 如果 api_key 是掩码或为空，尝试使用已保存的配置
    if api_key == '***' or not api_key:
        cfg = AIConfig.query.first()
        if cfg and cfg.api_key:
            api_key = cfg.api_key
            if not base_url:
                base_url = cfg.base_url or ''
        else:
            return jsonify({'error': '请先填写 API Key 或保存配置'}), 400

    if not base_url:
        return jsonify({'error': '请填写 API 地址'}), 400

    try:
        models, err, code = _do_fetch_models(base_url, api_key)
        if err:
            return jsonify({'error': err}), code
        return jsonify({'models': models})
    except requests.exceptions.ConnectionError:
        return jsonify({'error': '无法连接到服务器，请检查 API 地址是否正确'}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': '请求超时，请稍后重试'}), 400
    except Exception as e:
        return jsonify({'error': f'拉取模型失败：{str(e)}'}), 500


@app.route('/api/ai/test', methods=['POST'])
def test_ai_connection():
    """测试 AI 连接：发送一条简单消息验证配置是否可用"""
    data = request.json or {}
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    model = (data.get('model') or '').strip()

    # 如果 api_key 是掩码或为空，尝试使用已保存的配置
    if api_key == '***' or not api_key:
        cfg = AIConfig.query.first()
        if cfg and cfg.api_key:
            api_key = cfg.api_key
            if not base_url:
                base_url = cfg.base_url or ''
            if not model:
                model = cfg.model or ''
        else:
            return jsonify({'error': '请先填写 API Key 或保存配置'}), 400

    if not base_url or not api_key or not model:
        return jsonify({'error': '请填写完整的 API 地址、API Key 和模型名称'}), 400

    try:
        result, err, code = _do_test_connection(base_url, api_key, model)
        if err:
            return jsonify({'error': err}), code
        return jsonify(result)
    except requests.exceptions.ConnectionError:
        return jsonify({'error': '无法连接到服务器，请检查 API 地址'}), 400
    except requests.exceptions.Timeout:
        return jsonify({'error': '请求超时，请检查网络或稍后重试'}), 400
    except Exception as e:
        return jsonify({'error': f'测试失败：{str(e)}'}), 500

@app.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    data = request.json
    messages = data.get('messages', [])
    if not messages:
        return jsonify({'error': 'No messages'}), 400

    cfg = AIConfig.query.first()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    try:
        import requests as req
        base = cfg.base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = req.post(
            f"{base}/chat/completions",
            headers={
                'Authorization': f'Bearer {cfg.api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'model': cfg.model,
                'messages': messages,
                'temperature': cfg.temperature,
                'max_tokens': cfg.max_tokens,
                'stream': False
            },
            timeout=120
        )
        result = resp.json()
        if 'choices' in result and len(result['choices']) > 0:
            return jsonify({
                'content': result['choices'][0]['message']['content'],
                'usage': result.get('usage', {})
            })
        else:
            return jsonify({'error': str(result)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ai/chat/stream', methods=['POST'])
def ai_chat_stream():
    data = request.json
    messages = data.get('messages', [])

    cfg = AIConfig.query.first()
    if not cfg or not cfg.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    def generate():
        try:
            import requests as req
            base = cfg.base_url.rstrip('/')
            if not base.endswith('/v1'):
                base += '/v1'
            resp = req.post(
                f"{base}/chat/completions",
                headers={
                    'Authorization': f'Bearer {cfg.api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': cfg.model,
                    'messages': messages,
                    'temperature': cfg.temperature,
                    'max_tokens': cfg.max_tokens,
                    'stream': True
                },
                stream=True,
                timeout=120
            )
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        chunk = line[6:]
                        if chunk == '[DONE]':
                            yield 'data: [DONE]\n\n'
                            break
                        yield f'data: {chunk}\n\n'
        except Exception as e:
            yield f'data: {{"error": "{str(e)}"}}\n\n'

    return app.response_class(generate(), mimetype='text/event-stream')


# ==== AI Sessions ====

@app.route('/api/ai/sessions', methods=['GET'])
def list_ai_sessions():
    book_id = request.args.get('book_id')
    scope = request.args.get('scope')
    query = AISession.query
    if book_id:
        query = query.filter_by(book_id=book_id)
    if scope:
        query = query.filter_by(scope=scope)
    sessions = query.order_by(AISession.updated_at.desc()).all()
    return jsonify([s.to_dict() for s in sessions])

@app.route('/api/ai/sessions', methods=['POST'])
def create_ai_session():
    data = request.json
    session = AISession(
        book_id=data.get('book_id', ''),
        scope=data.get('scope', 'general'),
        scope_id=data.get('scope_id', ''),
        title=data.get('title', '新对话')
    )
    db.session.add(session)
    db.session.commit()
    return jsonify(session.to_dict()), 201

@app.route('/api/ai/sessions/<session_id>', methods=['PUT'])
def update_ai_session(session_id):
    session = AISession.query.get(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    data = request.json
    if 'messages' in data:
        session.messages_json = json.dumps(data['messages'], ensure_ascii=False)
    if 'title' in data:
        session.title = data['title']
    session.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(session.to_dict())

@app.route('/api/ai/sessions/<session_id>', methods=['DELETE'])
def delete_ai_session(session_id):
    session = AISession.query.get(session_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    db.session.delete(session)
    db.session.commit()
    return jsonify({'success': True})


# ==== Export API ====

@app.route('/api/books/<book_id>/export', methods=['GET'])
def export_book(book_id):
    fmt = request.args.get('format', 'txt')
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    if fmt == 'txt':
        output = f'《{book.title}》\n作者：{book.author}\n\n简介：{book.synopsis}\n\n{"="*50}\n\n'
        for ch in chapters:
            output += f'\n## {ch.title}\n\n{ch.content}\n\n{"="*50}\n'
        buf = BytesIO(output.encode('utf-8'))
        return send_file(buf, mimetype='text/plain', as_attachment=True, download_name=f'{book.title}.txt')

    elif fmt == 'docx':
        try:
            from docx import Document
            from docx.shared import Pt, Inches, RGBColor
            from docx.enum.text import WD_ALIGN_PARAGRAPH

            doc = Document()
            style = doc.styles['Normal']
            font = style.font
            font.name = 'SimSun'
            font.size = Pt(12)

            title_para = doc.add_heading(book.title, level=0)
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

            doc.add_paragraph(f'作者：{book.author}')
            if book.synopsis:
                doc.add_paragraph(f'简介：{book.synopsis}')
            doc.add_page_break()

            for ch in chapters:
                doc.add_heading(ch.title, level=1)
                for para_text in ch.content.split('\n'):
                    if para_text.strip():
                        doc.add_paragraph(para_text.strip())
                doc.add_page_break()

            buf = BytesIO()
            doc.save(buf)
            buf.seek(0)
            return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                             as_attachment=True, download_name=f'{book.title}.docx')
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    elif fmt == 'epub':
        try:
            from ebooklib import epub

            epub_book = epub.EpubBook()
            epub_book.set_identifier(book.id)
            epub_book.set_title(book.title)
            epub_book.set_language('zh')
            epub_book.add_author(book.author or '佚名')

            epub_chapters = []
            for ch in chapters:
                c = epub.EpubHtml(title=ch.title, file_name=f'ch_{ch.order_index}.xhtml', lang='zh')
                c.content = f'<h1>{ch.title}</h1>' + ''.join(f'<p>{p}</p>' for p in ch.content.split('\n') if p.strip())
                epub_book.add_item(c)
                epub_chapters.append(c)

            epub_book.toc = epub_chapters
            epub_book.add_item(epub.EpubNcx())
            epub_book.add_item(epub.EpubNav())

            style = 'BODY {color: black;}'
            nav_css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css", content=style)
            epub_book.add_item(nav_css)

            epub_book.spine = ['nav'] + epub_chapters

            buf = BytesIO()
            epub.write_epub(buf, epub_book, {})
            buf.seek(0)
            return send_file(buf, mimetype='application/epub+zip', as_attachment=True, download_name=f'{book.title}.epub')
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'Unsupported format'}), 400


# ==== Cover Upload ====

@app.route('/api/books/<book_id>/cover', methods=['POST'])
def upload_cover(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No filename'}), 400
    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        return jsonify({'error': 'Invalid image format'}), 400

    filename = f'{book_id}.{ext}'
    filepath = COVERS_DIR / filename
    file.save(filepath)
    book.cover_path = f'/api/covers/{filename}'
    db.session.commit()
    return jsonify({'cover_path': book.cover_path})

@app.route('/api/covers/<filename>')
def serve_cover(filename):
    return send_file(COVERS_DIR / filename)


# ==== Preferences ====

@app.route('/api/preferences', methods=['GET'])
def get_preferences():
    keys = request.args.get('keys', '')
    if keys:
        result = {}
        for k in keys.split(','):
            result[k] = AppPreference.get(k, '')
        return jsonify(result)
    prefs = AppPreference.query.all()
    return jsonify({p.key: p.value for p in prefs})

@app.route('/api/preferences', methods=['PUT'])
def set_preferences():
    data = request.json
    for k, v in data.items():
        AppPreference.set(k, str(v))
    return jsonify({'success': True})


# ==== Import/Export ZIP ====

@app.route('/api/books/<book_id>/export-zip', methods=['GET'])
def export_book_zip(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    tmpdir = tempfile.mkdtemp()
    try:
        metadata = book.to_dict()
        metadata['chapters'] = [c.to_dict(include_content=True) for c in Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()]
        metadata['characters'] = [c.to_dict() for c in Character.query.filter_by(book_id=book_id).all()]
        metadata['outlines'] = [o.to_dict() for o in Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()]

        with open(os.path.join(tmpdir, 'book.json'), 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        zippath = os.path.join(tmpdir, f'{book.title}.zip')
        with zipfile.ZipFile(zippath, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(os.path.join(tmpdir, 'book.json'), 'book.json')
            if book.cover_path:
                cover_file = COVERS_DIR / os.path.basename(book.cover_path)
                if cover_file.exists():
                    zf.write(cover_file, f'cover{cover_file.suffix}')

        return send_file(zippath, mimetype='application/zip', as_attachment=True, download_name=f'{book.title}.zip')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route('/api/books/<book_id>/export-full', methods=['GET'])
@login_required
def export_book_full(book_id):
    """导出小说的全部维度内容（除图谱外）和所有章节为独立文件，打包成zip下载"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    # 以小说标题命名文件夹，简单清理会影响路径的字符
    book_title = (book.title or '未命名').replace('/', '_').replace('\\', '_').replace(':', '_')

    bible = BookBible.query.filter_by(book_id=book_id).first()
    bible_data = bible.to_dict() if bible else {}

    # 维度配置：(文件名, BookBible字段, 中文标题)
    dimensions = [
        ('构思.md', 'concept', '构思'),
        ('设定.md', 'key_rules', '设定'),
        ('大纲.md', 'plot_design', '大纲'),
        ('世界观.md', 'worldbuilding', '世界观'),
        ('人物.md', 'character_profiles', '人物'),
        ('剧情时间线.md', 'timeline', '剧情时间线'),
        ('伏笔.md', 'foreshadowing', '伏笔'),
        ('地点.md', 'locations', '地点'),
        ('风格指南.md', 'style_guide', '风格指南'),
    ]

    # 章节按 order_index 排序，跳过卷标记
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 写入各维度 md 文件
        for filename, field, title in dimensions:
            content = (bible_data.get(field, '') or '').strip()
            body = content if content else '暂无内容'
            md_text = f'# {title}\n\n---\n\n{body}\n'
            zf.writestr(f'{book_title}/{filename}', md_text.encode('utf-8'))

        # 写入章节子文件夹，每章一个 txt
        for idx, ch in enumerate(chapters, start=1):
            ch_title = ch.title or f'第{idx}章'
            ch_filename = f'第{idx}章-{ch_title}.txt'
            ch_content = ch.content if (ch.content and ch.content.strip()) else '暂无内容'
            zf.writestr(f'{book_title}/章节/{ch_filename}', ch_content.encode('utf-8'))

    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=f'{book_title}.zip')


@app.route('/api/books/import-zip', methods=['POST'])
def import_book_zip():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    tmpdir = tempfile.mkdtemp()
    try:
        zippath = os.path.join(tmpdir, 'import.zip')
        file.save(zippath)
        with zipfile.ZipFile(zippath, 'r') as zf:
            zf.extractall(tmpdir)

        book_json = os.path.join(tmpdir, 'book.json')
        if not os.path.exists(book_json):
            return jsonify({'error': 'Invalid book package: no book.json'}), 400

        with open(book_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

        book = Book(
            title=data.get('title', '导入书籍'), author=data.get('author', ''),
            genre=data.get('genre', 'other'), book_type=data.get('book_type', 'novel'),
            synopsis=data.get('synopsis', ''), status='draft'
        )
        db.session.add(book)
        db.session.flush()

        for ch_data in data.get('chapters', []):
            ch = Chapter(
                book_id=book.id, title=ch_data.get('title', ''),
                content=ch_data.get('content', ''), order_index=ch_data.get('order_index', 0),
                is_volume=ch_data.get('is_volume', False), parent_id=ch_data.get('parent_id', ''),
                word_count=count_words(ch_data.get('content', ''))
            )
            db.session.add(ch)

        for char_data in data.get('characters', []):
            char = Character(
                book_id=book.id, name=char_data.get('name', ''),
                role=char_data.get('role', 'supporting'), description=char_data.get('description', ''),
                appearance=char_data.get('appearance', ''), personality=char_data.get('personality', ''),
                background=char_data.get('background', ''),
                relationships_json=json.dumps(char_data.get('relationships', []), ensure_ascii=False)
            )
            db.session.add(char)

        for o_data in data.get('outlines', []):
            outline = Outline(
                book_id=book.id, title=o_data.get('title', ''),
                content=o_data.get('content', ''), order_index=o_data.get('order_index', 0),
                level=o_data.get('level', 0), parent_id=o_data.get('parent_id', '')
            )
            db.session.add(outline)

        update_book_stats(book.id)
        return jsonify(book.to_dict()), 201
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def split_into_chapters(text):
    """将纯文本按章节标记拆分为多个章节，支持多种章节标题格式"""
    import re

    def _extract(matches, text, strip_prefix=''):
        """从正则匹配列表中提取章节"""
        chapters = []
        for i, m in enumerate(matches):
            title = m.group().strip()
            if strip_prefix:
                title = title.replace(strip_prefix, '').strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            chapters.append({'title': title[:100] or f'第{i + 1}章', 'content': content})
        return chapters

    # 1. 中文章节标题：第一章、第1章、第001章、第一回 等
    cn_pattern = re.compile(r'^[ \t]*第[零一二三四五六七八九十百千万\d]+[章节回卷][ \t]*.*$', re.MULTILINE)
    cn_matches = list(cn_pattern.finditer(text))
    if len(cn_matches) >= 1:
        return _extract(cn_matches, text)

    # 2. Markdown 标题 # xxx 或 ## xxx
    md_pattern = re.compile(r'^#{1,3}[ \t]+.+$', re.MULTILINE)
    md_matches = list(md_pattern.finditer(text))
    if len(md_matches) >= 1:
        return _extract(md_matches, text, strip_prefix='#')

    # 3. 英文 Chapter N / CHAPTER N
    en_pattern = re.compile(r'^[ \t]*[Cc][Hh][Aa][Pp][Tt][Ee][Rr][ \t]*\d+[ \t]*.*$', re.MULTILINE)
    en_matches = list(en_pattern.finditer(text))
    if len(en_matches) >= 1:
        return _extract(en_matches, text)

    # 4. 纯数字开头：1. xxx / 1、xxx / 1: xxx（至少2个才拆分，避免误拆）
    num_pattern = re.compile(r'^[ \t]*\d{1,4}[.、:][ \t]*\S.+$', re.MULTILINE)
    num_matches = list(num_pattern.finditer(text))
    if len(num_matches) >= 2:
        return _extract(num_matches, text)

    # 5. 【 xxx 】 或 『 xxx 』 格式标题
    bracket_pattern = re.compile(r'^[ \t]*[【『][^】』]+[】』][ \t]*.*$', re.MULTILINE)
    bracket_matches = list(bracket_pattern.finditer(text))
    if len(bracket_matches) >= 2:
        return _extract(bracket_matches, text)

    # 无法拆分，作为单个章节
    return None


@app.route('/api/books/import-files', methods=['POST'])
@login_required
def import_book_files():
    """从多个文本文件导入创建新作品，支持 txt/md/docx/zip"""
    files = request.files.getlist('files')
    if not files or len(files) == 0:
        return jsonify({'error': '未选择文件'}), 400

    title = request.form.get('title', '').strip()
    book_type = request.form.get('book_type', 'novel')
    genre = request.form.get('genre', 'other')

    tmpdir = tempfile.mkdtemp()
    try:
        all_chapters = []

        for f in files:
            if not f or not f.filename:
                continue
            # 用原始文件名检测扩展名，避免 secure_filename 移除中文后丢失扩展名
            original_name = f.filename
            ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
            if ext not in ('txt', 'md', 'docx', 'zip', 'json'):
                continue

            # 保存到临时文件，用 uuid 避免文件名冲突
            safe_name = f'{uuid.uuid4()}.{ext}'
            filepath = os.path.join(tmpdir, safe_name)
            f.save(filepath)
            text = extract_text_from_file(filepath, safe_name)

            if not text.strip():
                continue

            # 尝试拆分章节
            chapters = split_into_chapters(text)
            if chapters:
                all_chapters.extend(chapters)
            else:
                # 作为单个章节，用原始文件名作为章节标题
                ch_title = os.path.splitext(original_name)[0]
                all_chapters.append({'title': ch_title[:100], 'content': text})

        if not all_chapters:
            return jsonify({'error': '未能从文件中提取到有效内容，请检查文件格式或编码'}), 400

        # 从第一个文件名推断标题
        if not title:
            first_file = files[0].filename or '导入作品'
            title = os.path.splitext(first_file)[0][:50]

        book = Book(
            user_id=getattr(request, 'current_user_id', ''),
            title=title, author='', genre=genre, book_type=book_type,
            synopsis=f'从文件导入，共 {len(all_chapters)} 章', status='draft'
        )
        db.session.add(book)
        db.session.flush()

        for idx, ch_data in enumerate(all_chapters):
            ch = Chapter(
                book_id=book.id, title=ch_data['title'][:200] or f'第{idx + 1}章',
                content=ch_data['content'], order_index=idx,
                is_volume=False, parent_id='',
                word_count=count_words(ch_data['content'])
            )
            db.session.add(ch)

        # 自动按 50 章一组创建卷
        chapter_count = len(all_chapters)
        volume_interval = 50
        vol_count = (chapter_count + volume_interval - 1) // volume_interval
        for vidx in range(vol_count):
            ch_start = vidx * volume_interval + 1
            ch_end = min((vidx + 1) * volume_interval, chapter_count)
            vol = Chapter(
                book_id=book.id,
                title=f'第{vidx + 1}卷（第{ch_start}-{ch_end}章）',
                content='', order_index=chapter_count + vidx,
                is_volume=True, parent_id='', word_count=0,
            )
            db.session.add(vol)
            db.session.flush()
            # 将范围内章节归入该卷
            for cidx in range(vidx * volume_interval, min((vidx + 1) * volume_interval, chapter_count)):
                # 找到刚刚创建的章节（按 order_index 为 cidx）
                ch_obj = Chapter.query.filter_by(book_id=book.id, order_index=cidx, is_volume=False).first()
                if ch_obj:
                    ch_obj.parent_id = vol.id

        update_book_stats(book.id)
        return jsonify(book.to_dict()), 201
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ==== Count Words ====

@app.route('/api/utils/count-words', methods=['POST'])
def count_words_api():
    text = request.json.get('text', '')
    return jsonify({'count': count_words(text)})


# ==== Stage Content API ====

SHORT_STAGES = [
    {'key': 'character_design', 'label': '人物设计', 'icon': '👤', 'desc': '创建、补全和诊断人物设定'},
    {'key': 'plot_design', 'label': '剧情设计', 'icon': '📋', 'desc': '核心命题、人物目标、主要冲突、因果链', 'is_parent': True},
    {'key': 'intro_design', 'label': '导语设计', 'icon': '✍️', 'desc': '书名建议、开篇导语、前十秒钩子', 'parent': 'plot_design'},
    {'key': 'plot_refine', 'label': '剧情细化', 'icon': '🔍', 'desc': '场景链、节拍、信息投放、伏笔与回收', 'parent': 'plot_design'},
    {'key': 'outline', 'label': '大纲', 'icon': '📝', 'desc': '小节清单、字数分配、场景承接'},
    {'key': 'draft', 'label': '正文编写', 'icon': '📖', 'desc': '正式写作，支持专家模式分节写作'},
]

SCRIPT_STAGES = [
    {'key': 'character_design', 'label': '人物设计', 'icon': '👤', 'desc': '创建可表演的人物设定'},
    {'key': 'plot_design', 'label': '剧情设计', 'icon': '📋', 'desc': '可拍摄的故事架构', 'is_parent': True},
    {'key': 'plot_refine', 'label': '剧情细化', 'icon': '🔍', 'desc': '场次分解与节奏控制', 'parent': 'plot_design'},
    {'key': 'outline', 'label': '大纲', 'icon': '📝', 'desc': '场次列表与时长分配'},
    {'key': 'draft', 'label': '剧本编写', 'icon': '🎬', 'desc': '场景标题格式：序号. 内/外景 地点 - 时间'},
]

LONG_STAGES = [
    {'key': 'worldbuilding', 'label': '世界观', 'icon': '🌍', 'desc': '规则、势力、地理、历史、境界'},
    {'key': 'character_design', 'label': '人物', 'icon': '👤', 'desc': '主角、配角、路人'},
    {'key': 'plot_design', 'label': '剧情', 'icon': '📋', 'desc': '全书线、分卷、剧情弧、章卡、伏笔'},
    {'key': 'draft', 'label': '正文', 'icon': '📖', 'desc': '按章写作，状态账本落盘'},
    {'key': 'continuity_ledger', 'label': '状态账本', 'icon': '📒', 'desc': '时间线、人物状态、势力状态、伏笔回收'},
]

def get_stages_for_type(book_type):
    if book_type == 'novel':
        return LONG_STAGES
    elif book_type == 'script':
        return SCRIPT_STAGES
    return SHORT_STAGES


@app.route('/api/books/<book_id>/stages', methods=['GET'])
def list_stages(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    stages_def = get_stages_for_type(book.book_type)
    contents = {s.stage_key: s.to_dict() for s in StageContent.query.filter_by(book_id=book_id).all()}
    result = []
    for sd in stages_def:
        item = dict(sd)
        item['content'] = contents.get(sd['key'], {}).get('content', '')
        item['stage_id'] = contents.get(sd['key'], {}).get('id', '')
        result.append(item)
    return jsonify(result)

@app.route('/api/books/<book_id>/stages/<stage_key>', methods=['GET'])
def get_stage(book_id, stage_key):
    sc = StageContent.query.filter_by(book_id=book_id, stage_key=stage_key).first()
    return jsonify(sc.to_dict() if sc else {'book_id': book_id, 'stage_key': stage_key, 'content': ''})

@app.route('/api/books/<book_id>/stages/<stage_key>', methods=['PUT'])
def save_stage(book_id, stage_key):
    sc = StageContent.query.filter_by(book_id=book_id, stage_key=stage_key).first()
    if not sc:
        sc = StageContent(book_id=book_id, stage_key=stage_key)
        db.session.add(sc)
    sc.content = request.json.get('content', '')
    db.session.commit()
    return jsonify(sc.to_dict())


# ==== Prompt Template API ====

SEED_PROMPTS = [
    # 短篇 - 人物设计
    {'name': '短篇人物设计（通用）', 'agent_id': 'character_design', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '创建、补全、诊断和修改人物设计', 'content': '你是番薯写作的人物设计智能体。\n你的职责是创建、补全、诊断和修改人物设计。\n\n工作流程：\n1. 判断用户是在新建人物、补全人物，还是修改已有设定。\n2. 修改已有内容前，先读取人物设计阶段内容。\n3. 形成人物稿并使用工具写回人物编辑器。\n\n人物设计至少关注：\n- 身份与处境\n- 核心欲望/恐惧/缺陷/秘密/底线\n- 行动逻辑\n- 关系结构\n- 辨识度\n- 人物弧\n\n只写正式设定，不写分析过程。'},
    # 短篇 - 剧情设计（父阶段，含3个子槽位）
    {'name': '短篇剧情设计（通用）', 'agent_id': 'plot_design', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '统一负责剧情设计、导语设计和剧情细化三个子方向', 'content': '你是番薯写作的短篇剧情智能体，统一负责剧情设计、导语设计和剧情细化。\n\n三个内容槽位的边界：\n- 剧情设计（plot_design）：核心命题、人物目标、主要冲突、因果链、关键转折、真实时间线和结局兑现。\n- 导语设计（intro_design）：书名建议、开篇导语和前十秒钩子。\n- 剧情细化（plot_refine）：供正文直接执行的场景链、节拍、信息投放、人物选择、情绪推进、伏笔与回收。\n\n工作流程：\n1. 先确认用户本次处理哪个子方向。\n2. 读取人物设计和已有的剧情内容。\n3. 检查因果是否成立、冲突是否递进、转折是否由人物选择触发。\n4. 把成品写入正确的剧情子槽位。'},
    # 短篇 - 大纲
    {'name': '短篇大纲（通用）', 'agent_id': 'outline', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '把人物和剧情梳理成可直接指导分节写作的完整大纲', 'content': '你是番薯写作的短篇大纲智能体，负责把人物和剧情内容梳理成可直接指导分节写作的完整大纲。\n\n开始任何大纲任务前，必须分别读取：\n1. 人物设计\n2. 剧情设计\n3. 导语设计\n4. 剧情细化\n5. 当前大纲\n\n大纲成品必须包含：\n- 全文定位、主线目标、核心冲突、时间线与结局\n- 正文小节总数及顺序\n- 每个小节的标题/预估字数/出场人物/场景/起始状态/详细剧情\n- 小节之间的承接关系、人物状态变化、伏笔埋设与回收位置'},
    # 短篇 - 正文专家总控
    {'name': '短篇正文专家总控', 'agent_id': 'expert_draft_coordinator', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '负责正文结构管理、分节任务调度和成稿后的处理', 'content': '你是番薯写作的短篇正文专家编写智能体，负责正文结构管理、分节任务调度和成稿后的处理。\n\n你负责四类任务：\n1. 初始化：读取大纲，根据完整大纲创建导语、全部正文小节及人物状态槽位。\n2. 全部写作：先读取大纲并初始化，再启动自动写作。\n3. 单节写作：用户指定一个已初始化小节时，启动单节写作。\n4. 后处理：正文审阅、润色、去AI味、格式整理、章节名修改和局部修订。\n\n初始化前必须读取大纲；小节标题/顺序/数量必须与大纲一致。'},
    # 短篇 - 分节写手
    {'name': '短篇分节写手', 'agent_id': 'expert_section_writer', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '实际创作小说正文的主要智能体，一次只处理一个小节', 'content': '你是番薯写作的短篇分节写手智能体，是实际创作小说正文的主要智能体。\n你一次只处理当前上下文指定的一个小节，不得修改其它小节。\n\n写作前必须完成：\n1. 读取大纲；允许时补充读取剧情细化。\n2. 读取当前小节之前最近三个已有正文的小节。\n3. 必须读取紧邻上一节的人物状态。\n\n写作标准：\n- 严格执行当前小节在大纲中的任务、承接点和字数要求（默认800-1500字）。\n- 延续前文的时间、空间、人物关系、信息知情范围。\n- 让冲突通过人物行动、选择、对白和可感知细节推进。\n- 保持题材、叙述视角、文风和节奏一致。\n- 小节结尾应完成本节任务并留下明确承接点。'},
    # 言情专项
    {'name': '言情-人物设计', 'agent_id': 'character_design', 'book_type': 'short_story', 'genre': 'romance', 'is_builtin': True,
     'description': '网文爆款人物设计：标签叠加法、冲突校验', 'content': '你是番薯写作的言情人物设计智能体。\n\n网文爆款人物设计原则：\n1. 标签叠加法——选择3-5个反差标签叠加（如"高冷总裁+童年创伤+情感洁癖"）\n2. 冲突校验——每对人物之间至少存在价值观冲突、目标冲突、信息差和情感拉扯\n3. 辨识度——每个角色有独特的口头禅、行为习惯、情感表达方式\n4. 人物弧——明确起点状态和终点状态，设定合理的转折事件\n\n输出格式：\n- 核心身份信息（姓名、年龄、职业/身份）\n- 性格特质（3-5个核心标签）\n- 核心欲望与恐惧\n- 人物背景与转折点\n- 与其他角色的关系图谱'},
    # 玄幻专项
    {'name': '玄幻-人物设计', 'agent_id': 'character_design', 'book_type': 'short_story', 'genre': 'fantasy', 'is_builtin': True,
     'description': '玄幻修真专项：技术代价、道德边界、成长路径', 'content': '你是番薯写作的玄幻人物设计智能体。\n\n玄幻人物设计要点：\n1. 成长路径——从凡人到巅峰的阶梯式升级路线\n2. 技术代价——每次突破都应有对应的代价（寿命、情感、道德）\n3. 道德边界——定义角色不可触碰的底线\n4. 世界观适配——人物能力体系必须与世界规则一致\n\n输出格式：\n- 身份与修为等级\n- 功法体系与特殊能力\n- 性格特质与行为模式\n- 核心价值观与道德底线\n- 成长路线与关键转折'},
    # 悬疑专项
    {'name': '悬疑-剧情设计', 'agent_id': 'plot_design', 'book_type': 'short_story', 'genre': 'mystery', 'is_builtin': True,
     'description': '悬疑推理专项：秘密、动机、误导、信息差', 'content': '你是番薯写作的悬疑剧情设计智能体。\n\n悬疑剧情设计要点：\n1. 真相底牌——确定最终的真相是什么，所有线索都应指向它\n2. 线索结构——区分真线索（导向真相）和红鲱鱼（导向歧途）\n3. 信息释放节奏——每章递进式释放信息，读者和侦探之间的信息差\n4. 误导设计——每个红鲱鱼必须合理化，不能是纯粹欺骗\n5. 人物秘密——每个主要人物都有隐藏的秘密\n\n输出格式：\n- 真相概述\n- 线索链（真线索按发现顺序排列）\n- 红鲱鱼列表（每条附合理化解释）\n- 关键转折点\n- 结局兑现清单'},
    # 审稿提示词
    {'name': '正文审阅', 'agent_id': 'draft_review', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '5维度审稿：句式、标点、否定句、形容词、比喻', 'content': '你是番薯写作的正文审阅智能体。\n\n从以下5个维度审阅正文：\n\n1. 句式节奏：\n- 长短句交替，避免连续3句以上相同长度\n- 动作场景使用短句，情感场景使用中长句\n- 检查是否有"的的不休"现象\n\n2. 标点规范：\n- 禁止使用破折号（——）\n- 省略号统一使用"……"（6个点）\n- 引号嵌套不超过一层\n\n3. 否定句式：\n- 优先使用肯定句式\n- 每个自然段内否定句式不超过2处\n\n4. 形容词节制：\n- 每个名词前最多2个修饰形容词\n- 情感描写作减法而非加法\n\n5. 比喻节制：\n- 每千字比喻不超过3处\n- 比喻必须与小说世界观一致'},
]

def seed_prompt_templates():
    if PromptTemplate.query.filter_by(is_builtin=True).first():
        return
    for p in SEED_PROMPTS:
        pt = PromptTemplate(
            name=p['name'], agent_id=p['agent_id'], book_type=p['book_type'],
            genre=p['genre'], content=p['content'], is_builtin=True,
            description=p['description']
        )
        db.session.add(pt)
    db.session.commit()

@app.route('/api/prompts', methods=['GET'])
def list_prompts():
    book_type = request.args.get('book_type', '')
    agent_id = request.args.get('agent_id', '')
    query = PromptTemplate.query
    if book_type:
        query = query.filter_by(book_type=book_type)
    if agent_id:
        query = query.filter_by(agent_id=agent_id)
    prompts = query.order_by(PromptTemplate.is_builtin.desc(), PromptTemplate.name).all()
    return jsonify([p.to_dict() for p in prompts])

@app.route('/api/prompts/<prompt_id>', methods=['GET'])
def get_prompt(prompt_id):
    pt = PromptTemplate.query.get(prompt_id)
    if not pt:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(pt.to_dict())

@app.route('/api/prompts', methods=['POST'])
def create_prompt():
    data = request.json
    pt = PromptTemplate(
        name=data.get('name', ''), agent_id=data.get('agent_id', ''),
        book_type=data.get('book_type', 'short_story'), genre=data.get('genre', 'other'),
        content=data.get('content', ''), description=data.get('description', '')
    )
    db.session.add(pt)
    db.session.commit()
    return jsonify(pt.to_dict()), 201

@app.route('/api/prompts/<prompt_id>', methods=['PUT'])
def update_prompt(prompt_id):
    pt = PromptTemplate.query.get(prompt_id)
    if not pt:
        return jsonify({'error': 'Not found'}), 404
    data = request.json
    for field in ['name', 'agent_id', 'book_type', 'genre', 'content', 'description']:
        if field in data:
            setattr(pt, field, data[field])
    db.session.commit()
    return jsonify(pt.to_dict())


# ==== Book Bible API (项目宪法) ====

@app.route('/api/books/<book_id>/bible', methods=['GET'])
def get_book_bible(book_id):
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()
    return jsonify(bb.to_dict())

@app.route('/api/books/<book_id>/bible', methods=['PUT'])
def update_book_bible(book_id):
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
    data = request.json
    for field in ['worldbuilding', 'character_profiles', 'timeline', 'foreshadowing', 'style_guide', 'key_rules', 'locations', 'concept', 'plot_design']:
        if field in data:
            setattr(bb, field, data[field])
    db.session.commit()
    return jsonify(bb.to_dict())

@app.route('/api/books/<book_id>/bible/sync', methods=['POST'])
def sync_book_bible(book_id):
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
    chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    characters = Character.query.filter_by(book_id=book_id).all()
    outlines = Outline.query.filter_by(book_id=book_id).order_by(Outline.order_index).all()

    summary_parts = [
        f"## 作品信息\n书名：{Book.query.get(book_id).title}\n类型：{Book.query.get(book_id).genre}\n\n",
        f"## 角色档案 ({len(characters)}人)\n"
    ]
    for c in characters:
        parts = [f"- {c.name} ({c.role})"]
        if c.personality: parts.append(f" 性格：{c.personality[:200]}")
        if c.background: parts.append(f" 背景：{c.background[:200]}")
        summary_parts.append('\n'.join(parts) + '\n')

    summary_parts.append(f"\n## 大纲 ({len(outlines)}条)\n")
    for o in outlines:
        summary_parts.append(f"- {o.title}\n  {o.content[:200]}\n")

    summary_parts.append(f"\n## 章节 ({len(chapters)}章)\n")
    for ch in chapters:
        summary_parts.append(f"- 第{ch.order_index + 1}章 {ch.title} ({ch.word_count}字)\n")

    bb.generated_summary = ''.join(summary_parts)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(bb.to_dict())

# ==== AI Review API (AI 责编) ====

REVIEW_CRITERIA = {
    'opening_hook': {'label': '开篇钩子', 'weight': 20, 'desc': '前三段能否抓住读者'},
    'character_motivation': {'label': '人物动机', 'weight': 20, 'desc': '人物行为是否有清晰动机'},
    'pacing_rhythm': {'label': '节奏控制', 'weight': 15, 'desc': '张弛有度，爽点密度'},
    'chapter_ending': {'label': '章尾钩子', 'weight': 15, 'desc': '每章结尾是否留下追读期待'},
    'dialogue_quality': {'label': '对白质量', 'weight': 10, 'desc': '对白是否推动剧情/展现性格'},
    'world_consistency': {'label': '设定一致性', 'weight': 10, 'desc': '前后设定是否有矛盾'},
    'commercial_potential': {'label': '商业潜力', 'weight': 10, 'desc': '是否适合目标平台读者'},
}

@app.route('/api/books/<book_id>/review', methods=['POST'])
def review_book(book_id):
    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    scope = request.json.get('scope', 'latest')
    chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    if scope == 'latest' and chapters:
        text = chapters[-1].content or ''
        for ch in chapters[-3:-1]:
            if ch.content:
                text = ch.content + '\n\n' + text
    else:
        text = request.json.get('content', '')
        if not text:
            text = '\n\n'.join([(c.content or '') for c in chapters])

    if not text.strip():
        return jsonify({'error': 'No content to review'}), 400
    text = text[:6000]

    criteria_desc = '\n'.join([f"- {v['label']}({v['weight']}分): {v['desc']}" for v in REVIEW_CRITERIA.values()])
    system_prompt = f"""你是资深网文责编，服务于番茄小说/起点中文网。请从以下维度审稿并严格按JSON格式输出结果。

评分维度：
{criteria_desc}

输出格式（严格JSON，不要任何其他文字）：
{{"scores": {{"opening_hook": 0-100, "character_motivation": 0-100, "pacing_rhythm": 0-100, "chapter_ending": 0-100, "dialogue_quality": 0-100, "world_consistency": 0-100, "commercial_potential": 0-100}},
"total_score": 0-100,
"grade": "S/A/B/C/D",
"strengths": ["优点1", "优点2", "优点3"],
"weaknesses": ["问题1", "问题2", "问题3"],
"specific_suggestions": ["具体修改建议1", "具体修改建议2", "具体修改建议3"],
"platform_fit": "适合/不太适合番茄/起点/七猫的理由"}}"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role':'system','content':system_prompt},{'role':'user','content':text}],
                  'temperature': 0.3, 'max_tokens': 2000, 'response_format': {'type': 'json_object'}},
            timeout=120)
        result = resp.json()
        return jsonify(json.loads(result['choices'][0]['message']['content']))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==== Skill Pack API ====

SEED_SKILL_PACKS = [
    {'name': '番茄爽文三件套', 'description': '丝滑开篇+黄金三章+爽点卡带，番茄平台快速变现管线', 'genre': 'other', 'book_type': 'novel', 'icon':'🍅',
     'stage_keys': json.dumps(['character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'开篇钩子','desc':'用AI责编检查前三段钩子强度','prompt_key':'opening_hook'},
         {'step':2,'name':'黄金三章','desc':'完成前三章大纲与正文','prompt_key':'golden_three'},
         {'step':3,'name':'爽点卡带','desc':'每章埋2-3个爽点，章尾打脸或反转','prompt_key':'pleasure_points'},
         {'step':4,'name':'AI审稿','desc':'用责编审稿，确保商业潜力','prompt_key':'review'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'opening_hook': '你是番茄爽文开头专家。前三段要做到：1)冲突入画 2)一句话显主角特质 3)留疑问钩子。写出3个版本的开头。',
         'golden_three': '你是番茄爽文结构专家。规划黄金三章：第一章展示主角日常与困境，第二章引入金手指/转折，第三章展示初步成绩并留悬念。',
         'pleasure_points': '你是番茄爽文爽点设计专家。为当前章节设计：1)2-3个爽点事件 2)章尾打脸/反转/悬念钩子 3)交代信息量。',
         'review': '你是番茄审稿编辑。审核：1)开头3秒钩子是否到位 2)爽点是否够密集 3)章尾是否有追读期待 4)人物是否讨喜 5)是否有平台违禁内容。',
     }, ensure_ascii=False)},
    {'name': '起点升级流大师', 'description': '境界体系+升级节奏+副本设计，起点经典升级流全流程', 'genre': 'fantasy', 'book_type': 'novel', 'icon':'⚔️',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'境界体系','desc':'设计完整的境界升级体系','prompt_key':'level_system'},
         {'step':2,'name':'升级节奏','desc':'规划全书升级节奏曲线','prompt_key':'level_curve'},
         {'step':3,'name':'副本设计','desc':'设计主要副本/秘境','prompt_key':'dungeon_design'},
         {'step':4,'name':'分卷大纲','desc':'按卷规划大纲','prompt_key':'volume_outline'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'level_system': '你是玄幻升级体系设计师。设计详细境界体系：1)境界名称及每境界细分 2)突破条件与资源需求 3)每境界战力变化 4)境界对应寿命/地位。',
         'level_curve': '你是升级节奏规划师。规划：1)预计全文字数 2)每个境界的章节跨度 3)关键突破节点 4)压制与爆发节奏。',
         'dungeon_design': '你是副本设计师。设计：1)副本背景与入口 2)挑战层次 3)奖励机制 4)难度与战力匹配 5)隐藏要素。',
         'volume_outline': '你是网文编辑。为当前卷规划：1)卷核心目标 2)主要冲突线 3)人物成长目标 4)关键转折点 5)卷末状态。',
     }, ensure_ascii=False)},
    {'name': '女频甜宠六边形', 'description': '霸总/甜宠/虐渣全能配方，情感拉扯+节奏控制', 'genre': 'romance', 'book_type': 'short_story', 'icon':'💕',
     'stage_keys': json.dumps(['character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'人设反差','desc':'设计CP反差人设','prompt_key':'cp_design'},
         {'step':2,'name':'情感弧线','desc':'规划情感发展曲线','prompt_key':'emotion_arc'},
         {'step':3,'name':'甜蜜高光','desc':'设计甜宠高光场景','prompt_key':'sweet_highlights'},
         {'step':4,'name':'虐渣复仇','desc':'设计打脸虐渣爽点','prompt_key':'revenge_scenes'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'cp_design': '你是女频人设专家。设计CP：1)男女主身份反差标签 2)各自秘密与底线 3)相识方式 4)情感拉扯模式。',
         'emotion_arc': '你是情感弧线设计师。规划：1)从陌生到深爱的阶段划分 2)每阶段关键事件 3)糖度与虐度配比 4)读者情绪波动设计。',
         'sweet_highlights': '你是甜宠场景设计师。设计：1)每1000字至少1个甜蜜互动 2)专属小动作/昵称 3)"追妻火葬场"桥段 4)双向奔赴高光。',
         'revenge_scenes': '你是虐渣场景专家。设计：1)仇人/渣男/绿茶出场铺垫 2)真相揭晓节奏 3)打脸名场面 4)女主高光反击台词。',
     }, ensure_ascii=False)},
    {'name': '悬疑反转工厂', 'description': '5层信息差+红鲱鱼设计+结局反转，知乎盐选适配', 'genre': 'mystery', 'book_type': 'short_story', 'icon':'🔍',
     'stage_keys': json.dumps(['character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'真相底牌','desc':'确定最终真相，所有线索指向它','prompt_key':'truth_card'},
         {'step':2,'name':'信息差层','desc':'设计读者/侦探/角色的信息差','prompt_key':'info_gap'},
         {'step':3,'name':'红鲱鱼','desc':'设计至少3个合理误导','prompt_key':'red_herring'},
         {'step':4,'name':'反转节点','desc':'规划反转时间点','prompt_key':'twist_timing'},
         {'step':5,'name':'结局兑现','desc':'确保所有伏笔回收','prompt_key':'ending_payoff'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'truth_card': '你是悬疑真相设计师。确定：1)案件/事件最终真相 2)真凶/真相持有者 3)动机 4)作案手法 5)所有人物的秘密。',
         'info_gap': '你是信息差设计专家。设计读者、侦探、角色之间的信息差层级：1)读者知道但侦探不知 2)侦探知道但读者不知 3)某些角色知道但其他角色不知。',
         'red_herring': '你是红鲱鱼设计师。设计3个以上合理误导：1)可疑人物 2)误导线索 3)时间线错觉 4)每个都需要合理化，不能是纯粹欺骗。',
         'twist_timing': '你是反转节奏专家。规划：1)每个反转的位置 2)反转强度递增 3)反转前的铺垫 4)反转后的读者反应预期。',
         'ending_payoff': '你是结局兑现检查官。检查：1)所有伏笔是否回收 2)所有线索是否指向真相 3)每个红鲱鱼是否有合理解释 4)结局是否有情感冲击。',
     }, ensure_ascii=False)},
    {'name': '短篇冲榜模板', 'description': '知乎盐选/UC故事会适配，一句话梗到完整成稿', 'genre': 'other', 'book_type': 'short_story', 'icon':'🚀',
     'stage_keys': json.dumps(['character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'一句话梗','desc':'提炼一个爆款一句话梗概','prompt_key':'one_line_hook'},
         {'step':2,'name':'快速大纲','desc':'口播叙事化大纲','prompt_key':'quick_outline'},
         {'step':3,'name':'AI初稿','desc':'AI写正文分节','prompt_key':'draft'},
         {'step':4,'name':'AI审稿','desc':'责编审稿优化','prompt_key':'review'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'one_line_hook': '你是短篇爆款制造机。写3个版本的一句话梗概：1)含反转 2)含信息差 3)含情感冲击。格式：【类型】+【一句话】+【预期读者反应】。',
         'quick_outline': '你是口播叙事化大纲专家。用口语化语言写大纲：1)开篇冲突 2)发展转折(2-3个) 3)高潮反转 4)结局。每段200字左右。',
     }, ensure_ascii=False)},
    {'name': '世界观构建手册', 'description': '从零构建完整虚构世界，地理/种族/历史/魔法体系', 'genre': 'fantasy', 'book_type': 'novel', 'icon':'🌍',
     'stage_keys': json.dumps(['worldbuilding'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'基础规则','desc':'定义世界的基础物理/魔法规则','prompt_key':'base_rules'},
         {'step':2,'name':'地理势力','desc':'绘制世界版图与势力分布','prompt_key':'geography'},
         {'step':3,'name':'历史时间线','desc':'编写世界历史年表','prompt_key':'history'},
         {'step':4,'name':'种族文化','desc':'设计种族与文化体系','prompt_key':'cultures'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'base_rules': '你是世界观架构师。定义：1)世界物理法则(重力、元素、魔法来源) 2)能量/能力体系基本规则 3)生物进化基本逻辑 4)神/超自然存在的定位。',
         'geography': '你是世界地理设计师。绘制：1)大陆/海洋分布 2)关键地理特征 3)势力范围划分 4)资源分布 5)交通与贸易路线。',
         'history': '你是世界历史编年官。编写：1)创世/远古时期 2)重要历史节点 3)战争与和平时期 4)技术/魔法发展里程碑 5)当前时代的定位。',
         'cultures': '你是种族文化设计师。设计每个种族/民族的：1)外貌特征 2)社会结构 3)宗教/信仰 4)语言特点 5)与其他种族的关系。',
     }, ensure_ascii=False)},
    {'name': '都市职场商战', 'description': '职场升级+商战博弈+人情世故，番茄职场文全链路', 'genre': 'urban_business', 'book_type': 'novel', 'icon':'💼',
     'stage_keys': json.dumps(['character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'人物关系','desc':'设计职场生态与利益关系网','prompt_key':'office_ecosystem'},
         {'step':2,'name':'商战节奏','desc':'规划商战/职场冲突的升级曲线','prompt_key':'business_conflict'},
         {'step':3,'name':'专业细节','desc':'补充行业专业细节与职场暗语','prompt_key':'professional_details'},
         {'step':4,'name':'反转爽点','desc':'设计打脸/升职/签约名场面','prompt_key':'revenge_moments'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'office_ecosystem': '你是职场生态设计师。设计：1)公司/行业生态结构 2)关键人物的利益述求 3)盟友与对手阵营 4)潜规则与明规则对比。',
         'business_conflict': '你是商战节奏师。规划：1)项目/方案的竞标节奏 2)商业陷阱与反击 3)内部权力斗争 4)外部市场变化冲击。',
         'professional_details': '你是行业顾问。为选定行业补充：1)行业术语系统 2)工作流程真实感 3)KPI/考核体系 4)职场生存法则。',
         'revenge_moments': '你是职场爽文设计师。设计：1)方案碾压对手名场面 2)升职签约高光时刻 3)同事打脸反转 4)行业地位跃升。',
     }, ensure_ascii=False)},
    {'name': '历史权谋工坊', 'description': '王朝兴衰+权谋博弈+战争策略，起点历史类全栈', 'genre': 'history', 'book_type': 'novel', 'icon':'🏯',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'时代背景','desc':'确定历史朝代与政治格局','prompt_key':'era_setting'},
         {'step':2,'name':'权谋棋局','desc':'设计权力博弈的核心棋局','prompt_key':'power_game'},
         {'step':3,'name':'战争策略','desc':'规划军事战役与战术','prompt_key':'war_strategy'},
         {'step':4,'name':'人物阵营','desc':'设计各派系人物关系','prompt_key':'faction_design'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'era_setting': '你是历史设定专家。设定：1)朝代/架空王朝背景 2)政治制度与官制 3)经济与税收体系 4)外患与边疆态势。',
         'power_game': '你是权谋棋手。规划：1)核心权力矛盾 2)朝堂派系格局 3)每派人物目标与弱点 4)关键政变/宫变节点。',
         'war_strategy': '你是军事策略师。规划：1)兵力对比与部署 2)关键战役设计 3)后勤与粮草 4)间谍与情报战 5)战后格局变化。',
         'faction_design': '你是阵营设计师。为每个派系设计：1)核心理念 2)代表人物与性格 3)内部矛盾 4)与其他派系的合纵连横。',
     }, ensure_ascii=False)},
    {'name': '科幻未来创世', 'description': '硬科幻设定+赛博朋克+太空史诗，番茄科幻全流程', 'genre': 'scifi', 'book_type': 'novel', 'icon':'🚀',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'科技设定','desc':'设计核心技术体系与科技树','prompt_key':'tech_tree'},
         {'step':2,'name':'未来社会','desc':'构建未来社会结构与矛盾','prompt_key':'future_society'},
         {'step':3,'name':'英雄旅程','desc':'设计主角的科幻冒险主线','prompt_key':'hero_journey'},
         {'step':4,'name':'设定一致性','desc':'用AI责编检查科技逻辑一致性','prompt_key':'logic_check'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'tech_tree': '你是硬科幻设定师。设计：1)核心科技原理(需自洽) 2)科技发展里程碑 3)科技带来的社会变革 4)科技黑暗面与伦理困境。',
         'future_society': '你是未来社会学顾问。构建：1)政治体制演变 2)阶层/种姓/基因分化 3)AI与人类的共存模式 4)星际殖民的社会结构。',
         'hero_journey': '你是科幻冒险策划师。规划：1)主角从凡人到英雄的弧线 2)科技/异能觉醒节点 3)关键冒险任务 4)终极对抗与命运抉择。',
         'logic_check': '你是科幻逻辑审核员。检查：1)科技设定是否有内部矛盾 2)未来社会变迁是否合理 3)时间线/因果链是否自洽 4)角色行为是否符合科技环境。',
     }, ensure_ascii=False)},
    {'name': '无限流生存指南', 'description': '副本设计+能力进化+团战策略，起点头部题材全流程', 'genre': 'fantasy', 'book_type': 'novel', 'icon':'🌀',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'主神空间','desc':'设计无限流核心机制与规则','prompt_key':'infinity_rules'},
         {'step':2,'name':'副本设计','desc':'设计每个副本的世界与任务','prompt_key':'dungeon_design'},
         {'step':3,'name':'能力体系','desc':'设计主角的能力进化树','prompt_key':'ability_tree'},
         {'step':4,'name':'团战策略','desc':'设计团队协作与智斗','prompt_key':'team_tactics'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'infinity_rules': '你是无限流系统设计师。设定：1)主神空间/轮回世界的规则体系 2)轮回者的等级与权限 3)任务系统与奖励机制 4)惩罚与抹杀的触发条件。',
         'dungeon_design': '你是副本世界设计师。为每个副本设计：1)世界背景与规则(可借用知名IP) 2)主线任务与隐藏任务 3)BOSS战与关键道具 4)副本难度与玩家适配。',
         'ability_tree': '你是能力进化设计师。规划：1)能力体系分类 2)每级进化条件与效果 3)能力组合与开发 4)主角独特能力的来源与秘密。',
         'team_tactics': '你是团战策略师。设计：1)团队角色分工 2)经典配合战术 3)智斗名场面 4)背叛与信任危机。',
     }, ensure_ascii=False)},
    {'name': 'SoloEnt Vibe Writing', 'description': 'SoloEnt式人机共创哲学：作者主导，AI辅助，保留个人文风', 'genre': 'other', 'book_type': 'short_story', 'icon':'✨',
     'stage_keys': json.dumps(['character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'导入风格','desc':'导入个人旧作或参考文本，建立风格基线','prompt_key':'style_import'},
         {'step':2,'name':'拆解任务','desc':'将创作任务拆分为AI可辅助的小单元','prompt_key':'task_decompose'},
         {'step':3,'name':'AI初稿','desc':'AI生成初稿，保持个人风格','prompt_key':'first_draft'},
         {'step':4,'name':'自我蒸馏','desc':'反复修改润色，沉淀个人方法论','prompt_key':'self_distill'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_import': '你是风格分析师。分析提供的文本：1)句式偏好 2)用词习惯 3)节奏特征 4)情感表达方式。输出风格卡片。',
         'task_decompose': '你是创作任务拆解师。将大段写作任务拆解为：1)场景段落(200-500字) 2)对话片段 3)转场段落 4)内心独白 5)环境描写。每项标注AI辅助优先级。',
         'first_draft': '你是Vibe Writing写手。根据风格卡片和任务单元，输出初稿。关键：1)严格遵循风格卡片 2)每个单元独立完整 3)留出作者修改空间 4)标注不确定处。',
         'self_distill': '你是自我蒸馏教练。引导作者：1)对比修改前后差异 2)提炼复用经验 3)更新风格卡片 4)沉淀为个人创作方法论。',
     }, ensure_ascii=False)},
    {'name': 'AI责编精审套装', 'description': '5层审稿管线：逻辑→人设→节奏→商业→违禁，一站式过稿', 'genre': 'other', 'book_type': 'short_story', 'icon':'🔍',
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'逻辑审稿','desc':'检查情节逻辑与因果链','prompt_key':'logic_review'},
         {'step':2,'name':'人设审稿','desc':'检查人物行为是否一致','prompt_key':'character_review'},
         {'step':3,'name':'节奏审稿','desc':'检查爽点密度与追读期待','prompt_key':'rhythm_review'},
         {'step':4,'name':'商业审稿','desc':'评估平台适配与签约潜力','prompt_key':'commercial_review'},
         {'step':5,'name':'合规审稿','desc':'检查平台违禁内容','prompt_key':'compliance_review'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'logic_review': '你是情节逻辑审查员。逐章检查：1)因果是否成立 2)时间线是否自洽 3)信息差是否合理 4)转折是否有铺垫。输出每个逻辑漏洞及修复建议。',
         'character_review': '你是人设一致性审查员。检查：1)人物行为是否符合设定性格 2)人物关系是否前后一致 3)人物成长是否合理 4)配角是否有存在意义。',
         'rhythm_review': '你是节奏感审查员。逐章分析：1)爽点/冲突密度 2)章尾钩子强度 3)信息释放节奏 4)叙事速度变化。输出节奏评分及调整建议。',
         'commercial_review': '你是商业编辑。从平台视角评估：1)开篇钩子强度 2)核心卖点是否突出 3)目标读者匹配度 4)长期连载潜力 5)建议定价/签约策略。',
         'compliance_review': '你是合规审查员。检查：1)政治敏感内容 2)色情/暴力违规 3)价值观导向 4)版权风险 5)平台特有规则。',
     }, ensure_ascii=False)},
    {'name': '都市异能觉醒', 'description': '异能体系+日常战斗+组织对抗，番茄都市异能全流程', 'genre': 'urban_fantasy', 'book_type': 'novel', 'icon':'⚡',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'异能体系','desc':'设计独特的异能分类与等级','prompt_key':'power_system'},
         {'step':2,'name':'组织势力','desc':'设计异能组织的格局与冲突','prompt_key':'org_design'},
         {'step':3,'name':'日常战斗','desc':'设计融入日常生活的异能战斗','prompt_key':'urban_combat'},
         {'step':4,'name':'成长升级','desc':'规划能力成长与世界观扩展','prompt_key':'growth_path'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'power_system': '你是异能体系设计师。设计：1)异能来源(觉醒/遗传/实验) 2)分类体系(自然系/概念系/规则系) 3)等级划分与晋升条件 4)异能限制与代价。',
         'org_design': '你是势力格局设计师。设计：1)官方异能管理机构 2)地下异能组织 3)中立势力 4)敌对势力。每个势力的目标、核心人物、特色能力。',
         'urban_combat': '你是都市战斗设计师。设计：1)如何在城市环境隐藏异能战斗 2)善用日常物品适配异能 3)战斗节奏与创意应用 4)战斗对日常生活的涟漪效应。',
         'growth_path': '你是成长路径规划师。规划：1)主角能力进化阶梯 2)每阶段对手匹配 3)组织中的职级晋升 4)从街头到世界的格局跨越。',
     }, ensure_ascii=False)},
    {'name': '轻小说日式创作', 'description': '卷形式+角色驱动+日常与冒险，番茄轻小说全流程', 'genre': 'light_novel', 'book_type': 'novel', 'icon':'📚',
     'stage_keys': json.dumps(['character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'角色设计','desc':'设计萌点角色与关系','prompt_key':'character_moe'},
         {'step':2,'name':'每卷规划','desc':'规划卷结构与每卷故事','prompt_key':'volume_plan'},
         {'step':3,'name':'日常冒险','desc':'平衡日常与冒险的比例','prompt_key':'daily_adventure'},
         {'step':4,'name':'插画脚本','desc':'生成插画场景描述','prompt_key':'illustration_script'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'character_moe': '你是轻小说角色设计师。为每个角色设计：1)外貌萌点(发型/瞳色/特征) 2)性格标签与口头禅 3)能力设定 4)与其他角色的关系模式 5)成长主题。',
         'volume_plan': '你是轻小说编辑。规划每卷：1)卷主题与核心事件 2)开卷钩子与卷尾悬念 3)角色在本卷的成长 4)插画场景建议(2-3处)。',
         'daily_adventure': '你是轻小说节奏师。规划：1)日常场景(校园/职场/宿舍) 2)冒险/战斗场景 3)日常与冒险的切换节奏 4)每次冒险对日常的改变。',
         'illustration_script': '你是插画脚本师。为关键场景写脚本：1)场景氛围 2)角色表情与姿势 3)构图建议 4)背景描述。适配轻小说黑白插画风格。',
     }, ensure_ascii=False)},
    {'name': '军事谍战风云', 'description': '特种作战+情报博弈+家国情怀，番茄军事全流程', 'genre': 'military', 'book_type': 'novel', 'icon':'🎖️',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'时代背景','desc':'设定军事/谍战的时代与国际格局','prompt_key':'era_geopolitics'},
         {'step':2,'name':'战术作战','desc':'设计特种作战与战术细节','prompt_key':'tactics'},
         {'step':3,'name':'情报博弈','desc':'设计情报战与心理博弈','prompt_key':'intel_war'},
         {'step':4,'name':'人物弧线','desc':'设计军人的成长与牺牲','prompt_key':'soldier_arc'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'era_geopolitics': '你是军事背景设定师。设定：1)时代与国际格局 2)各方军事实力对比 3)核心矛盾与冲突缘由 4)武器技术发展水平。',
         'tactics': '你是战术顾问。设计：1)特种作战方案 2)小队编制与分工 3)装备与武器系统 4)地形利用与战术机动 5)撤退与应急预案。',
         'intel_war': '你是情报战专家。设计：1)情报获取方式(SIGINT/HUMINT) 2)敌方反情报能力 3)欺骗与反欺骗 4)情报分析关键节点 5)情报泄露与应急处理。',
         'soldier_arc': '你是军事人物设计师。规划：1)主角从新兵到精锐的成长 2)战友情的建立与考验 3)道德困境与选择 4)牺牲与荣耀的平衡 5)和平年代的军人价值。',
     }, ensure_ascii=False)},

    {'name': '长篇小说创作全流程', 'description': '从一句话构思到百万字完稿，总纲→卷纲→章纲→正文→审稿→记忆沉淀全链路', 'genre': 'other', 'book_type': 'novel', 'icon':'📖',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'一句话构思','desc':'用一句话提炼核心创意，AI扩展为3个方向供选择','prompt_key':'one_line_concept'},
         {'step':2,'name':'总纲设计','desc':'确定全书主线、卷数规划、核心冲突','prompt_key':'master_outline'},
         {'step':3,'name':'卷纲拆解','desc':'按卷规划：卷目标、关键事件、人物成长','prompt_key':'volume_breakdown'},
         {'step':4,'name':'章纲细化','desc':'每章目标、关键场景、字数预估','prompt_key':'chapter_plan'},
         {'step':5,'name':'正文写作','desc':'备上下文→起草→审查→润色→记录事实','prompt_key':'write_chapter'},
         {'step':6,'name':'记忆沉淀','desc':'每章写完后提取事实更新设定库','prompt_key':'memory_update'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'one_line_concept': '你是网文创意策划师。根据用户给出的一句话构思，扩展为3个不同方向的完整创意方案，每个包含：1)核心卖点 2)目标读者 3)主线冲突 4)独特亮点 5)预期字数。让用户选择最适合的方向。',
         'master_outline': '你是长篇网文总纲设计师。规划全书：1)核心主线（一句话）2)分卷规划（每卷核心目标与字数）3)主角成长弧线 4)主要势力格局 5)核心矛盾递进 6)大结局方向。确保总纲能支撑百万字以上。',
         'volume_breakdown': '你是卷纲设计师。为指定卷规划：1)卷核心目标 2)主要冲突线 3)关键转折点(2-3个) 4)新出场人物 5)人物关系变化 6)卷尾高潮与悬念 7)预估章数与字数。',
         'chapter_plan': '你是章纲设计师。为每章规划：1)章节目标 2)关键场景(2-3个) 3)出场人物 4)信息释放量 5)章尾钩子 6)预估字数(2400字±100)。保持章与章之间的节奏衔接。',
         'write_chapter': '你是专业网文写手。根据章纲写正文：1)严格遵循章纲目标 2)保持人物性格一致 3)控制节奏张弛有度 4)章尾留追读钩子 5)避免信息倾泻 6)每章2400字±100。写作前回顾项目宪法和前文摘要。',
         'memory_update': '你是记忆管理助手。从刚写完的章节中提取：1)新出场人物及特征 2)人物关系变化 3)新出现的设定/规则 4)埋下的伏笔 5)回收的伏笔 6)地点变更 7)时间线推进。输出结构化JSON供更新设定库。',
     }, ensure_ascii=False)},

    {'name': '正文写作工作流', 'description': '单章写作流水线：备上下文→起草→审查→润色→去AI味→记录事实→备份', 'genre': 'other', 'book_type': 'novel', 'icon':'✍️',
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'上下文准备','desc':'组装写作上下文包：前文摘要+设定+人物状态+伏笔','prompt_key':'context_pack'},
         {'step':2,'name':'正文起草','desc':'根据章纲和上下文写初稿','prompt_key':'draft_writing'},
         {'step':3,'name':'逻辑审查','desc':'检查因果链、时间线、人设一致性','prompt_key':'logic_check'},
         {'step':4,'name':'润色优化','desc':'优化文笔、对话、节奏','prompt_key':'polish'},
         {'step':5,'name':'去AI味终检','desc':'识别并消除AI写作痕迹','prompt_key':'de_ai_check'},
         {'step':6,'name':'事实提取','desc':'提取本章新增设定/伏笔/人物变化','prompt_key':'fact_extract'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'context_pack': '你是上下文管理助手。为当前章节组装写作上下文：1)前一章结尾(200字) 2)本章章纲 3)相关人物当前状态 4)活跃伏笔列表 5)世界览权限(本章不可违反的规则) 6)读者已知信息边界。输出结构化上下文包。',
         'draft_writing': '你是专业网文写手。根据上下文包和章纲写正文初稿。要求：1)严格遵循章纲 2)人物行为符合设定 3)对话自然有个性 4)场景描写简洁有力 5)节奏紧凑不注水 6)章尾留钩子。',
         'logic_check': '你是逻辑审查员。检查初稿：1)因果链是否完整 2)时间线是否自洽 3)人物行为是否符合设定 4)是否有信息矛盾 5)伏笔使用是否合理。输出问题清单和修复建议。',
         'polish': '你是文笔润色师。优化初稿：1)删减冗余描写 2)强化对话个性 3)调整节奏松紧 4)增加感官细节 5)优化转场衔接 6)提升金句密度。保持原意不变，只改表达。',
         'de_ai_check': '你是去AI味检测师。检查文本中的AI写作痕迹：1)过度工整的排比句式 2)"不仅...而且..."等模板化句式 3)每段长度过于均匀 4)缺少口语化表达 5)情感描写过于直白("他感到很悲伤") 6)转场生硬 7)总结性语句过多。指出具体位置并给出修改建议。',
         'fact_extract': '你是事实提取助手。从本章正文中提取：1)新出场人物(名字、外貌、性格特征) 2)人物关系变化 3)新设定/规则 4)埋下的伏笔(位置、内容) 5)回收的伏笔 6)地点信息 7)物品信息 8)时间推进。输出结构化JSON。',
     }, ensure_ascii=False)},

    {'name': '去AI味儿改稿心法', 'description': '三步去AI味：查痕迹→改表达→补人味，保留作者个人风格不被AI覆盖', 'genre': 'other', 'book_type': 'short_story', 'icon':'🎭',
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'AI痕迹检测','desc':'扫描全文识别AI写作套路','prompt_key':'detect_ai'},
         {'step':2,'name':'最小幅度改写','desc':'只改AI痕迹，不动作者原意','prompt_key':'minimal_rewrite'},
         {'step':3,'name':'人味儿补全','desc':'补充分散的、不完美的、真实感细节','prompt_key':'humanize'},
         {'step':4,'name':'终检自查','desc':'逐段对照AI写作特征清单','prompt_key':'final_check'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'detect_ai': '你是AI写作痕迹检测专家。逐段扫描文本，标记以下AI写作特征：1)排比句过多(连续3个以上相同句式) 2)模板化过渡("然而""不过""不禁""不由得") 3)每段长度过于均匀(缺少长短交替) 4)情感直述而非展示("他感到愤怒"vs"拳头攥紧") 5)总结性段落(段尾总结本段内容) 6)信息倾泻(大段设定解说) 7)对话过于书面化 8)缺少口语语气词 9)转场过于工整 10)形容词堆叠。输出每处位置和具体问题。',
         'minimal_rewrite': '你是改稿编辑。原则：编辑文字，不抹去文字背后的人。只修改被标记的AI痕迹，保持作者原意和风格不变。1)把模板句式改为自然表达 2)打散过于均匀的段落节奏 3)把情感直述改为行为展示 4)删除总结性段落 5)对话加入口语感。不要改写没问题的部分。',
         'humanize': '你是人味儿补全师。在保持原意的基础上增加真实感：1)加入不完美的细节(结巴、重复、打断) 2)增加感官碎片(气味、温度、触感) 3)插入人物的小动作/微表情 4)对话加入语气词和断句 5)适当留白不把话说满 6)加入个人化的比喻(基于人物视角)。关键是让文字背后站着一个具体的人。',
         'final_check': '你是终检审查员。对照以下清单逐段检查：1)是否还有排比句式 2)过渡词是否过多 3)段落节奏是否有变化 4)情感是否还在"直述" 5)对话是否足够口语化 6)是否有总结性段落残留 7)整体读感是否像人写的。输出通过率(百分比)和剩余问题。',
     }, ensure_ascii=False)},

    {'name': '长篇小说防遗忘系统', 'description': '混合记忆+伏笔追踪+角色认知管理，写到300章依然设定不崩、伏笔不漏', 'genre': 'other', 'book_type': 'novel', 'icon':'🧠',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'设定锁定','desc':'生成不可变事实清单，后续章节强制遵循','prompt_key':'lock_facts'},
         {'step':2,'name':'伏笔登记','desc':'建立伏笔线索库，标记待回收伏笔','prompt_key':'foreshadow_register'},
         {'step':3,'name':'角色认知','desc':'管理每个角色知道什么/不知道什么','prompt_key':'character_cognition'},
         {'step':4,'name':'写前检索','desc':'写新章前检索相关设定、伏笔、人物状态','prompt_key':'pre_write_query'},
         {'step':5,'name':'一致性校验','desc':'写完后检查是否违反已锁定设定','prompt_key':'consistency_check'},
         {'step':6,'name':'叙事债务','desc':'追踪悬念承诺与兑现平衡','prompt_key':'narrative_debt'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'lock_facts': '你是设定锁定员。从世界观、人物档案、大纲中提取不可变的核心事实，生成锁定清单：1)人物核心设定(名字、年龄、能力上限、性格底线) 2)世界规则(物理法则、力量体系、社会结构) 3)已发生的关键事件 4)已确定的人物关系。每条标注"不可变更"原因。后续章节必须遵循。',
         'foreshadow_register': '你是伏笔管理师。建立伏笔线索库：1)扫描已有内容找出所有伏笔 2)每条记录：位置、内容、预期回收章节、当前状态(待回收/已回收/已废弃) 3)按紧急度排序(离预期回收章节越近越紧急) 4)标记"叙事债务"(承诺了但还没兑现的悬念)。输出伏笔清单。',
         'character_cognition': '你是角色认知管理师。为每个角色建立认知档案：1)角色知道什么(信息列表) 2)角色不知道什么(禁忌信息) 3)读者知道但角色不知道的(信息差) 4)角色之间的关系认知(A认为B是盟友，实际B是间谍)。写新场景前先查角色认知，不该知道的就是不知道。',
         'pre_write_query': '你是上下文检索助手。写新章前检索：1)本章涉及的人物当前状态 2)本章涉及的地点信息 3)活跃伏笔(需在本章提及或推进的) 4)相关已锁定设定 5)前一章结尾(衔接) 6)角色认知边界(本章角色应知道/不知道什么)。输出写作上下文包。',
         'consistency_check': '你是一致性审查员。检查最新章节是否违反：1)已锁定的人物设定(名字记错、能力超限、性格突变) 2)世界规则(违反已建立的物理/魔法法则) 3)时间线(时间倒流、年龄错误) 4)人物认知(角色知道了不该知道的信息) 5)伏笔状态(已回收的伏笔又被当作未回收)。输出违规清单。',
         'narrative_debt': '你是叙事债务追踪师。盘点全书的悬念承诺与兑现：1)已兑现的悬念(承诺→兑现，正常) 2)待兑现的悬念(承诺了还没兑现，债务) 3)过度透支(承诺太多读者已遗忘) 4)未承诺但需要交代的信息。输出债务清单和回收优先级建议。',
     }, ensure_ascii=False)},

    {'name': '玄幻小说文风', 'description': '冷静克制硬朗文风：短句密集+对话驱动+禁词管控+长短句比例控制，玄幻正文专用文风锚定', 'genre': 'fantasy', 'book_type': 'novel', 'icon':'⚔️',
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'文风锚定','desc':'建立7维文风速查表，创作时照着写','prompt_key':'style_anchor'},
         {'step':2,'name':'正文写作','desc':'按文风铁律生成正文，短句密集对话驱动','prompt_key':'fantasy_draft'},
         {'step':3,'name':'禁词扫描','desc':'扫描禁词清单，逐词替换为口语替代','prompt_key':'forbidden_words'},
         {'step':4,'name':'句式检查','desc':'检查长短句比例、对话占比、段落节奏','prompt_key':'rhythm_check'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是玄幻文风锚定师。根据参考文本或偏好，生成7维文风速查表：1)整体风格(冷静/克制/硬朗) 2)长短句比例(短句20%-40%每句1-20字，中句40%-50%每句20-35字，长句≤20%每句35-70字) 3)对话占比(单章20%-60%) 4)叙述规则(描写服务情报和动作，不堆氛围) 5)信息推进方式(对话式推理优先) 6)章法要求(开头进场景，结尾留钩子) 7)禁用倾向(大段心理独白、形容词堆砌、总结性句子)。每维一行，创作时照着写。',
         'fantasy_draft': '你是玄幻小说写手。严格遵循以下文风铁律写作：\n【核心风格】冷静、克制、硬朗。短句密集，信息推进快。对白推动剧情，叙述只做必要交代。少抒情，少形容词堆砌，少大段心理描写。用动作、物象和对白表达情绪。悬念靠信息缺口、人物反应和新线索推进。\n【长短句比例】短句20%-40%(1-20字)用于回答/反问/动作/判断/悬念落点；中句40%-50%(20-35字)用于解释规则/交代背景/补充推理；长句≤20%(35-70字)只用于复杂设定说明或必要场面描述。超过70字的句子必须拆开。\n【对话占比】单章对白段落占比20%-60%。推理章可提高到60%以上。每轮对白只推进一个信息点。避免长篇独白。\n【叙述规则】描写要服务情报、动作和悬念。不堆氛围，不写泛泛的宏大抒情。人物情绪优先用动作体现。心理活动保持短促，避免反复解释。场景描写先给位置，再给关键物，再进入行动。\n【章法要求】每章开头尽快进入场景或事件。每章中段用对白和行动推进信息。每章结尾落在新风险、新线索或新决策上。不用空泛总结收尾。不用连续大段设定说明。\n【推荐结构】动作或场景一句→对白一句→短反应一句→继续对白→信息揭示一句。\n【禁用倾向】避免大段心理独白；避免连续堆形容词；避免用"震惊、复杂、激动"直接总结情绪；避免一段里同时写多个动作、解释和心理；避免超过70字的长句；避免使用总结性、评价性、升华的句子。\n输出2400字±100正文。',
         'forbidden_words': '你是禁词扫描员。逐句检查文本中是否出现以下禁词，发现即替换：\n【禁词清单】一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为、甚为、极为、缓缓、淡淡、轻轻、微微、毫无疑问、毋庸置疑、不言而喻、深吸一口气、眼中闪过一丝、心中暗想、心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、淡漠、漠然、眸子、嘴角微微上扬、如同、宛如、犹如、周身、周遭、气息、威压、那道身影、说话间、话音未落、当即、顿时、瞬时、因此、然而、显而易见、由此可见、总而言之、综上所述、震惊、复杂、激动。\n【替换表】一股暖流涌上心头→胸口一热/鼻子一酸；嘴角微微上扬→嘴角一翘/咧嘴；恍然大悟→哦了一声/拍了下大腿；深吸一口气→吐了口气/咬了咬牙；淡淡地说道→说/应了一声；不由得倒吸一口凉气→嘶了一声/愣住了；眼中闪过一丝寒芒→眼神一冷/目光像刀子；若有所思地点点头→点点头没说话；心中暗想→直接写内心OS；周身散发着强大气息→走过来的时候空气都变沉了。\n只输出替换后的正文，不解释改了什么。',
         'rhythm_check': '你是句式节奏检查员。逐段检查以下维度：1)短句是否占20%-40%(1-20字)？2)中句是否占40%-50%(20-35字)？3)长句是否控制在20%以内(35-70字)？4)是否存在超过70字的句子？5)对白段落是否约占20%-60%？6)每轮对白是否只推进一个信息点？7)章尾是否留下明确钩子？8)是否避免了总结性、评价性、升华的句子？9)是否避免了大段心理独白和形容词堆砌？10)描写是否服务情报、动作和悬念？11)有没有可以用动作代替的心理描写？12)内心独白是否控制在5%以内？输出每项的通过/未通过状态和具体位置。',
     }, ensure_ascii=False)},

    {'name': '番茄金番作者', 'description': '番茄男频全流程创作引擎：扫榜→方案→设定→大纲→情节→文风→逐章创作→审校，创审分离+5文件动态库+章型配额制', 'genre': 'other', 'book_type': 'novel', 'icon':'🏆',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'方案生成','desc':'扫榜趋势+爆款设定四要素+书名简介','prompt_key':'tomato_plan'},
         {'step':2,'name':'设定构建','desc':'金手指四法则+代价反噬+世界观模板','prompt_key':'tomato_setting'},
         {'step':3,'name':'人物系统','desc':'主角模板+CDL档案+配角六功能','prompt_key':'tomato_character'},
         {'step':4,'name':'分卷大纲','desc':'五幕模型+章型配额+四线并行','prompt_key':'tomato_outline'},
         {'step':5,'name':'逐章创作','desc':'文风锚定+行文铁律+对话驱动','prompt_key':'tomato_chapter'},
         {'step':6,'name':'去AI味审校','desc':'禁词扫描+浓度红线+人味注入','prompt_key':'tomato_deai'},
         {'step':7,'name':'节点诊断','desc':'章型分布+爽点审计+线程健康度','prompt_key':'tomato_diagnosis'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'tomato_plan': '你是番茄爆款方案策划师。根据用户构思生成完整方案：\n【爆款设定四要素】1)核心梗=身份/处境+反差/反常识+爽点预期(一句话能说清) 2)身份矛盾天然存在贯穿全书 3)可视化修炼体系让读者随时知道主角多强 4)核心恐惧/软肋让读者心疼。\n【书名】≤15字：身份标签+反差/爽点+情绪词。"开局XX"是万能前缀。\n【简介】≤100字：困境(代入)→反转/金手指(希望)→看点承诺(爽点)。绝对不写世界观背景和设定说明。\n【极简启动卡】1)主角是谁+什么困境 2)他想要什么 3)谁在拦他 4)靠什么翻盘(第1-2章亮金手指)。\n适用题材：都市高武/异能/灵气复苏/系统文/重生文/末世文/玄幻/仙侠。不适用女频言情。',
         'tomato_setting': '你是番茄设定构建师。根据方案生成详细设定：\n【金手指/奇遇四法则】1)一句话说清 2)自带冲突/笑点 3)能撑长篇(可成长/可循环/可扩展) 4)和主角性格绑定。\n【金手指代价/反噬】代价必须与能力成正比，不能完全可逆，要在剧情中有实际影响。金手指加速升级或改变升级方式，不替代升级体系。每次使用记录代价点数，累积到阈值触发负面剧情。\n【世界观模板】世界名称+核心规则(一句话)+等级体系(≥9级每级标志性能力)+社会结构+核心矛盾+禁忌/规则。\n【五不妥协原则】1)绝不在开篇用大段旁白介绍世界观 2)绝不让任何一句对话没有功能 3)绝不写超过3行的段落 4)绝不在章末平淡收尾 5)绝不连续使用同一种爽点超过2次。',
         'tomato_character': '你是番茄人物设计师。根据设定生成人物档案：\n【主角模板】年龄18-25/起点低被踩或天才强/不憋屈不圣母嘴硬痞/利益驱动/≥1个共情痛点/≥1个核心恐惧/≥1个口头禅/战斗中必须有嘴炮。不能让读者觉得主角憋屈。\n【CDL角色档案】姓名/年龄/身份/外貌特征(≤3个标签)/性格标签(3-5个)/核心动机/口头禅/战斗风格/关系网/成长弧线。\n【配角六种功能】1)信息源 2)陪衬/吐槽 3)阻碍者 4)助力者 5)情感寄托 6)伏笔载体。首秀铁律：配角首出场必须有明确的剧情功能，不能只是凑数。\n【女主角色卡】独立人格不花瓶/有自己的目标和弧线/与主角关系自然递进/感情线绑定主线不工业糖精。',
         'tomato_outline': '你是番茄分卷大纲设计师。根据设定规划大纲：\n【五幕模型】立身(1-5%金手指+首打脸)→立足(5-25%站稳+配角+世界观5-8章闭环)→立势(25-50%大舞台+强对手+团队8-12章)→立威(50-75%威名+组织敌+情感12-20章)→立命(75-95%终极挑战+信念冲突)→终局(95-100%伏笔收束+蜕变)。\n【章型配额制】主线推进章M(50%)、角色深挖章C(10%)、世界观展开章W(10%)、日常呼吸章D(20%)、伏笔暗线章F(10%)。相邻章节章型不能相同，每20章必须包含全部5种，每8章统计偏离配额>10%则强制补回。\n【四线并行】主线(每章推进)、副线A情感(≤10章)、副线B配角(≤25章)、暗线世界观(≤50章)。\n【小故事闭环】新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子引下一事件(5-8章)。',
         'tomato_chapter': '你是番茄金番写手。严格遵循以下规则写正文：\n【行文铁律】段落≤3行，对话/动作独立成段，心理描写一句话。全章对话+OS占比≥30%。信息靠对话和行动传递不靠旁白。\n【克制铁律】四不写：不写让读者停下来欣赏的句子/不写解释情绪的句子/不写展示阅读量的句子/不写为了质感的句子。形容词每10句0形容词≥4句。比喻每章≤1个。"的"字每句≤1个。\n【句式】禁止连续3句以上"主语+谓语"，五种交替：动作前置/名词前置/环境前置/连招式/短句爆发。同主语≤连续2次，长短交替，每400字≥1次突变。\n【情绪直给】写外在表现不写内心感受。震惊→？？？/瞳孔地震；愤怒→面色铁青/青筋暴起；爽→嘴角上扬/嘿嘿直乐；无语→……/满头黑线；害怕→脸色煞白/腿肚子打颤。\n【番茄体】？？？震惊/！！！激动/……沉默/OS内心吐槽。\n【字数】2400字±100，句子平均≤15字，最长≤30字。\n【三明治结构】苦(困境)→甜(获得力量)→爽(反击打脸)→钩子(新信息/新困境)。爽前必有憋屈(哪怕3句话)，爽后必跟钩子。\n【章尾钩子】七种不重复：身份揭露/新危机/荒诞反转/悬念/角色危机/能力突破/世界异常。',
         'tomato_deai': '你是番茄去AI味审查员。按以下流程逐项检查并修改：\n【优先级铁律】人味>克制>流畅。删完AI味后读起来像机器人汇报→加口语碎片。太啰嗦→删修饰。磕磕绊绊→调句式。判定标准：大声读一遍，不像人说话就改。\n【必删清单(28词)】一股/一抹/不由得/不禁/随即/旋即/与此同时/颇为/甚为/极为/缓缓/淡淡/轻轻/微微/毫无疑问/毋庸置疑/不言而喻/深吸一口气/眼中闪过一丝/心中暗想/心念电转/若有所思/不知不觉间/转眼间/恍然大悟/面无表情/淡漠/漠然/眸子/嘴角微微上扬/如同/宛如/犹如/周身/周遭/气息/威压/那道身影/说话间/话音未落/当即/顿时/瞬时。\n【口语化替换】因此→所以；颇为→特别/贼；随即→马上/下一秒；显而易见→说白了；或许→估计/大概。强制使用：合着/整半天/好家伙/说白了/得了吧/拉倒吧/啥玩意/搁这/说实话/你别说。\n【AI味浓度红线≤15%】AI味特征：排比句过多/模板化过渡/段落长度均匀/情感直述/总结性段落/信息倾泻/对话书面化/缺口语语气词/转场工整/形容词堆叠。\n【人味注入】加入不完美细节(结巴/重复/打断)/感官碎片/小动作微表情/语气词和断句/适当留白/个人化比喻。只输出修改后的正文。',
         'tomato_diagnosis': '你是番茄节点诊断师。为已完成章节生成多维诊断报告：\n【基础指标】章数/总字数/均字/章型分布(M__%/C__%/W__%/D__%/F__%对比配额)/对话占比趋势。\n【质量趋势】AI味浓度(最高/最低/均值/趋势)/爽点审计(8种中使用了哪几种)/微爽密度(均__个/400字目标≥1)/钩子类型(近5章是否重复)。\n【线程健康度】主线(每章推进✅)/副线A情感(距上次__章<10✅)/副线B配角(距上次__章<25✅)/暗线世界观(距上次__章<50✅)。\n【角色出场】配角出场统计/超10章未出场(🔴需安排)。\n【代价系统】金手指代价点数__/阈值→安全/接近/已触发。\n【改进建议】针对每个🔴和⚠️给出具体修复建议。',
     }, ensure_ascii=False)},
]

def seed_skill_packs():
    existing_packs = {p.name: p for p in SkillPack.query.filter_by(is_builtin=True).all()}
    added = False
    updated = False
    for sp in SEED_SKILL_PACKS:
        if sp['name'] in existing_packs:
            # 更新已存在内置技能包的提示词（同步字数等变更）
            pack = existing_packs[sp['name']]
            if pack.prompts_json != sp['prompts'] or pack.workflow_json != sp['workflow']:
                pack.prompts_json = sp['prompts']
                pack.workflow_json = sp['workflow']
                pack.description = sp['description']
                updated = True
            continue
        pack = SkillPack(
            name=sp['name'], description=sp['description'], genre=sp['genre'],
            book_type=sp['book_type'], stage_keys_json=sp['stage_keys'],
            workflow_json=sp['workflow'], prompts_json=sp['prompts'],
            is_builtin=True, icon=sp.get('icon', '📦')
        )
        db.session.add(pack)
        added = True
    if added or updated:
        db.session.commit()

@app.route('/api/skill-packs', methods=['GET'])
def list_skill_packs():
    genre = request.args.get('genre', '')
    book_type = request.args.get('book_type', '')
    query = SkillPack.query
    if genre:
        query = query.filter_by(genre=genre)
    if book_type:
        query = query.filter_by(book_type=book_type)
    packs = query.order_by(SkillPack.is_builtin.desc(), SkillPack.name).all()
    return jsonify([p.to_dict() for p in packs])

@app.route('/api/skill-packs/<pack_id>', methods=['GET'])
def get_skill_pack(pack_id):
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(pack.to_dict())

@app.route('/api/books/<book_id>/apply-skill-pack', methods=['POST'])
def apply_skill_pack(book_id):
    pack_id = request.json.get('pack_id')
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Skill pack not found'}), 404
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    prompts = json.loads(pack.prompts_json or '{}')
    for stage_key in json.loads(pack.stage_keys_json or '[]'):
        sc = StageContent.query.filter_by(book_id=book_id, stage_key=stage_key).first()
        if not sc:
            sc = StageContent(book_id=book_id, stage_key=stage_key)
            db.session.add(sc)
        stage_prompt = prompts.get(stage_key, '')
        if stage_prompt and not sc.content:
            sc.content = ''
    db.session.commit()
    return jsonify({'success': True, 'pack': pack.to_dict()})

@app.route('/api/skill-packs', methods=['POST'])
def create_skill_pack():
    data = request.json
    pack = SkillPack(
        name=data.get('name', '自定义技能'),
        description=data.get('description', ''),
        genre=data.get('genre', 'other'),
        book_type=data.get('book_type', 'novel'),
        stage_keys_json=json.dumps(data.get('stage_keys', []), ensure_ascii=False),
        workflow_json=json.dumps(data.get('workflow', []), ensure_ascii=False),
        prompts_json=json.dumps(data.get('prompts', {}), ensure_ascii=False),
        is_builtin=False,
        icon=data.get('icon', '📦')
    )
    db.session.add(pack)
    db.session.commit()
    return jsonify(pack.to_dict()), 201

@app.route('/api/skill-packs/<pack_id>', methods=['PUT'])
def update_skill_pack(pack_id):
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Not found'}), 404
    if pack.is_builtin:
        return jsonify({'error': '内置技能包不可修改'}), 403
    data = request.json
    for field in ['name', 'description', 'genre', 'book_type', 'icon']:
        if field in data:
            setattr(pack, field, data[field])
    if 'stage_keys' in data:
        pack.stage_keys_json = json.dumps(data['stage_keys'], ensure_ascii=False)
    if 'workflow' in data:
        pack.workflow_json = json.dumps(data['workflow'], ensure_ascii=False)
    if 'prompts' in data:
        pack.prompts_json = json.dumps(data['prompts'], ensure_ascii=False)
    db.session.commit()
    return jsonify(pack.to_dict())

@app.route('/api/skill-packs/<pack_id>', methods=['DELETE'])
def delete_skill_pack(pack_id):
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Not found'}), 404
    if pack.is_builtin:
        return jsonify({'error': '内置技能包不可删除'}), 403
    db.session.delete(pack)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/skill-packs/<pack_id>/clone', methods=['POST'])
def clone_skill_pack(pack_id):
    """克隆内置技能包为用户自己的技能包"""
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Not found'}), 404
    data = request.json or {}
    new_pack = SkillPack(
        name=data.get('name', f'{pack.name}（我的副本）'),
        description=pack.description,
        genre=pack.genre,
        book_type=pack.book_type,
        stage_keys_json=pack.stage_keys_json,
        workflow_json=pack.workflow_json,
        prompts_json=pack.prompts_json,
        is_builtin=False,
        icon=pack.icon
    )
    db.session.add(new_pack)
    db.session.commit()
    return jsonify(new_pack.to_dict()), 201

@app.route('/api/skill-packs/<pack_id>/publish', methods=['POST'])
def publish_skill_pack(pack_id):
    """将用户技能包发布为系统技能包，供所有用户使用"""
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': 'Not found'}), 404
    if pack.is_builtin:
        return jsonify({'error': '该技能包已是系统技能包'}), 400
    # 检查重名
    existing = SkillPack.query.filter_by(name=pack.name, is_builtin=True).first()
    if existing:
        return jsonify({'error': f'系统已存在同名技能包「{pack.name}」，请先重命名'}), 409
    pack.is_builtin = True
    db.session.commit()
    return jsonify(pack.to_dict())

@app.route('/api/analyze-book', methods=['POST'])
def analyze_book():
    config = AIConfig.query.first()
    api_key = os.environ.get('USER_LLM_API_KEY', '')
    base_url = os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    if config and config.api_key:
        api_key, base_url, model = config.api_key, config.base_url, config.model

    text = request.json.get('content', '')
    if not text.strip():
        return jsonify({'error': 'No content'}), 400
    text = text[:20000]

    system_prompt = """你是专业网文拆书分析师。分析提供的作品片段，严格按JSON格式输出。

输出格式（严格JSON）：
{"style_analysis": "文风特点(50字内)", "structure_analysis": "结构特点(50字内)", "rhythm_analysis": "节奏特点(50字内)",
"character_design_analysis": "人设特点(50字内)", "hook_techniques": ["钩子技巧1","钩子技巧2"],
"golden_lines": ["金句1","金句2"], "genre_tags": ["标签1","标签2","标签3"],
"target_platform": "番茄/起点/七猫/知乎盐选", "learnable_points": ["可学习的点1","可学习的点2","可学习的点3"]}"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role':'system','content':system_prompt},{'role':'user','content':text}],
                  'temperature': 0.3, 'max_tokens': 1500, 'response_format': {'type': 'json_object'}},
            timeout=120)
        result = resp.json()
        return jsonify(json.loads(result['choices'][0]['message']['content']))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# AI 一致性检查 / 继续写作
@app.route('/api/books/<book_id>/ai-continue', methods=['POST'])
def ai_continue(book_id):
    book = Book.query.get(book_id)
    if not book: return jsonify({'error': 'Not found'}), 404
    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    bb = BookBible.query.filter_by(book_id=book_id).first()
    bible_context = bb.generated_summary or '' if bb else ''
    if not bible_context:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    # 优先使用动态报告作为前文上下文（节省token），没有报告时回退到章节摘要
    recent_reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start.desc()
    ).limit(3).all()
    recent_reports.reverse()

    if recent_reports:
        # 使用动态报告作为前文记忆
        report_context = '\n\n'.join([f'【{r.title}】\n{r.content}' for r in recent_reports if r.content])
        # 仍取最近1章的尾部作为即时衔接
        recent_text = (chapters[-1].content or '')[-600:] if chapters else ''
        memory_section = f"""前文动态记忆（防遗忘摘要）：
{report_context}

最近章节衔接：
{recent_text}"""
    else:
        # 没有动态报告时，回退到旧逻辑：取最近3章摘要
        recent_text = '\n'.join([(c.content or '')[:500] for c in chapters[-3:]]) if chapters else ''
        memory_section = f'最近内容：\n{recent_text[:2000]}'

    instruction = request.json.get('instruction', '继续写下一章')

    system_prompt = f"""你是专业网文作者，正在协作写一本小说。
项目宪法（必须严格遵守）：
{bible_context[:3000]}

{memory_section}

写作要求：
1. 严格遵循项目宪法中的设定
2. 保持前后人物性格一致
3. 延续现有文风
4. 控制每章2400字±100"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role':'system','content':system_prompt},{'role':'user','content':instruction}],
                  'temperature': 0.7, 'max_tokens': 4000},
            timeout=180)
        result = resp.json()
        return jsonify({'content': result['choices'][0]['message']['content']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==== AI 共创 / 头脑风暴 ====

@app.route('/api/books/<book_id>/brainstorm', methods=['POST'])
def ai_brainstorm(book_id):
    """AI协同创作：用户给出一句话构思，AI返回多维度选项建议供用户选择"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    concept = request.json.get('concept', '')
    dimension = request.json.get('dimension', 'all')  # all/worldview/character/plot/locations/foreshadowing

    if not concept.strip():
        return jsonify({'error': '请输入一句话构思'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    existing_context = ''
    if bb and bb.generated_summary:
        existing_context = bb.generated_summary[:2000]

    dimension_prompts = {
        'concept': '构思扩展：为这个构思设计3个不同方向的创意方案。每个方案包含：核心卖点、目标读者、主线冲突、独特亮点。每个方案100-200字。',
        'settings': '核心设定：为这个构思设计3套不同的核心规则方案。每套包含：世界观必须遵循的规则、人物能力限制、禁忌事项、特殊机制。每套100-200字。',
        'outline': '大纲方向：为这个构思设计3条不同的大纲方案。每条包含：核心主线、分卷规划（每卷目标）、关键转折点、高潮设计、结局走向。每条100-200字。',
        'worldview': '世界观设定：为这个构思设计3个不同方向的世界观方案。每个方案包含：世界名称、核心规则(力量体系/科技水平/社会结构)、独特亮点、潜在冲突源。每个方案100-200字。',
        'character': '人物设计：为这个构思设计3组主角方案。每组包含：主角姓名、身份背景、性格特征(3个标签)、核心动机、成长弧线方向、独特能力/特质。每组100-200字。',
        'plot': '剧情方向：为这个构思设计3条不同的主线剧情方向。每条包含：核心冲突、主线事件链(3-5个关键节点)、高潮设计、结局走向。每条100-200字。',
        'chapters': '章节规划：为这个构思设计3种不同的前5章开篇方案。每种包含：每章标题和核心内容概要(50字内)、开篇钩子设计、节奏安排。每种150-250字。',
        'locations': '地点设计：为这个构思设计3个关键地点。每个包含：地点名称、地理特征、文化氛围、潜在事件、与主线的关系。每个80-150字。',
        'foreshadowing': '伏笔设计：为这个构思设计3条可埋设的伏笔线索。每条包含：伏笔内容、埋设时机、预期回收章节范围、回收方式、对剧情的影响。每条80-150字。',
    }

    if dimension == 'all':
        task = '为这个构思生成完整的创作建议方案，包含以下维度，每个维度给出3个选项：\n'
        for dim, prompt in dimension_prompts.items():
            task += f'\n### {dim}\n{prompt}\n'
    else:
        task = dimension_prompts.get(dimension, dimension_prompts['worldview'])

    system_prompt = f"""你是资深网文创意策划师，服务于番茄小说/起点中文网。用户正在创作一部{book.book_type}，题材为{book.genre}。

用户的一句话构思：{concept}

{f'已有设定上下文：{existing_context}' if existing_context else '这是全新创作，暂无已有设定。'}

请根据构思生成创作建议。严格按JSON格式输出，不要任何其他文字：
{{
  "concept_analysis": "对构思的分析(50字内)，指出核心卖点和潜在方向",
  "suggestions": {{
    "concept": [{{"title": "方案名", "description": "详细描述"}}],
    "settings": [{{"title": "方案名", "description": "详细描述"}}],
    "outline": [{{"title": "方案名", "description": "详细描述"}}],
    "worldview": [{{"title": "方案名", "description": "详细描述"}}],
    "character": [{{"title": "方案名", "description": "详细描述"}}],
    "plot": [{{"title": "方案名", "description": "详细描述"}}],
    "chapters": [{{"title": "方案名", "description": "详细描述"}}],
    "locations": [{{"title": "方案名", "description": "详细描述"}}],
    "foreshadowing": [{{"title": "方案名", "description": "详细描述"}}]
  }}
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': task}
                ],
                'temperature': 0.8,
                'max_tokens': 8000,
                'response_format': {'type': 'json_object'}
            },
            timeout=180)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        return jsonify(json.loads(content))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/books/<book_id>/ai-analyze-content', methods=['POST'])
@login_required
def ai_analyze_content(book_id):
    """AI分析作品内容，自动提取并填充构思、设定、大纲、世界观、人物、剧情、伏笔、地点等维度"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    config = AIConfig.query.first()
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
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
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


@app.route('/api/books/<book_id>/ai-analyze-dimension', methods=['POST'])
@login_required
def ai_analyze_dimension(book_id):
    """AI分析作品内容，只识别并填充指定维度"""
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

    config = AIConfig.query.first()
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

    system_prompt = f"""你是专业的小说分析师。请分析以下小说内容，提取并归纳「{dim_label}」维度的设定信息。
严格按JSON格式输出，不要任何其他文字：
{{
  "{field}": "{dim_prompts.get(field, dim_label)}"
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'作品标题：{book.title}\n\n以下是作品内容：\n\n{full_text}'}
                ],
                'temperature': 0.3,
                'max_tokens': 2000,
                'response_format': {'type': 'json_object'}
            },
            timeout=120)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        analysis = json.loads(content)

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


@app.route('/api/books/<book_id>/ai-analyze-character', methods=['POST'])
@login_required
def ai_analyze_character(book_id):
    """AI从章节内容中识别单个角色信息，或识别全部角色列表"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    character_name = data.get('character_name', '')  # 指定角色名，为空则识别全部角色列表

    config = AIConfig.query.first()
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
        system_prompt = f"""你是专业的小说分析师。请从以下小说内容中，提取角色「{character_name}」的详细档案。
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
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
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


@app.route('/api/books/<book_id>/ai-analyze-plot-volume', methods=['POST'])
@login_required
def ai_analyze_plot_volume(book_id):
    """AI识别指定卷的剧情大纲"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 获取该卷下的章节
    all_chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    volume_chapters = []
    if volume_id:
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

    if not volume_chapters:
        return jsonify({'error': '该卷没有章节内容'}), 400

    full_text = ''
    max_chars = 10000
    for ch in volume_chapters:
        segment = f'【{ch.title}】\n{(ch.content or "")[:1500]}\n\n'
        if len(full_text) + len(segment) > max_chars:
            remaining = max_chars - len(full_text)
            if remaining > 200:
                full_text += segment[:remaining]
            break
        full_text += segment

    vol_label = volume_title or '全部章节'

    system_prompt = f"""你是专业的小说分析师。请分析以下「{vol_label}」的章节内容，提取该卷的剧情大纲。
严格按JSON格式输出，不要任何其他文字：
{{
  "volume": "{vol_label}",
  "main_plot": "该卷主线剧情概述（100字内）",
  "key_events": ["关键事件1", "关键事件2", "关键事件3"],
  "turning_points": ["转折点1", "转折点2"],
  "climax": "高潮场景描述",
  "ending": "该卷结尾状态",
  "foreshadowing": ["埋设的伏笔"]
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': f'作品标题：{book.title}\n卷名：{vol_label}\n\n以下是该卷内容：\n\n{full_text}'}
                ],
                'temperature': 0.3,
                'max_tokens': 2000,
                'response_format': {'type': 'json_object'}
            },
            timeout=120)
        result = resp.json()
        content = result['choices'][0]['message']['content']
        analysis = json.loads(content)

        # 存储到 timeline 字段（JSON数组，按卷组织）
        bb = BookBible.query.filter_by(book_id=book_id).first()
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

        # 更新或添加该卷的剧情
        vol_data = analysis
        vol_data['volume_id'] = volume_id
        vol_data['volume'] = vol_label
        found = False
        for i, v in enumerate(volumes_data):
            if isinstance(v, dict) and v.get('volume_id') == volume_id:
                volumes_data[i] = {**v, **vol_data}
                found = True
                break
            if isinstance(v, dict) and v.get('volume') == vol_label:
                volumes_data[i] = {**v, **vol_data}
                found = True
                break
        if not found:
            volumes_data.append(vol_data)

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

@app.route('/api/books/<book_id>/dynamic-memory', methods=['GET'])
@login_required
def get_dynamic_memory(book_id):
    """获取动态文件库（5个JSON文件）"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    dm = DynamicMemory.query.filter_by(book_id=book_id).first()
    if not dm:
        # 自动初始化空模板
        dm = DynamicMemory(book_id=book_id)
        for key in DynamicMemory.FILE_KEYS:
            setattr(dm, key, DynamicMemory.get_empty_template(key))
        db.session.add(dm)
        db.session.commit()

    return jsonify(dm.to_dict())


@app.route('/api/books/<book_id>/dynamic-memory/<file_key>', methods=['PUT'])
@login_required
def update_dynamic_memory_file(book_id, file_key):
    """更新单个动态文件"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    if file_key not in DynamicMemory.FILE_KEYS:
        return jsonify({'error': f'未知文件: {file_key}'}), 400

    data = request.get_json() or {}
    content = data.get('content', '')

    # 验证JSON格式
    if content.strip():
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return jsonify({'error': f'JSON格式错误: {str(e)}'}), 400

    dm = DynamicMemory.query.filter_by(book_id=book_id).first()
    if not dm:
        dm = DynamicMemory(book_id=book_id)
        for key in DynamicMemory.FILE_KEYS:
            if key != file_key:
                setattr(dm, key, DynamicMemory.get_empty_template(key))
        db.session.add(dm)

    setattr(dm, file_key, content)
    db.session.commit()

    return jsonify({'success': True, 'file_key': file_key, 'content': content})


@app.route('/api/books/<book_id>/dynamic-memory/init', methods=['POST'])
@login_required
def init_dynamic_memory(book_id):
    """初始化动态文件库（用空模板填充所有文件）"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    dm = DynamicMemory.query.filter_by(book_id=book_id).first()
    if not dm:
        dm = DynamicMemory(book_id=book_id)
        db.session.add(dm)

    for key in DynamicMemory.FILE_KEYS:
        setattr(dm, key, DynamicMemory.get_empty_template(key))
    db.session.commit()

    return jsonify({'success': True, 'data': dm.to_dict()})


@app.route('/api/books/<book_id>/dynamic-memory/ai-generate', methods=['POST'])
@login_required
def ai_generate_dynamic_memory(book_id):
    """AI生成/更新动态文件库（基于已有章节内容）"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    data = request.get_json() or {}
    file_key = data.get('file_key', '')
    if file_key not in DynamicMemory.FILE_KEYS:
        return jsonify({'error': f'未知文件: {file_key}'}), 400

    # 获取作品信息
    bible = BookBible.query.filter_by(book_id=book_id).first()
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    # 拼接章节内容摘要
    chapter_summaries = []
    total_chars = 0
    max_chars = 8000
    for ch in chapters:
        summary = f'【{ch.title}】{(ch.content or "")[:500]}'
        if total_chars + len(summary) > max_chars:
            break
        chapter_summaries.append(summary)
        total_chars += len(summary)

    chapters_text = '\n\n'.join(chapter_summaries) if chapter_summaries else '暂无章节内容'

    file_descriptions = {
        'narrative_engine': '叙事引擎：包含state（书名/当前章/卷/节点/mc状态/最近事件/活跃钩子/待处理伏笔/代价点数）+ timeline[]（章号/故事天/事件/地点/角色/重要性）+ chapters[]（章号/标题/字数/摘要/关键事件/伏笔操作/钩子/爽点类型）',
        'foreshadowing_tracker': '伏笔追踪器：包含foreshadowing[]（id/类型/状态/埋设章/当前年龄/最大年龄/紧急度/描述/激活条件/计划回收章）+ scan_rules（短/中/长周期阈值+自动告警级别）',
        'character_ecosystem': '角色生态系统：包含characters[]（id/名字/角色/状态{能力/身体/精神}/CDL{驱动/恐惧/成长}/最近出场/台词量/关系温度）+ relationships[]（from/to/类型/等级/最近互动/趋势）',
        'ability_world': '能力与世界观：包含ability_log[]（章号/角色/变化/新能力/触发条件/代价变动）+ world_facts[]（id/事实/建立章/矛盾记录）',
        'health_dashboard': '健康度仪表盘：包含thread_health（四线各含最近推进章+状态）+ chapter_type_distribution + foreshadowing_aging + ai_flavor_trend[] + dialogue_ratio_trend[] + character_appearances + alerts[]',
    }

    # 获取当前文件内容
    dm = DynamicMemory.query.filter_by(book_id=book_id).first()
    current_content = ''
    if dm:
        current_content = getattr(dm, file_key, '') or ''

    system_prompt = f"""你是专业的小说分析师和创作助手。请根据小说的章节内容、设定信息，生成或更新「{file_descriptions.get(file_key, file_key)}」的JSON数据。

要求：
1. 严格输出有效的JSON格式，不要有任何其他文字
2. 中文字段名，UTF-8编码
3. 基于已有章节内容进行分析和提取
4. 如果已有当前文件内容，在原有基础上更新而非完全重写
5. 未涉及的字段保持原值或空值"""

    user_content = f"""作品标题：{book.title}
题材：{book.genre or '通用'}
类型：{book.book_type or '小说'}

构思：{bible.concept if bible else '无'}
世界观：{(bible.worldbuilding[:300] if bible and bible.worldbuilding else '无')}
人物：{(bible.character_profiles[:300] if bible and bible.character_profiles else '无')}
大纲：{(bible.plot_design[:300] if bible and bible.plot_design else '无')}

已有章节内容摘要：
{chapters_text}

当前{file_key}文件内容：
{current_content if current_content else '（空，请初始化）'}

请生成完整的JSON数据："""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content}
                ],
                'temperature': 0.4,
                'max_tokens': 4000,
                'response_format': {'type': 'json_object'}
            },
            timeout=180)

        result = resp.json()
        content = result['choices'][0]['message']['content']

        # 验证返回的JSON
        try:
            parsed = json.loads(content)
            content = json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass  # 保留原始内容

        # 保存到数据库
        if not dm:
            dm = DynamicMemory(book_id=book_id)
            db.session.add(dm)
        setattr(dm, file_key, content)
        db.session.commit()

        return jsonify({
            'success': True,
            'file_key': file_key,
            'content': content,
            'data': dm.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==== 动态报告 API（防遗忘摘要系统） ====

DYNAMIC_REPORT_INTERVAL = 5  # 每5章生成一份报告


def _generate_dynamic_report_content(book_id, chapter_start, chapter_end):
    """内部函数：调用AI生成动态报告内容"""
    book = Book.query.get(book_id)
    if not book:
        return None, 'Book not found'

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return None, '请先配置 AI 模型 API Key'

    bible = BookBible.query.filter_by(book_id=book_id).first()
    # 获取指定范围的章节（按order_index排序，非卷标）
    all_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    # chapter_start/chapter_end 是1-based的章号
    target_chapters = all_chapters[chapter_start - 1:chapter_end] if chapter_start <= len(all_chapters) else []

    if not target_chapters:
        return None, '指定范围内无章节'

    # 拼接章节内容（每章截取前800字，控制总量）
    chapters_text = []
    total_chars = 0
    max_chars = 6000
    for ch in target_chapters:
        snippet = f'【{ch.title}】\n{(ch.content or "")[:800]}'
        if total_chars + len(snippet) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 100:
                snippet = snippet[:remaining]
                chapters_text.append(snippet)
            break
        chapters_text.append(snippet)
        total_chars += len(snippet)

    chapters_content = '\n\n'.join(chapters_text)

    system_prompt = f"""你是专业的小说分析师。请仔细阅读以下第{chapter_start}章到第{chapter_end}章的内容，汇总成一份简洁的报告。

报告必须包含以下要素（如果出现的话），总字数不超过500字：

1. 【人物】本章新出场或有关键表现的角色及其状态变化
2. 【事件】关键剧情事件和转折
3. 【时间】故事内时间线推进
4. 【地点】涉及的重要地点
5. 【势力】出现或变动的势力/组织
6. 【伏笔】埋设或回收的伏笔
7. 【境界】角色境界/实力变化
8. 【关系】人物关系变化
9. 【物资】主要角色当前拥有的重要物品、装备、功法、丹药等（按角色列出）

格式要求：
- 用简洁的条目式写法，每类1-3条
- 只记录关键信息，不展开描述
- 不要写废话和过渡句
- 直接输出报告内容，不要加标题和前后缀"""

    user_content = f"""作品：{book.title}
题材：{book.genre or '通用'}

第{chapter_start}章到第{chapter_end}章内容：
{chapters_content}

请生成动态报告（≤500字）："""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_content}
                ],
                'temperature': 0.3,
                'max_tokens': 1200,
            },
            timeout=120)

        result = resp.json()
        content = result['choices'][0]['message']['content'].strip()
        return content, None
    except Exception as e:
        return None, str(e)


def _check_and_auto_generate_report(book_id):
    """检查是否需要自动生成动态报告（每5章触发）"""
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    chapter_count = len(chapters)
    if chapter_count == 0:
        return None

    # 计算当前应该有报告的最后一个区间
    # 例如：5章 -> 区间1-5，10章 -> 区间1-5和6-10
    current_end = (chapter_count // DYNAMIC_REPORT_INTERVAL) * DYNAMIC_REPORT_INTERVAL
    if current_end == 0:
        return None

    # 检查该区间是否已有报告
    existing = DynamicReport.query.filter_by(
        book_id=book_id, chapter_start=current_end - DYNAMIC_REPORT_INTERVAL + 1,
        chapter_end=current_end
    ).first()

    if existing:
        return None  # 已有报告，不重复生成

    # 需要生成新报告
    chapter_start = current_end - DYNAMIC_REPORT_INTERVAL + 1
    content, error = _generate_dynamic_report_content(book_id, chapter_start, current_end)
    if error:
        return {'error': error}

    title = f'动态-({chapter_start}-{current_end}章)'
    report = DynamicReport(
        book_id=book_id, title=title, content=content or '',
        chapter_start=chapter_start, chapter_end=current_end,
        auto_generated=True
    )
    db.session.add(report)
    db.session.commit()
    return {'report': report.to_dict()}


@app.route('/api/books/<book_id>/dynamic-reports', methods=['GET'])
@login_required
def list_dynamic_reports(book_id):
    """获取所有动态报告"""
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
    return jsonify([r.to_dict() for r in reports])


@app.route('/api/books/<book_id>/dynamic-reports', methods=['POST'])
@login_required
def create_dynamic_report(book_id):
    """手动创建动态报告"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    chapter_start = data.get('chapter_start', 0)
    chapter_end = data.get('chapter_end', 0)

    if not chapter_start or not chapter_end or chapter_end < chapter_start:
        return jsonify({'error': '请指定有效的章节范围'}), 400

    # 如果没有提供content，调用AI生成
    content = data.get('content', '')
    if not content:
        content, error = _generate_dynamic_report_content(book_id, chapter_start, chapter_end)
        if error:
            return jsonify({'error': error}), 500

    title = data.get('title', f'动态-({chapter_start}-{chapter_end}章)')
    report = DynamicReport(
        book_id=book_id, title=title, content=content,
        chapter_start=chapter_start, chapter_end=chapter_end,
        auto_generated=False
    )
    db.session.add(report)
    db.session.commit()
    return jsonify(report.to_dict()), 201


@app.route('/api/books/<book_id>/dynamic-reports/<report_id>', methods=['PUT'])
@login_required
def update_dynamic_report(book_id, report_id):
    """更新动态报告"""
    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404

    data = request.get_json() or {}
    for field in ['title', 'content', 'chapter_start', 'chapter_end']:
        if field in data:
            setattr(report, field, data[field])
    db.session.commit()
    return jsonify(report.to_dict())


@app.route('/api/books/<book_id>/dynamic-reports/<report_id>', methods=['DELETE'])
@login_required
def delete_dynamic_report(book_id, report_id):
    """删除动态报告"""
    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404
    db.session.delete(report)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/books/<book_id>/dynamic-reports/<report_id>/regenerate', methods=['POST'])
@login_required
def regenerate_dynamic_report(book_id, report_id):
    """重新生成动态报告内容（AI）"""
    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404

    content, error = _generate_dynamic_report_content(book_id, report.chapter_start, report.chapter_end)
    if error:
        return jsonify({'error': error}), 500

    report.content = content or ''
    db.session.commit()
    return jsonify(report.to_dict())


@app.route('/api/books/<book_id>/dynamic-reports/auto-check', methods=['POST'])
@login_required
def auto_check_dynamic_report(book_id):
    """检查并自动生成动态报告（章节保存后触发）"""
    result = _check_and_auto_generate_report(book_id)
    if result is None:
        return jsonify({'success': True, 'message': '无需生成新报告', 'report': None})
    if 'error' in result:
        return jsonify({'success': False, 'error': result['error']}), 500
    return jsonify({'success': True, 'message': '已自动生成动态报告', 'report': result['report']})


@app.route('/api/books/<book_id>/dynamic-reports/context', methods=['GET'])
@login_required
def get_dynamic_report_context(book_id):
    """获取最近的动态报告内容（用于AI创作时注入上下文，减少token）"""
    # 返回最近3份报告
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start.desc()
    ).limit(3).all()
    # 按正序返回
    reports.reverse()
    return jsonify({
        'reports': [r.to_dict() for r in reports],
        'context_text': '\n\n'.join([f'【{r.title}】\n{r.content}' for r in reports if r.content])
    })


@app.route('/api/books/<book_id>/ai-analyze-from-reports', methods=['POST'])
@login_required
def ai_analyze_from_reports(book_id):
    """从动态文件报告提取维度信息（地图/关系图谱/地点图谱/境界图谱等），节省token"""
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
        'relationGraph': 'character_profiles',
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

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 获取动态报告作为分析源（优先），没有时回退到章节内容
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start
    ).all()

    if reports:
        source_text = '\n\n'.join([f'【{r.title}】\n{r.content}' for r in reports if r.content])
        source_type = '动态文件报告'
    else:
        # 回退：取章节内容摘要
        chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
        if not chapters:
            return jsonify({'error': '作品没有章节内容，无法分析'}), 400
        source_text = ''
        max_chars = 8000
        for ch in chapters:
            segment = f'【{ch.title}】\n{(ch.content or "")[:800]}\n\n'
            if len(source_text) + len(segment) > max_chars:
                break
            source_text += segment
        source_type = '章节内容'

    # 获取已有的bible字段内容
    bible = BookBible.query.filter_by(book_id=book_id).first()
    existing_value = getattr(bible, field, '') if bible else ''

    # 不同维度的提取提示
    dim_prompts = {
        'locations': """请从以下内容中提取所有地点信息，按三级分类整理（大区域/城市/场景）。
输出JSON格式：
{"locations": [{"name":"大区域名","desc":"描述","children":[{"name":"城市名","desc":"描述","children":[{"name":"场景名","desc":"描述"}]}]}]}
如果没有明确地点信息，输出空数组。""",

        'relationGraph': """请从以下内容中提取所有人物及其关系，整理为人物档案。
输出JSON格式：
{"character_profiles": "人物1: 姓名|身份|性格|动机\\n人物2: 姓名|身份|性格|动机\\n关系: A与B-关系类型"}
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

    system_prompt = f"""你是专业的小说分析师。请从以下{source_type}中提取「{dim_label}」维度的信息。

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
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
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


@app.route('/api/books/<book_id>/sync-analysis', methods=['POST'])
@login_required
def sync_analysis_to_book(book_id):
    """将拆书分析结果同步到作品资料（BookBible），支持仿写/同人文/参考三种模式"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json()
    analysis = data.get('analysis', {})
    mode = data.get('mode', 'reference')  # imitate / fanfic / reference

    if not analysis:
        return jsonify({'error': '缺少分析结果数据'}), 400

    # 更新或创建 BookBible
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)

    updated_fields = []
    mode_label = {'imitate': '仿写参考', 'fanfic': '同人文素材', 'reference': '方法论参考'}.get(mode, '参考')
    header = f'\n\n【拆书分析·{mode_label}】\n'

    # 风格指南：文风特点 + 节奏特点
    style_parts = []
    if analysis.get('style_analysis'):
        style_parts.append(f'文风特点：{analysis["style_analysis"]}')
    if analysis.get('rhythm_analysis'):
        style_parts.append(f'节奏特点：{analysis["rhythm_analysis"]}')
    if analysis.get('target_platform'):
        style_parts.append(f'目标平台：{analysis["target_platform"]}')
    if analysis.get('genre_tags'):
        style_parts.append(f'题材标签：{"、".join(analysis["genre_tags"])}')
    if style_parts:
        existing = bb.style_guide or ''
        bb.style_guide = (existing + header if existing else '') + '\n'.join(style_parts)
        updated_fields.append('style_guide')

    # 大纲设计：结构特点
    if analysis.get('structure_analysis'):
        existing = bb.plot_design or ''
        content = f'结构参考：{analysis["structure_analysis"]}'
        bb.plot_design = (existing + header if existing else '') + content
        updated_fields.append('plot_design')

    # 人物档案：人设特点
    if analysis.get('character_design_analysis'):
        existing = bb.character_profiles or ''
        content = f'人设参考：{analysis["character_design_analysis"]}'
        if mode == 'fanfic' and analysis.get('golden_lines'):
            content += f'\n金句参考：{"；".join(analysis["golden_lines"][:5])}'
        bb.character_profiles = (existing + header if existing else '') + content
        updated_fields.append('character_profiles')

    # 伏笔：钩子技巧 + 可学方法
    foreshadow_parts = []
    if analysis.get('hook_techniques'):
        foreshadow_parts.append('钩子技巧：\n' + '\n'.join(f'· {h}' for h in analysis['hook_techniques']))
    if analysis.get('learnable_points'):
        foreshadow_parts.append('可学方法：\n' + '\n'.join(f'· {p}' for p in analysis['learnable_points']))
    if foreshadow_parts:
        existing = bb.foreshadowing or ''
        bb.foreshadowing = (existing + header if existing else '') + '\n'.join(foreshadow_parts)
        updated_fields.append('foreshadowing')

    # 同人文模式：额外同步世界观
    if mode == 'fanfic':
        fanfic_parts = []
        if analysis.get('style_analysis'):
            fanfic_parts.append(f'原作文风：{analysis["style_analysis"]}')
        if analysis.get('structure_analysis'):
            fanfic_parts.append(f'原作结构：{analysis["structure_analysis"]}')
        if analysis.get('character_design_analysis'):
            fanfic_parts.append(f'原作人设：{analysis["character_design_analysis"]}')
        if fanfic_parts:
            existing = bb.worldbuilding or ''
            bb.worldbuilding = (existing + header if existing else '') + '\n'.join(fanfic_parts)
            updated_fields.append('worldbuilding')

    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'updated_fields': updated_fields,
        'bible': bb.to_dict()
    })


# ==== File Upload / 拆书导入导出 ====

UPLOAD_DIR = DATA_DIR / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'md', 'docx', 'zip', 'json'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _detect_and_decode(raw_bytes):
    """自动检测字节流的编码并解码，支持 UTF-8 / GBK / GB18030 等中文编码"""
    # 优先尝试 UTF-8（含 BOM）
    if raw_bytes[:3] == b'\xef\xbb\xbf':
        return raw_bytes[3:].decode('utf-8', errors='replace')
    try:
        text = raw_bytes.decode('utf-8')
        # 检查是否有大量替换字符，如果有说明可能不是 UTF-8
        if text.count('\ufffd') < len(text) * 0.01:
            return text
    except UnicodeDecodeError:
        pass
    # 用 chardet 检测编码
    try:
        import chardet
        result = chardet.detect(raw_bytes)
        enc = result.get('encoding', '')
        if enc:
            try:
                return raw_bytes.decode(enc, errors='replace')
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass
    # 回退到 GB18030（兼容 GBK / GB2312）
    try:
        return raw_bytes.decode('gb18030', errors='replace')
    except Exception:
        return raw_bytes.decode('utf-8', errors='replace')


def extract_text_from_file(filepath, filename):
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext == 'txt':
        with open(filepath, 'rb') as f:
            raw = f.read()
        return _detect_and_decode(raw)
    elif ext in ('md', 'json'):
        with open(filepath, 'rb') as f:
            raw = f.read()
        return _detect_and_decode(raw)
    elif ext == 'docx':
        try:
            from docx import Document
            doc = Document(filepath)
            return '\n'.join([p.text for p in doc.paragraphs])
        except ImportError:
            return '[需要安装python-docx库来解析Word文档]'
    elif ext == 'zip':
        extracted_texts = []
        with zipfile.ZipFile(filepath, 'r') as zf:
            for name in zf.namelist():
                if name.endswith(('/', '\\')):
                    continue
                try:
                    content = zf.read(name)
                    inner_ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
                    if inner_ext in ('txt', 'md', 'json'):
                        decoded = _detect_and_decode(content)
                        extracted_texts.append(f'=== {name} ===\n{decoded}')
                    elif inner_ext == 'docx':
                        extracted_texts.append(f'=== {name} ===\n[需要python-docx库来解析Word文档]')
                except Exception:
                    pass
        return '\n\n'.join(extracted_texts)
    return ''

@app.route('/api/upload-analyze', methods=['POST'])
def upload_analyze():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file.filename or not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件格式，请上传txt/md/docx/zip/json'}), 400

    filename = secure_filename(file.filename)
    filepath = str(UPLOAD_DIR / f'{uuid.uuid4()}_{filename}')
    file.save(filepath)
    try:
        text = extract_text_from_file(filepath, filename)
        return jsonify({'filename': file.filename, 'content': text, 'length': len(text)})
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass

@app.route('/api/analyze/export', methods=['POST'])
def export_analysis():
    result = request.json
    if not result:
        return jsonify({'error': 'No data'}), 400
    export_text = json.dumps(result, ensure_ascii=False, indent=2)
    bio = BytesIO()
    bio.write(export_text.encode('utf-8'))
    bio.seek(0)
    return send_file(bio, mimetype='application/json', as_attachment=True, download_name='analysis_result.json')


def init_db():
    with app.app_context():
        db.create_all()
        # Migration: add new columns to book_bible if missing
        try:
            db.session.execute(db.text('ALTER TABLE book_bible ADD COLUMN locations TEXT'))
        except Exception:
            pass
        try:
            db.session.execute(db.text('ALTER TABLE book_bible ADD COLUMN concept TEXT'))
        except Exception:
            pass
        try:
            db.session.execute(db.text('ALTER TABLE book_bible ADD COLUMN plot_design TEXT'))
        except Exception:
            pass
        try:
            db.session.execute(db.text('ALTER TABLE ai_config ADD COLUMN recognition_model TEXT'))
        except Exception:
            pass
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        seed_builtin_templates()
        seed_prompt_templates()
        seed_skill_packs()


# ==== 前端静态文件托管（生产环境）====
# 当后端直接提供服务时，托管前端构建产物，避免前后端分离部署导致的 /api 请求失败

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """托管前端 SPA：所有非 /api 请求都返回静态文件或 index.html"""
    # 前端构建产物目录
    dist_dir = FRONTEND_DIST
    if not dist_dir.exists():
        # 如果 frontend/dist 不存在，尝试使用项目根目录（已构建的静态文件）
        root_dist = Path(__file__).parent.parent.parent
        if (root_dist / 'index.html').exists():
            dist_dir = root_dist
        else:
            return jsonify({'error': '前端构建产物未找到，请先运行 npm run build'}), 404

    # 如果请求的是具体文件且存在，直接返回
    if path:
        file_path = dist_dir / path
        if file_path.is_file():
            return send_from_directory(dist_dir, path)

    # 其他情况返回 index.html（SPA 路由回退）
    index_file = dist_dir / 'index.html'
    if index_file.exists():
        return send_from_directory(dist_dir, 'index.html')
    return jsonify({'error': 'index.html not found'}), 404


if __name__ == '__main__':
    init_db()
    # Render 等云平台通过 PORT 环境变量指定端口，本地默认 5000
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
