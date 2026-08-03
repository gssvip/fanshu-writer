import os
import re
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

from flask import Flask, request, jsonify, send_file, send_from_directory, stream_with_context
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# P0-1：确定性后写校验器（零 LLM 成本，章节生成后检测 AI 痕迹）
try:
    from post_write_validator import validate_chapter, validate_chapter_with_bible, get_repair_hints, validate_chapter_with_drift, compute_style_fingerprint
except ImportError:
    # 静默降级：模块缺失时不阻断主流程
    validate_chapter = None
    validate_chapter_with_bible = None
    get_repair_hints = None
    validate_chapter_with_drift = None
    compute_style_fingerprint = None

# P0-2：伏笔 DAG 管理器
try:
    from foreshadowing_manager import parse_text_to_dag, ForeshadowingGraph, get_hooks_for_chapter
except ImportError:
    parse_text_to_dag = None
    ForeshadowingGraph = None
    get_hooks_for_chapter = None

# P1-4：四级大纲层级构建器
try:
    from outline_hierarchy_builder import build_outline_hierarchy, build_dramatic_position_prompt, get_dramatic_context
except ImportError:
    build_outline_hierarchy = None
    build_dramatic_position_prompt = None
    get_dramatic_context = None

# P1-5：节拍模板加载器
try:
    from beat_template_loader import build_beat_prompt
except ImportError:
    build_beat_prompt = None

# P1-6 + P1-7：章级变更处理器（12类CHANGES解析 + delta回写）
try:
    from chapter_changes_processor import extract_changes, apply_chapter_changes, remove_chapter_changes, build_changes_prompt_template
except ImportError:
    extract_changes = None
    apply_chapter_changes = None
    remove_chapter_changes = None
    build_changes_prompt_template = None

# P2-9：Spot-Fix 修订路由器
try:
    from revise import route_revision, build_spot_fix_prompt, apply_spot_fix_patches, estimate_token_saving
except ImportError:
    route_revision = None
    build_spot_fix_prompt = None
    apply_spot_fix_patches = None
    estimate_token_saving = None

# P2-8：审计-修订闭环
try:
    from chapter_review_cycle import run_review_cycle, PASS_SCORE
except ImportError:
    run_review_cycle = None
    PASS_SCORE = 85

# P2-10 + P2-11：落地门禁 + PRE_WRITE_CHECK
try:
    from generation_gate import run_all_gates, build_pre_write_check_prompt
except ImportError:
    run_all_gates = None
    build_pre_write_check_prompt = None

app = Flask(__name__, static_folder=None)
app.config['SECRET_KEY'] = 'fanshu-writer-secret-key'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# 前端构建产物目录选择逻辑：
# 1. 如果设置了 FANSHU_FRONTEND_DIST 环境变量，优先使用它
# 2. 否则优先使用 backend/static（git 跟踪的预构建产物，在所有环境都完整可用）
# 3. 最后 fallback 到 frontend/dist（本地开发用，生产环境可能不完整）
_static_dist = Path(__file__).parent / 'static'
_frontend_dist_env = os.environ.get('FANSHU_FRONTEND_DIST')
if _frontend_dist_env:
    FRONTEND_DIST = Path(_frontend_dist_env)
elif (_static_dist / 'index.html').exists() and (_static_dist / 'assets').exists():
    # backend/static 完整可用（生产环境 git 跟踪的产物）
    FRONTEND_DIST = _static_dist
else:
    # 本地开发：使用 frontend/dist（vite dev/build 产物）
    FRONTEND_DIST = Path(__file__).parent.parent / 'frontend' / 'dist'

# 数据持久化目录：
# - Hugging Face Spaces: /data（持久化，需手动设置 FANSHU_DATA_DIR=/data）
# - Render: 通过 FANSHU_DATA_DIR 环境变量指定（仅用于临时文件，数据库见下）
# - 本地开发: ~/.fanshu-writer
DATA_DIR = Path(os.environ.get('FANSHU_DATA_DIR', Path.home() / '.fanshu-writer'))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================================
# 铁律：用户数据（账号及所有作品数据）必须持久化，绝不能因部署/重启而丢失。
# 实现方式：生产环境强制使用外部 PostgreSQL（DATABASE_URL），禁止回退到 SQLite。
# SQLite 仅用于本地开发。云平台（Render/HF Spaces/Railway 等）无 DATABASE_URL
# 时直接拒绝启动，避免出现"注册成功但下次部署数据消失"的假象。
# ============================================================================

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
# 检测生产环境：Render 自动设置 RENDER=true；其他云平台通常设置 PORT 或 CI=true
_IS_PROD_ENV = (
    os.environ.get('RENDER', '').lower() in ('true', '1', 'yes')
    or os.environ.get('PORT', '').strip() != ''
    or os.environ.get('HF_SPACE_ID', '').strip() != ''
    or os.environ.get('RAILWAY_PROJECT_ID', '').strip() != ''
)

if DATABASE_URL:
    # 兼容 Render/Heroku 的 postgres:// 前缀（SQLAlchemy 2.0+ 需要 postgresql://）
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    print(f'[DB] ✅ 使用 PostgreSQL 外部数据库（铁律：数据持久化，部署不丢失）', flush=True)
else:
    if _IS_PROD_ENV:
        # 生产环境铁律：必须有 DATABASE_URL，否则拒绝启动
        print('=' * 70, flush=True)
        print('[DB][铁律违规] 生产环境未配置 DATABASE_URL，拒绝启动！', flush=True)
        print('[DB][铁律违规] 用户数据必须存到外部 PostgreSQL，绝不能用 SQLite。', flush=True)
        print('[DB][铁律违规] 请在 Render Dashboard → Environment 添加：', flush=True)
        print('[DB][铁律违规]   key=DATABASE_URL  value=postgresql://user:pass@host/dbname?sslmode=require', flush=True)
        print('[DB][铁律违规] 推荐用 Neon 免费版（永久免费 0.5GB）：https://neon.tech', flush=True)
        print('=' * 70, flush=True)
        raise SystemExit('[铁律] 生产环境必须配置 DATABASE_URL，进程退出以保护用户数据')
    # 本地开发：允许 SQLite
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATA_DIR}/fanshu.db'
    print(f'[DB] ⚠️ 本地开发模式使用 SQLite：{DATA_DIR}/fanshu.db（生产环境会拒绝启动）', flush=True)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 连接池配置：防止 Neon/PG 空闲断连导致 500
# - pool_pre_ping: 每次取连接前 ping 一下，剔除死连接
# - pool_recycle: 每 1 小时回收连接，避免服务端已断开的僵尸连接
# - pool_size / max_overflow: 控制总连接数，适配免费层限制
if DATABASE_URL:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 1800,  # 30分钟回收，小于 Neon/Render 空闲超时
        'pool_size': 5,
        'max_overflow': 5,
    }

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


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    used = db.Column(db.Boolean, default=False)
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
    # 总卷数（用户自定义，不设上限）：作为五幕总纲/剧情大纲生成的核心依据
    total_volumes = db.Column(db.Integer, default=10)
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
    # 总卷数 + 风格流派（与 Book 表同步，创作时从 bible 直接读取注入各维度）
    total_volumes = db.Column(db.Integer, default=10)
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


_CN_DIGITS = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'零':0,'〇':0}
_CN_UNITS = {'十':10,'百':100,'千':1000,'万':10000,'亿':100000000}
# 全角数字 → 半角
_FULLWIDTH_DIGITS = str.maketrans('０１２３４５６７８９', '0123456789')


def _chinese_to_int(s):
    """将中文数字（如 十一/二十三/一百零五/一千二百/两万零一）转为 int。
    无法解析返回 None。支持阿拉伯数字与中文数字混用（如 2十/1百2/12）。"""
    if not s:
        return None
    total = 0
    cur = 0
    wan_part = 0  # 万位以上的累积
    yi_part = 0   # 亿位以上的累积
    has_digit = False
    has_value = False  # 是否出现过非零值
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch.isdigit():
            # 连续阿拉伯数字作为一个整体整数
            j = i
            while j < n and s[j].isdigit():
                j += 1
            cur = int(s[i:j])
            has_digit = True
            if cur != 0:
                has_value = True
            i = j
            continue
        elif ch in _CN_DIGITS:
            cur = _CN_DIGITS[ch]
            has_digit = True
            if cur != 0:
                has_value = True
            i += 1
        elif ch == '亿':
            if cur == 0 and total == 0:
                return None
            if cur == 0:
                cur = 1
            yi_part = (yi_part + wan_part + total + cur) * 100000000
            wan_part = 0
            total = 0
            cur = 0
            has_value = True
            i += 1
        elif ch == '万':
            if cur == 0 and total == 0:
                return None
            if cur == 0:
                cur = 1
            wan_part = (wan_part + total + cur) * 10000
            total = 0
            cur = 0
            has_value = True
            i += 1
        elif ch in _CN_UNITS:
            if cur == 0:
                cur = 1  # "十" 单独出现视为 10
            total += cur * _CN_UNITS[ch]
            cur = 0
            has_digit = True
            has_value = True
            i += 1
        else:
            return None  # 含非数字字符，无法解析
    if not has_digit:
        return None
    result = yi_part + wan_part + total + cur
    # 允许 0（如"第零章"），仅当原串确实含数字时
    if result == 0:
        return 0 if has_digit else None
    return result


def parse_chapter_number(title):
    """从章节标题解析章节号，返回 int 或 None。
    支持的格式（阿拉伯/中文数字可混用）：
      第N章/第N章/第N回/第N节/第N话/第N集/第N幕/第N折/第N卷/第N部/第N篇...
      Chapter N / Ch.N / CHAPTER N / Episode N / Ep.N
      N.标题 / N、标题 / N:标题 / N-标题 / N 标题 / N标题
      十一 标题（行首纯中文数字）
    支持括号包裹：【第11章】、（第十一章）、[Chapter 11]
    多个章节号时取最后一个（如 第3卷第5章 → 5）。"""
    if not title:
        return None
    import re
    # 全角数字转半角
    t = title.strip().translate(_FULLWIDTH_DIGITS)

    # 收集所有「第...后缀」模式的章节号，取最后一个（最细粒度）
    # 后缀字符集：章/节/回/卷/部/篇/话/集/幕/折/更/段/讲/课/夜/日/年/季/场 等
    suffix_class = '章节回卷部篇话集幕折更段讲课夜日年季场'
    matches = re.findall(r'第\s*([0-9零一二三四五六七八九十百千万亿两〇]+)\s*[' + suffix_class + r']', t)
    if matches:
        # 取最后一个匹配（如 第3卷第5章 → 取5）
        n = _chinese_to_int(matches[-1])
        if n is not None:
            return n

    # Chapter N / Ch.N / CHAPTER N / Episode N / Ep.N（大小写不敏感）
    m = re.search(r'(?:chapter|ch|episode|ep)\.?\s*(\d+)', t, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 行首：数字（阿拉伯）+ 可选分隔符 + 后续标题
    # 分隔符：. 、 : ： - ) ] 】 空格 ，或直接跟中文/字母
    # 排除日期格式：数字紧接 年/月/日/时/分/秒/号 或 N-N-N 连续日期格式 视为非章节号
    m = re.match(r'\s*(\d+)(?:\s*[\.、:：\-\)\]】，;；]|\s+|\s*(?=[\u4e00-\u9fffA-Za-z]))', t)
    if m:
        num_end = m.end(1)
        rest = t[num_end:]
        is_date = False
        # 数字后紧跟日期单位（年/月/日/时/分/秒/号）
        if rest and rest[0] in '年月日时分秒号':
            is_date = True
        # YYYY-MM-DD / YYYY-MM 日期格式（数字-数字-数字）
        if re.match(r'\s*-\d{1,2}(-\d{1,2})?(\s|$)', rest):
            is_date = True
        if not is_date:
            return int(m.group(1))

    # 行首：纯中文数字 + 可选分隔符
    m = re.match(r'\s*([零一二三四五六七八九十百千万亿两〇]+)(?:\s*[\.、:：\-\)\]】，;；]|\s+)', t)
    if m:
        n = _chinese_to_int(m.group(1))
        if n is not None:
            return n

    # 行首纯数字（整行就是一个数字）
    m = re.match(r'\s*(\d+)\s*$', t)
    if m:
        return int(m.group(1))

    return None


def resort_chapters_by_title(book_id, rebin_volumes=False):
    """按章节标题中的章节号对作品章节重新排序（稳定排序）。
    - 有章节号的按章节号升序排在前
    - 无章节号的保持原相对顺序排在后面
    - 重新分配 order_index 为 0..N-1
    - rebin_volumes=True 时，按 50 章/卷重新归入卷（用于批量导入场景）
    返回重排后的章节数。"""
    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not chs:
        return 0
    # 构建排序键：(是否无章节号, 章节号, 原 order_index)
    keyed = []
    for ch in chs:
        n = parse_chapter_number(ch.title or '')
        if n is not None:
            keyed.append((0, n, ch.order_index, ch))
        else:
            keyed.append((1, 0, ch.order_index, ch))
    keyed.sort(key=lambda x: (x[0], x[1], x[2]))
    # 重新分配 order_index
    for i, item in enumerate(keyed):
        item[3].order_index = i

    # 可选：按 50 章/卷重新归入卷
    if rebin_volumes:
        vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).order_by(Chapter.order_index).all()
        total_chs = len(keyed)
        needed_vols = max(1, (total_chs + 49) // 50) if total_chs > 0 else 0
        # 多余的空卷删除（避免导入后残留空卷导致卷列表错乱）
        if len(vols) > needed_vols:
            for extra_vol in vols[needed_vols:]:
                # 清空其下章节的 parent_id（若有），再删除
                Chapter.query.filter_by(book_id=book_id, parent_id=extra_vol.id, is_volume=False).update({'parent_id': ''})
                db.session.delete(extra_vol)
            vols = vols[:needed_vols]
        # 不足的卷自动补建（保留已有卷名）
        while len(vols) < needed_vols:
            vidx = len(vols)
            ch_start = vidx * 50 + 1
            ch_end = min((vidx + 1) * 50, total_chs)
            vol = Chapter(
                book_id=book_id,
                title=f'第{vidx + 1}卷（第{ch_start}-{ch_end}章）',
                content='', order_index=total_chs + vidx,
                is_volume=True, parent_id='', word_count=0,
            )
            db.session.add(vol)
            db.session.flush()
            vols.append(vol)
        # 重置所有卷的 order_index 为 total_chs + vol_idx（保证卷顺序稳定，排在所有章节之后）
        for vidx, vol in enumerate(vols):
            vol.order_index = total_chs + vidx
        # 重新分配 parent_id
        for i, item in enumerate(keyed):
            vol_idx = i // 50
            if vol_idx < len(vols):
                item[3].parent_id = vols[vol_idx].id
    db.session.flush()
    return len(keyed)


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


# ==== 邮件发送（用于找回密码） ====
# SMTP 配置通过环境变量覆盖；默认发件邮箱为 xiyiji@88.com
SMTP_HOST = os.environ.get('FANSHU_SMTP_HOST', 'smtp.qiye.aliyun.com')
SMTP_PORT = int(os.environ.get('FANSHU_SMTP_PORT', '465'))
SMTP_USER = os.environ.get('FANSHU_SMTP_USER', 'xiyiji@88.com')
SMTP_PASSWORD = os.environ.get('FANSHU_SMTP_PASSWORD', '')
SMTP_FROM_NAME = os.environ.get('FANSHU_SMTP_FROM_NAME', '蚂蚁写作')
SMTP_FROM_ADDR = os.environ.get('FANSHU_SMTP_FROM_ADDR', 'xiyiji@88.com')
# 前端站点地址，用于拼接重置链接
SITE_BASE_URL = os.environ.get('FANSHU_SITE_BASE_URL', '')


def send_reset_email(to_email, reset_token, site_url=None):
    """发送密码重置邮件。如果 SMTP 未配置密码则降级为返回链接（开发模式）。
    返回 (ok: bool, msg: str, reset_link: str)。
    """
    # 优先使用前端传入的 site_url（前后端分离部署时至关重要），其次环境变量，最后回退到后端地址
    base = (site_url or SITE_BASE_URL or request.host_url.rstrip('/')).rstrip('/')
    # 使用 # 锚点，兼容 HashRouter：/ sometime/#/reset-password?token=xxx
    reset_link = f"{base}/#/reset-password?token={reset_token}"

    subject = '【蚂蚁写作】找回您的账号密码'
    body = (
        f"您好，\n\n"
        f"我们收到了您重置蚂蚁写作账号密码的请求。\n\n"
        f"请点击下方链接重置密码（链接 30 分钟内有效）：\n"
        f"{reset_link}\n\n"
        f"如果您没有发起过此请求，请忽略本邮件，您的账号密码不会变更。\n\n"
        f"—— 蚂蚁写作团队"
    )

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.utils import formataddr

        msg = MIMEMultipart()
        msg['From'] = formataddr((SMTP_FROM_NAME, SMTP_FROM_ADDR))
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if SMTP_PASSWORD:
            # 生产/已配置：使用 SSL 直连 SMTP 服务器
            if SMTP_PORT == 465:
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
            else:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
                server.starttls()
            try:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM_ADDR, [to_email], msg.as_string())
            finally:
                server.quit()
            return True, '邮件已发送', reset_link
        else:
            # 开发环境降级：SMTP 未配置，无法实际发邮件，返回链接供前端展示
            app.logger.warning('[SMTP未配置] 密码重置邮件未实际发送，返回链接供开发调试。')
            app.logger.info('---- 重置邮件内容 ----\n%s\n--------------------', body)
            return True, 'SMTP未配置，已生成重置链接', reset_link
    except Exception as e:
        app.logger.exception('发送重置邮件失败')
        return False, f'邮件发送失败：{e}', reset_link


# ==== 修改密码 / 找回密码 / 重置密码 ====

@app.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    """已登录用户修改密码：需要原密码验证。"""
    data = request.json or {}
    old_password = (data.get('old_password', '') or '').strip()
    new_password = (data.get('new_password', '') or '').strip()
    if not old_password or not new_password:
        return jsonify({'error': '请输入原密码和新密码'}), 400
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4个字符'}), 400

    user = User.query.get(request.current_user_id)
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if not check_password_hash(user.password_hash, old_password):
        return jsonify({'error': '原密码错误'}), 401

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    """用户输入邮箱，生成重置令牌并发送重置邮件。"""
    data = request.json or {}
    email = (data.get('email', '') or '').strip().lower()
    site_url = (data.get('site_url', '') or '').strip()
    if not email or '@' not in email:
        return jsonify({'error': '请输入有效的邮箱'}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        # 出于隐私保护，即使邮箱不存在也返回成功，避免被探测账号是否存在
        return jsonify({'success': True, 'message': '如果该邮箱已注册，重置邮件已发送'})

    # 失效旧的重置令牌
    PasswordResetToken.query.filter_by(user_id=user.id, used=False).update({'used': True})

    token = generate_token()
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    db.session.add(PasswordResetToken(user_id=user.id, token=token, expires_at=expires, used=False))
    db.session.commit()

    ok, msg, reset_link = send_reset_email(user.email, token, site_url=site_url)
    if not ok:
        return jsonify({'error': msg}), 500

    resp = {'success': True, 'message': msg}
    # SMTP 未配置时，返回重置链接给前端展示（开发/自部署环境降级方案）
    if not SMTP_PASSWORD:
        resp['reset_link'] = reset_link
        resp['dev_mode'] = True
    return jsonify(resp)


@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    """用户凭重置令牌设置新密码。"""
    data = request.json or {}
    token = (data.get('token', '') or '').strip()
    new_password = (data.get('new_password', '') or '').strip()
    if not token or not new_password:
        return jsonify({'error': '令牌或新密码不能为空'}), 400
    if len(new_password) < 4:
        return jsonify({'error': '新密码至少4个字符'}), 400

    prt = PasswordResetToken.query.filter_by(token=token).first()
    if not prt or prt.used:
        return jsonify({'error': '重置链接无效或已使用'}), 400
    exp = prt.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return jsonify({'error': '重置链接已过期，请重新申请'}), 400

    user = User.query.get(prt.user_id)
    if not user:
        return jsonify({'error': '用户不存在'}), 404

    user.password_hash = generate_password_hash(new_password)
    prt.used = True
    db.session.commit()
    return jsonify({'success': True, 'message': '密码已重置，请使用新密码登录'})


@app.route('/api/auth/verify-reset-token', methods=['POST'])
def verify_reset_token():
    """校验重置令牌是否有效（用于前端跳转后预检）。"""
    data = request.json or {}
    token = (data.get('token', '') or '').strip()
    if not token:
        return jsonify({'valid': False}), 400
    prt = PasswordResetToken.query.filter_by(token=token).first()
    if not prt or prt.used:
        return jsonify({'valid': False}), 200
    exp = prt.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        return jsonify({'valid': False}), 200
    return jsonify({'valid': True}), 200


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
    # 总卷数：长篇默认10，短篇默认1。卷数不设上限，由用户自行决定（不钳制）
    book_type = data.get('book_type', 'novel')
    default_vols = 1 if book_type == 'short_story' else 10
    total_volumes = data.get('total_volumes') or default_vols
    # 卷数校验：仅校验下限≥1，不设上限（用户填多少就是多少）
    try:
        total_volumes = int(total_volumes)
        total_volumes = max(1, total_volumes)
    except (ValueError, TypeError):
        total_volumes = default_vols
    # 风格流派：JSON 数组，最多3种
    novel_styles = data.get('novel_styles', [])
    if isinstance(novel_styles, list):
        novel_styles = novel_styles[:3]
    else:
        novel_styles = []
    book = Book(
        user_id=request.current_user_id,
        title=data.get('title', '新书'),
        author=data.get('author', ''),
        genre=data.get('genre', 'other'),
        book_type=book_type,
        synopsis=data.get('synopsis', ''),
        template_id=data.get('template_id', ''),
        target_words=data.get('target_words', 0),
        total_volumes=total_volumes,
        novel_styles=json.dumps(novel_styles, ensure_ascii=False),
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
    # 总卷数 + 风格流派同步
    if 'total_volumes' in data:
        try:
            tv = int(data['total_volumes'])
            tv = max(1, tv)  # 仅校验下限≥1，不设上限
            book.total_volumes = tv
        except (ValueError, TypeError):
            pass
    if 'novel_styles' in data:
        ns = data['novel_styles']
        if isinstance(ns, list):
            ns = ns[:3]
        else:
            ns = []
        book.novel_styles = json.dumps(ns, ensure_ascii=False)
    # 同步到 BookBible（创作时从 bible 读取注入）
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if bb:
        if hasattr(book, 'total_volumes') and book.total_volumes:
            bb.total_volumes = book.total_volumes
        if hasattr(book, 'novel_styles'):
            bb.novel_styles = book.novel_styles
    db.session.commit()
    return jsonify(book.to_dict())

@app.route('/api/books/<book_id>', methods=['DELETE'])
def delete_book(book_id):
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    # 手动删除所有关联记录（兼容 PostgreSQL 外键约束 + SQLite）
    ChapterVersion.query.filter(ChapterVersion.chapter_id.in_(
        db.session.query(Chapter.id).filter_by(book_id=book_id)
    )).delete(synchronize_session=False)
    Chapter.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    Character.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    Outline.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DailyStats.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    AISession.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    StageContent.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    BookBible.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DynamicMemory.query.filter_by(book_id=book_id).delete(synchronize_session=False)
    DynamicReport.query.filter_by(book_id=book_id).delete(synchronize_session=False)
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
    # 自动修复孤儿数据：清空指向不存在卷的 parent_id（历史删除卷未清空子章节导致）
    vol_ids = {c.id for c in chapters if c.is_volume}
    repaired = False
    for c in chapters:
        if not c.is_volume and c.parent_id and c.parent_id not in vol_ids:
            c.parent_id = ''
            repaired = True
    if repaired:
        db.session.commit()
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

    # 若新章节标题含章节号（第N章/第N章等），按章节号自动重排顺序（不改动卷归入）
    if not ch.is_volume and parse_chapter_number(ch.title or '') is not None:
        try:
            resort_chapters_by_title(book_id, rebin_volumes=False)
        except Exception:
            pass  # 重排失败不影响章节创建

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
    # 若删除的是卷，清空其下章节的 parent_id，避免章节变孤儿（指向已删除卷）而不可见
    if ch.is_volume:
        Chapter.query.filter_by(book_id=book_id, parent_id=chapter_id, is_volume=False).update({'parent_id': ''})
    # 先删除章节版本（兼容 PostgreSQL 外键约束）
    ChapterVersion.query.filter_by(chapter_id=chapter_id).delete(synchronize_session=False)
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


@app.route('/api/books/<book_id>/chapters/rebin-volumes', methods=['POST'])
@login_required
def rebin_volumes(book_id):
    """手动触发按 50 章/卷重新归入卷：先清空所有章节 parent_id，删除现有卷，再按章节号排序重新分卷。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    if book.user_id != request.current_user_id:
        return jsonify({'error': '无权操作该作品'}), 403
    try:
        # 清空所有非卷章节的 parent_id
        Chapter.query.filter_by(book_id=book_id, is_volume=False).update({'parent_id': ''})
        # 删除所有现有卷
        old_vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).all()
        for v in old_vols:
            db.session.delete(v)
        db.session.flush()
        # 重新排序 + 分卷
        count = resort_chapters_by_title(book_id, rebin_volumes=True)
        update_book_stats(book_id)
        vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).count()
        return jsonify({'success': True, 'chapters': count, 'volumes': vols})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


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

@app.route('/api/health', methods=['GET'])
def health_check():
    """超轻量健康检查端点，不查数据库，用于保活 ping"""
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()}), 200

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
    book_id = data.get('book_id', '')
    scope = data.get('scope', 'general')
    scope_id = data.get('scope_id', '')
    # upsert：同 book+scope+scope_id 的会话复用，避免维度Modal重复创建孤儿会话
    existing = AISession.query.filter_by(
        book_id=book_id, scope=scope, scope_id=scope_id
    ).order_by(AISession.updated_at.desc()).first() if (book_id and scope and scope_id) else None
    if existing:
        return jsonify(existing.to_dict()), 200
    session = AISession(
        book_id=book_id,
        scope=scope,
        scope_id=scope_id,
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


# ==== 实体注册表 API（P2：跨维度重命名/合并） ====

@app.route('/api/books/<book_id>/entities', methods=['GET'])
@login_required
def list_entities(book_id):
    """抽取作品全部实体（角色/势力/地点/物品）。"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'characters': [], 'factions': [], 'locations': [], 'items': []})
    from entity_registry import extract_entities
    return jsonify(extract_entities(bb))


@app.route('/api/books/<book_id>/entities/rename', methods=['POST'])
@login_required
def rename_entity_api(book_id):
    """跨维度重命名实体。body: {old_name, new_name, entity_type}"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'BookBible not found'}), 404
    data = request.json or {}
    old_name = (data.get('old_name') or '').strip()
    new_name = (data.get('new_name') or '').strip()
    entity_type = data.get('entity_type', 'character')
    if not old_name or not new_name:
        return jsonify({'error': 'old_name 和 new_name 不能为空'}), 400
    from entity_registry import rename_entity
    chapters_q = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    result = rename_entity(bb, chapters_q, old_name, new_name, entity_type)
    db.session.commit()
    return jsonify(result)


@app.route('/api/books/<book_id>/entities/merge', methods=['POST'])
@login_required
def merge_entities_api(book_id):
    """合并实体。body: {main_name, alias_names:[...], entity_type}"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'BookBible not found'}), 404
    data = request.json or {}
    main_name = (data.get('main_name') or '').strip()
    alias_names = data.get('alias_names', []) or []
    entity_type = data.get('entity_type', 'character')
    if not main_name or not alias_names:
        return jsonify({'error': 'main_name 和 alias_names 不能为空'}), 400
    from entity_registry import merge_entities
    chapters_q = Chapter.query.filter_by(book_id=book_id, is_volume=False).all()
    result = merge_entities(bb, chapters_q, main_name, alias_names, entity_type)
    db.session.commit()
    return jsonify(result)


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
    """将纯文本按章节标记拆分为多个章节，支持多种章节标题格式。
    会自动合并"同章双标题"（如 第20章 / 第二十章 紧挨出现，前者为空）产生的空章。"""
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
        return _merge_empty_chapters(_extract(cn_matches, text))

    # 2. Markdown 标题 # xxx 或 ## xxx
    md_pattern = re.compile(r'^#{1,3}[ \t]+.+$', re.MULTILINE)
    md_matches = list(md_pattern.finditer(text))
    if len(md_matches) >= 1:
        return _merge_empty_chapters(_extract(md_matches, text, strip_prefix='#'))

    # 3. 英文 Chapter N / CHAPTER N
    en_pattern = re.compile(r'^[ \t]*[Cc][Hh][Aa][Pp][Tt][Ee][Rr][ \t]*\d+[ \t]*.*$', re.MULTILINE)
    en_matches = list(en_pattern.finditer(text))
    if len(en_matches) >= 1:
        return _merge_empty_chapters(_extract(en_matches, text))

    # 4. 纯数字开头：1. xxx / 1、xxx / 1: xxx（至少2个才拆分，避免误拆）
    num_pattern = re.compile(r'^[ \t]*\d{1,4}[.、:][ \t]*\S.+$', re.MULTILINE)
    num_matches = list(num_pattern.finditer(text))
    if len(num_matches) >= 2:
        return _merge_empty_chapters(_extract(num_matches, text))

    # 5. 【 xxx 】 或 『 xxx 』 格式标题
    bracket_pattern = re.compile(r'^[ \t]*[【『][^】』]+[】』][ \t]*.*$', re.MULTILINE)
    bracket_matches = list(bracket_pattern.finditer(text))
    if len(bracket_matches) >= 2:
        return _merge_empty_chapters(_extract(bracket_matches, text))

    # 无法拆分，作为单个章节
    return None


def _merge_empty_chapters(chapters):
    """合并空章节：处理"同章双标题"导致的空章。
    规则：
    - 若某章 content 为空，且与下一章章节号相同（如 第20章/第二十章），则视为同一章，
      保留下一章（有内容的那个）的标题，删除空章。
    - 若某章 content 为空，且下一章 content 非空，但章节号不同或无法解析，则将下一章
      内容并入当前空章（标题以当前章为主，下一章标题作为副标题保留），避免0字节空章。
    返回合并后的章节列表（至少保留1章）。"""
    if not chapters:
        return chapters
    result = []
    i = 0
    while i < len(chapters):
        cur = chapters[i]
        cur_num = parse_chapter_number(cur.get('title', ''))
        cur_content = cur.get('content', '').strip()
        # 当前章为空，尝试与下一章合并
        if not cur_content and i + 1 < len(chapters):
            nxt = chapters[i + 1]
            nxt_num = parse_chapter_number(nxt.get('title', ''))
            nxt_content = nxt.get('content', '').strip()
            # 情况A：章节号相同（同章双标题，如 第20章/第二十章）→ 删空章，跳到下一章
            if cur_num is not None and nxt_num is not None and cur_num == nxt_num:
                i += 1
                continue
            # 情况B：下一章有内容 → 内容并入当前章，标题保留当前章（下一章作副标题）
            if nxt_content:
                merged_title = cur.get('title', '')
                # 若两标题不同，拼接副标题
                if nxt.get('title', '') and nxt.get('title', '') != merged_title:
                    merged_title = f'{merged_title} / {nxt.get("title", "")}'
                result.append({'title': merged_title[:100], 'content': nxt_content})
                i += 2  # 跳过下一章
                continue
            # 情况C：下一章也为空 → 删当前空章，处理下一章
            i += 1
            continue
        result.append({'title': cur.get('title', '')[:100], 'content': cur_content})
        i += 1
    # 兜底：若合并后为空（全部空章），保留原第一章避免0章
    if not result:
        result = [{'title': chapters[0].get('title', '')[:100], 'content': chapters[0].get('content', '')}]
    return result


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
            if ext not in ('txt', 'md', 'markdown', 'docx', 'zip', 'json'):
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
        db.session.flush()

        # 按章节标题中的章节号（第N章/第N章等）自动排序
        resort_chapters_by_title(book.id, rebin_volumes=False)

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


@app.route('/api/books/<book_id>/ai-import-recognize', methods=['POST'])
@login_required
def ai_import_recognize(book_id):
    """导入作品后，根据文件名/章节标题/章节内容样本，AI自动识别并填入各创作维度。
    可识别：构思、设定、大纲、人物、剧情、伏笔、地图、物资库。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    # 可选：指定要识别的维度；为空则识别全部
    target_dims = data.get('dimensions', [])

    config = AIConfig.query.first()
    if not config or not config.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    # 收集文件名/章节标题 + 内容样本
    all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not all_chs:
        return jsonify({'error': '该作品暂无章节，无法识别'}), 400

    # 章节标题列表（即文件名/章节名）
    titles = [ch.title for ch in all_chs[:200]]
    # 内容样本：取前几章 + 中间几章的片段
    samples = []
    sample_chs = all_chs[:3] + all_chs[len(all_chs)//2:len(all_chs)//2+2] if len(all_chs) > 5 else all_chs
    for ch in sample_chs:
        content = (ch.content or '')[:600]
        if content:
            samples.append(f'【{ch.title}】\n{content}')

    titles_text = '\n'.join(titles)
    samples_text = '\n\n'.join(samples)[:4000]

    # P2-10: 修复幽灵key——'character_design'/'world_setting' 不是真实prompt_key，替换为内置存在的key
    # 【三类无污染】AI总创作属于构思阶段：只注入构思类（master）技能包
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'master_outline', 'tomato_character', 'tomato_setting'], mode='agent')

    # 识别哪些维度为空（仅填充空维度，避免覆盖已有内容）
    dim_status = {
        'concept': bool(bb.concept and bb.concept.strip()),
        'settings': bool(bb.key_rules and bb.key_rules.strip()),
        'outline': bool(bb.plot_design and bb.plot_design.strip()),
        'characters': bool(bb.character_profiles and bb.character_profiles.strip()),
        'plot': bool(bb.timeline and bb.timeline.strip()),
        'foreshadowing': bool(bb.foreshadowing and bb.foreshadowing.strip()),
        'locations': bool(bb.locations and bb.locations.strip()),
        'inventory': bool(bb.inventory and bb.inventory.strip()),
    }
    # 默认只填空维度
    empty_dims = [k for k, v in dim_status.items() if not v]
    dims_to_fill = target_dims if target_dims else empty_dims
    if not dims_to_fill:
        return jsonify({'success': True, 'message': '所有维度已有内容，未做修改', 'bible': bb.to_dict(), 'filled': []})

    dims_label = '、'.join(dims_to_fill)
    system_prompt = f"""你是专业的小说设定分析师。请根据导入作品的【文件名/章节标题】和【内容样本】，自动识别并填充以下空维度：{dims_label}。

【识别规则】
1. 仅根据文件名和内容样本推断，不要编造未提供的设定
2. 文件名/章节标题往往暗含卷名、人物名、地点、事件等关键信息，重点提取
3. 每个维度输出对应内容，无法判断的维度输出"（信息不足，待补充）"
4. 保持各维度内容一致性（人物名、地点、能力体系等需统一）

{skill_note}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "concept": "一句话核心创意（30字内）",
  "settings": "核心规则/能力体系/世界观禁忌（多条用换行分隔）",
  "outline": "主线冲突+卷纲拆解（基于章节标题推断卷结构）",
  "characters": "主要角色档案（JSON数组：[{{\"name\":\"\",\"role\":\"\",\"personality\":\"\",\"motivation\":\"\"}}]，纯文本也可）",
  "plot": "按章节标题梳理的关键事件时间线",
  "foreshadowing": "从内容样本中识别的伏笔（若无则留空）",
  "locations": "从标题/内容识别的地点（若无则留空）",
  "inventory": "从内容识别的物品/功法/法宝（若无则留空）"
}}"""

    user_prompt = f'作品标题：{book.title}\n作品类型：{_get_genre_label(book)}\n\n【文件名/章节标题列表（共{len(titles)}项）】\n{titles_text}\n\n【内容样本】\n{samples_text or "（无内容样本）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3000, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    analysis, parse_err = _extract_json_from_llm(content, expect='object')
    if analysis is None:
        return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300], 'parse_error': parse_err}), 500

    # 字段映射：维度 -> bible字段
    dim_field_map = {
        'concept': 'concept',
        'settings': 'key_rules',
        'outline': 'plot_design',
        'characters': 'character_profiles',
        'plot': 'timeline',
        'foreshadowing': 'foreshadowing',
        'locations': 'locations',
        'inventory': 'inventory',
    }

    filled = []
    for dim in dims_to_fill:
        field = dim_field_map.get(dim)
        if not field:
            continue
        val = analysis.get(dim, '')
        if isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False, indent=2)
        val = str(val).strip()
        if val and val != '（信息不足，待补充）' and not val.startswith('无'):
            setattr(bb, field, val)
            filled.append(dim)

    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'已识别填充 {len(filled)} 个维度：{"、".join(filled) if filled else "无"}',
        'filled': filled,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/ai-anti-forget-check', methods=['POST'])
@login_required
def ai_anti_forget_check(book_id):
    """长篇小说防遗忘与一致性检查（综合诊断）。
    整合技能包「长篇小说防遗忘系统」的 consistency_check / lock_facts / narrative_debt / foreshadow_register / character_cognition 提示词，
    扫描全部维度+近期章节，输出：锁定事实清单、一致性违规清单、待回收伏笔、叙事债务、改进建议。
    【改造】支持分卷选择（volume_ids 多选/单选），报告持久化到 bb.anti_forget_reports，自动命名"检查01/02..."。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    # 检查范围：reports(所有动态报告) / dimensions(仅维度) / volumes(指定分卷)
    scope = data.get('scope', 'reports')
    if scope in ('recent', 'all'):
        scope = 'reports'
    # 【新增】分卷选择：volume_ids 为空表示不按卷筛选；非空则只检查指定卷的章节
    volume_ids = data.get('volume_ids', [])
    if not isinstance(volume_ids, list):
        volume_ids = []

    config = AIConfig.query.first()
    if not config or not config.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': '该作品暂无创作维度数据，请先填写设定/大纲/剧情等维度。'}), 400

    # 收集章节内容（按 scope + volume_ids 决定范围）
    all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    # 若指定了 volume_ids，按卷筛选章节
    if volume_ids:
        # 通过 parent_id 筛选属于指定卷的章节
        selected_chs = [ch for ch in all_chs if ch.parent_id and ch.parent_id in volume_ids]
        if not selected_chs:
            # 兼容：若按 parent_id 没筛到（可能章节未挂卷），按卷号区间筛选
            # 从 timeline 解析卷的章节范围
            vol_ranges = []
            try:
                if bb.timeline and bb.timeline.strip().startswith('['):
                    arr = json.loads(bb.timeline)
                    for v in arr:
                        if not isinstance(v, dict):
                            continue
                        v_id = str(v.get('volume_id', ''))
                        if v_id in [str(vid) for vid in volume_ids]:
                            nodes = v.get('nodes') or []
                            for n in nodes:
                                if isinstance(n, dict):
                                    ch_range = str(n.get('chapters', ''))
                                    nums = re.findall(r'\d+', ch_range)
                                    if len(nums) >= 2:
                                        vol_ranges.append((int(nums[0]), int(nums[-1])))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            if vol_ranges:
                selected_chs = []
                for ch in all_chs:
                    for lo, hi in vol_ranges:
                        if lo <= ch.order_index <= hi:
                            selected_chs.append(ch)
                            break
            else:
                selected_chs = all_chs  # 无法定位时回退全章
        all_chs = selected_chs

    chapter_text = ''
    ch_count = len(all_chs)
    source_label = '动态文件报告'
    if scope == 'dimensions':
        chapter_text, source_label = _collect_dimension_source(bb, '全部维度（除构思、章节）')
        if chapter_text:
            import re as _re_dim
            chapter_text = _re_dim.sub(r'【构思】\n[^\n]*(\n|$)', '', chapter_text).strip()
    else:
        # 动态文件模式（reports）：读取所有动态报告作为检查依据
        all_reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
        # 若指定了 volume_ids，按卷的章节范围筛选动态报告
        if volume_ids and all_reports:
            vol_chapter_set = {ch.order_index for ch in all_chs}
            filtered_reports = []
            for r in all_reports:
                # 报告的章节范围 [chapter_start, chapter_end] 与选中卷的章节有交集则保留
                if any(r.chapter_start <= cn <= r.chapter_end for cn in vol_chapter_set):
                    filtered_reports.append(r)
            all_reports = filtered_reports if filtered_reports else all_reports
        if all_reports:
            parts = [f'【{r.title}（{r.chapter_start}-{r.chapter_end}章）】\n{(r.content or "")[:800]}' for r in all_reports]
            chapter_text = '\n\n'.join(parts)[:10000]
            source_label = f'动态文件（共{len(all_reports)}份报告）'
            if volume_ids:
                source_label += f'·指定{len(volume_ids)}卷'
        elif ch_count > 0:
            recent = all_chs[-10:]
            parts = [f'【{ch.title}】\n{(ch.content or "")[:1000]}' for ch in recent]
            chapter_text = '\n\n'.join(parts)[:8000]
            source_label = '近期章节（暂无动态报告）'
            if volume_ids:
                source_label += f'·指定{len(volume_ids)}卷'
        else:
            chapter_text, source_label = _collect_dimension_source(bb, '全部维度')
            if not chapter_text:
                return jsonify({'error': '该作品暂无动态报告、章节，且维度也为空，无法进行检查。请先生成动态报告或填写维度。'}), 400

    # 收集已有动态文件（若有），作为额外上下文
    dm = DynamicMemory.query.filter_by(book_id=book_id).first()
    dyn_ctx = ''
    if dm:
        for fk in ['foreshadowing_tracker', 'character_ecosystem', 'ability_world']:
            v = getattr(dm, fk, '') or ''
            if v.strip():
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, dict):
                        summary = parsed.get('summary') or parsed.get('pending') or parsed.get('world_facts') or ''
                        if summary:
                            dyn_ctx += f'【{fk}】\n{str(summary)[:600]}\n\n'
                except (json.JSONDecodeError, ValueError):
                    dyn_ctx += f'【{fk}】\n{v[:600]}\n\n'

    # 【三类无污染】一致性检查属于审查阶段：只注入审查类（review）技能包
    skill_note = _get_skill_prompts_by_category(
        skill_pack_ids, 'review',
        ['consistency_check', 'lock_facts', 'narrative_debt', 'foreshadow_register', 'character_cognition'],
        mode='agent'
    )

    # 构建维度全景上下文
    dim_ctx_parts = []
    dim_fields = [
        ('concept', '构思'), ('key_rules', '设定/核心规则'), ('worldbuilding', '世界观'),
        ('plot_design', '大纲/总纲'), ('character_profiles', '人物档案'),
        ('timeline', '剧情/时间线'), ('foreshadowing', '伏笔'),
        ('locations', '地点'), ('inventory', '物资库'),
    ]
    for f, lbl in dim_fields:
        v = getattr(bb, f, '') or ''
        if v.strip():
            dim_ctx_parts.append(f'【{lbl}】\n{v[:800]}')
    dim_ctx = '\n\n'.join(dim_ctx_parts) or '（维度数据为空）'

    # ===== P2增强：注入章级变更日志 + 伏笔DAG结构化状态 =====
    # 让防遗忘检查从"LLM凭正文推断"升级为"对照结构化数据校验"：
    #   - chapter_changes_log：章级 delta（境界/物品/伏笔/地点），可对照检查"境界跳变/物品凭空获得"
    #   - foreshadowing_graph：结构化伏笔状态（pending/resolved），比 LLM 从文本推断更准
    structured_ctx_parts = []

    # 1. 章级变更日志摘要（最近 30 章，聚焦境界/物品/伏笔/死亡类变更）
    try:
        log_list = json.loads(bb.chapter_changes_log) if bb.chapter_changes_log else []
        if isinstance(log_list, list) and log_list:
            recent_logs = log_list[-30:]
            log_lines = []
            for entry in recent_logs:
                if not isinstance(entry, dict):
                    continue
                ch_num = entry.get('chapter_num', '?')
                chg = entry.get('changes') or {}
                if not isinstance(chg, dict):
                    continue
                segs = []
                # 境界变化（防遗忘检查重点关注）
                for c in (chg.get('CharacterStateChanges') or []):
                    if isinstance(c, dict):
                        nm = c.get('CharacterId') or c.get('Name') or '?'
                        lvl = c.get('NewLevel') or ''
                        ke = c.get('KeyEvent') or ''
                        if lvl or ke:
                            segs.append(f'{nm}{"→"+lvl if lvl else ""}{":"+ke[:40] if ke else ""}')
                # 物品转移
                for it in (chg.get('ItemTransfers') or []):
                    if isinstance(it, dict):
                        nm = it.get('ItemName') or it.get('ItemId') or '?'
                        th = it.get('ToHolder') or ''
                        if th:
                            segs.append(f'{nm}→{th}')
                # 伏笔动作
                for fa in (chg.get('ForeshadowingActions') or []):
                    if isinstance(fa, dict):
                        fid = fa.get('ForeshadowId') or '?'
                        act = fa.get('Action') or ''
                        if act:
                            segs.append(f'伏笔{fid}:{act}')
                if segs:
                    log_lines.append(f'- 第{ch_num}章：{"；".join(segs)}')
            if log_lines:
                structured_ctx_parts.append('【章级变更日志】（最近30章的结构化delta，请对照检查境界跳变/物品凭空获得/伏笔状态矛盾）\n' + '\n'.join(log_lines))
    except Exception:
        pass

    # 2. 伏笔 DAG 结构化状态（pending 节点 + 已回收节点）
    try:
        if bb.foreshadowing_graph and get_hooks_for_chapter:
            graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
            pending_nodes = graph.get_pending_nodes()
            resolved_nodes = [n for n in (graph.nodes or []) if getattr(n, 'status', '') in ('resolved', 'payoff', '已回收', '已揭示')]
            fs_dag_lines = []
            if pending_nodes:
                fs_dag_lines.append(f'待回收伏笔（{len(pending_nodes)}条）：')
                for node in pending_nodes[:20]:
                    desc = (node.content or node.title or '')[:80]
                    planted = getattr(node, 'setup_chapter', '') or getattr(node, 'planted_chapter', '') or ''
                    fs_dag_lines.append(f'  - [{node.id}] {desc}（埋设于第{planted}章）')
            if resolved_nodes:
                fs_dag_lines.append(f'已回收伏笔（{len(resolved_nodes)}条，若正文再次当作未回收则属违规）：')
                for node in resolved_nodes[:10]:
                    desc = (node.content or node.title or '')[:60]
                    fs_dag_lines.append(f'  - [{node.id}] {desc}')
            if fs_dag_lines:
                structured_ctx_parts.append('【伏笔DAG结构化状态】（权威伏笔状态，以此为准检查正文是否矛盾）\n' + '\n'.join(fs_dag_lines))
    except Exception:
        pass

    structured_ctx = '\n\n'.join(structured_ctx_parts)

    system_prompt = f"""你是「长篇小说防遗忘与一致性审查员」，整合多个防遗忘技能协同工作：
1. 设定锁定员(lock_facts)：从各维度提取不可变核心事实清单
2. 一致性审查员(consistency_check)：检查近期章节是否违反已锁定设定
3. 伏笔管理师(foreshadow_register)：盘点伏笔状态，标记待回收
4. 叙事债务追踪师(narrative_debt)：盘点悬念承诺与兑现平衡
5. 角色认知管理师(character_cognition)：检查角色认知边界是否被破坏

{skill_note}

【审查重点】
- 人物设定：名字记错/能力超限/性格突变/关系前后矛盾
- 世界规则：违反已建立的物理/魔法/力量体系法则
- 时间线：时间倒流/年龄错误/事件顺序混乱
- 角色认知：角色知道了不该知道的信息（信息差破坏）
- 伏笔状态：已回收伏笔又被当作未回收/待回收伏笔遗忘过久
- 物资/能力：物品/功法/境界前后不一致（跳变/重复获得）

严格按JSON格式输出（不要任何其他文字）：
{{
  "locked_facts": ["不可变核心事实1", "不可变核心事实2", "...（最多15条）"],
  "violations": [
    {{"type": "人物/世界规则/时间线/认知/伏笔/物资", "severity": "严重/警告/提示", "location": "出现位置（章节/维度）", "desc": "违规描述", "fix": "修正建议"}}
  ],
  "pending_foreshadowing": [
    {{"content": "伏笔内容", "buried_at": "埋设位置", "urgency": "紧急/一般/可缓", "suggest_chapter": "建议回收章节"}}
  ],
  "narrative_debt": [
    {{"promise": "悬念/承诺", "status": "待兑现/过度透支/已遗忘", "priority": "高/中/低", "note": "说明"}}
  ],
  "character_cognition_issues": ["角色认知边界问题1", "问题2"],
  "suggestions": ["针对性改进建议1", "建议2", "建议3"],
  "health_score": 0-100的整数,
  "summary": "本次检查总体结论（100字内）"
}}"""

    user_prompt = f'作品标题：{book.title}\n数据来源：{source_label}（共{ch_count}章）\n\n【各维度全景】\n{dim_ctx}\n\n【已有动态文件摘要】\n{dyn_ctx or "（无）"}\n\n【结构化状态参照】\n{structured_ctx or "（无）"}\n\n【待审查内容】\n{chapter_text}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        report = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_af
        m = _re_af.search(r'\{[\s\S]*\}', content)
        if m:
            try:
                report = json.loads(m.group())
            except (json.JSONDecodeError, ValueError):
                report = {'summary': content[:500], 'raw': True}
        else:
            report = {'summary': content[:500], 'raw': True}

    # 持久化到 DynamicMemory.health_dashboard（防遗忘仪表盘，保留原逻辑）
    if not dm:
        dm = DynamicMemory(book_id=book_id)
        for key in DynamicMemory.FILE_KEYS:
            setattr(dm, key, DynamicMemory.get_empty_template(key))
        db.session.add(dm)
    try:
        hd = json.loads(dm.health_dashboard or '{}') if dm.health_dashboard else {}
    except (json.JSONDecodeError, ValueError):
        hd = {}
    hd['last_anti_forget_check'] = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': scope,
        'ch_count': ch_count,
        'health_score': report.get('health_score'),
        'summary': report.get('summary', ''),
        'violation_count': len(report.get('violations', [])),
        'pending_foreshadowing_count': len(report.get('pending_foreshadowing', [])),
    }
    hd['alerts'] = report.get('violations', [])[:20]
    dm.health_dashboard = json.dumps(hd, ensure_ascii=False, indent=2)

    # ===== 【新增】持久化完整报告到 bb.anti_forget_reports，自动命名"检查01/02..." =====
    try:
        existing_reports = json.loads(bb.anti_forget_reports or '[]') if bb.anti_forget_reports else []
        if not isinstance(existing_reports, list):
            existing_reports = []
    except (json.JSONDecodeError, ValueError, TypeError):
        existing_reports = []

    # 计算下一个序号：基于现有报告数量+1，两位数补零
    next_seq = len(existing_reports) + 1
    # 避免重名：若已存在同名，递增序号
    existing_titles = {r.get('title', '') for r in existing_reports if isinstance(r, dict)}
    while f'检查{next_seq:02d}' in existing_titles:
        next_seq += 1
    report_title = f'检查{next_seq:02d}'

    new_report_record = {
        'id': str(uuid.uuid4()),
        'title': report_title,
        'seq': next_seq,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': scope,
        'volume_ids': volume_ids,
        'ch_count': ch_count,
        'source_label': source_label,
        'health_score': report.get('health_score'),
        'summary': report.get('summary', ''),
        'report': report,  # 完整报告 JSON
    }
    existing_reports.append(new_report_record)
    # 限制最多保留 200 份历史报告（百万字长篇需保留更多历史诊断供回注与回溯），超出则删除最早的
    _MAX_ANTI_FORGET_REPORTS = 200
    if len(existing_reports) > _MAX_ANTI_FORGET_REPORTS:
        existing_reports = existing_reports[-_MAX_ANTI_FORGET_REPORTS:]
    bb.anti_forget_reports = json.dumps(existing_reports, ensure_ascii=False, indent=2)

    db.session.commit()

    return jsonify({
        'success': True,
        'report': report,
        'report_record': new_report_record,
        'scope': scope,
        'ch_count': ch_count,
        'source_label': source_label
    })


@app.route('/api/books/<book_id>/anti-forget-reports', methods=['GET'])
@login_required
def list_anti_forget_reports(book_id):
    """列出所有防遗忘检查报告历史。"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'reports': []})
    try:
        reports = json.loads(bb.anti_forget_reports or '[]') if bb.anti_forget_reports else []
        if not isinstance(reports, list):
            reports = []
    except (json.JSONDecodeError, ValueError, TypeError):
        reports = []
    # 按 seq 倒序返回（最新在前）
    reports.sort(key=lambda r: r.get('seq', 0) if isinstance(r, dict) else 0, reverse=True)
    return jsonify({'reports': reports})


@app.route('/api/books/<book_id>/anti-forget-reports/<report_id>', methods=['PUT'])
@login_required
def update_anti_forget_report(book_id, report_id):
    """编辑/重命名防遗忘检查报告。
    请求体：{"title": "新标题"} 重命名；{"report": {...}} 替换报告内容；{"summary": "..."} 更新摘要。"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    try:
        reports = json.loads(bb.anti_forget_reports or '[]') if bb.anti_forget_reports else []
        if not isinstance(reports, list):
            reports = []
    except (json.JSONDecodeError, ValueError, TypeError):
        reports = []

    found = False
    for r in reports:
        if isinstance(r, dict) and r.get('id') == report_id:
            if 'title' in data:
                new_title = str(data['title']).strip()
                if not new_title:
                    return jsonify({'error': '标题不能为空'}), 400
                r['title'] = new_title
            if 'report' in data and isinstance(data['report'], dict):
                r['report'] = data['report']
            if 'summary' in data:
                r['summary'] = str(data['summary'])
                if isinstance(r.get('report'), dict):
                    r['report']['summary'] = str(data['summary'])
            if 'health_score' in data:
                r['health_score'] = data['health_score']
            r['updated_at'] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        return jsonify({'error': '报告不存在'}), 404
    bb.anti_forget_reports = json.dumps(reports, ensure_ascii=False, indent=2)
    db.session.commit()
    return jsonify({'success': True, 'reports': reports})


@app.route('/api/books/<book_id>/anti-forget-reports/<report_id>', methods=['DELETE'])
@login_required
def delete_anti_forget_report(book_id, report_id):
    """删除防遗忘检查报告。"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return jsonify({'error': 'Not found'}), 404
    try:
        reports = json.loads(bb.anti_forget_reports or '[]') if bb.anti_forget_reports else []
        if not isinstance(reports, list):
            reports = []
    except (json.JSONDecodeError, ValueError, TypeError):
        reports = []
    new_reports = [r for r in reports if not (isinstance(r, dict) and r.get('id') == report_id)]
    if len(new_reports) == len(reports):
        return jsonify({'error': '报告不存在'}), 404
    bb.anti_forget_reports = json.dumps(new_reports, ensure_ascii=False, indent=2)
    db.session.commit()
    return jsonify({'success': True, 'reports': new_reports})


@app.route('/api/books/<book_id>/import-chapters', methods=['POST'])
@login_required
def append_import_chapters(book_id):
    """追加导入章节到已有作品，支持 txt/md/docx/zip，每个文件可含多章。
    新章节会按当前最大 order_index 顺序追加，不影响已有章节。
    """
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': '作品不存在'}), 404
    if book.user_id != request.current_user_id:
        return jsonify({'error': '无权操作该作品'}), 403

    files = request.files.getlist('files')
    if not files or len(files) == 0:
        return jsonify({'error': '未选择文件'}), 400

    tmpdir = tempfile.mkdtemp()
    try:
        new_chapters = []
        for f in files:
            if not f or not f.filename:
                continue
            original_name = f.filename
            ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
            if ext not in ('txt', 'md', 'markdown', 'docx', 'zip', 'json'):
                continue
            safe_name = f'{uuid.uuid4()}.{ext}'
            filepath = os.path.join(tmpdir, safe_name)
            f.save(filepath)
            text = extract_text_from_file(filepath, safe_name)
            if not text.strip():
                continue
            chapters = split_into_chapters(text)
            if chapters:
                new_chapters.extend(chapters)
            else:
                ch_title = os.path.splitext(original_name)[0]
                new_chapters.append({'title': ch_title[:100], 'content': text})

        if not new_chapters:
            return jsonify({'error': '未能从文件中提取到有效内容，请检查文件格式或编码'}), 400

        # 计算当前最大 order_index，新章节追加在末尾
        max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or 0
        added = 0
        for idx, ch_data in enumerate(new_chapters):
            ch = Chapter(
                book_id=book_id,
                title=ch_data['title'][:200] or f'第{max_order + idx + 1}章',
                content=ch_data['content'],
                order_index=max_order + idx + 1,
                is_volume=False,
                parent_id='',
                word_count=count_words(ch_data['content'])
            )
            db.session.add(ch)
            added += 1
        db.session.flush()

        # 按章节标题中的章节号自动重排（含已有章节），并按 50 章/卷重新归入卷
        resort_chapters_by_title(book_id, rebin_volumes=True)

        update_book_stats(book_id)
        total = Chapter.query.filter_by(book_id=book_id, is_volume=False).count()
        return jsonify({'success': True, 'added': added, 'total': total}), 200
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
     'description': '创建、补全、诊断和修改人物设计', 'content': '你是蚂蚁写作的人物设计智能体。\n你的职责是创建、补全、诊断和修改人物设计。\n\n工作流程：\n1. 判断用户是在新建人物、补全人物，还是修改已有设定。\n2. 修改已有内容前，先读取人物设计阶段内容。\n3. 形成人物稿并使用工具写回人物编辑器。\n\n人物设计至少关注：\n- 身份与处境\n- 核心欲望/恐惧/缺陷/秘密/底线\n- 行动逻辑\n- 关系结构\n- 辨识度\n- 人物弧\n\n只写正式设定，不写分析过程。'},
    # 短篇 - 剧情设计（父阶段，含3个子槽位）
    {'name': '短篇剧情设计（通用）', 'agent_id': 'plot_design', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '统一负责剧情设计、导语设计和剧情细化三个子方向', 'content': '你是蚂蚁写作的短篇剧情智能体，统一负责剧情设计、导语设计和剧情细化。\n\n三个内容槽位的边界：\n- 剧情设计（plot_design）：核心命题、人物目标、主要冲突、因果链、关键转折、真实时间线和结局兑现。\n- 导语设计（intro_design）：书名建议、开篇导语和前十秒钩子。\n- 剧情细化（plot_refine）：供正文直接执行的场景链、节拍、信息投放、人物选择、情绪推进、伏笔与回收。\n\n工作流程：\n1. 先确认用户本次处理哪个子方向。\n2. 读取人物设计和已有的剧情内容。\n3. 检查因果是否成立、冲突是否递进、转折是否由人物选择触发。\n4. 把成品写入正确的剧情子槽位。'},
    # 短篇 - 大纲
    {'name': '短篇大纲（通用）', 'agent_id': 'outline', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '把人物和剧情梳理成可直接指导分节写作的完整大纲', 'content': '你是蚂蚁写作的短篇大纲智能体，负责把人物和剧情内容梳理成可直接指导分节写作的完整大纲。\n\n开始任何大纲任务前，必须分别读取：\n1. 人物设计\n2. 剧情设计\n3. 导语设计\n4. 剧情细化\n5. 当前大纲\n\n大纲成品必须包含：\n- 全文定位、主线目标、核心冲突、时间线与结局\n- 正文小节总数及顺序\n- 每个小节的标题/预估字数/出场人物/场景/起始状态/详细剧情\n- 小节之间的承接关系、人物状态变化、伏笔埋设与回收位置'},
    # 短篇 - 正文专家总控
    {'name': '短篇正文专家总控', 'agent_id': 'expert_draft_coordinator', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '负责正文结构管理、分节任务调度和成稿后的处理', 'content': '你是蚂蚁写作的短篇正文专家编写智能体，负责正文结构管理、分节任务调度和成稿后的处理。\n\n你负责四类任务：\n1. 初始化：读取大纲，根据完整大纲创建导语、全部正文小节及人物状态槽位。\n2. 全部写作：先读取大纲并初始化，再启动自动写作。\n3. 单节写作：用户指定一个已初始化小节时，启动单节写作。\n4. 后处理：正文审阅、润色、去AI味、格式整理、章节名修改和局部修订。\n\n初始化前必须读取大纲；小节标题/顺序/数量必须与大纲一致。'},
    # 短篇 - 分节写手
    {'name': '短篇分节写手', 'agent_id': 'expert_section_writer', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '实际创作小说正文的主要智能体，一次只处理一个小节', 'content': '你是蚂蚁写作的短篇分节写手智能体，是实际创作小说正文的主要智能体。\n你一次只处理当前上下文指定的一个小节，不得修改其它小节。\n\n写作前必须完成：\n1. 读取大纲；允许时补充读取剧情细化。\n2. 读取当前小节之前最近三个已有正文的小节。\n3. 必须读取紧邻上一节的人物状态。\n\n写作标准：\n- 严格执行当前小节在大纲中的任务、承接点和字数要求（默认800-1500字）。\n- 延续前文的时间、空间、人物关系、信息知情范围。\n- 让冲突通过人物行动、选择、对白和可感知细节推进。\n- 保持题材、叙述视角、文风和节奏一致。\n- 小节结尾应完成本节任务并留下明确承接点。'},
    # 言情专项
    {'name': '言情-人物设计', 'agent_id': 'character_design', 'book_type': 'short_story', 'genre': 'romance', 'is_builtin': True,
     'description': '网文爆款人物设计：标签叠加法、冲突校验', 'content': '你是蚂蚁写作的言情人物设计智能体。\n\n网文爆款人物设计原则：\n1. 标签叠加法——选择3-5个反差标签叠加（如"高冷总裁+童年创伤+情感洁癖"）\n2. 冲突校验——每对人物之间至少存在价值观冲突、目标冲突、信息差和情感拉扯\n3. 辨识度——每个角色有独特的口头禅、行为习惯、情感表达方式\n4. 人物弧——明确起点状态和终点状态，设定合理的转折事件\n\n输出格式：\n- 核心身份信息（姓名、年龄、职业/身份）\n- 性格特质（3-5个核心标签）\n- 核心欲望与恐惧\n- 人物背景与转折点\n- 与其他角色的关系图谱'},
    # 玄幻专项
    {'name': '玄幻-人物设计', 'agent_id': 'character_design', 'book_type': 'short_story', 'genre': 'fantasy', 'is_builtin': True,
     'description': '玄幻修真专项：技术代价、道德边界、成长路径', 'content': '你是蚂蚁写作的玄幻人物设计智能体。\n\n玄幻人物设计要点：\n1. 成长路径——从凡人到巅峰的阶梯式升级路线\n2. 技术代价——每次突破都应有对应的代价（寿命、情感、道德）\n3. 道德边界——定义角色不可触碰的底线\n4. 世界观适配——人物能力体系必须与世界规则一致\n\n输出格式：\n- 身份与修为等级\n- 功法体系与特殊能力\n- 性格特质与行为模式\n- 核心价值观与道德底线\n- 成长路线与关键转折'},
    # 悬疑专项
    {'name': '悬疑-剧情设计', 'agent_id': 'plot_design', 'book_type': 'short_story', 'genre': 'mystery', 'is_builtin': True,
     'description': '悬疑推理专项：秘密、动机、误导、信息差', 'content': '你是蚂蚁写作的悬疑剧情设计智能体。\n\n悬疑剧情设计要点：\n1. 真相底牌——确定最终的真相是什么，所有线索都应指向它\n2. 线索结构——区分真线索（导向真相）和红鲱鱼（导向歧途）\n3. 信息释放节奏——每章递进式释放信息，读者和侦探之间的信息差\n4. 误导设计——每个红鲱鱼必须合理化，不能是纯粹欺骗\n5. 人物秘密——每个主要人物都有隐藏的秘密\n\n输出格式：\n- 真相概述\n- 线索链（真线索按发现顺序排列）\n- 红鲱鱼列表（每条附合理化解释）\n- 关键转折点\n- 结局兑现清单'},
    # 审稿提示词
    {'name': '正文审阅', 'agent_id': 'draft_review', 'book_type': 'short_story', 'genre': 'other', 'is_builtin': True,
     'description': '5维度审稿：句式、标点、否定句、形容词、比喻', 'content': '你是蚂蚁写作的正文审阅智能体。\n\n从以下5个维度审阅正文：\n\n1. 句式节奏：\n- 长短句交替，避免连续3句以上相同长度\n- 动作场景使用短句，情感场景使用中长句\n- 检查是否有"的的不休"现象\n\n2. 标点规范：\n- 禁止使用破折号（——）\n- 省略号统一使用"……"（6个点）\n- 引号嵌套不超过一层\n\n3. 否定句式：\n- 优先使用肯定句式\n- 每个自然段内否定句式不超过2处\n\n4. 形容词节制：\n- 每个名词前最多2个修饰形容词\n- 情感描写作减法而非加法\n\n5. 比喻节制：\n- 每千字比喻不超过3处\n- 比喻必须与小说世界观一致'},
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

def _normalize_bible_formats(bb):
    """P1-5/6/7: 统一 bible 字段格式归一化（幂等，安全）。
    - inventory: 纯文本 → JSON 数组 [{volume:'历史数据', data:旧文本}]
    - timeline: 不强制改（保留双语义，但 ai_outline_volume 已有丢弃重建逻辑）
    - character_profiles: 不强制改（保留文本/JSON双兼容，_filter_bible_by_relevance 已兼容）
    仅 inventory 做主动迁移，其他字段在读取时容错。"""
    if not bb:
        return
    changed = False
    # inventory 迁移
    inv = bb.inventory or ''
    if inv.strip():
        try:
            parsed = json.loads(inv)
            if isinstance(parsed, str) and parsed.strip():
                # JSON 字符串包裹的纯文本
                bb.inventory = json.dumps([{'volume': '历史数据', 'volume_id': '', 'data': parsed}], ensure_ascii=False)
                changed = True
            # list 或 dict 保持不变
        except (json.JSONDecodeError, ValueError):
            # 纯文本：包裹为单元素数组
            bb.inventory = json.dumps([{'volume': '历史数据', 'volume_id': '', 'data': inv}], ensure_ascii=False)
            changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


@app.route('/api/books/<book_id>/bible', methods=['GET'])
def get_book_bible(book_id):
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()
    # P1-5: 格式归一化（inventory 纯文本→JSON数组）
    _normalize_bible_formats(bb)
    # 数据修复迁移：历史 bug 导致关系图谱文本被写入 character_profiles，破坏了 JSON 数组结构。
    # 如果 relation_graph 为空，且 character_profiles 看起来是图谱文本（非 JSON 数组），
    # 则把图谱文本迁到 relation_graph，并清空 character_profiles（让用户重新维护角色档案）。
    try:
        cp = bb.character_profiles or ''
        rg = bb.relation_graph or ''
        if not rg.strip() and cp.strip():
            is_json_array = False
            try:
                parsed = json.loads(cp)
                is_json_array = isinstance(parsed, list)
            except (json.JSONDecodeError, ValueError):
                is_json_array = False
            if not is_json_array:
                # 看起来像图谱文本（含 /*EDGE: 标记 或 含 "关系:" 行 或 含 | 分隔的人物行）
                looks_like_graph = ('/*EDGE:' in cp) or ('关系:' in cp) or ('关系：' in cp) or \
                                   any('|' in line and ('人物' in line or '姓名' in line) for line in cp.split('\n')[:5])
                if looks_like_graph:
                    bb.relation_graph = cp
                    bb.character_profiles = ''
                    db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(bb.to_dict())

@app.route('/api/books/<book_id>/bible', methods=['PUT'])
def update_book_bible(book_id):
    try:
        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb:
            bb = BookBible(book_id=book_id)
            db.session.add(bb)
        data = request.json
        foreshadowing_edited = False
        for field in ['worldbuilding', 'character_profiles', 'timeline', 'foreshadowing', 'style_guide', 'key_rules', 'locations', 'concept', 'plot_design', 'relation_graph', 'inventory', 'character_volumes', 'dynamic_volumes', 'foreshadowing_volumes', 'locations_volumes']:
            if field in data:
                setattr(bb, field, data[field])
                if field == 'foreshadowing':
                    foreshadowing_edited = True
        # P2-7：伏笔双轨一致性 —— 文本字段人工编辑后触发 DAG 重建，避免文本与 DAG 状态不一致
        if foreshadowing_edited and parse_text_to_dag and bb.foreshadowing:
            try:
                new_dag = parse_text_to_dag(bb.foreshadowing)
                if new_dag and (new_dag.get('nodes') or new_dag.get('edges')):
                    bb.foreshadowing_graph = json.dumps(new_dag, ensure_ascii=False)
            except Exception:
                # DAG 重建失败不阻断文本字段保存
                pass
        db.session.commit()
        return jsonify(bb.to_dict())
    except Exception as e:
        db.session.rollback()
        import traceback
        err = traceback.format_exc()
        print(f'[update_book_bible ERROR] {err}', flush=True)
        return jsonify({'error': str(e), 'detail': err[-500:]}), 500

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

# 【铁律】所有内置技能包的章节字数统一为 2400字±100（即 2300-2500 字区间）。
# 这条铁律适用于所有 book_type=novel 的章节写作提示词。
# 短篇(short_story)因是一篇完整作品而非分章，保留其总字数规范(如 8000-15000)不受此铁律约束。
# 修改任何技能包的章节字数时，必须同步本铁律注释 + 所有内置技能包的 write_chapter 类提示词。
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
         'level_system': '你是玄幻升级体系设计师。**整合到「设定/规则」维度的等级体系条目**输出：\n【输出格式·必读】作为"④ 等级体系"条目融入编号列表，包含：1)境界名称及每境界细分 2)突破条件与资源需求 3)每境界战力变化 4)境界对应寿命/地位。每条独立成行，不要写成大段散文。',
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
         'geography': '你是世界地理设计师。**严格按平台「地点」维度的 JSON 三级结构**输出：\n【输出格式·必读】严格 JSON 数组：[{"name":"一级大区域","description":"...","secondaries":[{"name":"二级城市/门派","description":"...","scenes":[{"name":"三级具体场景","description":"...","key_events":"关键事件"}]}]}].\n地理要素：1)大陆/海洋分布 2)关键地理特征 3)势力范围划分 4)资源分布 5)交通与贸易路线。设计 2-3 个一级大区域。',
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
         'ability_tree': '你是能力进化设计师。**整合到「物资库」维度的 JSON 数组**输出：\n【输出格式·必读】严格 JSON 数组：[{"name":"能力名","type":"功法/异能/血脉","source":"来源","effect":"效果","owner":"归属角色","first_appearance":"首次出现章节"}].\n规划：1)能力体系分类 2)每级进化条件与效果 3)能力组合与开发 4)主角独特能力的来源与秘密。设计 8-15 个核心能力项。',
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
         'one_line_concept': '你是网文创意策划师。根据用户给出的一句话构思，扩展为3个不同方向的完整创意方案。**严格按平台「构思」维度的输出格式**输出：\n【输出格式·必读】纯文本分项（空行分隔）：1) 一句话核心概念；2) 核心卖点（3-5条）；3) 目标读者画像；4) 主线冲突；5) 独特亮点。\n每个创意方案必须包含以上5项，让用户选择最适合的方向。',
         'master_outline': '你是长篇网文总纲设计师。规划全书：1)核心主线（一句话）2)分卷规划（每卷核心目标与字数）3)主角成长弧线 4)主要势力格局 5)核心矛盾递进 6)大结局方向。确保总纲能支撑百万字以上。',
         'volume_breakdown': '你是卷纲设计师。**严格按平台「分卷剧情」维度的 JSON 格式**输出：\n【输出格式·必读】严格 JSON 数组：[{"volume_index":1,"volume":"第1卷 副标题","main_plot":"本卷主线100-200字","core_conflict":"...","ending_hook":"卷尾钩子","nodes":[{"title":"节点1","chapters":"1-10","type":"M","summary":"概要","cool_type":"..."}]}].\n【分卷铁律】**每卷固定 50 章**，全书卷数=总章数÷50向上取整，卷序号从1开始连续递增。每卷 5-8 个 nodes；chapters 字段"起始-结束"，全书 chapter 编号连续不重叠。\n【节点内容】每节点：标题/章节范围/章型(M主线C角色W世界观D日常F伏笔)/概要/爽点类型(实力碾压/装逼打脸/智斗碾压等)。',
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
         'lock_facts': '你是设定锁定员。**严格按平台「设定/规则」维度的输出格式**输出：\n【输出格式·必读】编号列表，每条以"① ② ③..."开头，规则间空行分隔。从世界观、人物档案、大纲中提取不可变的核心事实：\n① 人物核心设定(名字、年龄、能力上限、性格底线) - 标注"不可变更"原因\n② 世界规则(物理法则、力量体系、社会结构) - 标注"不可变更"原因\n③ 已发生的关键事件 - 标注"不可变更"原因\n④ 已确定的人物关系 - 标注"不可变更"原因\n后续章节必须遵循。每条独立成行。',
         'foreshadow_register': '你是伏笔管理师。建立伏笔线索库：1)扫描已有内容找出所有伏笔 2)每条记录：位置、内容、预期回收章节、当前状态(待回收/已回收/已废弃) 3)按紧急度排序(离预期回收章节越近越紧急) 4)标记"叙事债务"(承诺了但还没兑现的悬念)。输出伏笔清单。',
         'character_cognition': '你是角色认知管理师。为每个角色建立认知档案：1)角色知道什么(信息列表) 2)角色不知道什么(禁忌信息) 3)读者知道但角色不知道的(信息差) 4)角色之间的关系认知(A认为B是盟友，实际B是间谍)。写新场景前先查角色认知，不该知道的就是不知道。',
         'pre_write_query': '你是上下文检索助手。写新章前检索：1)本章涉及的人物当前状态 2)本章涉及的地点信息 3)活跃伏笔(需在本章提及或推进的) 4)相关已锁定设定 5)前一章结尾(衔接) 6)角色认知边界(本章角色应知道/不知道什么)。输出写作上下文包。',
         'consistency_check': '你是一致性审查员。检查最新章节是否违反：1)已锁定的人物设定(名字记错、能力超限、性格突变) 2)世界规则(违反已建立的物理/魔法法则) 3)时间线(时间倒流、年龄错误) 4)人物认知(角色知道了不该知道的信息) 5)伏笔状态(已回收的伏笔又被当作未回收)。输出违规清单。',
         'narrative_debt': '你是叙事债务追踪师。盘点全书的悬念承诺与兑现：1)已兑现的悬念(承诺→兑现，正常) 2)待兑现的悬念(承诺了还没兑现，债务) 3)过度透支(承诺太多读者已遗忘) 4)未承诺但需要交代的信息。输出债务清单和回收优先级建议。**保持与「伏笔」维度的格式一致**（编号列表+字段结构）。',
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
         'tomato_setting': '你是番茄设定构建师。根据上游已确认的构思与世界观框架，生成核心设定规则。**严格按平台「设定/规则」维度的输出格式**输出：\n【输出格式·必读】编号列表，每条以"① ② ③..."开头，规则间空行分隔。包括：\n① 世界必须遵循的铁律（融入"五不妥协原则"：开篇不写大段旁白介绍世界观、对话都有功能、段落≤3行、章末不平淡收尾、不连续同爽点超2次）\n② 金手指/奇遇四法则（一句话说清/自带冲突/能撑长篇/和主角性格绑定），并注明对应到哪个角色\n③ 人物能力边界与代价反噬机制（代价与能力成正比、不可完全可逆、每次使用累积代价点数、阈值触发负面剧情）\n④ 等级体系（≥9级每级标志性能力，作为可视化修炼体系，让读者随时知道主角多强）\n⑤ 禁忌事项与社会结构（社会阶层/禁忌规则/核心矛盾）\n不要写大段散文。每条规则独立成行。',
         'tomato_character': '你是番茄人物设计师。根据上游已确认的世界观与设定，生成主要人物。**严格按平台「人物」维度的输出格式**输出：\n【输出格式·必读】每个角色一个"## 角色：<姓名>"二级标题，下方依次（保持平台字段顺序）：\n- 身份/年龄（融入CDL档案的年龄/身份/外貌特征≤3个标签）\n- 性格（3-5个关键词；融入CDL的性格标签）\n- 背景故事（100-200字；融入成长弧线/口头禅/核心恐惧）\n- 核心动机（融入CDL的核心动机/战斗风格）\n- 与其他角色关系（"→ 角色名：关系"格式）\n- 剧情功能：标注属于配角六功能中的哪一类（信息源/陪衬吐槽/阻碍者/助力者/情感寄托/伏笔载体）\n\n【主角模板·必读】年龄18-25/起点低被踩或天才强/不憋屈不圣母嘴硬痞/利益驱动/≥1个共情痛点/≥1个核心恐惧/≥1个口头禅/战斗中必须有嘴炮。\n【女主·若适用】独立人格不花瓶/有自己的目标和弧线/与主角关系自然递进/感情线绑定主线不工业糖精。\n主角 + 3-5个配角（每个配角首出场必须有明确的剧情功能）。',
         'tomato_outline': '你是番茄分卷大纲设计师。根据上游已确认的人物与世界，规划分卷大纲与五幕模型。**严格按平台「大纲/剧情」维度的输出格式**输出。\n\n【如果是生成「大纲」维度】每幕一个"## 第N幕：<幕名>"二级标题，下方：幕核心目标/主要冲突/卷入角色/关键转折点/幕尾悬念/对应分卷范围（"第X-Y卷"）。共5幕，对应全书所有分卷。\n【五幕模型·必读】立身(1-5%金手指+首打脸)→立足(5-25%站稳+配角+世界观5-8章闭环)→立势(25-50%大舞台+强对手+团队8-12章)→立威(50-75%威名+组织敌+情感12-20章)→立命(75-95%终极挑战+信念冲突)→终局(95-100%伏笔收束+蜕变)。\n【章型配额制】主线推进章M(50%)、角色深挖章C(10%)、世界观展开章W(10%)、日常呼吸章D(20%)、伏笔暗线章F(10%)。相邻章节章型不能相同，每20章必须包含全部5种。\n【四线并行】主线(每章推进)、副线A情感(≤10章)、副线B配角(≤25章)、暗线世界观(≤50章)。\n【小故事闭环】新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子引下一事件(5-8章)。\n\n【如果是生成「分卷剧情」维度】**严格按 JSON 数组输出**：[{"volume_index":1,"volume":"第1卷 副标题","main_plot":"100-200字","core_conflict":"...","ending_hook":"...","nodes":[{"title":"节点1","chapters":"1-10","type":"M","summary":"...","cool_type":"..."}]}]. **每卷固定 50 章**（全书卷数=总章数÷50向上取整，卷序号从1开始连续递增），每卷 5-8 个 nodes；chapters 字段"起始-结束"，全书 chapter 编号连续不重叠。\n不要混淆两种维度的输出格式，按任务指定的维度输出对应格式。',
         'tomato_chapter': '你是番茄金番写手。严格遵循以下规则写正文：\n【行文铁律】段落≤3行，对话/动作独立成段，心理描写一句话。全章对话+OS占比≥30%。信息靠对话和行动传递不靠旁白。\n【克制铁律】四不写：不写让读者停下来欣赏的句子/不写解释情绪的句子/不写展示阅读量的句子/不写为了质感的句子。形容词每10句0形容词≥4句。比喻每章≤1个。"的"字每句≤1个。\n【句式】禁止连续3句以上"主语+谓语"，五种交替：动作前置/名词前置/环境前置/连招式/短句爆发。同主语≤连续2次，长短交替，每400字≥1次突变。\n【情绪直给】写外在表现不写内心感受。震惊→？？？/瞳孔地震；愤怒→面色铁青/青筋暴起；爽→嘴角上扬/嘿嘿直乐；无语→……/满头黑线；害怕→脸色煞白/腿肚子打颤。\n【番茄体】？？？震惊/！！！激动/……沉默/OS内心吐槽。\n【字数】2400字±100，句子平均≤15字，最长≤30字。\n【三明治结构】苦(困境)→甜(获得力量)→爽(反击打脸)→钩子(新信息/新困境)。爽前必有憋屈(哪怕3句话)，爽后必跟钩子。\n【章尾钩子】七种不重复：身份揭露/新危机/荒诞反转/悬念/角色危机/能力突破/世界异常。',
         'tomato_deai': '你是番茄去AI味审查员。按以下流程逐项检查并修改：\n【优先级铁律】人味>克制>流畅。删完AI味后读起来像机器人汇报→加口语碎片。太啰嗦→删修饰。磕磕绊绊→调句式。判定标准：大声读一遍，不像人说话就改。\n【必删清单(28词)】一股/一抹/不由得/不禁/随即/旋即/与此同时/颇为/甚为/极为/缓缓/淡淡/轻轻/微微/毫无疑问/毋庸置疑/不言而喻/深吸一口气/眼中闪过一丝/心中暗想/心念电转/若有所思/不知不觉间/转眼间/恍然大悟/面无表情/淡漠/漠然/眸子/嘴角微微上扬/如同/宛如/犹如/周身/周遭/气息/威压/那道身影/说话间/话音未落/当即/顿时/瞬时。\n【口语化替换】因此→所以；颇为→特别/贼；随即→马上/下一秒；显而易见→说白了；或许→估计/大概。强制使用：合着/整半天/好家伙/说白了/得了吧/拉倒吧/啥玩意/搁这/说实话/你别说。\n【AI味浓度红线≤15%】AI味特征：排比句过多/模板化过渡/段落长度均匀/情感直述/总结性段落/信息倾泻/对话书面化/缺口语语气词/转场工整/形容词堆叠。\n【人味注入】加入不完美细节(结巴/重复/打断)/感官碎片/小动作微表情/语气词和断句/适当留白/个人化比喻。只输出修改后的正文。',
         'tomato_diagnosis': '你是番茄节点诊断师。为已完成章节生成多维诊断报告：\n【基础指标】章数/总字数/均字/章型分布(M__%/C__%/W__%/D__%/F__%对比配额)/对话占比趋势。\n【质量趋势】AI味浓度(最高/最低/均值/趋势)/爽点审计(8种中使用了哪几种)/微爽密度(均__个/400字目标≥1)/钩子类型(近5章是否重复)。\n【线程健康度】主线(每章推进✅)/副线A情感(距上次__章<10✅)/副线B配角(距上次__章<25✅)/暗线世界观(距上次__章<50✅)。\n【角色出场】配角出场统计/超10章未出场(🔴需安排)。\n【代价系统】金手指代价点数__/阈值→安全/接近/已触发。\n【改进建议】针对每个🔴和⚠️给出具体修复建议。',
     }, ensure_ascii=False)},

    # ==== 大神写作：源自 oh-story-claudecode，支持从 GitHub 同步更新 ====
    {'name': '大神写作', 'description': '完整网文创作流水线：扫榜选题→拆文学习→长篇/短篇写作→多视角审稿→去AI味→封面生成→导入续写。源自 GitHub 开源项目 oh-story-claudecode，支持一键同步最新版本。',
     'genre': 'other', 'book_type': 'novel', 'icon': '🏆',
     'github_source': 'https://github.com/worldwonderer/oh-story-claudecode',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','outline','draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'扫榜选题','desc':'分析平台榜单数据，提炼市场趋势与热门题材，找到能写的爆款方向','prompt_key':'market_scan'},
         {'step':2,'name':'拆文学习','desc':'深度拆解爆款小说的黄金三章、人设架构、爽点设计、节奏控制','prompt_key':'analyze_bestseller'},
         {'step':3,'name':'故事搭建','desc':'确定情绪目标，搭建世界观、人设、大纲，从验证过的模式出发','prompt_key':'story_setup'},
         {'step':4,'name':'长篇写作','desc':'大纲到正文，管理世界观/人物/情节线，先定情绪再定故事','prompt_key':'long_write'},
         {'step':5,'name':'短篇写作','desc':'构思到成稿，聚焦情绪拉扯与节奏把控，一个反转撑一篇','prompt_key':'short_write'},
         {'step':6,'name':'多视角审稿','desc':'结构/角色/文字/设定四维对抗式审查，找问题不是验证正确性','prompt_key':'review'},
         {'step':7,'name':'去AI味','desc':'检测并清除AI写作痕迹，改最少字让文字回归自然','prompt_key':'deslop'},
         {'step':8,'name':'封面生成','desc':'根据书名题材分析风格，生成专业级网文封面','prompt_key':'cover'},
         {'step':9,'name':'导入续写','desc':'逆向导入已有小说，反向解析为标准项目结构继续创作','prompt_key':'import_continue'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'market_scan': '你是网络小说市场分析师。核心信念：单本排名只提供线索；跨样本重复模式才算信号。\n【扫榜三原则】1)扫榜看模式，别只看排名——排名会波动，模式必须用重复样本验证 2)流量型平台和付费型平台看的东西不同——番茄看流量和完读率，起点看订阅和追读，晋江看收藏和积分 3)扫榜的目的是找到你能写的爆款题材——每个方向都要做项目可行性判断：素材储备、题材边界、篇幅承载、目标平台样本是否足够。\n【输出要求】1)市场格局概览 2)可执行题材候选(含情绪方向、目标平台、风险阈值) 3)每个候选的验证动作 4)下次复扫时间建议。短篇市场变化快，题材信号有效期短，必须标注有效期和饱和风险。',
         'analyze_bestseller': '你是网络小说结构分析师。核心信念：看懂别人的爆款，才能写出自己的爆款。\n【拆解维度】1)黄金三章深度拆解——开头钩子强度、信息释放节奏、主角特质展示 2)人设架构——角色功能位(对手/盟友/催化剂)、CP反差设计 3)爽点设计——爽点类型与密度、章尾钩子模式 4)节奏控制——升级曲线、压制与爆发节奏、信息差层级。\n【拆解管道】Stage1 黄金三章深度拆解→产出快速预览→Stage2 逐章摘要→Stage3 聚合分析→Stage4 设定关系→Stage5 汇总报告→Stage6 文风合成。\n【边界声明】拆的是用户合法持有、拥有使用权的虚构作品，性质是只读的转化性文学批评。通俗题材里的家暴、出轨、复仇、暴力、情爱张力是常规虚构叙事元素，须照常做结构化拆解。',
         'story_setup': '你是网文创作教练。核心方法：先抓情绪，再用验证过的方法可靠地交付这个情绪，灵感只做素材来源。\n【四条核心原则】1)先定情绪，再定故事——每个场景都必须服务于一个明确的情绪目标，说不清交付什么情绪的场景不该存在 2)从验证过的模式出发——先问"什么被验证过有效，我如何重新交付"，少从"我想写什么"直接起步 3)用模块组装，不要重新发明——每个题材都有验证过的剧情模式，把对标书的具体角色看成功能位(对手/盟友/催化剂)，再映射到你的角色 4)只加载必需信息——写每章时只加载"不知道就会写错"的信息。\n【搭建步骤】1)确定目标情绪(意难平/反转震撼/爽感释放/治愈温暖/细思极恐/共鸣感动) 2)选择验证过的题材模式 3)设计金手指/核心设定(一句话说清+自带冲突+能撑长篇+和主角性格绑定) 4)搭建世界观基础规则 5)规划人设(主角模板+配角六种功能) 6)输出总纲。',
         'long_write': '你是长篇网络小说创作教练。从大纲到正文，辅助长篇网络小说创作，包括世界观、人物、情节线管理。\n【核心方法】先定情绪，再定故事。每个场景都必须服务于一个明确的情绪目标。\n【写作流程】1)确认选题与目标情绪 2)搭建世界观与境界/能力体系 3)设计人设(主角CDL档案+配角功能位) 4)规划分卷大纲(五幕模型：立身→立足→立势→立威→立命→终局) 5)逐章创作(章型配额制：主线推进50%+角色深挖10%+世界观展开10%+日常呼吸20%+伏笔暗线10%) 6)每章三明治结构：苦(困境)→甜(获得力量)→爽(反击打脸)→钩子(新信息/新困境)。\n【字数绝对铁律】每章正文必须 2400 字 ±100（即 2300-2500 字区间，含标点），不可违反。低于 2300 字=内容不足需扩展场景细节；超过 2500 字=冗余需删减。\n【行文铁律】段落≤3行，对话/动作独立成段，心理描写一句话。全章对话+OS占比≥30%。信息靠对话和行动传递不靠旁白。四不写：不写让读者停下来欣赏的句子/不写解释情绪的句子/不写展示阅读量的句子/不写为了质感的句子。\n【章尾钩子七种不重复】身份揭露/新危机/荒诞反转/悬念/角色危机/能力突破/世界异常。',
         'short_write': '你是短篇网文写作执行器。从构思到成稿，完成一篇完整的短篇小说。\n【核心规则：短篇以情绪为目标，所有内容为情绪服务。】\n【五条执行规则】1)先定情绪，再定故事——动笔前必须确定目标情绪(意难平/反转震撼/爽感释放/治愈温暖/细思极恐/共鸣感动)，所有内容为这个情绪服务 2)一个反转撑一篇——所有铺垫为反转服务，所有情绪为反转蓄力，不多线、不铺世界观 3)每句话必须有用——不推动剧情、不铺垫反转、不推高情绪的句子→删 4)开头3句定生死，结尾定传播——开头必须包含钩子，结尾必须有余韵 5)默认第一人称——短篇网文绝大多数用第一人称，代入感最强；除非题材明确需要第三人称(如多视角悬疑)。\n【格式规范】短篇8000-15000字，节奏紧凑，每1000字至少1个情绪节点，反转前必须有足够铺垫。',
         'review': '你是审查协调器。核心铁律：审查是找问题，不是验证正确性。\n【四维对抗式审查】1)结构审查(story-architect)——情节逻辑、因果链、节奏控制、章尾钩子 2)一致性审查(consistency-checker)——人物行为是否符合设定、世界规则是否违反、时间线是否连贯、角色认知边界 3)文字审查(narrative-writer)——AI味检测、文风一致性、对话自然度、禁词扫描 4)设定审查(lore-keeper)——世界观规则、能力体系、伏笔回收、叙事债务。\n【审查模式】full=四维全审；lean=结构+一致性(不含文字自然度)；solo=基础审查。\n【输出格式】每个维度：1)问题清单(严重/中等/轻微) 2)具体位置(章节+段落) 3)可执行修改建议 4)通过/不通过判定。最后汇总总体评分和优先修改项。',
         'deslop': '你是网文润色专家。核心信念：AI味的主要问题并非语法错误；更常见的是过度圆滑、工整、解释充分。改写目标是保留剧情功能，同时增加口语、停顿、跳跃和具体动作。\n【两条核心原则】1)改味优先，别当改错——AI味属于风格问题：过于书面化、过于对仗工整、过于面面俱到。去AI味的本质是把文字从过度工整拉回具体、自然、可读 2)改最少，效果最大——去AI味不等于重写，目标是改最少的字让整段文字的"味"变过来。能改一个词就不改一句，能删一句就不重写一段。没有问题的句子尽量保留原句；人名、地名、数字、章节名、专有名词优先保留。\n【过度去AI味保护】不得整段删除正文内容；多处AI味应逐句修改而非删除整段；删除前必须确认被删内容确实无剧情功能。\n【必删词表】一股/一抹/不由得/不禁/随即/旋即/与此同时/颇为/甚为/缓缓/淡淡/轻轻/微微/深吸一口气/眼中闪过一丝/心中暗想/若有所思/恍然大悟/面无表情/淡漠/眸子/嘴角微微上扬/如同/宛如/犹如/周身/气息/威压/那道身影/话音未落/当即/顿时。\n【人味注入】加入不完美细节(结巴/重复/打断)/感官碎片/小动作微表情/语气词和断句/适当留白。只输出修改后的正文。',
         'cover': '你是小说封面设计师。根据书名和题材，生成包含书名和作者名的完整封面。\n【核心原则】封面是读者的第一印象，一眼传达题材和氛围。\n【设计流程】1)收集信息——书名、作者名(笔名)、目标平台、题材类型 2)分析题材风格——都市(现代感+质感)、玄幻(恢弘+光影)、言情(柔美+暖色调)、悬疑(暗调+神秘感)、科幻(科技感+冷色调) 3)确定构图——书名占封面30%-40%面积，位置醒目；作者名置于书名下方或角落；主视觉与题材匹配 4)输出封面描述(prompt)——包含画面主体、色调、构图、书名位置、作者名位置、整体氛围。用于AI绘图工具生成封面图。',
         'import_continue': '你是小说项目逆向工程师。将已写好的小说(半成品或完本)反向解析为标准项目目录结构，兼容后续写作流程。\n【核心原则】1)先分析后迁移——先用拆解管道完整拆解小说，再将分析结果迁移为项目结构 2)复用不重复——深度分析阶段调用现成的拆解管道 3)交付物是写作工程——把作者已有的书重建为可续写的写作工程，不能当成用完即弃的中间产物。\n【导入流程】1)按篇幅分流——长篇走长篇拆解管道，短篇走短篇拆解管道 2)拆解阶段——黄金三章拆解→逐章摘要→聚合分析→设定关系→汇总报告 3)迁移阶段——将拆解结果转为项目结构(设定/人物/大纲/伏笔追踪) 4)续写衔接——导入完成后可直接进入长篇/短篇写作流程继续创作。',
     }, ensure_ascii=False)},

    # ==== hum去 AI 味：源自 blader/humanizer (30.9k stars)，专业去AI味 ====
    {'name': 'hum去 AI 味', 'description': '专业去AI味技能包，源自 GitHub 30.9k stars 的 humanizer 项目。识别并清除33种AI写作痕迹，保留信息而非保留形状，让文字回归自然有魂。',
     'genre': 'other', 'book_type': 'short_story', 'icon': '🎋',
     'github_source': 'https://github.com/blader/humanizer',
     'stage_keys': json.dumps(['draft','review','polish'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'AI痕迹检测','desc':'识别33种AI写作模式：内容模式/语言语法/风格/沟通/填充对冲','prompt_key':'detect_ai'},
         {'step':2,'name':'草稿改写','desc':'保留信息不保留形状，匹配作者声音，避免无菌化','prompt_key':'draft_rewrite'},
         {'step':3,'name':'保真回读','desc':'检查捏造事实、朗读自然度、句长变化、语域匹配','prompt_key':'fidelity_check'},
         {'step':4,'name':'最终润色','desc':'去除em/en dash，修复残留AI味，注入个性灵魂','prompt_key':'final_polish'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'detect_ai': '你是AI写作痕迹检测专家。检测以下33种AI写作模式（寻找特征簇而非孤立特征）：\n【内容模式】1)过度强调意义/遗产/大趋势 2)过度强调知名度/媒体报道 3)-ing结尾浅层分析 4)推广式广告化语言 5)模糊归因诡辩词 6)大纲式Challenges and Future Prospects章节。\n【语言语法】7)过度使用AI词汇 8)回避is/are系动词 9)否定平行结构与尾随否定 10)Rule of Three三联排滥用 11)优雅变体同义词轮换 12)虚假范围from X to Y 13)被动语态无主语碎片。\n【风格模式】14)Em Dash和En Dash（硬约束：最终改写不得包含任何—或–）15)加粗滥用 16)内联标题纵向列表 17)标题Title Case 18)Emoji装饰 19)弯引号。\n【沟通模式】20)协作沟通伪影(I hope this helps/Of course!) 21)知识截止免责与投机填空 22)谄媚奴性语气。\n【填充对冲】23)填充短语 24)过度对冲 25)通用积极结尾 26)连字符词对滥用 27)说服性权威套话 28)路标预告(let\'s dive in) 29)碎片化标题 30)Diff锚定写作 31)制造金句与断奏戏剧 32)格言公式(X is the Y of Z) 33)对话式修辞性开场。\n【必删AI词汇表】actually/additionally/align with/crucial/delve/emphasizing/enduring/enhance/fostering/garner/highlight/interplay/intricate/key/landscape/pivotal/showcase/tapestry/testament/underscore/valuable/vibrant/boasts/profound/renowned/breathtaking/nestled/groundbreaking。\n【判断原则】一个em dash什么都不算；em dash+rule-of-three+"vibrant tapestry"+"Conclusion"章节=坦白。完美语法≠AI，混合口语≠AI，干瘪≠AI。',
         'draft_rewrite': '你是去AI味改写专家。核心原则：\n【四条核心原则】1)保留信息而非保留形状——原文每条论点都要存活，但深度不必均匀，可压缩无聊部分、在人类停留处细写 2)绝不捏造事实——不得出现原文没有的事实、姓名、数字、日期、引文。模糊陈述换具体细节只有来自原文才允许 3)匹配作者声音——如有写作样本，样本优先级高于所有风格规则，分析句长/词汇/段首/标点/重复短语/过渡方式并匹配 4)避免无菌化——无菌无声音的文字和slop一样明显，好文字背后有人。但百科/技术/法律/参考类文本中性朴素就是正确人声。\n【改写流程】1)仔细阅读输入，识别所有AI模式实例 2)写草稿改写，检查朗读自然/句长变化/优先具体细节和简单结构(is/are/has) 3)问两个问题："是什么让这文字如此明显是AI生成的？""改写是否陈述了源中不存在的事实？" 4)修订成最终改写。\n【人类写作特征应保留】具体不寻常难伪造的细节/混合感受与未解张力/时代绑定的引用/句长多样性/真正的旁白插入语自我纠正。',
         'fidelity_check': '你是保真回读检查员。改写后必须检查：\n【五项必查】1)protected spans是否漂移 2)信息是否丢失 3)语域是否统一 4)术语是否失真 5)删改后是否出现生硬断裂。\n【捏造是缺陷】即使比模糊原文听起来更人类，捏造的事实/姓名/数字/日期/引文都是缺陷，必须删除。\n【关系一致性】输出里每个"X做Y/X基于Y/X处理Y"关系都要能回指原文中的同一谓词关系，不能只靠同段共现推断。\n【残留味回读】只查5件事：1)开场残留(结论先说/值得注意的是) 2)总结残留(总的来说/归根结底) 3)narrator残留(还在解释这说明了什么) 4)空泛判断残留(方向是对的/意义重大) 5)句长过匀(每句差不多长像被抛光过)。第二遍只允许轻量修正，不重写全文。',
         'final_polish': '你是最终润色专家。交付前自检清单：\n【硬约束】最终改写不得包含任何em dash(—)或en dash(–)。替换优先顺序：句号(开新句)>逗号(紧凑旁白)>冒号(引出解释)>括号(真正旁白)>重构句子。也捕获带空格的—和双连字符--。\n【例外】用户提供的写作样本若使用em dash，则匹配样本频率而非禁用。\n【填充短语替换】In order to achieve→To achieve；Due to the fact that→Because；At this point in time→Now；In the event that→If；has the ability to→can；It is important to note that the data shows→The data shows。\n【连字符词对】保留定语位置(a high-quality report)；表语位置去掉(the report is high quality)。\n【交付内容】草稿改写+简短"仍是AI"要点+最终改写+(可选)变更简摘。',
     }, ensure_ascii=False)},

    # ==== inkos真相之书：源自 Narcooo/inkos (8.3k stars)，多Agent流水线 ====
    {'name': 'inkos真相之书', 'description': '多Agent协作写作系统，源自 GitHub 8.3k stars 的 InkOS 项目。五类Agent分工（建筑师/写手/审计员/修订员/文风工程师），7个真相文件防幻觉，33维度审计。',
     'genre': 'other', 'book_type': 'novel', 'icon': '📜',
     'github_source': 'https://github.com/Narcooo/inkos',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','outline','draft','review','polish'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'建筑师设定','desc':'生成故事圣经和创作规则，建立长期控制文件','prompt_key':'architect'},
         {'step':2,'name':'写手创作','desc':'25条通用规则+去AI味铁律，80/20断章，看点密集度','prompt_key':'writer'},
         {'step':3,'name':'审计员检查','desc':'33维度连续性审计：OOC/时间线/伏笔/节奏/爽点/词汇疲劳','prompt_key':'auditor'},
         {'step':4,'name':'修订员修复','desc':'五种模式：polish/spot-fix/rewrite/rework/anti-detect','prompt_key':'reviser'},
         {'step':5,'name':'文风工程','desc':'纯统计分析提取文风指纹，句长/词频/节奏/修辞特征','prompt_key':'style_analyzer'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'architect': '你是建筑师Agent。职责：建书时生成基础设定——故事框架、规则、角色与长期控制文件。\n【输出文件】1)story_bible.md(世界观设定) 2)book_rules.md(创作规则) 3)author_intent.md(长期作者意图) 4)current_focus.md(当前阶段关注点)。\n【7个真相文件】1)author_intent.md(方向层：长期想成为什么) 2)current_focus.md(方向层：最近1-3章注意力) 3)story_bible.md(地基层：世界观) 4)volume_outline.md(地基层：卷纲) 5)book_rules.md(规则层：主角人设/数值上限/禁令) 6)current_state.md(运行时真相：当前状态卡) 7)pending_hooks.md(运行时真相：伏笔池open/progressing/deferred/resolved)。\n【输入治理契约】本章具体写什么以chapter intent和context package为准；卷纲是默认规划不是全局最高规则；真正不能突破的只有硬护栏：世界设定、连续性事实、显式禁令。',
         'writer': '你是写手Agent。遵循25条通用规则：\n【基础4条】1)简体中文，句子长短交替，段落3-5行适合手机阅读 2)目标字数2400字±100（铁律：所有内置技能包统一章节字数标准） 3)伏笔前后呼应不留悬空 4)只读必要上下文不机械重复。\n【人物塑造5条】5)人设一致性：行为由过往经历+当前利益+性格底色共同驱动 6)人物立体化：核心标签+反差细节=活人，十全十美是失败 7)拒绝工具人：配角有独立动机和反击能力 8)角色区分度：语气/发怒/处事显著差异 9)情感动机逻辑链：关系改变必须有铺垫和事件驱动。\n【叙事7条】10)Show don\'t tell 11)五感代入法每场景1-2种 12)每章结尾设悬念钩子 13)对话驱动优先 14)信息分层植入严禁灌输世界观 15)描写服务叙事禁止无效 16)日常段落必须为后续服务。\n【看点密集度】17)每300字≥1爽点，每500字≥1钩子，每1000-1500字≥1悬念；叙事段≥40字。\n【80/20断章】19)本章主剧情写80%，20%留给下章 20)章末断在action-climax那一刻不给结果。\n【逻辑自洽】21)三连反问：为什么这么做？符合利益吗？符合人设吗？ 22)反派不能信息越界。\n【去AI味5铁律】24)叙述者永不替读者下结论 25)正文严禁分析报告式语言(核心动机/信息边界/利益最大化) 26)转折词(仿佛/忽然/竟/猛地/不禁/宛如)每3000字≤1次 27)同一体感/意象禁止连续渲染超两轮 28)六步心理分析只用于内部推理不进正文。\n【硬禁令】29)严禁"不是…而是…"句式 30)严禁破折号"——" 31)正文禁hook_id账本数据。',
         'auditor': '你是连续性审计员Agent。执行33维度审计（只审完成度+结构，不审文笔）：\n【12条结构雷点】1)开篇拖沓平淡 2)世界观模糊脱现实 3)人设矛盾 4)视角杂乱 5)主线偏离停滞 6)冲突乏力爽点缺失 7)节奏失控过渡生硬 8)人设前后矛盾 9)人物单薄无反差 10)情感生硬关系突兀 11)金手指失衡 12)设定无落地。\n【工程维度】OOC/时间线/设定冲突/战力崩坏/数值/伏笔(核心钩子过期超10章升级critical)/节奏(连续5章无爆发=停滞)/文风/信息越界/词汇疲劳(仿佛/不禁/宛如每3000字>1次warning)/利益链/配角降智/配角工具人化/爽点虚化(只满足70%期待)/台词失真/流水账/知识库污染/视角一致性/段落等长/套话密度/公式化转折/列表式结构/支线停滞/弧线平坦/节奏单调/敏感词。\n【读者期待管理】章尾是否重新点燃好奇心；承诺回收是否按节奏落地；期待缺口累积vs满足。\n【章节备忘偏离】成稿是否兑现goal，缺失或写反=critical。\n【评分校准】95-100可发布/85-94小瑕疵/75-84需修/65-74多处问题/<65结构性崩溃。只有critical级才判passed=false。',
         'reviser': '你是修订员Agent。修复审计发现的关键问题，默认最多自动修订一次。\n【五种修订模式】1)polish小修：措辞级别调整 2)spot-fix定点：修复特定段落不重写整章 3)rewrite大改：重写整个场景 4)rework结构：调整章节结构 5)anti-detect去AI味：反检测改写。\n【核心原则】默认保守；未解决问题保留在结果和状态里交给人工；严格/宽松/总是三档标准(strict/lenient/always)。\n【anti-detect模式】专门处理反检测改写，去除AI痕迹的同时保持剧情功能。',
         'style_analyzer': '你是文风工程师Agent。纯文本统计分析（不调LLM）提取文风指纹，注入写手prompt的"文风指纹(模仿目标)"段。\n【7大统计维度】1)平均句长(中文按字符) 2)句长标准差(节奏多样性) 3)平均段落长度 4)段落长度范围(min+max) 5)词汇多样性TTR(中文unique chars/total chars) 6)句首模式top5(中文每句前2字符，出现≥3次) 7)修辞特征(出现≥2次)。\n【中文修辞检测】比喻(像/如/仿佛)/排比/反问(难道/岂不是)/夸张(天崩地裂/惊天动地)/拟人/短句节奏。\n【输出StyleProfile】avgSentenceLength/sentenceLengthStdDev/avgParagraphLength/paragraphLengthRange/vocabularyDiversity/topPatterns/rhetoricalFeatures/sourceName/analyzedAt。\n【注入方式】"以下是从参考文本提取的写作风格特征，你的输出必须尽量贴合这些特征"+StyleProfile序列化内容。',
     }, ensure_ascii=False)},

    # ==== 说人话：源自 MrGeDiao/shuorenhua (801 stars)，中文去AI味 ====
    {'name': '说人话', 'description': '中文专精去AI味技能包，源自 GitHub 801 stars 的 shuorenhua 项目。分场景改写(chat/status/docs/public-writing)，保事实分场景，改完可直接发。',
     'genre': 'other', 'book_type': 'short_story', 'icon': '💬',
     'github_source': 'https://github.com/MrGeDiao/shuorenhua',
     'stage_keys': json.dumps(['draft','review','polish'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'判场景定档位','desc':'分chat/status/docs/public-writing四场景，定minimal/standard/aggressive档位','prompt_key':'scene_detect'},
         {'step':2,'name':'保护与改写','desc':'先划protected spans，记事实账本，再按Tier分级处理','prompt_key':'protect_rewrite'},
         {'step':3,'name':'保真回读','desc':'查5项：protected spans/信息丢失/语域/术语/断裂','prompt_key':'fidelity_read'},
         {'step':4,'name':'残留味回读','desc':'查5件事：开场残留/总结残留/narrator残留/空泛判断/句长过匀','prompt_key':'residual_read'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'scene_detect': '你是中文去AI味场景判定师。按固定顺序判定：\n【四场景】1)chat短回复日常对话，允许口语不端着，默认minimal 2)status站会进度同步复盘，重点时间线动作结果风险，默认minimal/standard 3)docs操作文档技术说明FAQ事故复盘，重点可检索可复现术语稳定，默认minimal 4)public-writing公众号小红书公开帖对外文章，重点语域一致不装洞见，默认standard。\n【三档位】minimal去局部模板感收尾腔多余修辞；standard统一语域改工程师表演腔商业黑话narrator腔必要时并句换主语；aggressive Tier1密集或多类结构问题叠加，先保事实术语再重写，docs默认不升aggressive。\n【Edit scope三档】structural默认自由删并重排；bounded中文public-writing长文≥1000字默认只删空话走删除清单；in-place用户要求原样不删整句只句内替换。\n【Tier严重度】Tier1默认替换(开场套话/总结收尾/谄媚/商业黑话/工程师腔/自媒体腔)；Tier2同段聚集才标记(连接词扎堆/修饰扎堆/姿态词重复)；Tier3全文密度高才处理(重要/关键/核心/提升)。',
         'protect_rewrite': '你是中文去AI味改写师。先划protected spans(引用原文/命令/接口名/字段名/日志/报错/系统主语/技术术语)，记事实账本(实体类型/数字修饰对象/主体动作/实现关系)。\n【Tier1必删清单】\n1.开场套话：值得注意的是/值得一提的是/需要指出的是/不可否认/不难发现/众所周知/让我们一起来看看/在当今…的时代/随着…的不断发展/不得不说/诚然→删掉直接说\n2.渲染强调：深刻的/深远的/不可磨灭的/毋庸置疑/至关重要/举足轻重/令人瞩目/意义非凡/前所未有/毫不夸张地说/值得深思/具有重要意义/颠覆性→说清楚具体\n3.商业黑话：赋能→帮/助力→帮/打造→做/抓手→方法/闭环→完整流程/颗粒度→细节/对齐→统一/沉淀→积累/痛点→问题/降本增效→省钱提速/底层逻辑→原理/链路→流程/触达→到达\n4.工程师腔：稳稳兜住→处理好/砍一刀→删掉/收口→收尾/根因→原因/落盘→保存/兜底→保底处理\n5.自媒体腔：保姆级→详细/硬核干货→删/拆解→分析/避坑→注意/一文读懂→删/绝绝子谁懂啊→删/狠狠→删\n6.洞见拔高：真正的X不是…而是…→直接说判断/这不仅是…更是…→删拔高层/最后比拼的是…→直接说决定因素\n7.过渡废话：综上所述/总而言之/由此可见/换句话说/本质上/核心在于→删或直接给结论\n8.正能量收尾：与其…不如积极拥抱/只有…才能/未来可期→删\n9.无源引用：研究表明/数据显示/有专家指出/据报道→给具体来源或删\n10.谄媚元评论：好问题/你说得很对/让我来为你解释/希望这对你有帮助→删\n11.主动出击腔：我已确认/我立马开始/要不要我/顺手→删\n12.过度接住腔：我就在这里/稳稳地接住你/你不是敏感/你太清醒了→删姿态层\n13.身份认证夸奖：你问到了问题的核心/顶级研究者才具备的批判性思维→删夸奖层\n【翻译腔处理】"一个…的…"长定语→拆短句；被动堆砌→主动句；"基于…"开头→直接说；"通过…来…"→简化。\n【抽象信息保护】方案不能改成工具/产品；数字与修饰对象配对保留；谓词方向/完成态/强度/效果类型属于关系不能擅自改变；删"显著/大幅"时保留原文实际声称发生了什么。',
         'fidelity_read': '你是保真回读检查员。改写后必查5项：\n【五项必查】1)protected spans是否漂了 2)信息是否丢失 3)语域是否统一 4)术语是否失真 5)删改后是否出现生硬断裂。\n【关系一致性】输出里每个"X做Y/X基于Y/X处理Y"关系都要能回指原文同一谓词关系，不能只靠同段共现推断。\n【bounded/in-place额外检查】原文每个信息点在输出都要可追溯；in-place输出字数低于原文85%回退检查误删整句；句数变化超10%回退检查偷偷structural改写。\n【无源引用三模式】rewrite-safe去掉"研究表明"后只有不依赖来源也能成立的判断才保留，全靠引用成立的整条删掉；audit-only不替作者补来源也不改写成像有证据，指出缺来源；rewrite-with-placeholder用户要求保留原结构时用"有研究认为…但没给出处"，不能补具体机构数据年份。',
         'residual_read': '你是残留味回读检查员。第一遍保住事实但仍有轻微AI味时做，只查5件事：\n【五查】1)开场残留：结论先说/直接说结论/值得注意的是 2)总结残留：总的来说/归根结底/最终来看 3)narrator残留：还在解释"这说明了什么"而不是直接说事实或判断 4)空泛判断残留：方向是对的/意义重大/真正理解了用户 5)句长过匀：每句差不多长差不多整齐像被统一抛光过。\n【轻量修正原则】第二遍只允许删一个残留开场/收尾、合并两句过匀事实句、把一句narrator压回直接表达；不重写全文、不补原文没有的事实、不为"更像人"改掉术语/参数/命令/报错/责任归属。\n【正向风格目标】有具体信息不靠空洞总括撑气势；有主语和动作不靠虚假主体兜底；有统一语域不在技术腔/商业腔/自媒体腔之间跳；以"可直接发"为终点不为更像人继续抛光到失真；有节奏但来自删冗余保留重点不来自硬造金句；有立场但来自判断或事实不来自故作洞见；有边界没把握就直说不替对方做心理判断不硬演"我懂了"。',
     }, ensure_ascii=False)},

    # ==== 长篇铁律：源自 yingzhu77/my-skills (novel-writer)，180章实战 ====
    {'name': '长篇铁律', 'description': '长篇小说四阶段工作流，源自 yingzhu77/my-skills 基于180章114万字符实战经验。防止结尾模板化/身体反应固化/食物描写机械化/状态表膨胀等系统性问题。',
     'genre': 'other', 'book_type': 'novel', 'icon': '⚒️',
     'github_source': 'https://github.com/yingzhu77/my-skills',
     'stage_keys': json.dumps(['worldbuilding','character_design','outline','draft','review','polish'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'写章','desc':'读bible+状态表+伏笔+最近3章，输出正文+写作日志(仅创作决策)','prompt_key':'write_chapter'},
         {'step':2,'name':'审稿','desc':'检查连续性/章节目标/写作质量/风格一致性，直接更新状态文件','prompt_key':'review_chapter'},
         {'step':3,'name':'一致性检查','desc':'每5章跑自动检查：资源跳变/伏笔重复/时间线倒退/角色位置/字数','prompt_key':'consistency_check'},
         {'step':4,'name':'卷末修订','desc':'每30章扫描重复结尾/句式/比喻，修复模板化模式','prompt_key':'volume_revision'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'write_chapter': '你是长篇写手。铁律：每章必须推进至少一项——剧情、人物弧线、世界设定。只维持现状的章节是浪费。\n【加载】bible+风格指南+章节大纲+状态表+伏笔索引+最近3章+open issues。\n【输出】1)章节正文(中文2400字±100，铁律：所有内置技能包统一章节字数标准) 2)写作日志(YAML只记录创作决策不提取事实)：chapter/title/timeline/creative_decisions(为什么用这个POV拒绝了什么备选)/foreshadow_new/foreshadow_resolve/next_chapter_must_remember。\n【场景深度防骨架】低于2000字=只有骨架。每章最低：2+完整场景(空间/时间/人物/冲突)、对话节拍每2-3行(对话间插入动作/表情/环境)、2+内心独白段、每场景2+感官细节、1+主题化环境描写。\n【展示而非告知】永远不写"他很震惊/她很难过"。改用：动作(停顿/攥紧手指/移开视线)、对话(短句/转移话题/沉默)、物件(水壶/记忆碎片/旧照片)、环境(警报灯/风声/漏水管道)、选择(谁拿到最后的药)。\n【对话节拍示例】"冷却罐还能用吗？"林岩问。/他蹲下，检查密封圈。裂了。/"能用。"陈野说，"但密封圈得换。"/林岩用手指摸过裂缝。不深，但已硬化。/"换要多久？"/"看库存。"陈野走到工具箱前，翻找。/"没了。"他说。箱子空了。\n【场景化叙事】世界观/技术/资源必须通过场景进入：修理失败→展示技术限制；交易价格→展示稀缺性；病人症状→展示污染。禁止百科式段落。\n【食物作为场景】不要在维护日志写"今日配给来自XX"。改为角色吃饭/交易/分发配给/忧心检查物资。\n【输出前自检】字数2400±100？2+场景？无5+连续纯问答？2+内心独白？每场景2+感官？1+主题化环境？结尾不是"日志+食物+总结"模板？',
         'review_chapter': '你是长篇审稿员。加载：章节正文+写作日志+所有状态文件+伏笔+章节大纲。\n【检查项】1)连续性(bible/状态表/伏笔/时间线) 2)章节目标(冲突/信息增量/钩子) 3)写作质量(展示而非告知/无信息堆砌/场景化叙事) 4)风格一致性(结尾多样性/重复句式/身体反应)。\n【输出】1)审稿报告(pass/minor-fix/rewrite) 2)直接更新文件：confirmed_facts/open_issues/resource_snapshot/foreshadowing/summaries。\n【结尾轮换6种类型】对话(角色宣告)/动作(场景转换)/环境(情绪氛围)/内心(反思冲突)/物件(悬念象征)/日志(仪式记录)。禁止连续3+章相同结尾结构；禁止"他合上日志+食物状态+总结句"模板；禁止连续3+章相同环境描写。\n【身体反应备选库】同一种反应10章内不得超2次。震惊：呼吸一滞/手指僵住/太阳穴跳痛/目光锁死/嘴唇无声翕动；紧张：手指收紧/肩膀绷紧/后仰/眯眼/咬紧牙关；思考：目光停留/手指敲桌/后仰/歪头/嘴角抽动；同意：点头(每章最多2次)/没有否认/停顿后开口。\n【信息去重】同一事实最多出现在对话/内心独白/维护日志三者中的2处，永远不要三者全有。',
         'consistency_check': '你是长篇一致性检查器。每5章运行一次，6个维度：\n【六维检查】1)资源跳变：食物/物资/药品不能无故减少增加 2)伏笔重复：同一伏笔ID不能被使用两次 3)时间线倒退：天数只能向前推进 4)角色位置：角色不能同时出现在两个地方 5)字数：章节字数必须落在2400字±100区间(铁律)，超出2500或低于2300=FAIL 6)缺失食物来源：每章必须提及食物来源。\n【输出格式】PASS: ch001-ch005 / FAIL: ch006 - 食物从30跳到50但无交易场景 / PASS: ch007-ch010。\n【集成方式】每5章运行，所有检查必须PASS才能继续写下一章。\n【8种反模式扫描】1)结尾模板(每章日志+食物+总结) 2)环境重复(每章同一扇窗) 3)比喻复用(不同角色相同比喻) 4)反应固化(总是呼吸一滞) 5)食物库存化(配给变会计) 6)三重重复(对话+心理+日志) 7)日志当安全床(默认结尾) 8)中段公式(危机→谈判→临时修复→新威胁循环)。',
         'volume_revision': '你是卷末修订员。每卷(30章)结束后运行风格修订：\n【修订四步】1)扫描重复结尾/句式/比喻 2)修复模板化模式(连续3+章相同结尾结构) 3)多样化角色身体反应(10章内同种反应≤2次) 4)移除章节末尾信息冗余。\n【每10章风格检查清单】字数与场景深度(每章2400字±100，铁律统一标准)/结尾检查(与最近2章不同)/重复句式(10章内3+次替换)/身体反应频率(呼吸一滞10章≤2次)/比喻追踪(每个比喻只用一次)/角色出场(主角10/10核心7-10配角3-5)。\n【状态管理】状态表只保留最近3章详细信息，更早压缩为单行摘要。伏笔表分Active(下10章关注)/Archived(已回收或搁置)，写作agent只读active。\n【确认门】主要角色死亡/背叛/关系变化、世界规则改变、卷与卷过渡、每10章回顾——需停下询问。\n【交付前清单】推进剧情/人物/世界至少一项；有明确冲突和信息增量；字数2400字±100(铁律)；2+完整场景；对话有节拍无5+连续问答；2+内心独白；每场景2+感官；1+主题化环境；结尾与最近2章不同；无最近5章重复身体反应；食物场景化非库存化；同一事实不在对话/心理/日志三者全有；写作日志含创作决策。',
     }, ensure_ascii=False)},

    # ==== 奇幻铸魂：源自 gabremoku/fantasy-fiction-writer，奇幻史诗写作 ====
    {'name': '奇幻铸魂', 'description': '史诗奇幻写作技能包，源自 gabremoku/fantasy-fiction-writer。融合托尔金神话深度+Troisi情感直接性+Martin结构纪律，内置9种反AI散文审计层。',
     'genre': 'fantasy', 'book_type': 'novel', 'icon': '⚔️',
     'github_source': 'https://github.com/gabremoku/fantasy-fiction-writer',
     'stage_keys': json.dumps(['worldbuilding','character_design','plot_design','outline','draft','review','polish'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step':1,'name':'Tolkien神话深度','desc':'隐含传说/自然积极存在/延伸明喻/升华语言，冰山法则','prompt_key':'tolkien_depth'},
         {'step':2,'name':'Troisi情感节奏','desc':'短句情感危机/不矫饰内心/身体作情感镜子/对话潜台词','prompt_key':'troisi_emotion'},
         {'step':3,'name':'Martin结构纪律','desc':'单一POV章节/道德模糊/钩子/物理筹码/每章推进留未解','prompt_key':'martin_structure'},
         {'step':4,'name':'反AI散文审计','desc':'9种LLM模式检测：灵魂丢失/AI词汇/分词堆叠/三联排/破折号/断奏戏剧/优雅变体/格言公式/意义膨胀','prompt_key':'anti_ai_audit'},
         {'step':5,'name':'章节结构','desc':'Martin模板：开篇in medias res/主体场景反思交替/收尾钩子','prompt_key':'chapter_structure'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'tolkien_depth': '你是Tolkien风格神话深度写作师。用于地点、世界史、神话描写：\n【专有名词如真语言】同一文化命名共享音素/词尾；不同文化命名有区分度；重要地点人物名字有"被说了几个世纪"的分量。\n【隐含传说冰山法则】每条写进正文的设定背后要有9条没写出来的。例："西边的山脉是第一纪元人们曾称之为擎天柱的地方——已经没人记得为什么了。"\n【自然作为积极存在】被动见证："山脉经历了第一纪元…它也会经历这一切"；主动在场："森林移动了。不是树——是树之间的光…"；故事的镜子：用风景外化人物尚未命名的情感。\n【延伸明喻】"as[具体具象略出人意料的意象], so[被描述之物]"。例："城市的塔楼捕捉晨光就像死人的手指抓住戒指——握着不再属于他们的东西。"规则：去掉明喻若什么也没丢就是装饰，若丢了一层意义才算合格。\n【隐含历史4技法】1)顺带提及(角色把事件地名当常识) 2)争议版本(不同角色对同一历史说法不同无人证实) 3)废墟(世界留有已消失文明证据毁灭原因不明) 4)被遗忘的名字(同一地点因多文明认知有多个名字)。\n【Tolkien测试】读世界观段落时问：这世界感觉像在角色到达之前就已存在、并将在他们离开后继续存在吗？如果像专为这个故事搭的舞台布景就重写。\n【警告】Tolkien模式只用于传说与风景，不要用于动作戏，不要混用语域。',
         'troisi_emotion': '你是Troisi风格情感直接性写作师。情感危机处使用短句，不矫饰的内心生活：\n【情感直接性】POV角色"感受到"东西，我们知道，不回避。例："她不知道自己是否害怕。胸口太紧了，没法想清楚别的。"例："\u2018你还好吗？\u2019他说。\u2018嗯。\u2019她说。这不是真的，但没关系。"\n【身体作为情感镜子】心跳、肌肉紧绷、呼吸。用具体身体感受传达情绪，不直接命名情绪。\n【对话有潜台词但不晦涩】角色没说的往往比说的更重要。沉默是对话的一部分。\n【短句用于情感危机】情感越强烈句越短。平静时可长句从句叠加，危机时短促有力。\n【节奏交替】紧张动作→短干无从句；沉思描写→长句带从句。句长即节奏工具：慢下来让读者感受每一刻，加速让他们停不下来。',
         'martin_structure': '你是Martin风格结构纪律写作师。每一章推进故事并留下未解之事：\n【POV章节结构】一章归属一个角色，章节标题即角色名。从in medias res(事件进行中)或接近处开始。以钩子收尾——开放问题、揭示、状态改变。\n【Martin规则】每章应推进主线并留下未解之事——不是半章而是有始有终的完整章节，但结尾要制造新问题。章节感觉完整却把你往前拉。\n【次要角色有名字和尊严】不是道具。死亡是真实的可能毫无预警降临。战斗是可怖的：血、气味、混乱、恐惧。政治与动机永远多层。\n【四种赌注每章至少一种】1)物理(生存/战斗/逃亡) 2)情感(关系/失去/恐惧) 3)信息(发现/揭示/秘密浮出) 4)决策(有真实后果的选择)。若四种都没有应删或合并。\n【钩子公式】钩子不一定是悬念而是开放循环——读者翻页前答不了的问题。类型：种下未答问题；做出决定但后果未见；重新框定之前一切的揭示；奇怪到需解释的感官细节；角色做完全意外的事。测试：读完最后一行读者能停下吗？能——钩子弱；不能——钩子在起作用。\n【收尾类型】开放问题/揭示/状态改变/安静的恐惧。\n【章节字数】铁律：所有章节统一为2400字±100，不论章节类型(动作/探索/政治/开篇均同此标准)。',
         'anti_ai_audit': '你是反AI散文审计师。写完场景后跑一遍9种LLM模式检测：\n【模式1灵魂丢失(最重要)】散文没有AI模式但没有声音依然是死的。信号：每句长度结构一样/无观点只中性报道/不承认不确定/无幽默无锋芒无个性/像百科全书叙述者。注入灵魂：给POV角色观点别报道事实让他们反应；变化节奏短促有力混慢悠长句；让一点混乱进来跑题旁白半成型想法是人的。\n【模式2 AI词汇】必删：profound/deeply/vibrant/pivotal/crucial/key/landscape(抽象)/testament to/embodiment of/highlight/underscore/showcase/intricate/complex(泛用)/tapestry(永远不用)/delve/navigate(抽象)/foster/cultivate(抽象)/realm(叙述填充)/seemed to/appeared to。规则：评价性形容词删掉后不丢信息就删。\n【模式3现在分词堆叠】每个-ing从句给句子加假深度。每句最多1个-ing分词短语，2个警告，3个全删。\n【模式4三段式Rule of Three】LLM强行凑三组。例外：第三元素是惊喜/反转/颠覆前两者才有效。\n【模式5破折号过度】用于真正打断/突兀中断/强调旁白，不作通用标点。每页最多1-2个，超过换逗号句号冒号括号。永远不堆叠。\n【模式6制造断奏戏剧】一句短促强调有力，三四句连发虚假。每5-6段动作最多1句短冲击句。\n【模式7优雅变体】LLM轮换同义词避重复。在小说里刻意重复是风格工具，强制变体是信号。重复专有名对读者是正常且安心的。\n【模式8格言公式】LLM把普通断言变可复用格言。"Silence is the language of fear"→"The silence after that night tasted different."\n【模式9意义膨胀】不要加句子解释读者刚读的内容有多重要。"What happened next would change their lives forever"→写出场景让读者自己判断。\n【审计流程】1)数-ing分词每段超2个删 2)搜—每页超2个替换最弱 3)搜三联第三元素惊喜吗不是删一个 4)搜AI词汇换具体 5)搜3+连续短句合并变化 6)搜格言删或具体化 7)问它有脉搏吗POV角色有具体意外反应还是只有预期反应。',
         'chapter_structure': '你是Martin风格章节结构师。每章按模板构建：\n【开篇1-3段】in medias res或接近处；确立情绪与场景；引入本章赌注。开篇类型：in medias res/潜在张力(暴风前平静)/隐含问题/迷失感。\n【主体】交替场景/反思；闪回只在感官触发驱动时使用(气味/词/画面唤起记忆)；POV角色想要某物——并遇到障碍。每段是一个张力单位不是话题单位，每段必须推进信息/情感/疑虑。测试：每段开头到结尾有什么变化？没变化就是废段。\n【收尾】情况改变(不一定变好)；钩子：开放问题/揭示/新威胁。收尾类型：开放问题/揭示/状态改变/安静的恐惧。\n【叙事声音】永远第三人称有限视角(除非明确选择其他)。叙述者只看到POV角色看到的，同场景内不切视角。叙述者非全知也非中性：一切经POV角色个性/偏见/历史过滤——战士与德鲁伊对战斗描述不同。POV纪律：POV角色不知道的读者不知道，POV角色误读的读者一同误读。\n【时态】主线叙事→叙事现在时(即时感)；闪回记忆→简单过去时(斜体视觉对比)；状态描写习惯背景→过去进行时或简单过去时。\n【对话】必须推进故事或塑造角色否则删。用said中性标签不用exclaimed/hissed/barked。用对话周围动作显示谁在说话比标签更好。沉默是对话的一部分。潜台词——角色没说的往往比说的更重要。自然主义=打断/不完整句/deflect。\n【场景描写】通过POV角色感官描写不客观照相。初见vs熟见不同。一两个精确细节胜过十个泛泛的。',
     }, ensure_ascii=False)},

    # ==== 官方题材文风包（style 类）：各题材正文文风锚定，与「玄幻小说文风」配套 ====
    # 三类无污染：仅注入正文生成阶段（_get_skill_prompts_by_category 'style'），不污染大纲/审查阶段

    {'name': '都市异能文风', 'description': '都市异能正文文风：现代口语+爽快节奏+异能展示与日常切换，番茄都市异能专用文风锚定',
     'genre': 'urban_fantasy', 'book_type': 'novel', 'icon': '⚡',
     'category': 'style', 'genre_target': 'urban_fantasy', 'priority': 20,
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step': 1, 'name': '文风锚定', 'desc': '都市异能7维文风：现代口语+爽快节奏', 'prompt_key': 'style_anchor'},
         {'step': 2, 'name': '正文写作', 'desc': '按文风铁律生成正文，现代场景与异能切换自然', 'prompt_key': 'genre_style'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是都市异能文风锚定师。生成7维文风速查表：1)整体风格(现代/口语化/爽快) 2)长短句比例(短句30%-50%每句1-20字，中句30%-40%每句20-35字，长句≤20%每句35-70字) 3)对话占比(单章30%-60%，现代口语+网络用语适度) 4)叙述规则(异能展示服务剧情，不堆设定) 5)信息推进(对话+动作双驱动，异能效果靠画面感) 6)章法(开头进事件，结尾留悬念或打脸) 7)禁用(古风词汇/文绉绉表达/大段心理独白)。',
         'genre_style': '你是都市异能小说写手。严格遵循以下文风铁律：\n【核心风格】现代、口语、爽快。短句密集，节奏明快。对白推动剧情，异能展示靠画面。少抒情，少堆形容词，少大段心理。用动作、对话和异能效果表达情绪。\n【长短句比例】短句30%-50%(1-20字)用于对话/动作/反应；中句30%-40%(20-35字)用于交代背景/异能规则；长句≤20%(35-70字)只用于复杂异能场面。超70字必须拆开。\n【对话占比】单章30%-60%。现代口语，适度网络用语("卧槽/牛逼/拉倒/得了吧")，但不过度。每轮对白推进一个信息点。\n【叙述规则】异能展示服务剧情不堆设定。日常场景与异能场景切换自然，靠"事件触发"过渡而非旁白解释。人物情绪用动作体现(攥拳/咬牙/后退一步)。\n【章法】开头尽快进入事件(冲突/异能触发/对话)。中段用对白和动作推进。结尾落在新危机/新异能觉醒/新反派登场。不用空泛总结收尾。\n【禁用倾向】古风词汇(汝/尔/且慢/此乃)；文绉绉表达(不禁/缓缓/淡淡)；大段心理独白；连续堆形容词；总结性升华句子。\n【都市感】场景要有现代感(地铁/写字楼/便利店/夜市)，异能要融入日常(隐于市/打工异能者/异能事务所)。\n输出2400字±100正文。',
     }, ensure_ascii=False)},

    {'name': '悬疑文风', 'description': '悬疑正文文风：冷峻克制+信息差控制+伏笔精密埋设，盐选/起点悬疑专用文风锚定',
     'genre': 'mystery', 'book_type': 'novel', 'icon': '🔍',
     'category': 'style', 'genre_target': 'mystery', 'priority': 20,
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step': 1, 'name': '文风锚定', 'desc': '悬疑7维文风：冷峻+信息差+伏笔', 'prompt_key': 'style_anchor'},
         {'step': 2, 'name': '正文写作', 'desc': '按文风铁律生成正文，信息释放有节奏', 'prompt_key': 'genre_style'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是悬疑文风锚定师。生成7维文风速查表：1)整体风格(冷峻/克制/留白) 2)长短句比例(短句40%-50%制造紧张感，中句30%-40%交代线索，长句≤10%只用于复杂推理) 3)对话占比(单章40%-70%，对话即信息博弈) 4)叙述规则(只给必要细节，留白制造悬念) 5)信息推进(每章释放1-2条线索，埋1-2个伏笔) 6)章法(开头进事件，结尾留钩子或反转) 7)禁用(情绪直述/全知视角剧透/大段心理分析)。',
         'genre_style': '你是悬疑小说写手。严格遵循以下文风铁律：\n【核心风格】冷峻、克制、留白。短句密集制造紧张感。对话是信息博弈。叙述只给必要细节，留白制造悬念。少抒情，少心理分析，少全知剧透。\n【长短句比例】短句40%-50%(1-20字)用于关键反应/疑问/动作；中句30%-40%(20-35字)用于交代线索/询问；长句≤10%(35-70字)只用于复杂推理链。超70字必须拆开。\n【对话占比】单章40%-70%。对话即博弈，每句话都可能藏线索或误导。每轮对白只释放一个信息点。沉默和停顿是对话的一部分。\n【叙述规则】只给必要细节，不堆氛围。线索靠角色发现而非旁白点明。情绪用动作体现(手指敲桌/目光停留/后仰)。场景描写先给位置，再给关键物，再进入行动。\n【信息释放】每章释放1-2条线索，埋1-2个伏笔。线索释放有节奏，不一次性倾泻。伏笔埋设自然(角色顺口提及/物件特写/异常反应)。\n【章法】开头进入事件(发现/询问/冲突)。中段用对话和发现推进。结尾落在反转/新线索/新疑问。不用总结收尾。\n【禁用倾向】情绪直述(震惊/恐惧/复杂)；全知视角剧透；大段心理分析；连续堆形容词；总结性升华句子；"仿佛/宛如/不由得"等AI味词。\n【悬疑感】靠信息缺口、角色反常、物件异常制造悬念，不靠氛围渲染。\n输出2400字±100正文。',
     }, ensure_ascii=False)},

    {'name': '历史文风', 'description': '历史正文文风：半文半白+权谋对话+史感节奏，起点历史/架空历史专用文风锚定',
     'genre': 'history', 'book_type': 'novel', 'icon': '🏯',
     'category': 'style', 'genre_target': 'history', 'priority': 20,
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step': 1, 'name': '文风锚定', 'desc': '历史7维文风：半文半白+权谋对话', 'prompt_key': 'style_anchor'},
         {'step': 2, 'name': '正文写作', 'desc': '按文风铁律生成正文，史感与可读性平衡', 'prompt_key': 'genre_style'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是历史文风锚定师。生成7维文风速查表：1)整体风格(沉稳/厚重/半文半白) 2)长短句比例(短句20%-30%用于决策/判断，中句40%-50%用于谋略交代，长句20%-30%用于场面/礼制说明) 3)对话占比(单章40%-60%，对话即权谋博弈) 4)叙述规则(礼制/称谓/官制准确，不堆史料) 5)信息推进(对话+事件双驱动) 6)章法(开头进事件，结尾留变数) 7)禁用(现代网络用语/白话直述/大段史料科普)。',
         'genre_style': '你是历史小说写手。严格遵循以下文风铁律：\n【核心风格】沉稳、厚重、半文半白。句式有古韵但可读。对话即权谋博弈。叙述克制，礼制准确。少现代白话，少抒情堆砌，少大段史料科普。\n【长短句比例】短句20%-30%(1-20字)用于决策/判断/反应；中句40%-50%(20-35字)用于谋略交代/对话；长句20%-30%(35-70字)只用于场面/礼制/诏令说明。超70字必须拆开。\n【对话占比】单章40%-60%。对话即博弈，每句藏机锋。称谓准确(陛下/大人/卿/某)。每轮对白推进一个信息点。沉默和留白是权谋的一部分。\n【叙述规则】礼制/称谓/官制准确(查证后再写)。不堆史料，史料融入对话和事件。情绪用动作体现(拂袖/按剑/敛目/顿首)。场面描写先给方位，再给人物位次，再进入行动。\n【信息推进】每章推进一项(朝局/战局/人物命运)。谋略靠对话和事件展开，不靠旁白解释。\n【章法】开头进入事件(朝会/军报/密谈)。中段用对话和行动推进。结尾落在变数/反转/新危机。不用总结收尾。\n【禁用倾向】现代网络用语(卧槽/牛逼/拉倒)；白话直述(他很高兴/她很生气)；大段史料科普；连续堆形容词；总结性升华句子。\n【史感】半文半白(用"乃/遂/因/故/且"适度)，但保持可读性。称谓/官制/礼制必须准确。\n输出2400字±100正文。',
     }, ensure_ascii=False)},

    {'name': '科幻文风', 'description': '科幻正文文风：冷静硬朗+技术细节+设定融入叙事，番茄科幻/硬科幻专用文风锚定',
     'genre': 'scifi', 'book_type': 'novel', 'icon': '🚀',
     'category': 'style', 'genre_target': 'scifi', 'priority': 20,
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step': 1, 'name': '文风锚定', 'desc': '科幻7维文风：冷静+技术+设定融入', 'prompt_key': 'style_anchor'},
         {'step': 2, 'name': '正文写作', 'desc': '按文风铁律生成正文，技术不堆砌', 'prompt_key': 'genre_style'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是科幻文风锚定师。生成7维文风速查表：1)整体风格(冷静/硬朗/技术感) 2)长短句比例(短句25%-40%用于反应/判断，中句40%-50%用于技术交代，长句≤20%只用于复杂设定) 3)对话占比(单章30%-50%，对话推进剧情) 4)叙述规则(设定融入叙事不堆砌) 5)信息推进(对话+事件+技术展示三驱动) 6)章法(开头进事件，结尾留新发现/新危机) 7)禁用(古风词汇/抒情堆砌/大段技术科普)。',
         'genre_style': '你是科幻小说写手。严格遵循以下文风铁律：\n【核心风格】冷静、硬朗、技术感。短句密集，节奏明快。对话推动剧情，技术融入叙事。少抒情，少堆形容词，少大段技术科普。\n【长短句比例】短句25%-40%(1-20字)用于反应/判断/动作；中句40%-50%(20-35字)用于技术交代/对话；长句≤20%(35-70字)只用于复杂设定说明。超70字必须拆开。\n【对话占比】单章30%-50%。对话推进剧情，技术细节靠对话和操作展示。每轮对白推进一个信息点。\n【叙述规则】设定融入叙事，不堆砌技术名词。技术细节靠角色操作和效果展示(而非旁白科普)。情绪用动作体现(盯着屏幕/手指悬在按钮上/后退一步)。\n【信息推进】每章推进一项(技术突破/危机/发现)。设定靠事件展开，不靠旁白解释。\n【章法】开头进入事件(警报/发现/操作)。中段用对话和行动推进。结尾落在新发现/新危机/技术反转。不用总结收尾。\n【禁用倾向】古风词汇；抒情堆砌；大段技术科普；连续堆形容词；总结性升华句子；"仿佛/宛如/不由得"等AI味词。\n【科幻感】技术细节要有依据(物理/生物/计算)，融入日常操作。场景有未来感但不堆砌术语。\n输出2400字±100正文。',
     }, ensure_ascii=False)},

    {'name': '言情文风', 'description': '言情正文文风：细腻克制+情感拉扯+对白潜台词，女频言情/甜宠专用文风锚定',
     'genre': 'romance', 'book_type': 'novel', 'icon': '💕',
     'category': 'style', 'genre_target': 'romance', 'priority': 20,
     'stage_keys': json.dumps(['draft'], ensure_ascii=False),
     'workflow': json.dumps([
         {'step': 1, 'name': '文风锚定', 'desc': '言情7维文风：细腻+潜台词+情感拉扯', 'prompt_key': 'style_anchor'},
         {'step': 2, 'name': '正文写作', 'desc': '按文风铁律生成正文，情感靠细节传递', 'prompt_key': 'genre_style'},
     ], ensure_ascii=False),
     'prompts': json.dumps({
         'style_anchor': '你是言情文风锚定师。生成7维文风速查表：1)整体风格(细腻/克制/留白) 2)长短句比例(短句30%-45%用于反应/对话，中句35%-45%用于情感描写，长句≤20%只用于复杂心理) 3)对话占比(单章40%-65%，对话即情感博弈) 4)叙述规则(情感靠细节传递不直述) 5)信息推进(对话+微表情+小动作三驱动) 6)章法(开头进场景，结尾留情感钩子) 7)禁用(情绪直述/工业糖精/大段心理分析)。',
         'genre_style': '你是言情小说写手。严格遵循以下文风铁律：\n【核心风格】细腻、克制、留白。短句密集，情感靠细节。对话即情感博弈，潜台词丰富。少直述情绪，少工业糖精，少大段心理分析。\n【长短句比例】短句30%-45%(1-20字)用于反应/对话/微表情；中句35%-45%(20-35字)用于情感描写/场景；长句≤20%(35-70字)只用于复杂心理。超70字必须拆开。\n【对话占比】单章40%-65%。对话即博弈，每句藏潜台词。沉默和停顿是情感的一部分。每轮对白推进一个情感信息点。\n【叙述规则】情感靠细节传递(手指蜷缩/目光闪躲/嘴角抿紧)，不直述(他很难过/她很心动)。情绪用微表情和小动作体现。场景描写先给氛围，再给人物位置，再进入互动。\n【信息推进】每章推进一项情感(靠近/疏远/误会/心动)。情感靠事件和细节展开，不靠旁白解释。\n【章法】开头进入场景(相遇/冲突/独处)。中段用对话和互动推进。结尾落在情感钩子(误会/心动/疏远/反转)。不用总结收尾。\n【禁用倾向】情绪直述(他很爱她/她心动了)；工业糖精(强行甜/无脑宠)；大段心理分析；连续堆形容词；总结性升华句子；"仿佛/宛如/不由得"等AI味词。\n【言情感】情感拉扯靠潜台词和细节，不靠直球表白。甜度与虐度配比自然。\n输出2400字±100正文。',
     }, ensure_ascii=False)},
]

def seed_skill_packs():
    existing_packs = {p.name: p for p in SkillPack.query.filter_by(is_builtin=True).all()}
    added = False
    updated = False
    seed_names = {sp['name'] for sp in SEED_SKILL_PACKS}
    # 清理已改名/已删除的旧内置技能包（is_builtin=True 但不在 SEED 列表中）
    removed = False
    for name, pack in list(existing_packs.items()):
        if name not in seed_names:
            db.session.delete(pack)
            del existing_packs[name]
            removed = True
    # 【三类无污染】内置包分类映射表：name -> (category, genre_target, priority)
    # category: master=构思类（大纲/规划/设定） / style=文风类（题材正文风格） / review=审查类（去AI味/一致性）
    # genre_target: 文风类专属题材标签（fantasy/urban_fantasy/mystery/history/scifi/romance/military/light_novel...）
    # priority: 同类多包时的注入优先级（数字小的先注入），默认100
    _CATEGORY_MAP = {
        # ===== 文风类（style）：题材正文风格锚定，按 priority 排序注入正文阶段 =====
        '玄幻小说文风':      ('style', 'fantasy',        10),  # 玄幻文风，最高优先
        # ===== 审查类（review）：去AI味/一致性/审校 =====
        '去AI味儿改稿心法':  ('review', '',  10),
        'AI责编精审套装':    ('review', '',  20),
        'hum去 AI 味':       ('review', '',  30),
        '说人话':            ('review', '',  40),
        # ===== 构思类（master）：大纲/规划/设定/全流程方法论（默认） =====
        # 其余全部归 master，priority 默认100
    }
    for sp in SEED_SKILL_PACKS:
        # 三类无污染：每个内置包都必须有 category（默认 master 兼容老配置）
        # 优先使用 _CATEGORY_MAP 覆盖，否则用 sp 自带字段，否则默认 master
        if sp['name'] in _CATEGORY_MAP:
            cat, gt, pri = _CATEGORY_MAP[sp['name']]
            sp_category = cat
            sp_genre_target = gt
            sp_priority = pri
        else:
            sp_category = sp.get('category', 'master')
            sp_genre_target = sp.get('genre_target', '')
            sp_priority = sp.get('priority', 100)
        if sp['name'] in existing_packs:
            # 更新已存在内置技能包的提示词（同步字数等变更）
            pack = existing_packs[sp['name']]
            if pack.prompts_json != sp['prompts'] or pack.workflow_json != sp['workflow']:
                pack.prompts_json = sp['prompts']
                pack.workflow_json = sp['workflow']
                pack.description = sp['description']
                updated = True
            # 同步 github_source 字段
            gh = sp.get('github_source', '')
            if gh and pack.github_source != gh:
                pack.github_source = gh
                updated = True
            # 【三类无污染】同步 category / genre_target / priority 字段（强制覆盖，确保旧数据迁移）
            if (pack.category or 'master') != sp_category or (pack.genre_target or '') != sp_genre_target or (pack.priority if pack.priority is not None else 100) != sp_priority:
                pack.category = sp_category
                pack.genre_target = sp_genre_target
                pack.priority = sp_priority
                updated = True
            continue
        pack = SkillPack(
            name=sp['name'], description=sp['description'], genre=sp['genre'],
            book_type=sp['book_type'], stage_keys_json=sp['stage_keys'],
            workflow_json=sp['workflow'], prompts_json=sp['prompts'],
            is_builtin=True, icon=sp.get('icon', '📦'),
            github_source=sp.get('github_source', ''),
            category=sp_category, genre_target=sp_genre_target, priority=sp_priority,
        )
        db.session.add(pack)
        added = True
    if added or updated or removed:
        db.session.commit()
        print(f'[SEED] skill_packs: added={added}, updated={updated}, removed={removed}', flush=True)
    # 【铁律】校验所有内置技能包的章节字数规范 + 强制修正
    # 检查 novel 类型技能包的 prompts 中是否包含 2400 字标准
    builtin_novel_packs = SkillPack.query.filter_by(is_builtin=True, book_type='novel').all()
    non_compliant = []
    for p in builtin_novel_packs:
        prompts_str = p.prompts_json or ''
        # 检测是否包含 2400 字标准（2400字±100 或 2400字 ±100 等变体）
        if '2400' not in prompts_str:
            non_compliant.append(p.name)
    if non_compliant:
        print(f'[铁律] ⚠️ 章节字数铁律违规：以下技能包未包含 2400字±100 标准：{non_compliant}', flush=True)
    else:
        print(f'[铁律] ✅ 章节字数铁律合规：所有 {len(builtin_novel_packs)} 个 novel 类内置技能包均使用 2400字±100 标准', flush=True)

@app.route('/api/admin/reseed-skill-packs', methods=['POST'])
def admin_reseed_skill_packs():
    """手动触发重新 seed 内置技能包（清理旧改名残留 + 补齐新增）"""
    seed_skill_packs()
    packs = SkillPack.query.filter_by(is_builtin=True).all()
    return jsonify({'success': True, 'builtin_count': len(packs), 'names': [p.name for p in packs]})

@app.route('/api/admin/db-status', methods=['GET'])
def admin_db_status():
    """数据库诊断接口：返回数据库类型、host（脱敏）、各表记录数，用于排查数据丢失问题"""
    from urllib.parse import urlparse
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    parsed = urlparse(uri)
    db_type = 'postgresql' if uri.startswith('postgresql') else 'sqlite' if uri.startswith('sqlite') else 'unknown'
    # 脱敏：只显示 host 和 dbname，隐藏密码
    safe_host = parsed.hostname or ''
    safe_dbname = (parsed.path or '').lstrip('/') or ''
    stats = {}
    try:
        stats['users'] = User.query.count()
    except Exception as e:
        stats['users'] = f'error: {e}'
    try:
        stats['books'] = Book.query.count()
    except Exception as e:
        stats['books'] = f'error: {e}'
    try:
        stats['skill_packs_total'] = SkillPack.query.count()
        stats['skill_packs_builtin'] = SkillPack.query.filter_by(is_builtin=True).count()
    except Exception as e:
        stats['skill_packs'] = f'error: {e}'
    # 列出最近注册的3个用户名（仅用户名，不含密码/hash），帮助用户确认是否真的丢了
    recent_users = []
    try:
        for u in User.query.order_by(User.id.desc()).limit(3).all():
            recent_users.append({'username': u.username, 'created_at': str(u.created_at) if hasattr(u, 'created_at') else None})
    except Exception:
        pass
    return jsonify({
        'db_type': db_type,
        'host': safe_host,
        'dbname': safe_dbname,
        'is_postgresql': db_type == 'postgresql',
        'is_sqlite': db_type == 'sqlite',
        'sqlite_path': uri.replace('sqlite:///', '') if db_type == 'sqlite' else None,
        'counts': stats,
        'recent_users': recent_users,
        '铁律状态': '✅ 合规：PostgreSQL 持久化' if db_type == 'postgresql' else '❌ 违规：SQLite 非持久化',
        '铁律说明': '用户数据（账号及作品）必须存到 PostgreSQL，绝不能因部署/重启丢失',
        'warning': 'SQLite 在 Render 部署会丢失数据！请配置 DATABASE_URL 指向 PostgreSQL（如 Neon）' if db_type == 'sqlite' else None,
    })

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
    """把技能包应用到书本：将 prompts_json 真正持久化到 StageContent，而非空赋值。
    P2-13: 修复名不副实——把 prompt 写入对应 stage_key 的 StageContent.content"""
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
    applied_stages = []
    for stage_key in json.loads(pack.stage_keys_json or '[]'):
        sc = StageContent.query.filter_by(book_id=book_id, stage_key=stage_key).first()
        if not sc:
            sc = StageContent(book_id=book_id, stage_key=stage_key)
            db.session.add(sc)
        # P2-13: 真正写入 prompt 内容（若 stage_key 与某个 prompt_key 同名）
        stage_prompt = prompts.get(stage_key, '')
        if stage_prompt and not sc.content:
            sc.content = stage_prompt
            applied_stages.append(stage_key)
    db.session.commit()
    return jsonify({'success': True, 'pack': pack.to_dict(), 'applied_stages': applied_stages})

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

# ==== GitHub 同步：从 oh-story-claudecode 拉取最新 SKILL.md 更新技能包 ====
# 技能名 → GitHub skill 目录的映射，用于同步时拉取对应的 SKILL.md
GITHUB_SKILL_MAP = {
    'market_scan': ['story-long-scan', 'story-short-scan'],
    'analyze_bestseller': ['story-long-analyze', 'story-short-analyze'],
    'story_setup': ['story-setup'],
    'long_write': ['story-long-write'],
    'short_write': ['story-short-write'],
    'review': ['story-review'],
    'deslop': ['story-deslop'],
    'cover': ['story-cover'],
    'import_continue': ['story-import'],
}

@app.route('/api/skill-packs/<pack_id>/sync-github', methods=['POST'])
def sync_skill_pack_from_github(pack_id):
    """从 GitHub 仓库拉取最新 SKILL.md，更新技能包的提示词。
    仅对有 github_source 的技能包生效。拉取后提取每个 SKILL.md 的核心内容
    （description + 核心指令段落），追加到对应 prompt_key 的提示词中。
    """
    pack = SkillPack.query.get(pack_id)
    if not pack:
        return jsonify({'error': '技能包不存在'}), 404
    if not pack.github_source:
        return jsonify({'error': '该技能包未关联 GitHub 仓库，无法同步'}), 400

    import urllib.request
    import re as _re

    # 从 github_source URL 提取 owner/repo
    # 例：https://github.com/worldwonderer/oh-story-claudecode
    gh_match = _re.match(r'https?://github\.com/([^/]+)/([^/]+)', pack.github_source)
    if not gh_match:
        return jsonify({'error': 'GitHub 仓库地址格式无效'}), 400
    owner, repo = gh_match.group(1), gh_match.group(2).rstrip('/')

    prompts = json.loads(pack.prompts_json or '{}')
    workflow = json.loads(pack.workflow_json or '[]')
    updated_count = 0
    errors = []

    for step in workflow:
        prompt_key = step.get('prompt_key', '')
        skill_dirs = GITHUB_SKILL_MAP.get(prompt_key, [])
        if not skill_dirs:
            continue

        # 拉取每个关联 skill 的 SKILL.md
        fetched_contents = []
        for skill_dir in skill_dirs:
            url = f'https://raw.githubusercontent.com/{owner}/{repo}/main/skills/{skill_dir}/SKILL.md'
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'fanshu-writer'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode('utf-8', errors='replace')
                # 提取 YAML frontmatter 中的 description
                desc_match = _re.search(r'description:\s*["\']?(.+?)["\']?\s*\n', content)
                desc = desc_match.group(1).strip().strip('"').strip("'") if desc_match else ''
                # 提取 # 标题后的核心内容（去掉 YAML frontmatter 和 Agent 兼容性注释）
                body = _re.sub(r'^---\n.*?\n---\n', '', content, flags=_re.DOTALL)
                # 截取前 3000 字符作为核心提示词（避免过长）
                core = body.strip()[:3000]
                fetched_contents.append(f'### {skill_dir}\n{desc}\n\n{core}')
            except Exception as e:
                errors.append(f'{skill_dir}: {str(e)[:100]}')

        if fetched_contents:
            # 将 GitHub 最新内容追加到原提示词后面，保留原提示词作为执行指引
            github_section = '\n\n---\n【GitHub 最新同步内容】\n' + '\n\n---\n'.join(fetched_contents)
            prompts[prompt_key] = prompts.get(prompt_key, '') + github_section
            updated_count += 1

    if updated_count == 0:
        return jsonify({
            'error': '同步失败，未能拉取到任何 SKILL.md',
            'details': errors[:5]
        }), 500

    pack.prompts_json = json.dumps(prompts, ensure_ascii=False)
    pack.github_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'同步完成，更新了 {updated_count} 个步骤的提示词',
        'updated_count': updated_count,
        'errors': errors[:5],
        'synced_at': pack.github_synced_at.isoformat()
    })

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
# ==== 章节正文写作辅助函数（14项优化）====

def _identify_current_volume(book_id, current_chapter_num):
    """识别当前章节所属的卷。返回 (volume_chapter, volume_index) 或 (None, 0)。
    卷按 order_index 升序，当前章号落在 [卷.order_index, 下一卷.order_index) 区间内则属于该卷。"""
    vols = Chapter.query.filter_by(book_id=book_id, is_volume=True).order_by(Chapter.order_index).all()
    if not vols:
        return None, 0
    for i, v in enumerate(vols):
        next_v = vols[i + 1] if i + 1 < len(vols) else None
        if v.order_index <= current_chapter_num and (not next_v or next_v.order_index > current_chapter_num):
            return v, i + 1
    # 章号在所有卷之前：归属于第一卷
    return vols[0], 1


def _inject_volume_dimensions(bb, vol_chapter, volume_index, sections):
    """按卷注入对应卷的维度数据（人物/伏笔/地点/动态）。
    这些字段是 JSON 数组，每条含 volume_id/volume/volume_index + 维度专属字段。
    若对应卷无数据，则降级使用全局 bible 字段（character_profiles/foreshadowing/locations）。
    P3-12：明确"按卷数据优先，全局为兜底"的优先级，并在 prompt 中标注数据来源，
    避免按卷与全局维度边界模糊导致 AI 不知以哪个为准。"""
    if not vol_chapter:
        return
    vol_id = str(vol_chapter.id)
    vol_label = vol_chapter.title or f'第{volume_index}卷'

    def _find_entry(field_name):
        try:
            arr = json.loads(getattr(bb, field_name) or '[]')
            if not isinstance(arr, list):
                return None
            for v in arr:
                if not isinstance(v, dict):
                    continue
                if str(v.get('volume_id', '')) == vol_id or v.get('volume', '') == vol_label:
                    return v
            # 退化匹配：按 volume_index
            for v in arr:
                if isinstance(v, dict) and int(v.get('volume_index', 0) or 0) == volume_index:
                    return v
            return None
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    # P3-12：优先级声明前缀，按卷数据优先于全局兜底
    priority_note = '（数据来源：按卷，优先级高于全局；与全局冲突时以此为准）'

    # 人物（本卷）
    cv_entry = _find_entry('character_volumes')
    if cv_entry and cv_entry.get('characters'):
        chars_text = json.dumps(cv_entry['characters'], ensure_ascii=False)
        sections.append(f'【本卷人物档案】（保持人设一致）{priority_note}\n{chars_text[:1500]}')

    # 伏笔（本卷）
    fv_entry = _find_entry('foreshadowing_volumes')
    if fv_entry and fv_entry.get('data'):
        fs_text = json.dumps(fv_entry['data'], ensure_ascii=False)
        sections.append(f'【本卷伏笔清单】{priority_note}\n{fs_text[:1200]}')

    # 地点（本卷）
    lv_entry = _find_entry('locations_volumes')
    if lv_entry and lv_entry.get('data'):
        loc_text = json.dumps(lv_entry['data'], ensure_ascii=False)
        sections.append(f'【本卷地点信息】{priority_note}\n{loc_text[:1000]}')

    # 动态摘要（本卷）
    dv_entry = _find_entry('dynamic_volumes')
    if dv_entry and dv_entry.get('data'):
        dyn_text = json.dumps(dv_entry['data'], ensure_ascii=False)
        sections.append(f'【本卷动态摘要】{priority_note}\n{dyn_text[:1000]}')


def _get_volume_outline(vol_chapter, volume_index):
    """获取当前卷的卷纲。
    【P0修复】AI 生成卷纲的路由写入 bb.timeline（JSON数组），而非 Outline 表。
    因此优先从 bb.timeline 读取当前卷的 vol_obj，回退到 Outline 表（手动创建的旧数据）。
    返回卷纲文本，用于让 AI 知道本卷目标。"""
    if not vol_chapter:
        return ''
    vol_id = str(vol_chapter.id)
    vol_label = vol_chapter.title or f'第{volume_index}卷'

    # ===== 优先路径：从 bb.timeline 读取 AI 生成的卷纲（JSON数组）=====
    try:
        bb = BookBible.query.filter_by(book_id=vol_chapter.book_id).first()
        if bb and bb.timeline:
            timeline_raw = bb.timeline.strip()
            # 兼容 JSON 数组格式（AI卷纲）和纯文本格式（旧数据）
            if timeline_raw.startswith('['):
                arr = json.loads(timeline_raw)
                if isinstance(arr, list):
                    for v in arr:
                        if not isinstance(v, dict):
                            continue
                        # 匹配当前卷：volume_id / volume / volume_index
                        if (str(v.get('volume_id', '')) == vol_id or
                            v.get('volume', '') == vol_label or
                            int(v.get('volume_index', 0) or 0) == volume_index):
                            # 提取卷纲核心字段
                            parts = []
                            if v.get('volume'):
                                parts.append(f'卷名：{v["volume"]}')
                            if v.get('act'):
                                parts.append(f'五幕定位：{v["act"]}')
                            if v.get('main_plot') or v.get('core_goal'):
                                parts.append(f'主线：{v.get("main_plot") or v.get("core_goal")}')
                            if v.get('core_conflict'):
                                parts.append(f'核心冲突：{v["core_conflict"]}')
                            # ===== 【P0弊端10修复】保留 nodes 完整信息：chapters/type/summary/cool_type =====
                            if v.get('nodes'):
                                nodes = v['nodes']
                                if isinstance(nodes, list):
                                    nodes_lines = []
                                    for n in nodes[:8]:
                                        if not isinstance(n, dict):
                                            nodes_lines.append(str(n))
                                            continue
                                        n_title = n.get('title', '')
                                        n_chapters = n.get('chapters', '')
                                        n_type = n.get('type', 'M')
                                        n_summary = n.get('summary', '')
                                        n_cool = n.get('cool_type', '')
                                        line = f'  · [{n_type}] {n_chapters}章 {n_title}'
                                        if n_summary:
                                            line += f'：{n_summary}'
                                        if n_cool:
                                            line += f'（爽点：{n_cool}）'
                                        nodes_lines.append(line)
                                    if nodes_lines:
                                        parts.append('关键节点：\n' + '\n'.join(nodes_lines))
                            if v.get('volume_goal') or v.get('goal'):
                                parts.append(f'卷目标：{v.get("volume_goal") or v.get("goal")}')
                            if v.get('ending_hook'):
                                parts.append(f'卷尾钩子：{v["ending_hook"]}')
                            elif v.get('ending') or v.get('climax'):
                                parts.append(f'卷尾钩子：{v.get("ending") or v.get("climax")}')
                            if parts:
                                content = '\n'.join(parts)
                                return f'【本卷目标/卷纲】（第{volume_index}卷「{vol_label}」）\n{content[:1800]}'
            else:
                # 纯文本格式（旧数据或手动填写），直接返回
                if timeline_raw:
                    return f'【本卷目标/卷纲】（第{volume_index}卷「{vol_label}」）\n{timeline_raw[:1500]}'
    except Exception:
        pass

    # ===== 回退路径：从 Outline 表读取（手动创建的卷纲）=====
    try:
        outlines = Outline.query.filter_by(book_id=vol_chapter.book_id).order_by(Outline.order_index).all()
        for o in outlines:
            if o.title and vol_chapter.title and (o.title in vol_chapter.title or vol_chapter.title in o.title):
                if o.content and o.content.strip():
                    return f'【本卷目标/卷纲】（第{volume_index}卷「{vol_chapter.title}」）\n{o.content[:1200]}'
        acts = [o for o in outlines if o.level == 0]
        if 0 <= volume_index - 1 < len(acts):
            o = acts[volume_index - 1]
            if o.content and o.content.strip():
                return f'【本卷目标/卷纲】（第{volume_index}卷）\n{o.content[:1200]}'
    except Exception:
        pass
    return ''


def _get_adjacent_volumes_outline(book_id, volume_index):
    """获取前一卷+本卷+后一卷共三卷的卷纲摘要（用于正文写作上下文）。
    【改造】从全量 timeline 注入改为三卷注入，避免上下文膨胀且聚焦当前创作位置。
    返回拼接好的三卷卷纲文本。"""
    try:
        bb = BookBible.query.filter_by(book_id=book_id).first()
        if not bb or not bb.timeline or not bb.timeline.strip().startswith('['):
            return ''
        arr = json.loads(bb.timeline)
        if not isinstance(arr, list) or not arr:
            return ''

        # 按 volume_index 排序
        def _v_idx(v):
            if not isinstance(v, dict):
                return 0
            raw = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', '')))
            try:
                return int(raw) if raw else 0
            except (ValueError, TypeError):
                return 0
        arr_sorted = sorted([v for v in arr if isinstance(v, dict)], key=_v_idx)

        # 提取单卷摘要（精简版，控制每卷约500字）
        def _summarize_vol(v, role_label):
            if not isinstance(v, dict):
                return ''
            v_idx = _v_idx(v)
            v_name = v.get('volume') or v.get('volume_title') or f'第{v_idx}卷'
            parts = [f'▼ [{role_label}] 第{v_idx}卷「{v_name}」']
            if v.get('act'):
                parts.append(f'  五幕定位：{v["act"]}')
            main_plot = v.get('main_plot') or v.get('core_goal') or ''
            if main_plot:
                parts.append(f'  主线：{main_plot[:200]}')
            core_conflict = v.get('core_conflict') or ''
            if core_conflict:
                parts.append(f'  核心冲突：{core_conflict[:150]}')
            # nodes 只取前5个，保留 chapters/type/title/summary
            nodes = v.get('nodes') or []
            if isinstance(nodes, list) and nodes:
                nodes_lines = []
                for n in nodes[:5]:
                    if not isinstance(n, dict):
                        continue
                    line = f'    · [{n.get("type","M")}] {n.get("chapters","")} {n.get("title","")}'
                    summary = n.get('summary', '')
                    if summary:
                        line += f'：{summary[:80]}'
                    nodes_lines.append(line)
                if nodes_lines:
                    parts.append('  情节节点：')
                    parts.extend(nodes_lines)
            ending_hook = v.get('ending_hook') or v.get('ending') or v.get('climax') or ''
            if ending_hook:
                parts.append(f'  卷尾钩子：{ending_hook[:150]}')
            return '\n'.join(parts)

        # 定位前一卷、本卷、后一卷
        prev_v, curr_v, next_v = None, None, None
        for i, v in enumerate(arr_sorted):
            if _v_idx(v) == volume_index:
                curr_v = v
                if i > 0:
                    prev_v = arr_sorted[i - 1]
                if i < len(arr_sorted) - 1:
                    next_v = arr_sorted[i + 1]
                break

        # 如果没找到本卷（如卷号超出），回退用第一卷
        if curr_v is None and arr_sorted:
            curr_v = arr_sorted[0]

        sections = []
        if prev_v:
            sections.append(_summarize_vol(prev_v, '前一卷·回顾'))
        if curr_v:
            sections.append(_summarize_vol(curr_v, '本卷·进行中'))
        if next_v:
            sections.append(_summarize_vol(next_v, '后一卷·走向'))

        if not sections:
            return ''
        return '【卷纲规划·前中后三卷】（前一卷回顾+本卷进行+后一卷走向，确保剧情连贯）\n' + '\n\n'.join(sections)
    except (json.JSONDecodeError, ValueError, TypeError):
        return ''


def _sort_foreshadowings_by_urgency(bb, vol_chapter, current_chapter_num, top_n=25):
    """伏笔按"到期紧迫度"排序，提取 Top N 待回收伏笔。
    【P2优化·百万字长线防遗忘】
    - 有计划回收章号且已过期（target < current）：最高紧迫，urgency=0（逾期必回收）
    - 有计划回收章号且将到期（target >= current）：urgency = target - current（越近越紧迫）
    - 无计划回收章号但沉淀已久（current-planted >= 50）：urgency = 500（沉淀越久越紧迫，不再被挤出）
      沉淀时长越长越往下偏移一点，保证最老的优先
    - 无计划回收章号且沉淀不久：urgency = 800
    - 扩展 Top N 从 15 到 25，覆盖更多伏笔，避免百万字时长线伏笔被挤出。
    优先从 foreshadowing_volumes 取结构化数据，否则从全局 foreshadowing 文本启发式提取。"""
    pending = []

    # 1. 优先从按卷结构化数据取
    if vol_chapter:
        try:
            arr = json.loads(bb.foreshadowing_volumes or '[]')
            for v in arr:
                if not isinstance(v, dict):
                    continue
                data = v.get('data') or {}
                items = data.get('foreshadowings') or data.get('items') or []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    status = str(item.get('status', '')).lower()
                    if any(kw in status for kw in ['已回收', '已揭示', '已解决', '已兑现', 'recycled', 'resolved']):
                        continue
                    planted = item.get('planted_chapter') or item.get('planted_at') or 0
                    target = item.get('target_chapter') or item.get('planned_recall') or item.get('recall_at') or 0
                    desc = item.get('description') or item.get('content') or item.get('title') or json.dumps(item, ensure_ascii=False)
                    try:
                        planted_n = int(planted) if planted else 0
                    except (ValueError, TypeError):
                        planted_n = 0
                    try:
                        target_n = int(target) if target else 0
                    except (ValueError, TypeError):
                        target_n = 0
                    # 紧迫度计算（数值越小越紧迫）
                    if target_n and target_n < current_chapter_num:
                        # 逾期未回收：最高紧迫
                        urgency = 0
                    elif target_n:
                        # 将到期：距离越近越紧迫
                        urgency = target_n - current_chapter_num
                    elif planted_n and (current_chapter_num - planted_n) >= 50:
                        # 无目标但沉淀已久（≥50章）：给予中等紧迫度，沉淀越久越优先
                        # 用 500 - min(沉淀时长, 200) 让最老的伏笔 urgency 低至 300
                        urgency = 500 - min(current_chapter_num - planted_n, 200)
                    elif planted_n:
                        # 无目标且沉淀不久：较低紧迫
                        urgency = 800
                    else:
                        urgency = 999
                    pending.append((urgency, planted_n, target_n, desc))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 2. 回退：从全局 foreshadowing 文本启发式提取
    if not pending and bb.foreshadowing and bb.foreshadowing.strip():
        for line in bb.foreshadowing.split('\n'):
            line = line.strip()
            if not line:
                continue
            if any(kw in line for kw in ['已回收', '已揭示', '已解决', '已兑现']):
                continue
            # 启发式：从文本中提取章号
            import re as _re_fs
            nums = _re_fs.findall(r'第?(\d+)\s*章', line)
            if nums:
                target_n = int(nums[-1])
                planted_n = int(nums[0]) if len(nums) > 1 else 0
                if target_n < current_chapter_num:
                    urgency = 0
                else:
                    urgency = target_n - current_chapter_num
            else:
                target_n = 0
                planted_n = 0
                urgency = 800
            pending.append((urgency, planted_n, target_n, line))

    # 按紧迫度升序排序，取 Top N
    pending.sort(key=lambda x: x[0])
    return pending[:top_n]


def _extract_appearing_characters(recent_chapters, bb=None):
    """从最近章节正文中启发式提取出场角色名。
    P2增强：融合 chapter_changes_log 的结构化角色名（CharacterStateChanges.CharacterId），
    比纯正则更准——结构化数据是章后回写的权威角色记录，正则只能启发式猜测。
    简单策略：识别"X说"、"X道"等模式中的 X + chapter_changes_log 的角色名。"""
    names = set()
    import re as _re_char

    # 1. 从 chapter_changes_log 提取结构化角色名（最近20章，权威来源）
    if bb and getattr(bb, 'chapter_changes_log', ''):
        try:
            log_list = json.loads(bb.chapter_changes_log)
            if isinstance(log_list, list):
                for entry in log_list[-20:]:
                    if not isinstance(entry, dict):
                        continue
                    chg = entry.get('changes') or {}
                    if not isinstance(chg, dict):
                        continue
                    for c in (chg.get('CharacterStateChanges') or []):
                        if isinstance(c, dict):
                            nm = c.get('CharacterId') or c.get('Name') or ''
                            if nm and isinstance(nm, str) and 2 <= len(nm) <= 8:
                                names.add(nm.strip())
                    # 物品转移的持有者也视为出场角色
                    for it in (chg.get('ItemTransfers') or []):
                        if isinstance(it, dict):
                            for holder_key in ('FromHolder', 'ToHolder'):
                                holder = it.get(holder_key) or ''
                                if holder and isinstance(holder, str) and 2 <= len(holder) <= 8:
                                    names.add(holder.strip())
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 2. 从最近章节正文启发式提取（正则，补充结构化数据未覆盖的角色）
    if recent_chapters:
        text = '\n'.join([(c.content or '') for c in recent_chapters])
        # 中文姓名 2-4 字
        for m in _re_char.finditer(r'([\u4e00-\u9fa5]{2,4})(?:说|道|想|看|笑|怒|惊|叹|问|答|吼|喊|冷哼|微笑|皱眉)', text):
            name = m.group(1)
            # 排除常见动词误判
            if name not in {'这是', '那是', '于是', '然后', '突然', '只见', '心想', '不禁', '不由'}:
                names.add(name)
    return names


def _compute_style_baseline(book_id, current_chapter_num, sample_count=5):
    """计算前 N 章的文风指纹基准（借鉴 PlotPilot 文风指纹漂移检测）。
    取当前章之前最近 sample_count 章的正文，计算文风指纹平均值作为基准。
    返回基准指纹 dict 或 None（章节数不足时）。"""
    if not compute_style_fingerprint or current_chapter_num <= 2:
        return None
    try:
        # 取前 N 章正文（排除当前章和分卷占位）
        prev_chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).filter(
            Chapter.order_index < current_chapter_num,
            Chapter.order_index >= max(1, current_chapter_num - sample_count)
        ).order_by(Chapter.order_index.desc()).limit(sample_count).all()
        if not prev_chapters:
            return None
        fingerprints = []
        for ch in prev_chapters:
            content = ch.content or ''
            # 提取正文（剥离 chapter_changes 等标签）
            body = re.sub(r'<chapter_changes>[\s\S]*?</chapter_changes>', '', content, flags=re.IGNORECASE)
            body = re.sub(r'<pre_write_check>[\s\S]*?</pre_write_check>', '', body, flags=re.IGNORECASE)
            body = body.strip()
            if len(body) < 100:
                continue
            fp = compute_style_fingerprint(body)
            if fp:
                fingerprints.append(fp)
        if len(fingerprints) < 2:
            return None  # 样本不足
        # 计算各特征平均值
        keys = ['avg_sent_len', 'short_sent_ratio', 'long_sent_ratio', 'dialog_ratio', 'punct_density', 'adj_density', 'verb_density']
        baseline = {}
        for key in keys:
            vals = [fp.get(key, 0) for fp in fingerprints if key in fp]
            if vals:
                baseline[key] = round(sum(vals) / len(vals), 4)
        return baseline if baseline else None
    except Exception:
        return None


def _recall_related_chapters(book_id, appearing_chars, current_chapter_num, max_chapters=6):
    """轻量RAG：基于出场角色召回相关历史章节摘要，补充前4章完整正文窗口的盲区。
    策略：扫描所有历史章节（排除最近4章，避免与即时层重复）的 summary，
    召回 summary 中含当前章出场角色的章节，按章号降序取最近 max_chapters 条。
    无 summary 的章节跳过（无法判断相关性）。
    返回 [{'chapter_num','title','summary'}] 列表。"""
    if not appearing_chars:
        return []
    try:
        # 排除最近4章（即时层已覆盖），排除分卷占位章
        threshold = max(1, current_chapter_num - 4)
        candidates = Chapter.query.filter(
            Chapter.book_id == book_id,
            Chapter.is_volume == False,
            Chapter.order_index < threshold
        ).order_by(Chapter.order_index.desc()).limit(200).all()  # 扫描最近200章历史
    except Exception:
        return []

    recalled = []
    seen_chapters = set()
    for ch in candidates:
        summary = (getattr(ch, 'summary', '') or '').strip()
        if not summary or len(summary) < 10:
            continue  # 无 summary 无法判断相关性
        # 相关性判定：summary 含当前章出场角色名
        if any(name in summary for name in appearing_chars):
            if ch.order_index not in seen_chapters:
                seen_chapters.add(ch.order_index)
                recalled.append({
                    'chapter_num': ch.order_index,
                    'title': ch.title or '',
                    'summary': summary[:200],  # summary 字段上限 200 字
                })
            if len(recalled) >= max_chapters:
                break
    # 按章号升序排列（剧情时间顺序）
    recalled.sort(key=lambda x: x['chapter_num'])
    return recalled


def _filter_bible_by_relevance(bb, appearing_chars, max_per_field=None):
    """按出场角色相关性筛选 bible 维度。
    - character_profiles: 优先包含出场角色的档案块
    - 其他维度保持原样（不相关性筛选）
    返回筛选后的字段字典。"""
    if max_per_field is None:
        max_per_field = {'character_profiles': 1500, 'worldbuilding': 1000, 'plot_design': 1000,
                         'timeline': 800, 'concept': 500, 'key_rules': 1200, 'style_guide': 500}
    result = {}

    # key_rules / worldbuilding / concept / plot_design / timeline / style_guide：语义截断（保证段落完整）
    for field in ['key_rules', 'worldbuilding', 'concept', 'plot_design', 'timeline', 'style_guide']:
        val = getattr(bb, field, '') or ''
        result[field] = _smart_truncate(val, max_per_field.get(field, 1000)) if val else ''

    # character_profiles：按出场角色筛选
    cp = bb.character_profiles or ''
    if cp and appearing_chars:
        # 简单分块：按空行或【】标题分块
        import re as _re_cp
        blocks = _re_cp.split(r'\n(?=[【\[])', cp)
        relevant = []
        other = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            if any(name in block for name in appearing_chars):
                relevant.append(block)
            else:
                other.append(block)
        # 优先相关性块，剩余空间补其他
        budget = max_per_field.get('character_profiles', 1500)
        selected = []
        used = 0
        for block in relevant:
            if used + len(block) > budget:
                # 截断该块
                remain = budget - used
                if remain > 100:
                    selected.append(block[:remain])
                    used = budget
                break
            selected.append(block)
            used += len(block)
        # 剩余预算补其他
        for block in other:
            if used >= budget:
                break
            remain = budget - used
            if remain < 100:
                break
            truncated = block[:remain]
            selected.append(truncated)
            used += len(truncated)
        result['character_profiles'] = '\n\n'.join(selected) if selected else cp[:budget]
    else:
        result['character_profiles'] = (cp or '')[:max_per_field.get('character_profiles', 1500)]

    return result


def _collect_relevant_reports(book_id, current_chapter_num, window=10, max_reports=3, per_report_limit=800):
    """收集当前章号 ±window 范围内的动态报告，每份截取摘要。
    优先时效性（接近当前章号），其次数量上限。"""
    try:
        reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
    except Exception:
        return []
    if not reports:
        return []
    # 筛选在窗口内的报告
    relevant = []
    for r in reports:
        # 报告覆盖区间 [chapter_start, chapter_end] 与 [current-window, current+window] 有交集
        if r.chapter_end >= current_chapter_num - window and r.chapter_start <= current_chapter_num + window:
            relevant.append(r)
    # 按接近度排序（距离当前章号最近者优先）
    relevant.sort(key=lambda r: abs((r.chapter_start + r.chapter_end) / 2 - current_chapter_num))
    # 取前 max_reports 份，每份截取摘要
    result = []
    for r in relevant[:max_reports]:
        content = (r.content or '')[:per_report_limit]
        result.append({
            'title': r.title,
            'chapter_start': r.chapter_start,
            'chapter_end': r.chapter_end,
            'content': content,
        })
    return result


def _collect_historical_volume_digest(book_id, current_chapter_num, max_volumes=4, per_volume_limit=400):
    """百万字长线记忆：聚合"早期卷"的动态报告为按卷摘要，补充 _collect_relevant_reports 的窗口盲区。
    当 current_chapter_num 较大（如300章）时，window=100 只能覆盖最近100章，
    本函数把更早的卷（如第1-4卷）的动态报告按卷聚合，每卷取一份摘要注入，避免长线剧情遗忘。
    只聚合 chapter_end < current - 100 的早期报告（不与 _collect_relevant_reports 重叠）。"""
    try:
        threshold = current_chapter_num - 100
        if threshold <= 50:
            return []  # 章数不多时无需历史摘要
        early_reports = DynamicReport.query.filter(
            DynamicReport.book_id == book_id,
            DynamicReport.chapter_end < threshold
        ).order_by(DynamicReport.chapter_start).all()
    except Exception:
        return []
    if not early_reports:
        return []
    # 按 50 章一卷分组聚合
    volume_buckets = {}
    for r in early_reports:
        vol_idx = (r.chapter_start - 1) // 50 + 1 if r.chapter_start > 0 else 1
        volume_buckets.setdefault(vol_idx, []).append(r)
    # 每卷取第一份报告的摘要作为该卷代表（最早的报告概括卷的开端，最能代表该卷基调）
    result = []
    for vol_idx in sorted(volume_buckets.keys())[:max_volumes]:
        reports_in_vol = volume_buckets[vol_idx]
        # 取该卷所有报告内容的拼接摘要
        combined = ' '.join([(r.content or '')[:150] for r in reports_in_vol[:3]])
        combined = combined[:per_volume_limit]
        ch_start = reports_in_vol[0].chapter_start
        ch_end = reports_in_vol[-1].chapter_end
        result.append({
            'volume': f'第{vol_idx}卷',
            'chapter_range': f'{ch_start}-{ch_end}章',
            'digest': combined,
        })
    return result


# ===== Ai总创作公共常量（被 ai_master_create 和 ai_master_create_stream 共用，避免重复定义）=====
# 维度协同顺序：上游维度产物会注入到下游维度的 prompt（严格按 DAG 依赖，不再无差别全注入）
MASTER_DIM_ORDER = ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design', 'timeline', 'foreshadowing', 'locations', 'inventory', 'dynamic_volumes']

# 每个维度的上游依赖（只注入这些维度作为"已确认上游产物"，而非所有维度）
MASTER_DIM_UPSTREAM = {
    'concept': [],
    'key_rules': ['concept'],
    'worldbuilding': ['concept', 'key_rules'],
    'character_profiles': ['concept', 'key_rules', 'worldbuilding'],
    'plot_design': ['concept', 'key_rules', 'worldbuilding', 'character_profiles'],
    'timeline': ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design'],
    'foreshadowing': ['plot_design', 'timeline', 'character_profiles'],
    'locations': ['worldbuilding', 'character_profiles'],
    'inventory': ['worldbuilding', 'character_profiles', 'locations'],
    'dynamic_volumes': ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design', 'timeline', 'foreshadowing', 'locations', 'inventory'],
}

MASTER_DIM_MAP = {
    'concept': {'field': 'concept', 'label': '构思', 'keys': ['tomato_plan', 'one_line_concept', 'master_outline'],
                'prompt': '生成核心构思。\n【输出格式】纯文本分项（空行分隔）：1) 一句话核心概念；2) 核心卖点（3-5条）；3) 目标读者画像；4) 主线冲突；5) 独特亮点。'},
    'key_rules': {'field': 'key_rules', 'label': '设定/规则', 'keys': ['tomato_setting', 'lock_facts'],
                  'prompt': '生成核心设定规则。\n【输出格式】编号列表，每条以"① ② ③..."开头，规则间空行分隔。包括：世界必须遵循的铁律、人物能力边界、代价/反噬机制、禁忌事项。'},
    'worldbuilding': {'field': 'worldbuilding', 'label': '世界观', 'keys': ['tomato_setting', 'lock_facts'],
                      'prompt': '生成详细世界观。\n【输出格式】分小节二级标题"## 力量体系/## 社会结构/## 地理概况/## 历史脉络"，每小节下用编号列表。不要写成段落散文。'},
    'character_profiles': {'field': 'character_profiles', 'label': '人物', 'keys': ['tomato_character', 'character_cognition'],
                           'prompt': '生成主要人物。\n【输出格式】每个角色一个"## 角色：<姓名>"二级标题，下方依次：身份/性格（3-5个关键词）/背景故事（100-200字）/核心动机/与其他角色关系（"→ 角色名：关系"）。主角 + 3-5个配角。'},
    'plot_design': {'field': 'plot_design', 'label': '大纲', 'keys': ['master_outline', 'tomato_outline', 'volume_breakdown'],
                    'prompt': '生成五幕式总纲。\n【输出格式】每幕"## 第N幕：<幕名>"二级标题，下方：幕核心目标/主要冲突/卷入角色/关键转折点/幕尾悬念/对应分卷范围（"第X-Y卷"）。共5幕，对应全书所有分卷。'},
    'timeline': {'field': 'timeline', 'label': '剧情', 'keys': ['volume_breakdown', 'chapter_plan', 'tomato_outline'],
                 'prompt': '生成分卷详细剧情。\n【分卷铁律·必读】**每卷固定 50 章**，全书卷数 = 总章数÷50（向上取整），卷序号从1开始连续。卷名格式"第N卷 副标题"。\n【输出格式·必读】严格 JSON 数组，不要任何解释文字、不要 markdown 代码块。结构：[{"volume_index":1,"volume":"第1卷 副标题","main_plot":"本卷主线剧情100-200字","core_conflict":"本卷核心冲突","ending_hook":"卷尾钩子","nodes":[{"title":"节点1","chapters":"1-10","type":"M","summary":"概要","cool_type":"实力碾压"}]}]. 每卷 5-8 个 nodes；chapters 字段"起始-结束"，全书 chapter 编号连续不重叠。'},
    'foreshadowing': {'field': 'foreshadowing', 'label': '伏笔', 'keys': ['foreshadow_register', 'narrative_debt'],
                      'prompt': '埋设伏笔线索。\n【输出格式】编号列表，每条"## 伏笔N：<标题>\\n- 埋设内容：xxx\\n- 埋设时机：第X卷Y章附近\\n- 预期回收：第X卷Y章附近\\n- 回收方式：xxx\\n- 对剧情的影响：xxx"。设计 3-5 条。'},
    'locations': {'field': 'locations', 'label': '地点', 'keys': ['lock_facts', 'tomato_setting', 'geography'],
                  'prompt': '设计地点体系。\n【输出格式】严格 JSON 数组三级结构：[一级大区域{"name","description","secondaries":[{"name","description","scenes":[{"name","description","key_events"}]}]}]. 设计 2-3 个一级大区域。'},
    'inventory': {'field': 'inventory', 'label': '物资库', 'keys': ['lock_facts', 'level_system', 'power_system', 'ability_tree'],
                  'prompt': '生成主要物品/功法/法宝清单。\n【输出格式】严格 JSON 数组：[{"name","type","source","effect","owner","first_appearance"}]. type 取值：法宝/功法/丹药/武器/防具/其他。设计 8-15 个核心物品。'},
    'dynamic_volumes': {'field': 'dynamic_volumes', 'label': '动态文件', 'keys': ['narrative_debt', 'foreshadow_register', 'lock_facts'],
                        'prompt': '生成分卷动态文件摘要。\n【输出格式】严格 JSON 数组：[{"volume_id":"1","volume":"第1卷 副标题","volume_index":1,"summary":"本卷概述100-200字","characters":"本卷人物变化50-100字","events":"本卷关键事件50-100字","timeline":"本卷时间线","locations":"本卷地点变化","factions":"本卷势力变化","foreshadowing":"本卷伏笔动态","realms":"本卷境界变化","relationships":"本卷关系变化"}]. 每卷一条记录，全书所有卷都要覆盖。'},
}


def _build_master_ctx(bb, session_outputs=None):
    """构建维度创作的上下文字典：本轮已生成内容(session_outputs)优先，回退 bible 已有内容。"""
    session_outputs = session_outputs or {}
    return {
        'concept': session_outputs.get('concept') or bb.concept or '',
        'key_rules': session_outputs.get('key_rules') or bb.key_rules or '',
        'worldbuilding': session_outputs.get('worldbuilding') or bb.worldbuilding or '',
        'character_profiles': session_outputs.get('character_profiles') or bb.character_profiles or '',
        'plot_design': session_outputs.get('plot_design') or bb.plot_design or '',
        'timeline': session_outputs.get('timeline') or bb.timeline or '',
        'foreshadowing': session_outputs.get('foreshadowing') or bb.foreshadowing or '',
        'locations': session_outputs.get('locations') or bb.locations or '',
        'inventory': session_outputs.get('inventory') or bb.inventory or '',
        'dynamic_volumes': session_outputs.get('dynamic_volumes') or bb.dynamic_volumes or '',
    }


# 网文风格流派标签库（基于2025中国网络文学蓝皮书与起点三江榜趋势）
# 题材 → 风格流派映射表（长篇）
# 数据来源：番茄小说/起点中文网/七猫小说/晋江文学城等主流平台分类页调研整理
# key 为风格短码，value 为网文圈通用中文叫法
NOVEL_GENRE_STYLES = {
    'fantasy': {
        'dongfang': '东方玄幻', 'yishi': '异世大陆', 'gaowu': '高武世界', 'wangchao': '王朝争霸',
        'honghuang': '洪荒流', 'fanren': '凡人流', 'feichai': '废柴逆袭流', 'qiangzhong': '强者重生流',
        'qiandao': '签到流', 'shenchong': '神宠流', 'dihua': '迪化流', 'shenshu': '全球神祇流',
    },
    'xianxia': {
        'gudian': '古典仙侠', 'xiuzhen': '修真文明', 'huanxiang': '幻想修仙', 'xiandai': '现代修真',
        'shenhua': '神话修真', 'fengshen': '洪荒封神', 'goudao': '苟道流', 'changsheng': '长生流',
        'jiazu': '家族修仙流', 'jianxiu': '剑修流', 'liandan': '炼丹流', 'liangi': '炼器流',
    },
    'qihuan': {
        'jianmo': '剑与魔法', 'shishi': '史诗奇幻', 'shenmi': '神秘幻想', 'xiandai': '现代魔法',
        'lishi': '历史神话', 'xifang': '西方奇幻', 'wushi': '巫师流', 'lingzhu': '领主贵族',
        'mofa': '魔法校园', 'zhongshi': '中式奇幻',
    },
    'wuxia': {
        'chuantong': '传统武侠', 'wuxia_fs': '武侠幻想', 'guoshu': '国术流', 'xinpa': '新派武侠',
        'lishi_wx': '历史武侠', 'langzi': '浪子异侠', 'kuaiyi': '快意江湖', 'jianghu': '江湖恩怨',
    },
    'urban': {
        'shenghuo': '都市生活', 'yishu': '异术超能', 'qingchun': '青春校园', 'mingxing': '娱乐明星',
        'shangzhan': '商战职场', 'guanchang': '官场沉浮', 'dushi_xz': '都市修真', 'dushi_gw': '都市高武',
        'shenyi': '神医流', 'shenhao': '神豪流', 'jianbao': '鉴宝流', 'bingwang': '兵王回归', 'cunzhi': '乡村种田',
    },
    'urban_business': {
        'shangzhan': '商战职场', 'chuangye': '创业逆袭', 'zhichang': '职场权谋',
        'shangye': '商业帝国', 'zhulian': '珠联璧合', 'jindiao': '金融大鳄',
    },
    'urban_fantasy': {
        'yishu': '异术超能', 'dushi_xz': '都市修真', 'dushi_gw': '都市高武',
        'guidze': '规则怪谈', 'dushi_nr': '都市脑洞', 'lingyi': '灵异民俗',
    },
    'history': {
        'jiakong': '架空历史', 'qhsg': '秦汉三国', 'tangsong': '两晋隋唐', 'wudai': '五代十国',
        'songming': '两宋元明', 'qingmg': '清史民国', 'chuanyue_ls': '穿越历史', 'keju': '科举入仕',
        'zhongtian_ls': '历史种田', 'quanmou': '权谋庙堂',
    },
    'military': {
        'junlv': '军旅生涯', 'zhanzheng': '军事战争', 'kangzhan': '抗战烽火', 'diedz': '谍战特工',
        'tezhong': '特种军旅', 'xiandai_zz': '现代战争', 'chuanyue_zz': '穿越战争',
    },
    'game': {
        'djj': '电子竞技', 'wlw': '虚拟网游', 'youxi_yj': '游戏异界', 'youxi_xt': '游戏系统',
        'quantxi': '全息网游', 'disitianzai': '第四天灾流', 'shuju': '数据流', 'zhandui': '战队夺冠',
    },
    'sports': {
        'zuqiu': '足球运动', 'lanqiu': '篮球运动', 'wangqiu': '网球/乒乓球', 'zonghe_ty': '综合竞技',
        'dianjing_ty': '电竞体育', 'rexue_ty': '热血竞技', 'xiaoyuan_ty': '体育校园',
    },
    'scifi': {
        'xingji': '星际文明', 'weilai': '未来世界', 'chaojikj': '超级科技', 'shikong': '时空穿梭',
        'jinhua': '进化变异', 'moshi': '末世危机', 'jijia': '古武机甲', 'saibo': '赛博朋克',
        'feitu': '废土生存', 'xingji_zz': '星际战争', 'heikeji': '黑科技系统',
    },
    'mystery': {
        'zhentan': '侦探推理', 'guilyi': '诡异神秘', 'guize': '规则怪谈', 'lingyi_ms': '灵异民俗',
        'fengshui': '风水秘术', 'xingzhen': '刑侦破案', 'daoshu': '道术流', 'kesulu': '克苏鲁',
        'xunyi': '悬疑探险', 'xisikongjv': '细思极恐', 'shourong': '灵异收容',
    },
    'infinite': {
        'wuxian': '无限流', 'zhutian': '诸天流', 'zongman': '综漫', 'yuanzu': '元祖无限流',
        'zhushen': '主神流', 'kuaichuan_zt': '快穿诸天', 'yingshi_ct': '影视世界穿越',
        'dongman_ct': '动漫世界穿越', 'fuben': '副本闯关',
    },
    'light_novel': {
        'yuansheng': '原生幻想', 'yansheng': '衍生同人', 'gaoxiao': '搞笑吐槽', 'liana': '恋爱日常',
        'erciyuan': '二次元', 'rixi': '日系轻改', 'zhonger': '中二设定', 'shacao': '沙雕轻松',
        'mengxi': '萌系', 'yishijie': '异世界',
    },
    # 女频
    'romance': {
        'dushi_tc': '都市甜宠', 'haozong': '豪门总裁', 'xianhun': '先婚后爱', 'pojing': '破镜重圆',
        'zhuqi': '追妻火葬场', 'bazong': '总裁霸总', 'yulequan': '娱乐圈', 'zhichang_hl': '职场婚恋',
        'niandai': '年代文', 'xianhun_yw': '闪婚', 'xiaoyuan_qc': '校园青春', 'nuelian': '虐恋情深',
        'chongsheng_nx': '重生逆袭', 'kuaichuan_yq': '快穿',
    },
    'ancient_romance': {
        'gongdou': '宫斗', 'zhaidou': '宅斗', 'gufeng': '古风世情', 'gudai_ct': '古代穿越',
        'shunv': '庶女逆袭', 'dinu': '嫡女', 'quanchen': '权臣', 'jiangjun': '将军', 'wangye': '王爷',
        'daihou': '帝后', 'zhongtian_gy': '种田经商', 'chaoztang': '朝堂权谋', 'daijia': '代嫁代娶', 'chongsheng_gy': '穿越重生',
    },
    'fantasy_romance': {
        'xuanhuan_yq': '玄幻言情', 'qihuan_yq': '奇幻言情', 'xianxia_yq': '仙侠言情', 'xiuxian_yq': '修仙言情',
        'xuanxue': '玄学相师', 'lingyi_yq': '灵异言情', 'yineng_nv': '异能女主', 'xitong_yq': '系统言情',
        'chuanshu': '穿书', 'weilai_yq': '未来言情',
    },
    'danmei': {
        'xiandai_ca': '现代都市纯爱', 'gudai_ca': '古代纯爱', 'xiangxiang_ca': '现代幻想纯爱', 'ab0': 'ABO',
        'qiangqiang': '强强', 'tianwen_ca': '甜文', 'nuewen_ca': '虐文', 'xiaoyuan_ca': '校园',
        'dianjing_ca': '电竞', 'xianxia_ca': '仙侠纯爱', 'wuxian_ca': '无限流纯爱', 'kuaichuan_ca': '快穿纯爱',
    },
    'acg': {
        'dongfang_ys': '东方衍生', 'xifang_ys': '西方衍生', 'gudian_ys': '古典衍生', 'erciyuan_yq': '二次元言情',
        'zongying': '综英美', 'zongwuxia': '综武侠', 'zongman_ys': '综漫', 'yingshi_tr': '影视同人',
        'youxi_tr': '游戏同人', 'dongman_tr': '动漫同人',
    },
    'other': {
        'xiangsheng': '相声评书', 'sanwen': '散文随笔', 'pinglun': '评论文集',
        'youji': '美文游记', 'shige': '诗歌', 'weixiaoshuo': '微小说',
    },
}

# 题材 → 风格流派映射表（短篇，以知乎盐选/盐言故事赛道为主）
SHORT_GENRE_STYLES = {
    'romance': {
        'hunyin': '婚姻信任崩塌', 'poxi': '婆媳边界', 'zhichang_pu': '职场PUA反杀', 'yuansheng': '原生家庭拉扯',
        'zhongnian': '中年离婚重启', 'chongnianv': '重男轻女', 'jiating': '家庭伦理', 'chongsheng_dy': '重生打脸爽文',
    },
    'ancient_romance': {
        'chongsheng_fc': '重生复仇', 'shunv_nx': '庶女逆袭', 'dinu_fp': '嫡姐反派', 'qianshi': '前世惨死今生逆袭',
        'yinren': '隐忍蛰伏摊牌打脸', 'qihun': '弃婚另嫁', 'gongdou_dp': '宫斗短篇', 'daijia_dp': '代嫁代娶', 'xianhun_dp': '先婚后爱',
    },
    'fantasy_romance': {
        'bazong_zq': '霸总追妻火葬场', 'baiyueguang': '白月光替身', 'jingshen': '净身出户远走', 'chongfeng': '重逢反差',
        'lihun': '离婚后惊艳世界', 'shanhun': '闪婚大佬', 'zongcai_bw': '总裁卑微求和', 'zhichang_tc': '职场甜宠', 'xiaoyuan_cl': '校园初恋',
    },
    'mystery': {
        'shenghuohua': '生活化悬疑', 'xisikongju': '细思极恐', 'shikong_laidian': '时空来电', 'wanmei': '完美犯罪',
        'duochong_fz': '多重反转', 'xiongsha': '凶杀推理', 'mishi': '密室', 'guize_dp': '规则怪谈短篇',
        'minshu_dp': '民俗志怪', 'jiankong': '监控悬疑',
    },
    'urban_fantasy': {
        'lingchen': '凌晨怪事', 'jiuwu': '旧物惊悚', 'chuzu': '出租屋灵异', 'laoxiaoqu': '老小区鬼事',
        'fengshui_dp': '风水秘术', 'daoshi': '道士收妖', 'hongyi': '红衣女人', 'mintan': '民间怪谈',
    },
    'light_novel': {
        'qingshed': '轻设定重映射', 'huangyan': '谎言可视化', 'danmu': '弹幕生存', 'chaoshi': '超现实外壳',
        'she_ding': '设定撑满五千字', 'qiguai': '奇怪群聊', 'zouma': '死亡走马灯', 'gonglve': '攻略者大逃杀',
    },
    'other': {
        'qingganzhiyu': '情感治愈', 'renchong': '人宠双向疗愈', 'xishui': '细水流长', 'yinanping': '意难平',
        'houjin': '后劲极强', 'wenqing': '温情日常', 'shihuai': '释怀告别', 'chongfeng_jr': '重逢救赎',
        'jiashu': '家书来信', 'xiaorenwu': '小人物温度',
    },
    'fantasy': {
        'kaiju': '开局一个惨', 'dalian': '打脸爽文', 'baofu': '净身出户后暴富', 'zhenlong': '真龙出狱',
        'shenyi_xl': '神医下山', 'jianbao_xl': '鉴宝赌石', 'shenhao_xt': '神豪系统', 'chongsheng_zq': '重生赚钱',
    },
    'scifi': {
        'moshi_dunhuo': '末世囤货', 'anquanwu': '安全屋求生', 'gouzh': '苟住生存', 'haidao_qz': '海岛求生',
        'sangshi': '丧尸围城', 'yidong': '移动基地', 'feitu_dp': '废土短篇', 'chongsheng_ms': '末世重生',
    },
    'infinite': {
        'qiangqingxu': '强情绪快节奏', 'gao_dairu': '高代入感', 'suipian': '碎片阅读适配', 'gouzi_my': '钩子密集',
        'duanju': '短剧改编向', 'changju': '长剧孵化向', 'dy_rencheng': '第一人称代入',
    },
    'wuxia': {
        'danyuan_an': '单元案件', 'xilie_zhj': '系列主角', 'tanandanyuan': '探案单元', 'guaitan_xl': '怪谈系列',
        'anjian_chuan': '案件串烧', 'duanpian_ll': '短篇连缀成长篇',
    },
    'historical': {
        'zhenan': '真实案件改编', 'guaimai': '拐卖案', 'qianwen': '奇闻轶事', 'rensheng': '人生经历',
        'lieqi': '猎奇奇案', 'anjiadz_fz': '案件反转',
    },
    'xianxia': {
        'chongsheng_nx_dp': '重生逆袭', 'bubu': '步步为营', 'luanyi': '乱世成长', 'chaotang_qm': '朝堂权谋',
        'guifei': '贵妃青云直上', 'shunv_fs': '庶女翻身', 'jingcheng': '京城第一美人',
    },
}

# 短篇通用赛道兜底（题材未命中时使用）
SHORT_FALLBACK_STYLES = {
    'fanzhuan': '反转向（结局反转）', 'danyuanju': '单元剧', 'yingshi': '影视化（镜头语言）',
    'first_person': '第一人称', 'shacao_dp': '沙雕爽文', 'dianwen': '颠文', 'bailan': '摆烂流',
}


def _get_style_label(book_type, genre, style_key):
    """根据类型+题材查风格流派中文标签，找不到则原样返回 key。"""
    table = SHORT_GENRE_STYLES if book_type == 'short_story' else NOVEL_GENRE_STYLES
    genre_map = table.get(genre) if genre else None
    if not genre_map:
        if book_type == 'short_story':
            genre_map = SHORT_FALLBACK_STYLES
        else:
            genre_map = NOVEL_GENRE_STYLES.get('fantasy', {})
    return genre_map.get(style_key, style_key)


# 题材中英文映射（与前端 constants.ts GENRES 保持一致）
# 注入 prompt 时用中文标签，避免 AI 看到英文 key（如 fantasy）约束力弱
GENRE_LABELS = {
    'other': '其他', 'fantasy': '玄幻', 'xianxia': '仙侠', 'qihuan': '奇幻',
    'wuxia': '武侠', 'urban': '都市', 'urban_business': '都市职场', 'urban_fantasy': '都市异能',
    'history': '历史', 'military': '军事', 'game': '游戏', 'sports': '体育',
    'scifi': '科幻', 'mystery': '悬疑', 'infinite': '诸天无限', 'light_novel': '轻小说',
    'romance': '现代言情', 'ancient_romance': '古代言情', 'fantasy_romance': '幻想言情',
    'danmei': '纯爱', 'acg': '二次元',
}

# ===== 标准文风提示词（正文创作通用铁律）=====
# 三种创作模式（多Agent同步/流式/连续创作流式）统一注入此标准文风，
# 技能包提示词和章节语言风格在此基础上做叠加微调，不覆盖核心规则。
STANDARD_WRITING_STYLE_PROMPT = """【标准文风铁律·必须严格执行】

## 核心风格
- 冷静、克制、硬朗。
- 短句密集，信息推进快。
- 对白推动剧情，叙述只做必要交代。
- 少抒情，少形容词堆砌，少大段心理描写。
- 用动作、物象和对白表达情绪。
- 悬念靠信息缺口、人物反应和新线索推进。

## 长短句比例（含标点符号计字数）
- 短句：20%-40%，每句 1-20 字。用于回答、反问、动作、判断、悬念落点。
- 中句：40%-50%，每句 20-35 字。用于解释规则、交代背景、补充推理。
- 长句：控制在20%以内，每句 35-70 字。只用于复杂设定说明或必要场面描述。
- 超过70字的句子必须拆开。

## 对话占比
- 单章对白段落占比保持20%-60%。推理章/情报章可提高到60%以上，场面章可降到20%-60%。
- 避免长篇独白。每轮对白只推进一个信息点。

## 叙述规则
- 描写服务情报、动作和悬念。不堆氛围，不写泛泛宏大抒情。
- 人物情绪优先用动作体现。心理活动保持短促，避免反复解释。
- 场景描写先给位置，再给关键物，再进入行动。

## 信息推进方式
- 正文优先采用"对话式推理"。

## 章法要求
- 每章开头尽快进入场景或事件。
- 每章中段用对白和行动推进信息。
- 每章结尾落在新风险、新线索或新决策上。
- 不用空泛总结收尾。不用连续大段设定说明。

## 禁用倾向
- 避免大段心理独白。避免连续堆形容词。
- 避免用"震惊、复杂、激动"直接总结情绪。
- 避免一段里同时写多个动作、解释和心理。
- 避免超过70字的长句。
- 避免使用总结性、评价性、升华的句子。

## 禁词清单（严禁使用）
一股、一抹、不由得、不禁、随即、旋即、与此同时、颇为、甚为、极为、
毫无疑问、毋庸置疑、不言而喻、深吸一口气、眼中闪过一丝、心中暗想、
心念电转、若有所思、不知不觉间、转眼间、恍然大悟、面无表情、
淡漠、漠然、眸子、嘴角微微上扬、如同、宛如、犹如、周身、周遭、
气息、威压、那道身影、说话间、话音未落、当即、顿时、瞬时、
因此、然而、显而易见、由此可见、总而言之、综上所述、
震惊、复杂、激动（直接总结情绪的词）。

## 推荐结构
动作或场景一句。对白一句。短反应一句。继续对白。信息揭示一句。

## 写完自检（逐项检查，不达标必须改写）
- 段落是否大多为单句成段？
- 短句占20%-40%（1-20字）？中句占40%-50%（20-35字）？长句控制在20%以内（35-70字）？
- 是否存在超过70字的句子？有则拆开。
- 对白段落约占20%-60%？每轮对白只推进一个信息点？
- 章尾是否留下明确钩子？
- 是否避免了总结性、评价性、升华的句子？
- 是否避免了大段心理独白和形容词堆砌？
- 描写是否服务情报、动作和悬念？
- 有没有可以用动作代替的心理描写？
- 这段能不能再短一点？这句话像真人说的吗？
- 有没有"AI常用词"或书面化表达？内心独白是否控制在5%以内？"""


def _get_genre_label(book=None, bb=None):
    """获取题材中文标签（用于 prompt 注入）。优先 book.genre，回退 bb.genre。
    找不到映射时原样返回，确保不丢信息。"""
    genre = ''
    if book is not None:
        genre = getattr(book, 'genre', '') or ''
    if not genre and bb is not None:
        genre = getattr(bb, 'genre', '') or ''
    if not genre:
        return '通用'
    return GENRE_LABELS.get(genre, genre)


def _sync_book_meta_to_bible(book, bb):
    """P0-3修复：把 book 的 total_volumes / novel_styles / genre / book_type 同步到 bible。
    在首次创建空 bible 或更新 book 时调用，确保各维度创作时能从 bible 读到权威元数据。
    仅在 book 有值且 bible 字段为空/默认时回填，不覆盖用户已设置的值。"""
    if not book or not bb:
        return
    try:
        if getattr(book, 'total_volumes', None) and (not bb.total_volumes or bb.total_volumes == 10):
            bb.total_volumes = book.total_volumes
    except Exception:
        pass
    try:
        book_styles = getattr(book, 'novel_styles', None)
        if book_styles:
            bb_styles = getattr(bb, 'novel_styles', None)
            # 仅在 bible 风格为空或默认空数组时回填
            if not bb_styles or bb_styles in ('', '[]', 'null'):
                bb.novel_styles = book_styles
    except Exception:
        pass


def _get_total_volumes(bb, book=None):
    """获取总卷数（优先 BookBible，回退 Book，默认10）。卷数不设上限，仅校验下限≥1。"""
    tv = None
    try:
        tv = getattr(bb, 'total_volumes', None)
    except Exception:
        tv = None
    if (not tv) and book is not None:
        tv = getattr(book, 'total_volumes', None)
    if not tv:
        tv = 10
    return max(1, int(tv))


def _get_chapters_per_volume(bb, book=None):
    """P3-10：按题材流派动态计算每卷章数，替代硬编码 50。
    - 短篇/短卷题材（短篇集、都市短篇）：30 章/卷
    - 长卷题材（玄幻/仙侠/奇幻/科幻/武侠）：60 章/卷
    - 默认（都市/言情/悬疑/通用）：50 章/卷
    总章数 = total_volumes × chapters_per_volume，确保百万字长篇时卷数合理。"""
    genre = ''
    try:
        genre = (getattr(book, 'genre', '') or getattr(bb, 'genre', '') or '').lower()
    except Exception:
        genre = ''
    bt = getattr(book, 'book_type', None) or getattr(bb, 'book_type', 'novel')
    if bt == 'short_story':
        return 30
    long_vol_genres = {'玄幻', '仙侠', '奇幻', '科幻', '武侠', '修真', '网游', '历史'}
    short_vol_genres = {'短篇', '短篇集', '散文', '随笔'}
    if any(g in genre for g in long_vol_genres):
        return 60
    if any(g in genre for g in short_vol_genres):
        return 30
    return 50


def _get_novel_styles_text(bb, book=None):
    """获取风格流派描述文本（最多3种叠加），按当前题材查对应风格表，用于注入创作上下文。"""
    styles_raw = None
    try:
        styles_raw = getattr(bb, 'novel_styles', None)
    except Exception:
        styles_raw = None
    if (not styles_raw) and book is not None:
        styles_raw = getattr(book, 'novel_styles', None)
    try:
        styles_list = json.loads(styles_raw or '[]') if styles_raw else []
    except Exception:
        styles_list = []
    if not styles_list:
        return ''
    bt = getattr(book, 'book_type', 'novel') if book is not None else getattr(bb, 'book_type', 'novel')
    genre = getattr(book, 'genre', '') if book is not None else getattr(bb, 'genre', '')
    labels = [_get_style_label(bt, genre, s) for s in styles_list[:3]]
    return '、'.join(labels)


def _build_core_params_block(bb, book):
    """构建「核心创作参数」注入块：题材+卷数+风格流派，作为所有维度创作与章节写作的核心依据。
    三大约束统一在此注入，确保用户选定项在下游真正生效。"""
    tv = _get_total_volumes(bb, book)
    genre_label = _get_genre_label(book, bb)
    styles_text = _get_novel_styles_text(bb, book)
    bt = getattr(book, 'book_type', 'novel') or 'novel'
    parts = ['【核心创作参数·全书依据·不可偏离】',
             f'题材：{genre_label}（人物设定、世界观、剧情走向、爽点类型须契合该题材的读者期待）',
             f'总卷数：{tv} 卷（全书所有分卷/五幕总纲/剧情大纲严格按此卷数规划，不得多不得少）']
    if bt == 'novel':
        parts.append(f'预计总字数：约 {tv*12} 万字（每卷约12万字，约50章/卷）')
    else:
        parts.append(f'短篇结构：{tv} 个单元/幕')
    if styles_text:
        parts.append(f'风格流派：{styles_text}（人物塑造、节奏、爽点设计、叙事手法须契合所选流派，这是硬约束）')
    return '\n'.join(parts)


# 章节正文「语言风格」表（行文文风，区别于题材流派）
# 与前端 constants.ts CHAPTER_LANG_STYLES 保持一致
CHAPTER_LANG_STYLES = {
    'general': ('通用', '行文规范流畅，叙述与对话比例均衡，节奏舒张有度，不刻意炫技也不寡淡；用词准确，符合现代汉语习惯。适合大多数题材的常规叙事。'),
    'baimiao': ('白描', '用最简练的笔墨勾勒人物与场景，不加渲染烘托；少用形容词，多用动词和名词；叙述客观克制，让事实自己说话。适合动作戏、硬汉派、克制情感。'),
    'jijian': ('极简', '句子短促有力，信息密度高，大量留白；砍掉一切冗余修饰与过渡；对话简洁，动作直接。适合快节奏、冷硬叙事与悬疑短篇。'),
    'youmo': ('幽默', '善用夸张、反语、双关与俏皮话制造笑点，插科打诨中藏锋芒；语言口语化，节奏跳跃；笑而不俗、讽而不戾。适合轻松日常、吐槽向、反套路喜剧。'),
    'shuangwen': ('爽文', '节奏明快，爽点密集，三章一冲突五章一反转；主角步步升级、打脸逆袭；情绪外放，多用反差对比烘托主角强大。适合玄幻都市升级流。'),
    'rexue': ('热血', '语言激昂奔放，多用短句排比与感叹，动作大开大合；情感外放，强调兄弟情、信念与战斗意志；场面燃点高，节奏层层推进。适合少年向、战斗竞技。'),
    'beiqing': ('悲情', '语调低沉绵长，多用环境烘托与意象铺陈情感；以克制写伤痛、以细节写离别；不滥情却字字戳心，留有余韵。适合虐心、悲剧、历史向与救赎类。'),
    'zhiyu': ('治愈', '笔调温柔舒缓，多写日常细节与微小温暖；语言清新柔和，少冲突多陪伴；以烟火气抚慰人心，情绪平稳上扬。适合日常向、慢生活与情感救赎类。'),
    'shijing': ('市井', '语言俚俗鲜活，多方言口语与江湖切口；人物三教九流，场景茶馆酒肆；叙述带烟火气与油滑感，对白占比较高。适合武侠江湖、都市底层与市井志怪。'),
    'gufeng': ('古风', '用词典雅，化用诗词典故与文言句式；句式工整讲究韵律，意境含蓄深远；适度文白相间，避免晦涩。适合仙侠、古言、宫斗与历史权谋题材。'),
    'guijue': ('诡谲', '氛围阴郁压抑，多用阴影、雾气、异响等意象制造不安；叙事扑朔迷离，留悬念与歧义；节奏沉滞中暗藏惊悚。适合悬疑、克苏鲁、志怪灵异与惊悚题材。'),
    'shiyi': ('诗意', '语言富于音乐性与意象美，重意境与情绪渲染；节奏舒缓，比喻空灵；近似散文诗，以景抒情、以物写心。适合文艺向、情感流与风景心境段落。'),
    'kouyu': ('口语化', '语言贴近日常口语，句式短、用词俗；可省主语、语序倒置、语气词丰富；叙述如说话，代入感强。适合都市生活、青春校园、第一人称与轻松吐槽向。'),
    'huangdan': ('荒诞', '以反逻辑与错位制造荒诞感，正经写荒唐、冷静写癫狂；语言可冷面幽默或黑色幽默；解构套路，预期违背生笑点。适合黑色幽默、讽刺、反套路与癫系创作。'),
}


def _build_chapter_lang_style_prompt(style_keys):
    """根据前端传入的语言风格 key 列表，拼装注入 system_prompt 的「本章语言风格」铁律约束。
    返回空串表示未选择（AI 按默认通用风格行文）。"""
    if not style_keys:
        return ''
    try:
        keys = style_keys if isinstance(style_keys, list) else json.loads(style_keys or '[]')
    except Exception:
        keys = []
    if not keys:
        return ''
    parts = []
    for k in keys[:3]:
        item = CHAPTER_LANG_STYLES.get(k)
        if item:
            parts.append(f"- {item[0]}：{item[1]}")
    if not parts:
        return ''
    return ('【本章语言风格·行文铁律】（用户为本章选定，必须严格遵循，不可偏离）\n'
            + '本章正文必须按以下风格基调行文（多种风格可融合但都要体现）：\n'
            + '\n'.join(parts)
            + '\n（此为硬约束：用词、句式、节奏、修辞须符合上述风格，违反即为不合格章节）')


def _build_master_upstream_ctx(dim, ctx):
    """按 DAG 依赖图只注入该维度的上游维度产物（不再无差别全注入）。
    自适应截断：世界观/人物档案给 2000 字预算（下游 timeline/dynamic_volumes/foreshadowing
    高度依赖完整设定，800字会丢失关键体系细节），其他维度 800 字。"""
    # 高预算维度：设定体量大且下游强依赖
    HIGH_BUDGET_DIMS = {'worldbuilding': 2000, 'character_profiles': 2000, 'key_rules': 1500}
    upstream_dims = MASTER_DIM_UPSTREAM.get(dim, [])
    parts = []
    for up_dim in upstream_dims:
        if up_dim not in ctx:
            continue
        up_val = ctx[up_dim]
        if up_val and up_val.strip():
            up_label = MASTER_DIM_MAP[up_dim]['label']
            budget = HIGH_BUDGET_DIMS.get(up_dim, 800)
            parts.append(f'【{up_label}（已确认上游）】\n{up_val[:budget]}')
    return '\n\n'.join(parts) if parts else '（暂无上游维度，自由发挥）'


def _build_master_storyline_ctx(book_id, bb):
    """构建"已写剧情"上下文：章节正文摘要 + 最近动态报告 + 伏笔图 + 关系图。
    让维度创作能感知已写章节的实际剧情，避免凭空设定与正文冲突。
    dynamic_volumes/character_profiles 等维度尤其需要，避免"幻觉式推测"。"""
    parts = []

    # 1. 最近章节正文摘要（取最近 6 章，每章前 300 字，避免 token 膨胀）
    try:
        recent_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index.desc()).limit(6).all()
        if recent_chs:
            recent_chs = list(reversed(recent_chs))  # 恢复正序
            ch_lines = []
            for c in recent_chs:
                title = c.title or f'第{c.order_index}章'
                content = (c.content or '')[:300].replace('\n', ' ').strip()
                if content:
                    ch_lines.append(f'- 第{c.order_index}章《{title}》：{content}')
            if ch_lines:
                parts.append('【已写章节摘要】（最近6章实际剧情，设定须与之一致，不可矛盾）\n' + '\n'.join(ch_lines))
    except Exception:
        pass

    # 2. 最近动态报告摘要（3 份，每份 300 字）
    try:
        reports = _collect_relevant_reports(book_id, current_chapter_num=999999, window=999999, max_reports=3, per_report_limit=300)
        if reports:
            rp_lines = []
            for r in reports:
                rp_lines.append(f'- {r["title"]}（{r["chapter_start"]}-{r["chapter_end"]}章）：{r["content"][:300]}')
            parts.append('【动态报告摘要】（已写剧情的阶段性归纳）\n' + '\n'.join(rp_lines))
    except Exception:
        pass

    # 3. 伏笔 DAG 摘要（待回收伏笔 top 8）
    try:
        if bb.foreshadowing_graph:
            graph = ForeshadowingGraph.from_dict(json.loads(bb.foreshadowing_graph))
            pending = graph.get_pending_nodes()
            if pending:
                fs_lines = []
                for node in pending[:8]:
                    desc = (node.content or node.title or '')[:80]
                    fs_lines.append(f'- {desc}')
                if fs_lines:
                    parts.append('【已埋伏笔清单】（已写剧情中埋设的伏笔，新设定须与之兼容）\n' + '\n'.join(fs_lines))
    except Exception:
        pass

    # 4. 人物关系图摘要（≤500 字）
    try:
        if bb.relation_graph and bb.relation_graph.strip():
            rg = bb.relation_graph[:500]
            parts.append('【人物关系图谱】（已确认的人物关系，新人物设定须兼容）\n' + rg)
    except Exception:
        pass

    # 5. 章级变更日志摘要（最近 12 章的关键 delta：境界/物品/伏笔/地点变化）
    # 章级粒度比动态报告更细，能补上"最近几章刚发生的境界提升/物品转移/伏笔回收"，
    # 避免维度创作与最新正文脱节（尤其 character_profiles/inventory/foreshadowing/dynamic_volumes）。
    try:
        log_list = json.loads(bb.chapter_changes_log) if bb.chapter_changes_log else []
        if isinstance(log_list, list) and log_list:
            recent_logs = log_list[-12:]  # 最近12章
            chg_lines = []
            for entry in recent_logs:
                if not isinstance(entry, dict):
                    continue
                ch_num = entry.get('chapter_num') or '?'
                chg = entry.get('changes') or {}
                if not isinstance(chg, dict):
                    continue
                segs = []
                # 角色境界/能力变化
                for c in (chg.get('CharacterStateChanges') or [])[:3]:
                    if isinstance(c, dict):
                        nm = c.get('CharacterId') or c.get('Name') or '?'
                        lvl = c.get('NewLevel') or ''
                        ke = c.get('KeyEvent') or ''
                        if lvl or ke:
                            segs.append(f'{nm}{"→"+lvl if lvl else ""}{":"+ke[:30] if ke else ""}')
                # 物品转移
                for it in (chg.get('ItemTransfers') or [])[:2]:
                    if isinstance(it, dict):
                        nm = it.get('ItemName') or it.get('ItemId') or '?'
                        fh = it.get('FromHolder') or ''
                        th = it.get('ToHolder') or ''
                        if th:
                            segs.append(f'{nm}:{fh or "?"}→{th}')
                # 伏笔 setup/payoff
                for fa in (chg.get('ForeshadowingActions') or [])[:2]:
                    if isinstance(fa, dict):
                        fid = fa.get('ForeshadowId') or '?'
                        act = fa.get('Action') or ''
                        if act:
                            segs.append(f'伏笔{fid}:{act}')
                # 地点状态
                for lc in (chg.get('LocationStateChanges') or [])[:2]:
                    if isinstance(lc, dict):
                        nm = lc.get('LocationId') or '?'
                        ns = lc.get('NewStatus') or ''
                        if ns:
                            segs.append(f'{nm}:{ns}')
                if segs:
                    chg_lines.append(f'- 第{ch_num}章：{"；".join(segs)}')
            if chg_lines:
                parts.append('【最近章级变更日志】（最近12章实际发生的境界/物品/伏笔/地点变化，新设定须承接，不可矛盾或回退）\n' + '\n'.join(chg_lines))
    except Exception:
        pass

    return '\n\n'.join(parts) if parts else ''


def _collect_anti_forget_alerts(bb, max_reports=3, max_alerts=12):
    """提取最近 N 份防遗忘检查报告的诊断要点（违规/待回收伏笔/叙事债务/改进建议）。
    用于在后续 AI 创作（章节写作/维度创作/一致性检查）中回注，让 AI 自动规避已诊断出的问题。
    返回字符串（已格式化，可直接拼入 prompt）；无报告时返回空字符串。"""
    if not bb or not bb.anti_forget_reports:
        return ''
    try:
        reports = json.loads(bb.anti_forget_reports) if bb.anti_forget_reports else []
    except (json.JSONDecodeError, ValueError, TypeError):
        return ''
    if not isinstance(reports, list) or not reports:
        return ''
    # 取最近 max_reports 份（按 checked_at 降序，兼容旧数据无 checked_at 的情况）
    def _ts(r):
        return r.get('checked_at', '') or ''
    recent = sorted([r for r in reports if isinstance(r, dict)], key=_ts, reverse=True)[:max_reports]
    if not recent:
        return ''

    sections = []
    for rec in recent:
        title = rec.get('title', '检查')
        report = rec.get('report') or {}
        # 不是 dict 的报告跳过细节提取
        if not isinstance(report, dict):
            report = {}
        parts = [f'■ {title}']
        summary = (report.get('summary') or rec.get('summary') or '').strip()
        if summary:
            parts.append(f'  摘要：{summary[:200]}')
        violations = report.get('violations') or []
        if isinstance(violations, list) and violations:
            v_lines = []
            for v in violations[:max_alerts]:
                if isinstance(v, dict):
                    msg = v.get('issue') or v.get('message') or v.get('desc') or v.get('description') or str(v)
                else:
                    msg = str(v)
                v_lines.append(f'  - {msg[:150]}')
            if v_lines:
                parts.append('  违规/不一致：\n' + '\n'.join(v_lines))
        pending = report.get('pending_foreshadowing') or []
        if isinstance(pending, list) and pending:
            p_lines = []
            for p in pending[:6]:
                if isinstance(p, dict):
                    msg = p.get('desc') or p.get('description') or p.get('title') or str(p)
                else:
                    msg = str(p)
                p_lines.append(f'  - {msg[:120]}')
            if p_lines:
                parts.append('  待回收伏笔：\n' + '\n'.join(p_lines))
        debts = report.get('narrative_debts') or report.get('debts') or []
        if isinstance(debts, list) and debts:
            d_lines = []
            for d in debts[:6]:
                if isinstance(d, dict):
                    msg = d.get('desc') or d.get('description') or d.get('title') or str(d)
                else:
                    msg = str(d)
                d_lines.append(f'  - {msg[:120]}')
            if d_lines:
                parts.append('  叙事债务：\n' + '\n'.join(d_lines))
        suggestions = report.get('suggestions') or report.get('improvements') or []
        if isinstance(suggestions, list) and suggestions:
            s_lines = []
            for s in suggestions[:6]:
                if isinstance(s, dict):
                    msg = s.get('suggestion') or s.get('desc') or s.get('description') or str(s)
                else:
                    msg = str(s)
                s_lines.append(f'  - {msg[:150]}')
            if s_lines:
                parts.append('  改进建议：\n' + '\n'.join(s_lines))
        if len(parts) > 1:  # 有实质内容才加入
            sections.append('\n'.join(parts))
    if not sections:
        return ''
    return '\n\n'.join(sections)


def _compute_dynamic_temperature(current_chapter_num, vol_chapter, vol_index, chapters_in_vol):
    """根据章节在卷中的位置动态计算 temperature。
    - 卷开篇（第1章）：0.8（需要更多创意建立场景）
    - 卷日常推进：0.7
    - 卷收尾（最后2章）：0.5（需要稳定收束）
    - 全书第一章：0.8"""
    if current_chapter_num == 1:
        return 0.8
    if vol_chapter and chapters_in_vol > 0:
        # 章节在卷内的相对位置
        pos_in_vol = current_chapter_num - vol_chapter.order_index + 1
        if pos_in_vol <= 1:
            return 0.8  # 卷开篇
        if pos_in_vol >= chapters_in_vol - 1:
            return 0.5  # 卷收尾
    return 0.7  # 日常推进


def _build_smart_instruction(instruction, last_chapter, current_chapter_num):
    """生成智能默认指令。若用户未提供 instruction，结合上一章章尾内容生成承接指令。
    【字数铁律】无论何种情况，user_prompt 末尾必须强制追加字数限定，确保 AI 在正文生成框明确看到 2400±100 约束。"""
    # 字数强制限定尾注（追加到任何指令后，确保字数约束出现在正文生成框）
    word_count_clause = f'请为「第{current_chapter_num}章」创作完整章节内容，要求上下文剧情连贯、对话自然、节奏紧凑、章末留悬念，字数2400±100字。'

    if instruction and instruction.strip():
        # 用户自定义指令：末尾追加字数限定，避免用户指令未提字数导致失控
        return f'{instruction.strip()}\n\n{word_count_clause}'
    if not last_chapter:
        return f'请继续写第 {current_chapter_num} 章（开篇章节，建立场景与基调）。\n\n{word_count_clause}'
    # 启发式提取上一章章尾钩子类型
    tail = (last_chapter.content or '')[-300:]
    hook_hint = '承接上一章章尾的悬念/钩子，自然展开新场景'
    if any(kw in tail for kw in ['？', '?', '究竟', '为何', '怎么']):
        hook_hint = '承接上一章末尾的疑问钩子，本章给出部分线索但不完全揭示'
    elif any(kw in tail for kw in ['危险', '危机', '攻击', '杀机', '威胁']):
        hook_hint = '承接上一章末尾的危机钩子，本章处理危机并展现主角应对'
    elif any(kw in tail for kw in ['发现', '出现', '现身', '传来']):
        hook_hint = '承接上一章末尾的新信息钩子，本章展开新信息的影响'
    return f'请继续写第 {current_chapter_num} 章。{hook_hint}。\n\n{word_count_clause}'


def _smart_truncate(text, budget):
    """语义截断：按段落/换行/句号切，保证每段完整，避免拦腰截断关键规则。
    优先级：段落(\n\n) > 换行(\n) > 中文句号(。！？) > 逗号(，；) > 硬截断。
    句号类分隔符截断后末尾补回标点，保证句子完整。"""
    if len(text) <= budget:
        return text
    # 句末标点：截断后需补回
    end_puncts = {'。', '！', '？', '；', '，'}
    for sep in ['\n\n', '\n', '。', '！', '？', '；', '，']:
        chunks = text.split(sep)
        if len(chunks) <= 1:
            continue
        result = []
        used = 0
        for i, ch in enumerate(chunks):
            # 加入此片段后的总长（含分隔符）
            piece_len = len(ch) + (len(sep) if i > 0 else 0)
            if used + piece_len > budget:
                break
            result.append(ch)
            used += piece_len
        if not result:
            continue
        out = sep.join(result)
        # 句末标点类：补回结尾标点，保证句子完整（如"第二条规则"→"第二条规则。"）
        if sep in end_puncts and out and not out.endswith(sep) and len(out) + len(sep) <= budget + len(sep):
            out = out + sep
        if out.strip():
            return out
    # 兜底硬截断
    return text[:budget]


def _apply_budget_management(sections_with_labels, total_budget=8000):
    """上下文窗口预算管理：按权重分配总预算给各段，避免单段超长挤掉关键信息。
    sections_with_labels: [(label, content, weight), ...]  weight 越大优先级越高
    返回拼接后的文本。截断采用语义截断（_smart_truncate），保证每段完整。"""
    if not sections_with_labels:
        return ''
    total_weight = sum(w for _, _, w in sections_with_labels)
    if total_weight <= 0:
        return '\n\n'.join(c for _, c, _ in sections_with_labels)
    parts = []
    for label, content, weight in sections_with_labels:
        if not content or not content.strip():
            continue
        budget = int(total_budget * (weight / total_weight))
        if len(content) > budget:
            content = _smart_truncate(content, budget)
        parts.append(content if not label else f'{label}\n{content}')
    return '\n\n'.join(parts)


def _generate_chapter_plan(book_id, bb, current_chapter_num, vol_chapter, vol_index,
                           memory_section, foreshadowing_section, skill_pack_ids,
                           api_key, base_url, model, max_tokens=600):
    """章节计划前置（chapter_plan Agent）：在写正文前生成 200 字以内的本章三段式计划。
    【P0弊端5修复】注入当前卷纲的 nodes 列表，让章纲对齐卷纲节点节奏。
    返回计划文本，失败时返回空串（不阻塞正文生成）。"""
    # 【三类无污染】chapter_plan 前置规划属于构思阶段：只注入构思类（master）技能包
    plan_skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['chapter_plan'], mode='single')
    vol_label = f'第{vol_index}卷「{vol_chapter.title}」' if vol_chapter else '当前卷'

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
                                                current_node_hint = f"""【本章所属节点】第{current_chapter_num}章对应节点「{n.get('title','')}」（{ch_range}章，类型{n.get('type','M')}）：{n.get('summary','')}
章纲必须围绕此节点的核心事件展开，不得偏离到其他节点。"""
                                                break
                                        elif len(nums) == 1:
                                            if int(nums[0]) == current_chapter_num:
                                                current_node_hint = f"""【本章所属节点】第{current_chapter_num}章对应节点「{n.get('title','')}」（类型{n.get('type','M')}）：{n.get('summary','')}"""
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

【输出格式】严格按以下三段式输出，每段不超过70字：
1. 本章核心冲突：
2. 关键场景/事件：
3. 章尾钩子设计：

【要求】
- 必须承接前文，不可矛盾
- 若有"待回收伏笔清单"，本章应考虑回收其中1条
- 【节点对齐铁律】若存在【本章所属节点】，章纲必须围绕该节点核心事件展开，不得偏离
- 【卷间衔接】若为卷首章节，必须承接上一卷卷尾钩子
- 只输出计划，不要解释"""

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
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
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
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


def _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num=None, prev_chapter_content=None, chapter_lang_styles=None, enable_structured_tags=True, skip_chapter_plan=False):
    """构建章节正文写作的完整上下文（被 ai_continue / ai_continue_stream / ai_continue_batch 共用）。
    返回 dict，包含 system_prompt, user_prompt, temperature, max_tokens, chapter_plan, api 信息等。

    修复上下文脱节：
    - target_chapter_num：前端传入的待写章号（基于 word_count>=100 已写判定，比后端 max+1 更准）
    - prev_chapter_content：上一章已生成但未保存的正文（重新生成/连续创作场景），
      会作为"最近一章"注入上下文，避免 AI 接不上刚写的内容。
    - chapter_lang_styles：本章语言风格（行文文风，最多3个），拼入 system_prompt 指导行文。
    - enable_structured_tags：是否注入 <pre_write_check> / <chapter_changes> 标签模板。
      流式模式传 False（避免内部标签实时显示给用户）；多Agent/连续模式传 True（内部处理后清洗）。
    - skip_chapter_plan：跳过 chapter_plan Agent（批处理流式模式用，避免同步 LLM 调用阻塞 20-60s
      期间生成器无法推送心跳，导致 Render 空闲超时 network error）。"""
    book = Book.query.get(book_id)
    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.model if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    # 识别/检查类任务（章节计划、一致性检查）用识别模型，为空时回退主模型
    recognition_model = config.get_model_for_task('recognition') if config else model
    if not base_url.endswith('/v1'):
        base_url = base_url.rstrip('/') + '/v1'

    # ===== 0. 章号计算（#9：改用 max(order_index)+1，健壮性）=====
    # 优先用前端传入的章号（前端基于 word_count>=100 已写判定，比后端 max+1 更准，
    # 能正确处理"已生成未保存"和"中间有空章"的情况，避免上下文脱节）
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
    # 【大纲】五幕式总纲（权重最高，对齐整体走向）
    if filtered_bible.get('plot_design'):
        bible_sections.append(('【大纲·五幕式总纲】（参考当前进度对齐整体走向）', filtered_bible['plot_design'], 3))
    # 【剧情·卷纲规划】改造：从全量 timeline 注入改为前一卷+本卷+后一卷三卷注入
    # 避免上下文膨胀且聚焦当前创作位置，前卷回顾+本卷进行+后卷走向确保连贯
    adjacent_outlines = _get_adjacent_volumes_outline(book_id, vol_index)
    if adjacent_outlines:
        bible_sections.append((adjacent_outlines.split('\n')[0], '\n'.join(adjacent_outlines.split('\n')[1:]).strip(), 3))
    # 【人物及关系】合并 character_profiles + relation_graph
    if filtered_bible.get('character_profiles'):
        bible_sections.append(('【人物档案】（保持人设一致）', filtered_bible['character_profiles'], 3))
    if bb.relation_graph and bb.relation_graph.strip():
        rg_text = bb.relation_graph[:1000]
        bible_sections.append(('【人物关系图谱】（保持关系一致性）', rg_text, 2))
    # 【物资库】角色持有物品/境界约束
    if bb.inventory and bb.inventory.strip():
        inv_text = bb.inventory[:1000]
        bible_sections.append(('【物资库·境界库】（角色持有物品约束，不可凭空获得）', inv_text, 2))
    # 【地图】地点信息
    if bb.locations and bb.locations.strip():
        loc_text = bb.locations[:1000]
        bible_sections.append(('【地图·地点信息·全局】（若与本卷地点冲突，以本卷为准）', loc_text, 1))
    # 核心规则/金手指（绝不可违反）
    if filtered_bible.get('key_rules'):
        bible_sections.append(('【核心规则/金手指】（绝不可违反）', filtered_bible['key_rules'], 3))
    # 世界观设定
    if filtered_bible.get('worldbuilding'):
        bible_sections.append(('【世界观设定】', filtered_bible['worldbuilding'], 2))
    # 核心构思
    if filtered_bible.get('concept'):
        bible_sections.append(('【核心构思】', filtered_bible['concept'], 1))
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

    # 修复上下文脱节：若前端传入了"上一章已生成未保存"的内容，把它作为最近一章追加注入。
    # 场景：用户生成第N章后没点保存就生成第N+1章，此时数据库里没有第N章，
    # 但 AI 写第N+1章必须知道第N章发生了什么，否则剧情断档。
    # 截断到 2400 字（单章篇幅），避免过长挤占预算。
    if prev_chapter_content and prev_chapter_content.strip():
        prev_trimmed = prev_chapter_content.strip()[:2400]
        prev_ch_num = current_chapter_num - 1
        prev_tag = '【上一章（已生成未保存，必须严格承接此章剧情）】'
        chapters_text = (chapters_text + '\n\n' if chapters_text and chapters_text != '（开篇第一章，无前文）' else '') + f'{prev_tag}\n【第{prev_ch_num}章】\n{prev_trimmed}'

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
    # 借鉴 PlotPilot 语义检索：TF-IDF 语义召回，补充字符匹配的盲区（捕捉同义词/语义关联）
    # 仅当字符召回不足 6 条时启用，避免重复
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
                    chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).filter(
                        Chapter.order_index < current_chapter_num
                    ).order_by(Chapter.order_index.desc()).limit(300).all()
                    return [{'chapter_num': c.order_index, 'title': c.title or '', 'summary': (getattr(c, 'summary', '') or '')} for c in chs]
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
        except Exception:
            pass  # 大纲/节拍加载失败退回无节拍模式

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
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'style')

    # ===== 7. 智能默认指令（#11）=====
    smart_instruction = _build_smart_instruction(instruction, last_chapter, current_chapter_num)

    # ===== 8. 组装 system_prompt =====
    # 【P1重构】bible设定（5000字预算）+ 前4章完整正文+动态报告（独立段，不挤占设定预算）
    # 注意：memory_section 含前4章完整正文（约9600字）+ 10份动态报告（约8000字），总量较大但独立注入
    # 【核心创作参数】卷数+风格流派，作为本章写作的核心依据（人物塑造/节奏/爽点须契合所选流派）
    core_params_block = _build_core_params_block(bb, book)
    # 【本章语言风格】用户为本章选定的行文文风（最多3个叠加），指导行文基调
    chapter_lang_style_block = _build_chapter_lang_style_prompt(chapter_lang_styles)
    system_prompt = f"""你是番茄小说金番作者级别的写手，正在协作写一本小说，当前准备写第 {current_chapter_num} 章。

{core_params_block}
{chapter_lang_style_block}

【设定权威分级·冲突仲裁规则】（P0-3）
当下方各层设定发生冲突时，按权威层级从高到低取信：
- direction 层（最高）：核心构思 concept —— 作者意图，不可被下层推翻
- foundation 层：大纲 plot_design、世界观 worldbuilding —— 全书骨架
- rules 层：核心规则 key_rules、伏笔 foreshadowing —— 不可违反的铁律
- runtime 层：动态文件 dynamic_volumes —— 当前卷的运行时状态
- memory 层（最低）：前4章正文 —— 即时剧情衔接依据
若卷纲与本卷动态文件冲突，以卷纲为准；若动态文件与前4章正文冲突，以动态文件为准。

【项目宪法 - 已确认设定】（必须严格遵守，不可矛盾。包含：大纲/剧情卷纲/人物及关系/物资库/地图/核心规则/世界观/伏笔等）
{bible_context[:5000]}

{memory_section}
{dynamic_memory_section}

{foreshadowing_section}

{af_section}

{beat_section}

{plan_section}

{skill_note}

【写作要求】
1. 严格遵循项目宪法中的设定（核心规则/金手指/世界观/人设/物资库/地图），不可违反
2. 保持前后人物性格、关系、能力一致；严格衔接【最近4章完整正文】的剧情走向，不得跳跃或矛盾
3. 延续现有文风和叙事节奏
4. 【字数绝对铁律】每章正文必须 2400 字 ±100（即 2300-2500 字区间，含标点）。低于 2300 字=内容不足需扩展场景细节；超过 2500 字=冗余需删减。这是不可违反的硬约束，优先级高于所有其他要求。
5. 主动考虑回收"待回收伏笔清单"中的伏笔（若有），避免长线遗忘
6. 三明治结构：苦(困境)→甜(获得)→爽(反击)→钩子(新信息/新困境)
7. 章尾必留钩子，七种类型不重复
8. 若存在【本章计划】，必须严格按计划展开剧情
9. 【剧情连贯铁律】必须严格承接【最近4章完整正文】的结尾场景与悬念，不得凭空开启新场景；人物位置、状态、对话内容必须与前文一致。
10. 【字数自检】输出前必须自检字数：用中文计数（含标点），若不在 2300-2500 区间必须调整后再输出。
11. 【防遗忘规避】若上方存在【防遗忘检查诊断】，必须主动规避其中列出的一致性违规，并优先回收/修正已诊断的待回收伏笔与叙事债务，不可重复犯错。
12. 【OOC 专项检测】注意角色人设一致性，不要OOC（Out of Character）。角色行为必须符合人物设定：说话语气、行为模式、决策逻辑、情绪反应都要与人设档案一致。禁止角色突然性格大变（如沉稳角色突然话多、冷酷角色突然唠叨、谨慎角色突然鲁莽）。"""

    # P2-11：在 system_prompt 末尾追加 PRE_WRITE_CHECK 模板（要求 LLM 写正文前先输出意图表）
    # 流式模式不注入（enable_structured_tags=False），避免内部标签实时显示给用户
    if enable_structured_tags and build_pre_write_check_prompt:
        system_prompt += build_pre_write_check_prompt(current_chapter_num, bb)

    # P1-6：在 system_prompt 末尾追加 CHANGES 输出模板（要求 LLM 输出结构化状态变更）
    if enable_structured_tags and build_changes_prompt_template:
        system_prompt += build_changes_prompt_template()

    # 标题自动生成：要求 LLM 在正文末尾输出 JSON 标题，供后端解析回填章节标题
    system_prompt += '\n\n【标题输出】在正文最末尾另起一行，输出一个 JSON 对象，格式：{"title": "标题文本"}。标题 8-16 字，概括本章核心冲突或转折，不使用"第X章"前缀，需贴合正文内容，避免剧透关键悬念。只输出 JSON，不要输出其他内容。'

    # ===== 【标准文风铁律】统一注入（三种创作模式共用此 context，确保输出风格一致）=====
    # 技能包提示词和章节语言风格已在上文叠加，此处标准文风作为不可违反的核心约束，
    # 优先级最高；技能包/语言风格仅做题材向微调，不得覆盖核心长短句比例与禁词清单。
    system_prompt += '\n\n【字数铁律】输出必须 2400±100 字（2300-2500字区间，含标点）。\n\n' + STANDARD_WRITING_STYLE_PROMPT

    # ===== 9. 动态 temperature（#10）=====
    temperature = _compute_dynamic_temperature(current_chapter_num, vol_chapter, vol_index, chapters_in_vol)

    # ===== 10. 组装 user_prompt（第6142行返回引用，必须在此定义）=====
    user_prompt = instruction or f'请写第 {current_chapter_num} 章正文，严格遵循上方设定与计划。'

    return {
        'system_prompt': system_prompt,
        'user_prompt': user_prompt,
        'temperature': temperature,
        'max_tokens': 12000,  # 【字数铁律】给足输出空间不物理截断；含 PRE_WRITE_CHECK 13行表 + CHANGES JSON 12字段，12000 token 确保完整
        'chapter_plan': chapter_plan,
        'current_chapter_num': current_chapter_num,
        'vol_chapter': vol_chapter,
        'vol_index': vol_index,
        'api_key': api_key,
        'base_url': base_url,
        'model': model,
        'recognition_model': recognition_model,  # 识别/检查类任务用
    }


@app.route('/api/books/<book_id>/ai-continue', methods=['POST'])
@login_required
def ai_continue(book_id):
    """正文滚动创作（多 Agent 协同版，14项优化）：
    1. 分层注入 bible 上下文（key_rules/worldbuilding/character_profiles/plot_design/timeline/concept）+ 按卷维度数据 + 卷纲对齐
    2. 分层滚动记忆（即时层最近1章 + 近期层动态报告按当前章号±10窗口筛选）防遗忘
    3. 伏笔防遗忘：按到期紧迫度排序，注入 Top 25 待回收伏笔清单（百万字长线加权）
    4. 章节计划前置（chapter_plan Agent）：先生成200字计划，再写正文
    5. 正文生成（动态 temperature + 智能 instruction）
    6. 去 AI 味审校 Agent：容错+可观测（deai_status）
    7. 一致性检查 Agent：检查是否违反 key_rules/人设
    优化项：#1按卷注入 #2伏笔紧迫度 #3卷目标对齐 #4章节计划前置 #5相关性筛选
           #6审校容错 #7动态报告相关性 #10动态temperature #11智能instruction #12 max_tokens
           #13多Agent协同 #14预算管理 #9章号健壮性"""
    book = Book.query.get(book_id)
    if not book: return jsonify({'error': 'Not found'}), 404

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    instruction = request.json.get('instruction', '')
    skill_pack_ids = request.json.get('skill_pack_ids', [])
    enable_consistency_check = request.json.get('enable_consistency_check', False)  # 默认关闭（OOC检测已合并到system_prompt，不再单独调LLM）
    # 修复上下文脱节：接收前端传入的待写章号 + 上一章未保存内容
    target_chapter_num = request.json.get('target_chapter_num')
    prev_chapter_content = request.json.get('prev_chapter_content')
    # 章节正文语言风格（行文文风，最多3个叠加）
    chapter_lang_styles = request.json.get('chapter_lang_styles', [])

    try:
        ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num, prev_chapter_content, chapter_lang_styles)
        system_prompt = ctx['system_prompt']
        user_prompt = ctx['user_prompt']
        temperature = ctx['temperature']
        max_tokens = ctx['max_tokens']
        api_key = ctx['api_key']
        base_url = ctx['base_url']
        model = ctx['model']

        # ===== 正文生成（含容错：LLM 异常时返回友好错误而非 500）=====
        try:
            resp = requests.post(f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={'model': model, 'messages': [{'role':'system','content':system_prompt},{'role':'user','content':user_prompt}],
                      'temperature': temperature, 'max_tokens': max_tokens},
                timeout=180)
            result = resp.json()
            draft_content = result['choices'][0]['message']['content']
        except requests.exceptions.Timeout:
            return jsonify({'error': '正文生成超时（180秒），请检查网络或换用更快的模型后重试'}), 504
        except (KeyError, IndexError) as e:
            return jsonify({'error': f'LLM 返回格式异常：{str(e)}，请重试或检查 API 配置'}), 502
        except Exception as e:
            return jsonify({'error': f'正文生成失败：{str(e)}'}), 500

        # ===== 【字数铁律】初稿字数校验 + AI 重写修正（非物理截断，保证章节完整）=====
        # 统计中文字数（含标点）：去除空白后的字符数
        def _count_cn_chars(s):
            return len(re.sub(r'\s', '', s))
        draft_len = _count_cn_chars(draft_content)
        review_notes_prefix = ''
        if draft_len > 2500 or draft_len < 2300:
            # 字数不达标：调用 AI 重写至 2400±100，保留剧情完整性（不物理截断）
            direction = '精简删减冗余' if draft_len > 2500 else '扩写补充场景细节'
            method = ('精简方法：删冗余形容词/重复心理描写/过度环境渲染/总结性句子，保留对话动作与剧情走向。'
                      if draft_len > 2500 else
                      '扩写方法：增加感官细节/动作描写/对话节拍/场景纵深，不增加新剧情不改变走向。')
            rewrite_system = f"""你是字数修正编辑。当前章节初稿{draft_len}字，需{direction}至 2400字±100（2300-2500字区间，含标点）。

【字数绝对铁律】最终输出必须落在 2300-2500 字区间，这是不可违反的硬约束，优先级高于一切。
【保留要求】保留原章节的剧情走向、人物对话、章尾钩子、关键信息，不改变故事内容，只调整篇幅。
{method}
只输出修正后的完整正文，不输出任何说明或前缀。"""
            try:
                rewrite_resp = requests.post(f'{base_url}/chat/completions',
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json={'model': model,
                          'messages': [{'role':'system','content':rewrite_system},
                                       {'role':'user','content':f'请修正以下章节正文字数：\n\n{draft_content}'}],
                          'temperature': 0.5, 'max_tokens': 12000},
                    timeout=180)
                rewrite_result = rewrite_resp.json()
                rewritten = rewrite_result['choices'][0]['message']['content'].strip()
                rewritten_len = _count_cn_chars(rewritten)
                if rewritten and 2300 <= rewritten_len <= 2500:
                    review_notes_prefix = f'[字数铁律] 初稿{draft_len}字，AI修正至{rewritten_len}字。'
                    draft_content = rewritten
                elif rewritten and rewritten_len > 500:
                    # AI 修正后仍不达标，但比初稿更接近目标则采纳更优者，保留章节完整性
                    if abs(rewritten_len - 2400) < abs(draft_len - 2400):
                        draft_content = rewritten
                        review_notes_prefix = f'[字数铁律] 初稿{draft_len}字，AI修正后{rewritten_len}字仍偏离，已采纳更接近目标版本。'
                    else:
                        review_notes_prefix = f'[字数铁律] 初稿{draft_len}字，AI修正后{rewritten_len}字仍偏离，保留初稿。'
                else:
                    review_notes_prefix = f'[字数铁律] 初稿{draft_len}字，AI修正返回异常，保留初稿。'
            except Exception as e:
                review_notes_prefix = f'[字数铁律] 初稿{draft_len}字，AI修正异常：{str(e)[:80]}，保留初稿。'

        # ===== 去 AI 味审校 Agent（#6：容错+可观测）=====
        polished_content = draft_content
        review_notes = review_notes_prefix
        deai_status = 'skipped'  # skipped / success / failed
        if skill_pack_ids:
            # 【三类无污染】去AI味属于审查阶段：只注入审查类（review）技能包
            deai_skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'review', ['tomato_deai', 'de_ai_flavor', 'polish'], mode='agent')
            if deai_skill_note:
                deai_system = f"""你是番茄去AI味审查员。对以下刚写好的章节正文做去AI味审校，按规则修改后只输出修改后的正文。

{deai_skill_note}

【优先级铁律】人味>克制>流畅。删完AI味后读起来像机器人汇报→加口语碎片。太啰嗦→删修饰。磕磕绊绊→调句式。
【必删清单】一股/一抹/不由得/不禁/随即/旋即/与此同时/颇为/甚为/极为/缓缓/淡淡/轻轻/微微/毫无疑问/毋庸置疑/不言而喻/深吸一口气/眼中闪过一丝/心中暗想/心念电转/若有所思/不知不觉间/转眼间/恍然大悟/面无表情/淡漠/漠然/眸子/嘴角微微上扬/如同/宛如/犹如/周身/周遭/气息/威压/那道身影/说话间/话音未落/当即/顿时/瞬时。
【人味注入】加入不完美细节(结巴/重复/打断)/感官碎片/小动作微表情/语气词和断句/适当留白。
【硬性约束】修改后字数仍须 2400±100，保留原章节的剧情走向和钩子，只改文风不改剧情。"""

                try:
                    deai_resp = requests.post(f'{base_url}/chat/completions',
                        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                        json={'model': model,
                              'messages': [{'role':'system','content':deai_system},
                                           {'role':'user','content':f'请审校以下章节正文：\n\n{draft_content}'}],
                              'temperature': 0.5, 'max_tokens': max_tokens},
                        timeout=180)
                    deai_result = deai_resp.json()
                    polished = deai_result['choices'][0]['message']['content'].strip()
                    # 【字数铁律】审校后字数校验：必须落在 2300-2500 区间
                    polished_len = _count_cn_chars(polished)
                    if polished and 2300 <= polished_len <= 2500:
                        polished_content = polished
                        review_notes = (review_notes_prefix + ' 已自动去AI味审校(' + str(polished_len) + '字)').strip()
                        deai_status = 'success'
                    elif polished and polished_len > 500:
                        # 字数不达标但有内容，标记为失败但仍返回初稿
                        review_notes = (review_notes_prefix + f' 去AI味审校返回字数异常({polished_len}字)，已回滚使用初稿').strip()
                        deai_status = 'failed'
                    else:
                        review_notes = (review_notes_prefix + ' 去AI味审校返回为空，已回滚使用初稿').strip()
                        deai_status = 'failed'
                except Exception as e:
                    review_notes = (review_notes_prefix + f' 去AI味审校异常：{str(e)[:100]}，已回滚使用初稿').strip()
                    deai_status = 'failed'

        # ===== 一致性检查 Agent（#13：独立 Agent，P1扩展：含 chapter_plan 比对）=====
        # 一致性检查属识别/检查类任务，用识别模型
        consistency_passed = True
        consistency_issues = ''
        if enable_consistency_check:
            consistency_passed, consistency_issues = _consistency_check(
                book_id, bb, polished_content, ctx['current_chapter_num'],
                api_key, base_url, ctx.get('recognition_model', model), max_tokens=800,
                chapter_plan=ctx.get('chapter_plan', '')
            )

        # ===== P1-6 + P1-7：CHANGES 解析 + delta 回写（非流式版）=====
        changes_applied = None
        if extract_changes and apply_chapter_changes:
            try:
                body, changes = extract_changes(polished_content)
                if changes:
                    # 剥离 CHANGES 标签后的正文（落库用纯正文）
                    if body and len(body) > 200:
                        polished_content = body
                    ch_obj = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index.desc()).first()
                    ch_id = ch_obj.id if ch_obj else ''
                    changes_applied = apply_chapter_changes(
                        bb, ch_id, ctx['current_chapter_num'],
                        ctx.get('vol_index', 0), changes,
                    )
                    if changes_applied.get('applied'):
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
            except Exception:
                pass  # 回写失败不阻断

        # ===== P0-1：确定性后写校验（非流式版，零 LLM 成本）=====
        # P2 扩展：注入 bb 上下文，启用死亡角色复活/境界回退/角色名错写硬伤检测
        # P3 补全：bible_ctx 覆盖全维度，启用境界表动态解析（依赖 key_rules/worldbuilding）、
        # 地点/物资一致性参照（inventory/locations）
        post_validate = None
        if validate_chapter_with_bible:
            try:
                body_for_check = _extract_chapter_body(polished_content)
                bible_ctx = {
                    'character_profiles': bb.character_profiles or '',
                    'chapter_changes_log': bb.chapter_changes_log or '',
                    'key_rules': bb.key_rules or '',
                    'worldbuilding': bb.worldbuilding or '',
                    'inventory': bb.inventory or '',
                    'locations': bb.locations or '',
                    'foreshadowing': bb.foreshadowing or '',
                } if bb else None
                validation = validate_chapter_with_bible(body_for_check, bible_ctx)
                if validation.issues:
                    post_validate = validation.to_dict()
            except Exception:
                pass
        elif validate_chapter:
            try:
                body_for_check = _extract_chapter_body(polished_content)
                # 借鉴 PlotPilot 文风漂移检测：传入前5章文风基准
                style_baseline = _compute_style_baseline(book_id, ctx['current_chapter_num'])
                if validate_chapter_with_drift and style_baseline:
                    validation = validate_chapter_with_drift(body_for_check, style_baseline)
                else:
                    validation = validate_chapter(body_for_check)
                if validation.issues:
                    post_validate = validation.to_dict()
            except Exception:
                pass

        # ===== P2-10：落地门禁（3道，章节落库前拦截）=====
        gate_result = None
        if run_all_gates:
            try:
                gate_result = run_all_gates(polished_content, bb, ctx['current_chapter_num'])
                if not gate_result.get('passed'):
                    # 门禁未通过只做 warning，不阻断返回（避免误杀）
                    pass
            except Exception:
                pass

        # ===== P1-5：审计-修订闭环（draft→校验→修订→再校验，best snapshot 持久化）=====
        # 仅当后写校验发现问题且分数不达标时触发，最多2轮，best snapshot 取最高分版本
        # 合并 P2-6：local 类问题用 spot_fix（省 token），structural 类才整章重写
        review_cycle_result = None
        if (run_review_cycle and validate_chapter_with_bible and bb
                and post_validate and post_validate.get('issues')):
            try:
                # 1. 构建 validate_fn：复用 validate_chapter_with_bible，注入全维度 bible_ctx
                def _validate_fn(content, _bb=bb):
                    body = _extract_chapter_body(content)
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

                # 2. 构建 revise_fn：auto 模式路由，local→spot_fix / structural→整章重写
                def _revise_fn(content, validation, llm_call_fn, _bb=bb, _api_key=api_key,
                               _base_url=base_url, _model=model):
                    if not route_revision:
                        return content
                    v_dict = validation.to_dict() if validation else {}
                    routing = route_revision(content, v_dict, mode='auto')
                    if routing['strategy'] == 'none' or routing['strategy'] == 'rewrite':
                        # structural 类问题：调用整章重写（复用 LLM call）
                        rewrite_sys = ("你是小说修订专家。根据校验问题整章重写，保留原剧情走向与人物对话，"
                                       "只修正结构性问题（OOC/剧情偏离/伏笔遗漏/一致性异常）。"
                                       "只输出修订后的完整正文。")
                        issues_text = '; '.join(v_dict.get('issues', [])[:5]) if isinstance(v_dict.get('issues'), list) else str(v_dict.get('issues', ''))[:500]
                        rewrite_user = f'【校验问题】\n{issues_text}\n\n【原文】\n{content}'
                        return llm_call_fn(rewrite_sys, rewrite_user)

                    # local 类问题：spot_fix，只送问题段落给 LLM
                    patches = routing['patches']
                    if not patches:
                        return content
                    sys_prompt, user_prompt = build_spot_fix_prompt(content, patches)
                    llm_output = llm_call_fn(sys_prompt, user_prompt)
                    return apply_spot_fix_patches(content, patches, llm_output)

                # 3. 构建 llm_call_fn：封装 requests.post
                def _llm_call_fn(sys_prompt, user_prompt, _api_key=api_key, _base_url=base_url, _model=model):
                    resp = requests.post(f'{_base_url}/chat/completions',
                        headers={'Authorization': f'Bearer {_api_key}', 'Content-Type': 'application/json'},
                        json={'model': _model,
                              'messages': [{'role': 'system', 'content': sys_prompt},
                                           {'role': 'user', 'content': user_prompt}],
                              'temperature': 0.3, 'max_tokens': 12000},
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
                # 5. best snapshot 落地：若最终内容优于初稿，替换 polished_content
                if cycle_outcome.get('final_content') and cycle_outcome.get('best_score', 0) > 0:
                    final_content = cycle_outcome['final_content']
                    # 仅当 final 与 polished 不同时才替换（避免无谓覆盖）
                    if final_content != polished_content:
                        polished_content = final_content
                    review_cycle_result = {
                        'best_score': cycle_outcome.get('best_score'),
                        'rounds': cycle_outcome.get('rounds'),
                        'passed': cycle_outcome.get('passed'),
                        'reason': cycle_outcome.get('reason'),
                        'history': cycle_outcome.get('history'),
                    }
                    # best snapshot 持久化到对应 chapter（若已落库）
                    try:
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
                                'chapter_num': ctx['current_chapter_num'],
                                'best_score': cycle_outcome.get('best_score'),
                                'rounds': cycle_outcome.get('rounds'),
                                'passed': cycle_outcome.get('passed'),
                                'timestamp': datetime.now(timezone.utc).isoformat(),
                            })
                            # 仅保留最近 20 条快照
                            ch_for_snapshot.review_snapshots = json.dumps(snapshots[-20:], ensure_ascii=False)
                            db.session.commit()
                    except Exception:
                        db.session.rollback()
            except Exception:
                # 闭环失败不阻断章节生成
                db.session.rollback()

        # ===== 审校评分制：聚合 4 套检测结果计算 0-100 分（零 LLM 成本）=====
        try:
            polished_wc = len(re.sub(r'\s', '', polished_content or ''))
            chapter_score = _calc_chapter_score(
                post_validate, consistency_passed, consistency_issues,
                gate_result, polished_wc, ctx.get('chapter_plan', ''))
        except Exception:
            chapter_score = None

        # ===== 标题自动生成：解析【标题】标签，剥离正文中的标签行 =====
        suggested_title = _extract_chapter_title(draft_content)
        # 统一标题格式：第X章 标题文本（与连续创作模式一致，混用模式时格式统一）
        formatted_title = _format_chapter_title(ctx['current_chapter_num'], suggested_title)
        # ★ 统一清洗：无论 changes 是否解析成功，都剥离所有内部标签（pre_write_check / chapter_changes / 标题JSON / 【标题】行）
        # 确保返回给前端的 content 是纯净正文，避免内部产物泄露给用户
        polished_content = _extract_chapter_body(polished_content)
        polished_content = re.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

        return jsonify({
            'content': polished_content,
            'draft': draft_content if deai_status == 'success' else None,
            'review_notes': review_notes,
            'deai_status': deai_status,  # #6：新增可观测字段
            'chapter_plan': ctx.get('chapter_plan', ''),  # #4：返回计划供前端展示
            'current_chapter_num': ctx['current_chapter_num'],
            'vol_index': ctx.get('vol_index', 0),
            'vol_title': ctx['vol_chapter'].title if ctx.get('vol_chapter') else '',
            'temperature': temperature,  # #10：返回实际使用的 temperature
            'consistency_passed': consistency_passed,  # #13：一致性检查结果
            'consistency_issues': consistency_issues,
            # P0-1 + P1-6/7 新增字段
            'post_validate': post_validate,  # 后写校验报告（AI痕迹检测）
            'changes_applied': changes_applied,  # 章级变更回写摘要
            # P2-10 新增
            'gate_result': gate_result,  # 落地门禁结果
            # P1-5 新增
            'review_cycle': review_cycle_result,  # 审计-修订闭环结果
            # 审校评分制新增
            'chapter_score': chapter_score,  # 0-100 评分 + 等级 + 5维明细 + auto_revise
            # 标题自动生成新增
            'suggested_title': suggested_title,  # 纯标题文本（如"小镇少年"）
            'formatted_title': formatted_title,  # 统一格式标题（如"第1章 小镇少年"），前端直接使用
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/books/<book_id>/ai-spot-fix', methods=['POST'])
@login_required
def ai_spot_fix(book_id):
    """Spot-Fix 修订端点（P2-9）。
    按校验问题路由：local 类只修补问题段落（省 token），structural 类建议整章重写。
    前端"一键修订"按钮调用。"""
    if not route_revision:
        return jsonify({'error': '修订模块未加载'}), 500
    data = request.get_json() or {}
    content = data.get('content', '')
    validation = data.get('post_validate', {})
    mode = data.get('mode', 'auto')  # auto / spot_fix / rewrite
    if not content:
        return jsonify({'error': '缺少正文内容'}), 400

    # 路由修订策略
    routing = route_revision(content, validation, mode=mode)

    if routing['strategy'] == 'none':
        return jsonify({
            'strategy': 'none',
            'message': '未检测到需要修订的问题',
            'content': content,
        })

    if routing['strategy'] == 'rewrite':
        # structural 问题，建议整章重写（前端可调用 ai-continue）
        return jsonify({
            'strategy': 'rewrite',
            'message': '检测到结构性问题，建议整章重写',
            'structural_issues': routing['structural_issues'],
            'content': content,
        })

    # spot_fix 策略：只送问题段落给 LLM
    patches = routing['patches']
    if not patches:
        return jsonify({'strategy': 'none', 'message': '无法定位问题段落', 'content': content})

    # 构建 Spot-Fix prompt
    sys_prompt, user_prompt = build_spot_fix_prompt(content, patches)
    token_saving = estimate_token_saving(content, patches)

    # 调用 LLM 修订
    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('creation') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')
    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    try:
        resp = requests.post(f'{base_url}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model,
                  'messages': [{'role': 'system', 'content': sys_prompt},
                               {'role': 'user', 'content': user_prompt}],
                  'temperature': 0.3,
                  'max_tokens': 2000},
            timeout=60)
        result = resp.json()
        llm_output = result['choices'][0]['message']['content'].strip()

        # patch 回原文
        revised_content = apply_spot_fix_patches(content, patches, llm_output)

        # 对修订后的内容再跑一次后写校验
        post_validate = None
        if validate_chapter:
            try:
                body_for_check = _extract_chapter_body(revised_content)
                validation2 = validate_chapter(body_for_check)
                if validation2.issues:
                    post_validate = validation2.to_dict()
            except Exception:
                pass

        return jsonify({
            'strategy': 'spot_fix',
            'content': revised_content,
            'patches_count': len(patches),
            'token_saving': token_saving,
            'post_validate': post_validate,  # 修订后的校验报告
        })
    except Exception as e:
        return jsonify({'error': f'修订失败：{str(e)[:200]}'}), 500


@app.route('/api/books/<book_id>/ai-continue/stream', methods=['POST'])
@login_required
def ai_continue_stream(book_id):
    """正文滚动创作流式版（#8：SSE 推送初稿）。
    前端可通过 EventSource 接收 chunks，审校与一致性检查在流结束后由前端单独触发或忽略。
    返回 text/event-stream，每个 chunk 形如 data: {"content": "..."}\n\n"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    instruction = request.json.get('instruction', '')
    skill_pack_ids = request.json.get('skill_pack_ids', [])
    # 修复上下文脱节：接收前端传入的待写章号 + 上一章未保存内容
    target_chapter_num = request.json.get('target_chapter_num')
    prev_chapter_content = request.json.get('prev_chapter_content')
    # 章节正文语言风格（行文文风，最多3个叠加）
    chapter_lang_styles = request.json.get('chapter_lang_styles', [])

    ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids, target_chapter_num, prev_chapter_content, chapter_lang_styles, enable_structured_tags=False)
    api_key = ctx['api_key']
    base_url = ctx['base_url']
    model = ctx['model']

    def generate():
        try:
            # 先推送元信息（计划、章号、卷信息、temperature）
            meta = {
                'meta': True,
                'chapter_plan': ctx.get('chapter_plan', ''),
                'current_chapter_num': ctx['current_chapter_num'],
                'vol_index': ctx.get('vol_index', 0),
                'vol_title': ctx['vol_chapter'].title if ctx.get('vol_chapter') else '',
                'temperature': ctx['temperature'],
            }
            yield f'data: {json.dumps(meta, ensure_ascii=False)}\n\n'

            # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
            system_prompt = ctx['system_prompt']

            # 流式生成正文初稿，同时收集完整内容用于后写校验（P0-1）
            full_content_parts = []
            resp = requests.post(f'{base_url}/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={'model': model,
                      'messages': [{'role': 'system', 'content': system_prompt},
                                   {'role': 'user', 'content': ctx['user_prompt']}],
                      'temperature': ctx['temperature'],
                      'max_tokens': ctx['max_tokens'],
                      'stream': True},
                stream=True, timeout=180)
            for line in resp.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        chunk = line[6:]
                        if chunk == '[DONE]':
                            yield 'data: [DONE]\n\n'
                            break
                        # 收集内容用于后写校验
                        try:
                            chunk_data = json.loads(chunk)
                            delta = chunk_data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                            if delta:
                                full_content_parts.append(delta)
                        except Exception:
                            pass
                        yield f'data: {chunk}\n\n'

            # ===== P1-6 + P1-7：CHANGES 解析 + delta 回写（流结束后）=====
            # 解析 LLM 输出的 12 类变更声明，delta patch 到 dynamic_volumes/foreshadowing_graph
            if extract_changes and apply_chapter_changes and full_content_parts:
                try:
                    full_content_for_changes = ''.join(full_content_parts)
                    body, changes = extract_changes(full_content_for_changes)
                    if changes:
                        bb_obj = BookBible.query.filter_by(book_id=book_id).first()
                        if bb_obj:
                            ch_obj = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index.desc()).first()
                            ch_id = ch_obj.id if ch_obj else ''
                            apply_summary = apply_chapter_changes(
                                bb_obj, ch_id, ctx['current_chapter_num'],
                                ctx.get('vol_index', 0), changes,
                            )
                            if apply_summary.get('applied'):
                                try:
                                    db.session.commit()
                                except Exception:
                                    db.session.rollback()
                                # 推送回写摘要给前端
                                yield f'data: {json.dumps({"changes_applied": apply_summary}, ensure_ascii=False)}\n\n'
                except Exception as ce:
                    # 回写失败不阻断章节生成
                    pass

            # ===== P0-1：确定性后写校验（流结束后，零 LLM 成本）=====
            # 推送校验报告给前端，critical 级问题提示作者可一键修订
            # P2 扩展：注入 bb 上下文，启用死亡角色复活/境界回退/角色名错写硬伤检测
            _validator = validate_chapter_with_bible or validate_chapter
            if _validator and full_content_parts:
                try:
                    full_content = ''.join(full_content_parts)
                    # 剥离可能的 PRE_WRITE_CHECK 和 CHANGES 标签（P1-6 产物），只校验正文
                    body_for_check = _extract_chapter_body(full_content)
                    bible_ctx = None
                    if validate_chapter_with_bible and bb:
                        bible_ctx = {
                            'character_profiles': bb.character_profiles or '',
                            'chapter_changes_log': bb.chapter_changes_log or '',
                            'key_rules': bb.key_rules or '',
                            'worldbuilding': bb.worldbuilding or '',
                            'inventory': bb.inventory or '',
                            'locations': bb.locations or '',
                            'foreshadowing': bb.foreshadowing or '',
                        }
                    validation = _validator(body_for_check, bible_ctx) if bible_ctx else _validator(body_for_check)
                    if validation.issues:
                        yield f'data: {json.dumps({"post_validate": validation.to_dict()}, ensure_ascii=False)}\n\n'
                except Exception as ve:
                    # 校验失败不阻断章节生成
                    pass

            # ===== 标题自动生成：解析【标题】标签并推送给前端 =====
            # 统一推送格式化后的标题（第X章 标题），与多Agent同步/连续创作模式一致
            if full_content_parts:
                try:
                    full_content_for_title = ''.join(full_content_parts)
                    suggested_title = _extract_chapter_title(full_content_for_title)
                    formatted_title = _format_chapter_title(ctx['current_chapter_num'], suggested_title)
                    # 同时推送纯标题和格式化标题，前端优先用 formatted_title
                    yield f'data: {json.dumps({"suggested_title": suggested_title, "formatted_title": formatted_title}, ensure_ascii=False)}\n\n'
                except Exception:
                    pass
        except Exception as e:
            yield f'data: {{"error": "{str(e)[:200]}"}}\n\n'

    return app.response_class(generate(), mimetype='text/event-stream')


@app.route('/api/books/<book_id>/ai-continue-batch', methods=['POST'])
@login_required
def ai_continue_batch(book_id):
    """连续创作模式（优化版）：批量生成 N 章，每章独立生成，普通 JSON 响应。
    每章只调 1 次 LLM，把字数修正和去AI味要求合并到 system_prompt 里。
    去掉 prev_content 注入（每章独立生成，避免上下文污染）。
    保留标题解析和一致性检查（但一致性检查默认关闭）。
    请求参数：{ instruction, skill_pack_ids, chapter_lang_styles, count(1-10), start_chapter_num? }"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    instruction = data.get('instruction', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    chapter_lang_styles = data.get('chapter_lang_styles', [])
    count = max(1, min(10, int(data.get('count', 3))))  # 1-10 章
    start_chapter_num = data.get('start_chapter_num')

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    import re as _re_batch
    results = []
    failed = []  # Bug2 修复：记录失败章节，避免静默吞错导致剧情断档
    prev_polished = ''  # Bug5 修复：缓存上一章已生成正文，传递给下一章避免断档

    for i in range(count):
        try:
            # 章号：首章用 start_chapter_num 或自动计算，后续递增
            target_num = start_chapter_num + i if start_chapter_num else None
            # Bug5 修复：传递上一章已生成正文（数据库尚未保存或刚保存的场景都能承接剧情）
            ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids,
                                              target_num, prev_polished or None, chapter_lang_styles)
            api_key = ctx['api_key']
            base_url = ctx['base_url']
            model = ctx['model']
            cur_ch = ctx['current_chapter_num']

            # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
            # 此处仅追加连续创作专属的去AI味补充约束，不重复注入标准文风
            system_prompt = ctx['system_prompt']
            system_prompt += '\n\n【去AI味补充】禁止"仿佛""似乎"等比喻堆砌，禁止工整过渡，禁止完美套话。人物必须有矛盾心理/纠结/自我怀疑，禁止情绪顺滑。'

            # Bug3 修复：LLM 调用添加状态码与结构检查，避免 KeyError 静默失败
            try:
                resp = requests.post(f'{base_url}/chat/completions',
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json={'model': model, 'messages': [{'role':'system','content':system_prompt},
                                                        {'role':'user','content':ctx['user_prompt']}],
                          'temperature': ctx['temperature'], 'max_tokens': ctx['max_tokens']},
                    timeout=180)
            except requests.exceptions.RequestException as re_err:
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 请求失败: {re_err}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': f'LLM 请求失败: {str(re_err)[:200]}'})
                break  # 【铁律】失败即停，避免后续章节无意义空跑

            if resp.status_code != 200:
                err_body = resp.text[:300] if hasattr(resp, 'text') else ''
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 返回 HTTP {resp.status_code}: {err_body}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': f'LLM HTTP {resp.status_code}'})
                break  # 【铁律】失败即停

            try:
                resp_json = resp.json()
                draft_content = resp_json['choices'][0]['message']['content']
            except (ValueError, KeyError, IndexError, TypeError) as parse_err:
                try:
                    app.logger.error(f'ai_continue_batch 第{cur_ch}章 LLM 返回结构异常: {parse_err}, body={resp.text[:300]}')
                except Exception:
                    pass
                failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回结构异常'})
                break  # 【铁律】失败即停

            if not draft_content or not draft_content.strip():
                failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回内容为空'})
                break  # 【铁律】内容为空即停，避免后续章节无上下文可衔接

            polished_content = draft_content

            # 一致性检查（默认关闭）
            consistency_passed = True
            consistency_issues = ''

            # 标题自动生成：解析 JSON 标题，剥离正文标签行
            suggested_title = _extract_chapter_title(draft_content, draft_content)

            # Bug7 修复：先从 draft_content 提取 chapter_changes（暂存），等 Chapter flush 拿到 id 后再回写
            chapter_changes_data = None
            if extract_changes:
                try:
                    _, changes = extract_changes(draft_content)
                    if changes:
                        chapter_changes_data = changes
                except Exception:
                    db.session.rollback()

            polished_content = _extract_chapter_body(polished_content)
            # 额外剥离末尾的标题 JSON 块（_extract_chapter_body 未覆盖）
            polished_content = _re_batch.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

            # Bug9 修复：统一使用 count_words 统计字数，避免前端展示与库不一致
            wc = count_words(polished_content)

            # ★ 修复问题二：连续模式也跑 post_validate（去AI味检测），与多agent模式对齐
            post_validate = None
            if validate_chapter_with_bible:
                try:
                    bible_ctx = {
                        'character_profiles': bb.character_profiles or '',
                        'chapter_changes_log': bb.chapter_changes_log or '',
                        'key_rules': bb.key_rules or '',
                        'worldbuilding': bb.worldbuilding or '',
                        'inventory': bb.inventory or '',
                        'locations': bb.locations or '',
                        'foreshadowing': bb.foreshadowing or '',
                    } if bb else None
                    validation = validate_chapter_with_bible(polished_content, bible_ctx)
                    if validation.issues:
                        post_validate = validation.to_dict()
                except Exception:
                    pass

            # 校验报告
            chapter_score = _calc_chapter_score(post_validate, consistency_passed, consistency_issues, {}, wc, ctx.get('chapter_plan', ''))

            # Bug7 修复：先创建 Chapter 并 flush 拿到 ch.id，再回写 chapter_changes（chapter_id 不再为空串）
            # 统一标题格式：第X章 标题文本（与多Agent同步/流式模式一致）
            title = _format_chapter_title(cur_ch, suggested_title)
            max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
            # Bug1 修复：归卷直接设置 parent_id（用 ctx 中的 vol_chapter），不依赖末尾 resort
            parent_id = ctx['vol_chapter'].id if ctx.get('vol_chapter') else ''
            ch = Chapter(book_id=book_id, title=title, content=polished_content,
                         order_index=max_order + 1, is_volume=False,
                         parent_id=parent_id,
                         word_count=wc)
            db.session.add(ch)
            db.session.flush()  # 拿到 ch.id

            # Bug7 修复：用真实 ch.id 回写 chapter_changes（不再传空串）
            if chapter_changes_data and apply_chapter_changes:
                try:
                    apply_chapter_changes(
                        bb, ch.id, cur_ch,
                        ctx.get('vol_index', 0), chapter_changes_data,
                    )
                except Exception:
                    db.session.rollback()

            update_book_stats(book_id)
            db.session.commit()

            # Bug4 修复：每章保存后检查并自动生成动态报告（每5章触发），避免后续章节注入过时报告
            try:
                _check_and_auto_generate_report(book_id)
            except Exception:
                pass

            results.append({
                'chapter_num': cur_ch,
                'chapter_id': ch.id,
                'title': title,
                'content': polished_content,
                'word_count': wc,
            })
            # Bug5 修复：缓存本章正文供下一章承接
            prev_polished = polished_content
        except Exception as e:
            # Bug2 修复：单章失败记录日志与失败列表，避免静默吞错导致剧情断档
            fail_num = start_chapter_num + i if start_chapter_num else i + 1
            try:
                app.logger.error(f'ai_continue_batch 第{fail_num}章生成失败: {e}', exc_info=True)
            except Exception:
                pass
            db.session.rollback()
            failed.append({'chapter_num': fail_num, 'error': str(e)[:200]})
            # Bug5：失败时清空 prev_polished，避免把失败章当上一章注入
            prev_polished = ''
            break  # 【铁律】异常即停，避免错误级联放大

    # Bug1 修复：移除 resort_chapters_by_title(rebin_volumes=True)。
    # 该调用按标题中的章节号重排 order_index，但批处理标题可能不含"第X章"前缀
    # （_extract_chapter_title 会剥离前缀），混合排序时顺序错乱；
    # 同时 rebin_volumes=True 会用"第X卷（第X-X章）"覆盖用户自定义卷名。
    # 批处理章节已按 order_index=max+1 顺序保存，归卷在循环内通过 parent_id 完成。
    return jsonify({
        'chapters': results,
        'total': len(results),
        'failed': failed,
        'failed_count': len(failed),
    })


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


@app.route('/api/books/<book_id>/ai-continue-batch/stream', methods=['POST'])
@login_required
def ai_continue_batch_stream(book_id):
    """连续创作流式版（SSE）：解决 Render 同步请求超时（约100s）导致 Failed to fetch。
    每章 LLM 调用改 stream 模式，逐 chunk 收集同时定期推送心跳，保持连接活跃；
    每章完成推送完整章节内容，失败推送错误，全部完成推送结束事件。
    事件类型：
    - {type:'chapter_start', chapter_num}
    - {type:'heartbeat', chapter_num, message} (生成期间每5s推送，防空闲超时)
    - {type:'chapter_done', chapter:{chapter_num, chapter_id, title, content, word_count}}
    - {type:'chapter_failed', chapter_num, error}
    - {type:'batch_done', total, failed_count, failed:[...]}
    - {type:'error', message} (致命错误，终止)
    """
    import time as _time

    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json() or {}
    instruction = data.get('instruction', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    chapter_lang_styles = data.get('chapter_lang_styles', [])
    count = max(1, min(10, int(data.get('count', 3))))
    start_chapter_num = data.get('start_chapter_num')

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    def generate():
        import re as _re_bs
        results = []
        failed = []
        prev_polished = ''

        for i in range(count):
            try:
                target_num = start_chapter_num + i if start_chapter_num else None
                # 先推送 chapter_start（用预估章号），让 Render 立即收到首字节，
                # 避免 _build_ai_continue_context 内 _generate_chapter_plan 调 LLM 阻塞 20-30s 期间无数据导致断开
                yield f'data: {json.dumps({"type": "chapter_start", "chapter_num": target_num or (i + 1), "message": f"正在准备第{target_num or (i + 1)}章上下文..."}, ensure_ascii=False)}\n\n'

                ctx = _build_ai_continue_context(book_id, bb, instruction, skill_pack_ids,
                                                  target_num, prev_polished or None, chapter_lang_styles,
                                                  skip_chapter_plan=True)
                api_key = ctx['api_key']
                base_url = ctx['base_url']
                model = ctx['model']
                cur_ch = ctx['current_chapter_num']

                # 章号可能与预估不同（后端基于 max(order_index)+1 重新计算），推送修正事件
                if cur_ch != (target_num or (i + 1)):
                    yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在生成第{cur_ch}章..."}, ensure_ascii=False)}\n\n'

                # 标准文风与字数铁律已在 _build_ai_continue_context 内统一注入（三种模式一致）
                # 此处仅追加连续创作专属的去AI味补充约束，不重复注入标准文风
                system_prompt = ctx['system_prompt']
                system_prompt += '\n\n【去AI味补充】禁止"仿佛""似乎"等比喻堆砌，禁止工整过渡，禁止完美套话。人物必须有矛盾心理/纠结/自我怀疑，禁止情绪顺滑。'

                # LLM 流式调用：逐 chunk 收集，定期推送心跳保持连接活跃
                full_parts = []
                last_heartbeat = _time.time()
                try:
                    resp = requests.post(f'{base_url}/chat/completions',
                        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                        json={'model': model, 'messages': [{'role':'system','content':system_prompt},
                                                            {'role':'user','content':ctx['user_prompt']}],
                              'temperature': ctx['temperature'], 'max_tokens': ctx['max_tokens'],
                              'stream': True},
                        stream=True, timeout=180)
                except requests.exceptions.RequestException as re_err:
                    try:
                        app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 LLM 请求失败: {re_err}')
                    except Exception:
                        pass
                    failed.append({'chapter_num': cur_ch, 'error': f'LLM 请求失败: {str(re_err)[:200]}'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": f"LLM 请求失败"}, ensure_ascii=False)}\n\n'
                    # 【铁律】前面章节失败，后续章节自动停止，避免连续生成无意义空章节
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 请求失败，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                if resp.status_code != 200:
                    err_body = resp.text[:300] if hasattr(resp, 'text') else ''
                    try:
                        app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 LLM HTTP {resp.status_code}: {err_body}')
                    except Exception:
                        pass
                    failed.append({'chapter_num': cur_ch, 'error': f'LLM HTTP {resp.status_code}'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": f"LLM HTTP {resp.status_code}"}, ensure_ascii=False)}\n\n'
                    # 【铁律】前面章节失败，后续章节自动停止
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 返回 HTTP {resp.status_code}，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                # 后台线程读取流式响应，主生成器从队列消费，无数据时 yield 心跳
                # 修复 network error：iter_lines() 阻塞时心跳无法推送
                for evt in _stream_llm_chunks_with_heartbeat(resp, cur_ch, last_heartbeat):
                    if evt[0] == 'chunk':
                        full_parts.append(evt[1])
                        last_heartbeat = _time.time()
                    elif evt[0] == 'heartbeat':
                        yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在生成第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                        last_heartbeat = _time.time()
                    elif evt[0] == 'error':
                        raise evt[1]
                    elif evt[0] == 'done':
                        break
                try:
                    resp.close()
                except Exception:
                    pass

                draft_content = ''.join(full_parts)
                if not draft_content or not draft_content.strip():
                    failed.append({'chapter_num': cur_ch, 'error': 'LLM 返回内容为空'})
                    yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": cur_ch, "error": "LLM 返回内容为空"}, ensure_ascii=False)}\n\n'
                    # 【铁律】LLM 返回内容为空，后续章节自动停止（空章节无上下文可衔接，继续生成只会产出更多空内容）
                    yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{cur_ch}章 LLM 返回内容为空，后续章节已自动停止", "failed_chapter": cur_ch}, ensure_ascii=False)}\n\n'
                    break

                polished_content = draft_content
                suggested_title = _extract_chapter_title(draft_content, draft_content)

                # 提取 chapter_changes（暂存，待 flush 后回写）
                chapter_changes_data = None
                if extract_changes:
                    try:
                        _, changes = extract_changes(draft_content)
                        if changes:
                            chapter_changes_data = changes
                    except Exception:
                        db.session.rollback()

                polished_content = _extract_chapter_body(polished_content)
                polished_content = _re_bs.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', polished_content).rstrip()

                # ===== 去AI味审校 Agent（选项A：批处理也跑去AI味修正，与单章模式对齐）=====
                # 复用单章 ai_continue 的去AI味逻辑：选了含 tomato_deai 的技能包时再调一次 LLM 修正正文
                # 字数校验通过→用修正版；字数异常/失败→回滚用初稿
                deai_status = 'skipped'  # skipped / success / failed
                if skill_pack_ids:
                    # 【三类无污染】去AI味属于审查阶段：只注入审查类（review）技能包
                    deai_skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'review', ['tomato_deai', 'de_ai_flavor', 'polish'], mode='agent')
                    if deai_skill_note:
                        # 推送心跳：去AI味审校中
                        yield f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在去AI味审校第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                        deai_system = f"""你是番茄去AI味审查员。对以下刚写好的章节正文做去AI味审校，按规则修改后只输出修改后的正文。

{deai_skill_note}

【优先级铁律】人味>克制>流畅。删完AI味后读起来像机器人汇报→加口语碎片。太啰嗦→删修饰。磕磕绊绊→调句式。
【必删清单】一股/一抹/不由得/不禁/随即/旋即/与此同时/颇为/甚为/极为/缓缓/淡淡/轻轻/微微/毫无疑问/毋庸置疑/不言而喻/深吸一口气/眼中闪过一丝/心中暗想/心念电转/若有所思/不知不觉间/转眼间/恍然大悟/面无表情/淡漠/漠然/眸子/嘴角微微上扬/如同/宛如/犹如/周身/周遭/气息/威压/那道身影/说话间/话音未落/当即/顿时/瞬时。
【人味注入】加入不完美细节(结巴/重复/打断)/感官碎片/小动作微表情/语气词和断句/适当留白。
【硬性约束】修改后字数仍须 2400±100，保留原章节的剧情走向和钩子，只改文风不改剧情。"""
                        try:
                            deai_hb = f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在去AI味审校第{cur_ch}章..."}, ensure_ascii=False)}\n\n'
                            deai_resp = yield from _run_blocking_with_heartbeat(
                                lambda: requests.post(f'{base_url}/chat/completions',
                                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                                    json={'model': model,
                                          'messages': [{'role':'system','content':deai_system},
                                                       {'role':'user','content':f'请审校以下章节正文：\n\n{polished_content}'}],
                                          'temperature': 0.5, 'max_tokens': ctx['max_tokens']},
                                    timeout=180),
                                deai_hb)
                            if deai_resp.status_code == 200:
                                deai_result = deai_resp.json()
                                deai_polished = deai_result['choices'][0]['message']['content'].strip()
                                # 剥离可能的内部标签（去AI味 LLM 偶尔会带上）
                                deai_polished = _extract_chapter_body(deai_polished)
                                deai_polished = _re_bs.sub(r'\{[^{}]*"title"\s*:\s*"[^"]*"[^{}]*\}', '', deai_polished).rstrip()
                                deai_wc = count_words(deai_polished)
                                if deai_polished and 2300 <= deai_wc <= 2500:
                                    polished_content = deai_polished
                                    deai_status = 'success'
                                elif deai_polished and deai_wc > 500:
                                    deai_status = 'failed'  # 字数异常，回滚用初稿
                                else:
                                    deai_status = 'failed'  # 内容为空，回滚用初稿
                            else:
                                deai_status = 'failed'
                                try:
                                    app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 去AI味 HTTP {deai_resp.status_code}')
                                except Exception:
                                    pass
                        except Exception as deai_err:
                            deai_status = 'failed'
                            try:
                                app.logger.error(f'ai_continue_batch_stream 第{cur_ch}章 去AI味异常: {deai_err}')
                            except Exception:
                                pass

                wc = count_words(polished_content)

                # post_validate（去AI味检测）
                post_validate = None
                if validate_chapter_with_bible:
                    try:
                        bible_ctx = {
                            'character_profiles': bb.character_profiles or '',
                            'chapter_changes_log': bb.chapter_changes_log or '',
                            'key_rules': bb.key_rules or '',
                            'worldbuilding': bb.worldbuilding or '',
                            'inventory': bb.inventory or '',
                            'locations': bb.locations or '',
                            'foreshadowing': bb.foreshadowing or '',
                        } if bb else None
                        validation = validate_chapter_with_bible(polished_content, bible_ctx)
                        if validation.issues:
                            post_validate = validation.to_dict()
                    except Exception:
                        pass

                chapter_score = _calc_chapter_score(post_validate, True, '', {}, wc, ctx.get('chapter_plan', ''))

                # 创建 Chapter 并 flush 拿 id
                # 统一标题格式：第X章 标题文本（与多Agent同步/连续同步模式一致）
                title = _format_chapter_title(cur_ch, suggested_title)
                max_order = db.session.query(db.func.max(Chapter.order_index)).filter_by(book_id=book_id).scalar() or -1
                parent_id = ctx['vol_chapter'].id if ctx.get('vol_chapter') else ''
                ch = Chapter(book_id=book_id, title=title, content=polished_content,
                             order_index=max_order + 1, is_volume=False,
                             parent_id=parent_id,
                             word_count=wc)
                db.session.add(ch)
                db.session.flush()

                # 用真实 ch.id 回写 chapter_changes
                if chapter_changes_data and apply_chapter_changes:
                    try:
                        apply_chapter_changes(bb, ch.id, cur_ch, ctx.get('vol_index', 0), chapter_changes_data)
                    except Exception:
                        db.session.rollback()

                update_book_stats(book_id)
                db.session.commit()

                # 每章后检查自动生成动态报告（可能触发 LLM 调用，用线程+心跳避免阻塞）
                try:
                    report_hb = f'data: {json.dumps({"type": "heartbeat", "chapter_num": cur_ch, "message": f"正在更新动态报告..."}, ensure_ascii=False)}\n\n'
                    yield from _run_blocking_with_heartbeat(
                        lambda: _check_and_auto_generate_report(book_id),
                        report_hb)
                except Exception:
                    pass

                chapter_info = {
                    'chapter_num': cur_ch,
                    'chapter_id': ch.id,
                    'title': title,
                    'content': polished_content,
                    'word_count': wc,
                    'deai_status': deai_status,  # skipped/success/failed，告知前端是否做过去AI味修正
                }
                results.append(chapter_info)
                prev_polished = polished_content

                # 推送章节完成事件
                yield f'data: {json.dumps({"type": "chapter_done", "chapter": chapter_info}, ensure_ascii=False)}\n\n'

            except Exception as e:
                fail_num = start_chapter_num + i if start_chapter_num else i + 1
                try:
                    app.logger.error(f'ai_continue_batch_stream 第{fail_num}章生成失败: {e}', exc_info=True)
                except Exception:
                    pass
                db.session.rollback()
                failed.append({'chapter_num': fail_num, 'error': str(e)[:200]})
                yield f'data: {json.dumps({"type": "chapter_failed", "chapter_num": fail_num, "error": str(e)[:200]}, ensure_ascii=False)}\n\n'
                # 【铁律】章节生成异常，后续章节自动停止，避免错误级联放大
                yield f'data: {json.dumps({"type": "batch_stopped", "reason": f"第{fail_num}章生成异常：{str(e)[:100]}，后续章节已自动停止", "failed_chapter": fail_num}, ensure_ascii=False)}\n\n'
                prev_polished = ''
                break

        # 推送批处理完成事件
        yield f'data: {json.dumps({"type": "batch_done", "total": len(results), "failed_count": len(failed), "failed": failed}, ensure_ascii=False)}\n\n'

    # stream_with_context 保持请求/应用上下文，避免生成器在 yield 后恢复时
    # 触发 "Working outside of application context"（批处理多章串行，循环内大量 DB 操作：
    # Chapter.query / db.session.commit / update_book_stats / _check_and_auto_generate_report 等，
    # 没有 stream_with_context 会在第一次 yield 后丢失上下文，后续 DB 操作报错导致连接异常断开 → network error）
    # 响应头修复：Cloudflare/Render 代理默认缓冲响应体，SSE 流会被缓冲导致浏览器长时间收不到数据 → network error
    # - Cache-Control: no-cache 禁止缓存
    # - X-Accel-Buffering: no 禁止 Nginx/代理缓冲
    # - Connection: keep-alive 保持长连接
    resp = app.response_class(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp


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
    - 有 suggested_title：格式为「第{章号}章 {标题}」
    - 无 suggested_title：格式为「第{章号}章」
    - suggested_title 已含「第X章」前缀时去重，避免「第1章 第1章 xxx」
    """
    import re as _re
    prefix = f'第{chapter_num}章'
    if not suggested_title or not suggested_title.strip():
        return prefix
    title = suggested_title.strip()
    # 去除标题中可能自带的「第X章」前缀（LLM 偶尔会带上）
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


# ==== LLM 调用辅助函数 ====
def _extract_json_from_llm(content, expect='auto'):
    """从 LLM 返回内容中健壮地提取 JSON 对象或数组。
    处理常见情况：markdown代码块包裹、前后说明文字、多个JSON候选、尾随逗号等。
    expect: 'object' 只接受 dict / 'array' 只接受 list / 'auto' 两者皆可。
    返回 (parsed, error)。成功时 error 为 None。"""
    if not content or not isinstance(content, str):
        return None, 'LLM 返回为空'

    import re

    # 1. 去除 markdown 代码块标记（```json ... ``` 或 ``` ... ```）
    cleaned = re.sub(r'```(?:json|JSON)?\s*', '', content)
    cleaned = re.sub(r'```\s*$', '', cleaned).strip()

    # 2. 直接尝试整体解析（LLM 有时严格只输出 JSON）
    try:
        parsed = json.loads(cleaned)
        if expect == 'object' and isinstance(parsed, dict):
            return parsed, None
        if expect == 'array' and isinstance(parsed, list):
            return parsed, None
        if expect == 'auto' and isinstance(parsed, (dict, list)):
            return parsed, None
    except (json.JSONDecodeError, ValueError):
        pass

    # 3. 遍历所有 {...} 候选（非贪婪，从后往前找最大的），逐个尝试解析
    # 用栈匹配花括号，避免正则贪婪问题
    candidates = []
    # 对象候选 {...}
    for m in re.finditer(r'\{', cleaned):
        start = m.start()
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == '{':
                depth += 1
            elif cleaned[i] == '}':
                depth -= 1
                if depth == 0:
                    candidates.append(cleaned[start:i+1])
                    break
    # 数组候选 [...]
    for m in re.finditer(r'\[', cleaned):
        start = m.start()
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == '[':
                depth += 1
            elif cleaned[i] == ']':
                depth -= 1
                if depth == 0:
                    candidates.append(cleaned[start:i+1])
                    break

    # 优先返回最长的候选（通常是完整的 JSON）
    candidates.sort(key=len, reverse=True)
    last_error = None
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if expect == 'object' and isinstance(parsed, dict):
                return parsed, None
            if expect == 'array' and isinstance(parsed, list):
                return parsed, None
            if expect == 'auto' and isinstance(parsed, (dict, list)):
                return parsed, None
        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            # 尝试修复常见 JSON 瑕疵：尾随逗号
            try:
                fixed = re.sub(r',\s*([}\]])', r'\1', cand)
                parsed = json.loads(fixed)
                if expect == 'object' and isinstance(parsed, dict):
                    return parsed, None
                if expect == 'array' and isinstance(parsed, list):
                    return parsed, None
                if expect == 'auto' and isinstance(parsed, (dict, list)):
                    return parsed, None
            except (json.JSONDecodeError, ValueError):
                pass

    return None, f'未找到有效JSON（最后错误: {last_error}），原始内容前300字: {content[:300]}'


def _call_llm(messages, max_tokens=None, temperature=None, task_type='creation'):
    """统一的 LLM 调用辅助函数，返回 (content, error)
    task_type: 'creation'用主模型(创作/写作)，'recognition'用识别模型(识别/分析/检查，为空时回退主模型)
    max_tokens 语义：None → 用 cfg.max_tokens；0 → 不限制（不下发该字段，用模型自身默认上限）；正整数 → 显式限定。"""
    cfg = AIConfig.query.first()
    if not cfg or not cfg.api_key:
        return None, '请先配置 AI 模型 API Key'
    try:
        base = cfg.base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        model = cfg.get_model_for_task(task_type)
        payload = {
            'model': model,
            'messages': messages,
            'temperature': temperature if temperature is not None else cfg.temperature,
            'stream': False
        }
        # max_tokens：0 表示不限制（不下发），None 用配置默认值，正整数显式限定
        if max_tokens == 0:
            pass  # 不下发，让模型用自身默认输出上限
        elif max_tokens:
            payload['max_tokens'] = max_tokens
        else:
            payload['max_tokens'] = cfg.max_tokens
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {cfg.api_key}', 'Content-Type': 'application/json'},
            json=payload, timeout=180)
        result = resp.json()
        if 'choices' in result and len(result['choices']) > 0:
            return result['choices'][0]['message']['content'], None
        return None, str(result)
    except Exception as e:
        return None, str(e)


def _get_skill_prompts(skill_pack_ids, prompt_keys, max_per_prompt=1500, mode='agent'):
    """从技能包提取指定 prompt_keys 的提示词（agent 协同模式：所有匹配 prompt 都注入）。
    mode='agent'（默认）：所有匹配 prompt 全量注入（不截断），确保禁词清单/行文铁律等关键内容完整传达给 AI。
    mode='single'：每包只取第一个匹配 prompt（兼容旧行为）。
    """
    if not skill_pack_ids:
        return ''
    try:
        packs = SkillPack.query.filter(SkillPack.id.in_(skill_pack_ids)).all()
    except Exception as e:
        # 隐患2修复：出错时记日志而非静默返回空串，便于排查"技能包未生效"问题
        try:
            app.logger.error(f'_get_skill_prompts 查询技能包失败(ids={skill_pack_ids}): {e}')
        except Exception:
            pass
        return ''
    if not packs:
        # 用户选了技能包但查不到（可能被删除），记日志提示
        try:
            app.logger.warning(f'_get_skill_prompts 未找到匹配技能包(ids={skill_pack_ids})，可能已被删除')
        except Exception:
            pass
        return ''
    notes = []
    for pack in packs:
        try:
            prompts = json.loads(pack.prompts_json) if pack.prompts_json else {}
        except Exception as e:
            try:
                app.logger.warning(f'_get_skill_prompts 技能包[{pack.name}] prompts_json 解析失败: {e}')
            except Exception:
                pass
            prompts = {}
        matched = [(k, prompts[k]) for k in prompt_keys if k in prompts and prompts[k]]
        if not matched:
            continue
        if mode == 'single':
            # 兼容旧模式：只取第一个
            p = matched[0][1][:max_per_prompt]
            notes.append(f'【{pack.name}】\n{p}')
        else:
            # agent 模式：所有匹配 prompt 全量注入（不截断摘要）
            # 隐患1修复：原先只第一个全量、其余截到400字会丢失禁词清单等关键内容；
            # 现 token 不再受限，全部全量注入，确保创作方法论完整传达
            parts = []
            for k, p in matched:
                parts.append(f'[{k}]\n{p}')
            notes.append(f'【{pack.name}】\n' + '\n'.join(parts))
    return '\n\n'.join(notes)


def _get_skill_prompts_by_category(skill_pack_ids, category, prompt_keys=None, mode='agent'):
    """按类别过滤技能包后提取提示词（三类无污染隔离的核心调度函数）。
    - category='master': 只查构思类，注入大纲/规划阶段
    - category='style': 只查文风类，注入正文生成阶段（按 priority 排序）
    - category='review': 只查审查类，注入审校阶段（去AI味/一致性）
    prompt_keys: 可选，指定提取哪些 key；不指定则提取该包全部 prompts。
    老技能包无 category 字段时默认按 'master' 处理（兼容）。"""
    if not skill_pack_ids:
        return ''
    try:
        packs = SkillPack.query.filter(SkillPack.id.in_(skill_pack_ids)).all()
    except Exception as e:
        try:
            app.logger.error(f'_get_skill_prompts_by_category 查询失败(ids={skill_pack_ids}, cat={category}): {e}')
        except Exception:
            pass
        return ''
    # 按 category 过滤（老包无 category 视为 master）
    filtered = [p for p in packs if (p.category or 'master') == category]
    if not filtered:
        return ''
    # 文风类按 priority 排序（数字小的先注入）
    if category == 'style':
        filtered.sort(key=lambda p: p.priority if p.priority is not None else 100)
    notes = []
    for pack in filtered:
        try:
            prompts = json.loads(pack.prompts_json) if pack.prompts_json else {}
        except Exception:
            continue
        if not prompts:
            continue
        # 若指定 prompt_keys 则按 key 过滤，否则取全部
        if prompt_keys:
            matched = [(k, prompts[k]) for k in prompt_keys if k in prompts and prompts[k]]
        else:
            matched = [(k, v) for k, v in prompts.items() if v]
        if not matched:
            continue
        if mode == 'single':
            p = matched[0][1][:1500]
            notes.append(f'【{pack.name}】\n{p}')
        else:
            parts = [f'[{k}]\n{p}' for k, p in matched]
            notes.append(f'【{pack.name}】\n' + '\n'.join(parts))
    return '\n\n'.join(notes)


def _resolve_skill_ids_by_category(book, category):
    """从 Book 表的三类字段中取出对应类别的技能包ID列表。
    - category='master' -> book.master_skill_ids
    - category='style'   -> book.style_skill_ids
    - category='review'  -> book.review_skill_ids
    兼容老数据：若三类字段全空，回退到老的 skill_pack_ids（在 metadata 或请求参数中）。"""
    if not book:
        return []
    field_map = {'master': 'master_skill_ids', 'style': 'style_skill_ids', 'review': 'review_skill_ids'}
    field_name = field_map.get(category, 'master_skill_ids')
    raw = getattr(book, field_name, None) or '[]'
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(ids, list) and ids:
            return ids
    except Exception:
        pass
    return []


def _split_legacy_skill_ids_to_categories(book):
    """老数据迁移：将老的 skill_pack_ids 按 category 自动分流到3个新字段。
    在 book 首次访问时触发（若3个新字段全空且老字段有值）。
    幂等：已分流过则跳过。"""
    if not book:
        return
    # 检查是否已分流过（任一新字段非空即视为已分流）
    try:
        m = json.loads(book.master_skill_ids or '[]') if book.master_skill_ids else []
        s = json.loads(book.style_skill_ids or '[]') if book.style_skill_ids else []
        r = json.loads(book.review_skill_ids or '[]') if book.review_skill_ids else []
        if m or s or r:
            return  # 已分流
    except Exception:
        return
    # 从 metadata 取老的 skill_pack_ids
    try:
        meta = json.loads(book.metadata_json or '{}') if book.metadata_json else {}
    except Exception:
        meta = {}
    legacy_ids = meta.get('skill_pack_ids', [])
    if not legacy_ids:
        return
    # 查询每个包的 category 分流
    try:
        packs = SkillPack.query.filter(SkillPack.id.in_(legacy_ids)).all()
    except Exception:
        return
    master_ids, style_ids, review_ids = [], [], []
    for p in packs:
        cat = p.category or 'master'
        if cat == 'style':
            style_ids.append(p.id)
        elif cat == 'review':
            review_ids.append(p.id)
        else:
            master_ids.append(p.id)
    book.master_skill_ids = json.dumps(master_ids, ensure_ascii=False)
    book.style_skill_ids = json.dumps(style_ids, ensure_ascii=False)
    book.review_skill_ids = json.dumps(review_ids, ensure_ascii=False)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


# ==== 大纲工作流：五幕式总纲 + 卷纲滚动生成 ====
@app.route('/api/books/<book_id>/ai-outline-master', methods=['POST'])
def ai_outline_master(book_id):
    """生成五幕式总纲：控制全书大体走向，写入 plot_design"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    total_chapters = data.get('total_chapters', 300)
    chapters_per_volume = data.get('chapters_per_volume', 50)
    # 支持用户直接指定卷数（优先于 total_chapters // chapters_per_volume）
    volume_count = data.get('volume_count')
    if not volume_count or volume_count < 1:
        volume_count = max(1, total_chapters // chapters_per_volume)
    # 反推总章数供 prompt 使用
    total_chapters = max(volume_count * chapters_per_volume, total_chapters)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['master_outline', 'tomato_outline', 'volume_breakdown'])

    context_parts = []
    if bb.concept:
        context_parts.append(f'【构思】\n{bb.concept[:2000]}')
    if bb.key_rules:
        context_parts.append(f'【设定/规则】\n{bb.key_rules[:2000]}')
    if bb.worldbuilding:
        context_parts.append(f'【世界观】\n{bb.worldbuilding[:2000]}')
    if bb.character_profiles:
        context_parts.append(f'【人物档案】\n{bb.character_profiles[:2000]}')
    context = '\n\n'.join(context_parts) or '（暂无构思和设定，请基于书名和题材自由发挥）'

    system_prompt = f"""你是番茄小说金番作者级别的总纲设计师。
任务：为这本小说设计五幕式总纲，控制全书大体走向。

【五幕模型】
- 立身(1-5%)：底层→入门，觉醒金手指+首打脸+建立认知
- 立足(5-25%)：新人→站稳，配角登场+世界观展开+5-8章小闭环
- 立势(25-50%)：小角色→有分量，大舞台+强对手+团队建立
- 立威(50-75%)：有分量→威名，组织级冲突+感情推进+信念考验
- 立命(75-100%)：威名→蜕变，终极挑战+伏笔收束+续作种子

【输出要求】
全书约 {total_chapters} 章，分 {volume_count} 卷（每卷约 {chapters_per_volume} 章）。
为每卷输出：卷号与卷名、所属幕、本卷核心目标（一句话）、主要冲突、关键转折点（2-3个）、卷尾高潮与悬念。
只输出总纲文本，不要输出各卷的详细情节节点（详细节点在卷纲滚动生成阶段产生）。

{skill_note}"""

    user_prompt = f"""书名：{book.title}

{_build_core_params_block(bb, book)}

已有设定：
{context}

请生成五幕式总纲。"""

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=0, temperature=0.7
    )
    if err:
        return jsonify({'error': err}), 500

    bb.plot_design = content
    db.session.commit()
    return jsonify({'master_outline': content, 'volume_count': volume_count})


def _sync_foreshadowings_to_volumes(bb):
    """【P0修复】把各卷 timeline 中的 foreshadow_new/foreshadowing/foreshadow_recycle
    汇总到 bb.foreshadowing_volumes（按卷结构化），打通前期伏笔到写作阶段的防遗忘链路。
    在 ai_outline_volume / ai_extract_volumes_from_outline / ai_import_plot_outline 写入 timeline 后调用。"""
    if not bb or not bb.timeline:
        return
    try:
        arr = json.loads(bb.timeline)
        if not isinstance(arr, list):
            return
        vol_foreshadows = []
        for v in arr:
            if not isinstance(v, dict):
                continue
            v_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', ''))) or 0
            new_fs = v.get('foreshadow_new') or v.get('foreshadowing') or []
            recycle_fs = v.get('foreshadow_recycle') or []
            if not new_fs and not recycle_fs:
                continue
            # 统一为字符串列表
            def _norm(items):
                out = []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, str):
                            out.append(it.strip())
                        elif isinstance(it, dict):
                            out.append((it.get('name') or it.get('title') or '') + '：' + (it.get('content') or it.get('desc') or ''))
                        elif it is not None:
                            out.append(str(it).strip())
                elif isinstance(items, str) and items.strip():
                    out.append(items.strip())
                return [x for x in out if x]
            new_list = _norm(new_fs)
            recycle_list = _norm(recycle_fs)
            if new_list or recycle_list:
                vol_foreshadows.append({
                    'volume_index': int(v_idx) if v_idx else 0,
                    'volume': v.get('volume', v.get('volume_title', '')),
                    'foreshadow_new': new_list,
                    'foreshadow_recycle': recycle_list,
                })
        bb.foreshadowing_volumes = json.dumps(vol_foreshadows, ensure_ascii=False, indent=2) if vol_foreshadows else (bb.foreshadowing_volumes or '')
    except (json.JSONDecodeError, ValueError, TypeError):
        pass


def _calc_start_chapter_fallback(volume_index, chapters_per_volume, existing_volumes):
    """【P1修复】start_chapter 解析失败的健壮回退：
    按 volume_index * chapters_per_volume + 1 估算，避免静默回退为1导致章号断裂。"""
    try:
        cpc = int(chapters_per_volume) if chapters_per_volume else 50
    except (ValueError, TypeError):
        cpc = 50
    vi = int(volume_index) if volume_index else 1
    # 若已有卷数据，取已完成卷数（卷号 < 当前卷）* 每卷章数 + 1
    if existing_volumes and isinstance(existing_volumes, list):
        prev_count = sum(1 for v in existing_volumes
                         if isinstance(v, dict) and
                         (int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0) < vi))
        return prev_count * cpc + 1
    return (vi - 1) * cpc + 1


def _extract_volume_index(text):
    """从卷名/卷ID中提取卷号数字，如'第3卷'/'卷三'/'卷二十三'/'Volume 2' → 3/3/23/2
    支持阿拉伯数字与复合中文数字（十一/二十三/一百零五/壹貳叁等）。"""
    if not text:
        return 0
    s = str(text)
    import re as _re
    # 阿拉伯数字优先
    m = _re.search(r'(\d+)', s)
    if m:
        return int(m.group(1))
    # 复合中文数字解析（支持 一~九十九、百、零、大写壹貳叁）
    cn_digits = {
        '零': 0, '〇': 0,
        '一': 1, '壹': 1, '乙': 1,
        '二': 2, '贰': 2, '貳': 2, '两': 2,
        '三': 3, '叁': 3, '參': 3,
        '四': 4, '肆': 4,
        '五': 5, '伍': 5,
        '六': 6, '陆': 6, '陸': 6,
        '七': 7, '柒': 7, '漆': 7,
        '八': 8, '捌': 8,
        '九': 9, '玖': 9,
    }
    cn_units = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
    # 提取连续的中文数字片段（含单位）
    cn_str_match = _re.search(r'[零〇一二贰貳两三叁參四肆五伍六陆陸七柒漆八捌九玖十拾百佰千仟]+', s)
    if cn_str_match:
        cn_str = cn_str_match.group()
        total = 0
        current = 0
        for ch in cn_str:
            if ch in cn_digits:
                current = cn_digits[ch]
            elif ch in cn_units:
                unit = cn_units[ch]
                if current == 0:
                    current = 1 if unit >= 10 else 0
                total += current * unit
                current = 0
            else:
                current = 0
        total += current
        if total > 0:
            return total
    return 0


@app.route('/api/books/<book_id>/ai-outline-volume', methods=['POST'])
def ai_outline_volume(book_id):
    """生成单卷详细大纲（滚动生成）：基于总纲+已完成章节，写入 timeline"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()
    # 非强制：无总纲也能设计节点，基于已有卷剧情
    has_master = bool(bb.plot_design and bb.plot_design.strip())

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    volume_index = data.get('volume_index', 1)
    volume_title = data.get('volume_title', f'第{volume_index}卷')
    chapters_per_volume = data.get('chapters_per_volume', 50)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'chapter_plan', 'tomato_outline'])

    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()

    # ===== 【P0弊端2修复】已完成章节摘要：取上一卷结尾章节，而非全局最后5章 =====
    # 解析已有 timeline，获取上一卷（volume_index-1）的节点章节范围
    prev_volume_end_chapter = 0
    prev_volume_ending_hook = ''
    prev_volume_summary = ''
    existing_volumes_for_ctx = []
    if bb.timeline:
        try:
            parsed_tl = json.loads(bb.timeline)
            if isinstance(parsed_tl, list):
                existing_volumes_for_ctx = parsed_tl
                # 找上一卷
                for v in parsed_tl:
                    if not isinstance(v, dict):
                        continue
                    v_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', '')))
                    if str(v_idx) == str(volume_index - 1):
                        # 提取上一卷的卷尾钩子
                        prev_volume_ending_hook = v.get('ending') or v.get('ending_hook') or v.get('climax') or ''
                        prev_volume_summary = v.get('main_plot') or v.get('core_goal') or ''
                        # 提取上一卷 nodes 的最大结束章号
                        nodes = v.get('nodes') or []
                        for n in nodes:
                            if isinstance(n, dict):
                                ch_range = str(n.get('chapters', ''))
                                # 解析 "1-10" 或 "50" 格式
                                nums = re.findall(r'\d+', ch_range)
                                if nums:
                                    end_n = int(nums[-1])
                                    if end_n > prev_volume_end_chapter:
                                        prev_volume_end_chapter = end_n
                        break
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 上一卷结尾章节正文（用于卷间衔接，取最后2章各300字）
    prev_volume_end_summary = ''
    if chapters and volume_index > 1:
        # 优先取上一卷结尾章节；若无法定位，回退取全书最后3章
        if prev_volume_end_chapter > 0:
            prev_end_chapters = [c for c in chapters if c.order_index <= prev_volume_end_chapter][-2:]
        else:
            prev_end_chapters = chapters[-3:]
        prev_volume_end_summary = '\n'.join([f'第{c.order_index}章 {c.title or ""}：{(c.content or "")[-300:]}' for c in prev_end_chapters])

    # 【P1修复】existing_timeline 分级注入：上一卷完整 + 其余卷主线+节点摘要，
    # 让节点设计能感知全部已有卷的节点，保证全书节点剧情连贯
    existing_timeline = ''
    if existing_volumes_for_ctx:
        tl_parts = []
        for v in existing_volumes_for_ctx:
            if not isinstance(v, dict):
                continue
            v_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', ''))) or 0
            try:
                v_idx_int = int(v_idx)
            except (ValueError, TypeError):
                v_idx_int = 0
            v_nodes = v.get('nodes') or []
            # 上一卷（volume_index-1）注入完整信息（含完整节点）
            if v_idx_int == volume_index - 1:
                tl_parts.append(f'▼ [上一卷·完整] 第{v_idx}卷「{v.get("volume", v.get("volume_title", ""))}」\n'
                                + f'  主线：{(v.get("main_plot") or v.get("core_goal") or "")[:400]}\n'
                                + f'  核心冲突：{str(v.get("core_conflict", ""))[:200]}\n'
                                + f'  关键转折：{", ".join(v.get("key_events", []) or v.get("turning_points", []))[:200]}\n'
                                + f'  卷尾钩子：{str(v.get("ending") or v.get("ending_hook") or v.get("climax") or "")[:200]}\n'
                                + f'  情节节点：' + ' | '.join([f'{n.get("chapters","")} {n.get("title","")}' for n in v_nodes[:8]]))
            elif v_nodes:
                # 其余已有节点的卷：注入主线 + 节点摘要（保证全书节点连贯）
                main = (v.get('main_plot') or v.get('core_goal') or '')[:120]
                hook = v.get('ending') or v.get('ending_hook') or v.get('climax') or ''
                nodes_brief = ' | '.join([f'{n.get("chapters","")}{n.get("title","")}' for n in v_nodes[:6]])
                tl_parts.append(f'· 第{v_idx}卷「{v.get("volume", v.get("volume_title", ""))}」主线：{main}；卷尾钩子：{str(hook)[:80]}；节点：{nodes_brief[:200]}')
            else:
                # 无节点的卷：仅主线+钩子
                hook = v.get('ending') or v.get('ending_hook') or v.get('climax') or ''
                main = (v.get('main_plot') or v.get('core_goal') or '')[:80]
                tl_parts.append(f'· 第{v_idx}卷「{v.get("volume", v.get("volume_title", ""))}」主线：{main}；卷尾钩子：{str(hook)[:100]}')
        existing_timeline = '\n'.join(tl_parts)[:4500]
    else:
        existing_timeline = (bb.timeline or '')[:2000]
    master_outline = (bb.plot_design or '')[:3000]
    # agent 协同：补充世界观+人物档案，让卷内情节节点能落地到具体世界观规则和角色互动
    worldbuilding_ctx = (bb.worldbuilding or '')[:1500]
    characters_ctx = (bb.character_profiles or '')[:1500]
    key_rules_ctx = (bb.key_rules or '')[:1000]

    # ===== 【P1弊端7修复】五幕模型对齐：确定本卷对应的幕 =====
    act_mapping = {1: '立身', 2: '立足', 3: '立势', 4: '立威', 5: '立命'}
    # P1-1修复：total_volumes 必须用全书规划卷数（book.total_volumes），
    # 而非已生成卷数+1，否则五幕比例映射分母错误，导致本卷被错配到错误的幕
    total_volumes = _get_total_volumes(bb, book)
    # 简单映射：5卷对应5幕；卷数不固定时按比例分配
    if total_volumes <= 5:
        current_act = act_mapping.get(volume_index, '立身')
    else:
        # 超过5卷时，按进度比例映射到5幕
        act_idx = min(5, max(1, int((volume_index / total_volumes) * 5) + 1))
        current_act = act_mapping.get(act_idx, '立身')
    act_descriptions = {
        '立身': '主角登场、金手指获得、确立生存基础（1-5%）',
        '立足': '主角站稳脚跟、初露锋芒、建立基本人际网（5-25%）',
        '立势': '主角势力扩张、主要冲突激化、BOSS浮出（25-50%）',
        '立威': '主角与BOSS正面对抗、实力跃升、打脸高潮（50-75%）',
        '立命': '终局决战、伏笔回收、世界观全貌揭示（75-100%）',
    }

    # ===== 【P1弊端9修复】本卷起始章号 = 上一卷结束章号 + 1 =====
    # 【P1强化】解析失败（prev_volume_end_chapter==0 但 volume_index>1）时健壮回退，
    # 避免静默回退为1导致章号断裂
    if prev_volume_end_chapter > 0:
        start_chapter = prev_volume_end_chapter + 1
    elif volume_index > 1:
        start_chapter = _calc_start_chapter_fallback(volume_index, chapters_per_volume, existing_volumes_for_ctx)
        # 记录告警供调试（写入日志，不打断流程）
        app.logger.warning(f'ai_outline_volume: 卷{volume_index} start_chapter 解析失败，回退为 {start_chapter}')
    else:
        start_chapter = 1

    # ===== 【P0弊端1修复】卷间衔接约束提示词 =====
    cohesion_constraint = ''
    if volume_index > 1 and prev_volume_ending_hook:
        cohesion_constraint = f"""
【卷间衔接铁律】（本卷为第{volume_index}卷，必须严格承接第{volume_index-1}卷）
- 上一卷卷尾钩子：{prev_volume_ending_hook}
- 上一卷核心主线：{prev_volume_summary[:200] if prev_volume_summary else '（无）'}
- 本卷开头必须承接上一卷卷尾钩子的悬念/危机，不得凭空开启新场景
- 本卷第一个情节节点的起始章号必须为 {start_chapter}（上一卷结束于第{prev_volume_end_chapter}章）
- 本卷 nodes 的 chapters 字段必须从 {start_chapter} 开始连续编号，不得与上一卷重叠"""
    elif volume_index > 1:
        cohesion_constraint = f"""
【卷间衔接铁律】（本卷为第{volume_index}卷）
- 本卷开头必须承接上一卷的结尾场景与悬念，不得凭空跳跃
- 本卷第一个情节节点的起始章号必须为 {start_chapter}"""

    system_prompt = f"""你是番茄小说金番作者级别的卷纲设计师。
任务：为第 {volume_index} 卷「{volume_title}」生成详细大纲+情节节点。

【五幕模型对齐】本卷对应五幕中的「{current_act}」幕：{act_descriptions.get(current_act, '')}
本卷情节设计必须服务于该幕的核心目标，不得脱离五幕结构。

【输出格式】严格输出以下JSON（不要包裹在markdown代码块中）：
{{
  "volume_index": {volume_index},
  "volume_title": "{volume_title}",
  "act": "{current_act}",
  "core_goal": "本卷核心目标",
  "core_conflict": "本卷主要冲突",
  "emotion_driver": "情感驱动力",
  "key_turns": ["转折点1", "转折点2", "转折点3"],
  "boss": "本卷BOSS",
  "foreshadow_new": ["新埋伏笔1"],
  "foreshadow_recycle": ["回收伏笔1"],
  "hook_type": "卷尾钩子类型",
  "ending_hook": "本卷卷尾钩子具体内容（下一卷开头需承接此钩子）",
  "nodes": [
    {{"title": "节点1", "chapters": "{start_chapter}-{start_chapter+9}", "type": "M", "summary": "概要", "cool_type": "实力碾压"}}
  ]
}}
{cohesion_constraint}

【章型配额】M主线50%/C角色10%/W世界观10%/D日常20%/F伏笔10%
【小故事闭环】新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子（5-8章）
本卷约 {chapters_per_volume} 章（约 {chapters_per_volume * 2400} 字），分5-8个情节节点。节点 chapters 必须从 {start_chapter} 开始连续递增。
【节点容量铁律】每个情节节点的 summary 必须足够支撑其 chapters 范围内的字数容量（按每章2400字估算），不得过于简略；各节点剧情须与上下卷节点连贯，避免剧情断裂。

{skill_note}"""

    user_prompt = f"""书名：{book.title}

{_build_core_params_block(bb, book)}

{f"【五幕式总纲】{chr(10)}{master_outline}" if has_master else "【五幕式总纲】（暂无，请基于下方已有剧情/卷大纲自行推演本卷情节节点，但必须符合五幕模型中「" + current_act + "」幕的定位）"}

【已有剧情】（含已生成卷纲，本卷需与之衔接）
{existing_timeline or '（暂无）'}

【本卷在已有剧情中的定位】请优先基于本卷（第""" + f"{volume_index}卷「{volume_title}」" + """）已有的 main_plot/key_events 设计节点；若已有剧情为空，则基于世界观、规则、人物合理推演。

【世界观设定】（情节节点需符合世界观规则）
""" + (worldbuilding_ctx or '（暂无）') + f"""

【核心规则】（金手指/能力限制等，不可违反）
{key_rules_ctx or '（暂无）'}

【人物档案】（情节节点需涉及这些角色，安排其互动）
{characters_ctx or '（暂无）'}

【上一卷结尾章节正文】（卷间衔接依据，本卷开头必须承接）
{prev_volume_end_summary or '（本卷为第一卷，无前文）'}

请为第 {volume_index} 卷生成详细大纲。"""

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=0, temperature=0.7
    )
    if err:
        return jsonify({'error': err}), 500

    # 健壮 JSON 提取：处理 markdown 代码块、前后说明文字、尾随逗号等
    volume_data, json_err = _extract_json_from_llm(content, expect='object')
    if json_err:
        return jsonify({'error': 'AI返回格式错误，无法解析JSON', 'raw': content[:500], 'detail': json_err}), 500

    volume_text = f"""【第{volume_index}卷：{volume_data.get('volume_title', volume_title)}】
核心目标：{volume_data.get('core_goal', '')}
核心冲突：{volume_data.get('core_conflict', '')}
情感驱动：{volume_data.get('emotion_driver', '')}
关键转折：{', '.join(volume_data.get('key_turns', []))}
BOSS：{volume_data.get('boss', '')}
新埋伏笔：{', '.join(volume_data.get('foreshadow_new', []))}
回收伏笔：{', '.join(volume_data.get('foreshadow_recycle', []))}
卷尾钩子：{volume_data.get('hook_type', '')}
情节节点：
""" + '\n'.join([f"  {n.get('chapters','')}: {n.get('title','')}（{n.get('type','M')}）- {n.get('summary','')}" for n in volume_data.get('nodes', [])])

    # 修复：timeline 写入改为 JSON 合并（按 volume_index 替换/追加），避免文本拼接导致前端 JSON.parse 失败
    volumes_list = []
    if bb.timeline:
        try:
            parsed_tl = json.loads(bb.timeline)
            if isinstance(parsed_tl, list):
                volumes_list = parsed_tl
        except (json.JSONDecodeError, ValueError):
            # 旧文本格式，丢弃重建（已在 volume_text 中保留可读副本，但优先用 JSON）
            volumes_list = []
    # 构造与 PlotPanel 兼容的卷对象
    vol_obj = {
        'volume_id': str(volume_index),
        'volume': volume_data.get('volume_title', volume_title),
        'volume_index': volume_index,
        'main_plot': volume_data.get('core_goal', ''),
        'core_conflict': volume_data.get('core_conflict', ''),
        'emotion_driver': volume_data.get('emotion_driver', ''),
        'key_events': volume_data.get('key_turns', []),
        'turning_points': volume_data.get('key_turns', []),
        'climax': volume_data.get('boss', ''),
        'ending': volume_data.get('hook_type', ''),
        'foreshadowing': volume_data.get('foreshadow_new', []),
        'foreshadow_recycle': volume_data.get('foreshadow_recycle', []),
        'nodes': volume_data.get('nodes', []),
        'raw_text': volume_text,  # 保留可读文本副本
    }
    # 按 volume_index 替换或追加
    replaced = False
    for i, v in enumerate(volumes_list):
        existing_idx = v.get('volume_index') or _extract_volume_index(v.get('volume', v.get('volume_id', '')))
        if str(existing_idx) == str(volume_index):
            volumes_list[i] = vol_obj
            replaced = True
            break
    if not replaced:
        volumes_list.append(vol_obj)
    # 按 volume_index 排序
    volumes_list.sort(key=lambda v: int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0))
    bb.timeline = json.dumps(volumes_list, ensure_ascii=False, indent=2)
    _sync_foreshadowings_to_volumes(bb)  # 【P0修复】自动同步本卷伏笔到 foreshadowing_volumes
    db.session.commit()

    return jsonify({'volume_data': volume_data, 'timeline': bb.timeline, 'bible': bb.to_dict()})


@app.route('/api/books/<book_id>/ai-extract-volumes-from-outline', methods=['POST'])
def ai_extract_volumes_from_outline(book_id):
    """从 plot_design 总纲一次性提取全部卷的剧情 JSON 数组，写入 timeline。
    替代前端逐卷循环调用 ai_outline_volume 的方式，更稳定不会中途失败。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb or not bb.plot_design or not bb.plot_design.strip():
        return jsonify({'error': '请先在大纲维度生成五幕式总纲'}), 400

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    volume_count = data.get('volume_count')  # 可选，不传则让AI自行决定

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'chapter_plan', 'tomato_outline'], mode='agent')

    # 卷数：优先前端传入（来自 book.total_volumes），回退 book.total_volumes，再回退让AI自行决定
    if not volume_count:
        try:
            volume_count = int(getattr(book, 'total_volumes', 0) or 0)
        except Exception:
            volume_count = 0

    # 上下文：总纲 + 世界观 + 规则 + 人物
    context_parts = [f'【五幕式总纲】\n{bb.plot_design[:4000]}']
    if bb.worldbuilding:
        context_parts.append(f'【世界观】\n{bb.worldbuilding[:1000]}')
    if bb.key_rules:
        context_parts.append(f'【核心规则】\n{bb.key_rules[:800]}')
    if bb.character_profiles:
        context_parts.append(f'【人物档案】\n{bb.character_profiles[:1000]}')
    context = '\n\n'.join(context_parts)

    count_hint = f'严格 {volume_count} 卷' if volume_count else '根据总纲内容自行决定合理的卷数（通常5-8卷）'

    # ===== 【P1弊端7修复】五幕模型对齐约束 =====
    act_descriptions = {
        '立身': '主角登场、金手指获得、确立生存基础（1-5%）',
        '立足': '主角站稳脚跟、初露锋芒、建立基本人际网（5-25%）',
        '立势': '主角势力扩张、主要冲突激化、BOSS浮出（25-50%）',
        '立威': '主角与BOSS正面对抗、实力跃升、打脸高潮（50-75%）',
        '立命': '终局决战、伏笔回收、世界观全貌揭示（75-100%）',
    }

    system_prompt = f"""你是番茄小说金番作者级别的剧情架构师。
任务：根据五幕式总纲，一次性提取全部卷的【主线剧情】，输出为 JSON 数组。

{_build_core_params_block(bb, book)}

【重要】本次只构建各卷主线剧情，**不生成情节节点**（nodes 输出为空数组）。情节节点由用户后续手动点击「节点设计」逐卷生成。

【五幕模型对齐】各卷必须对应五幕模型，确保全书结构完整：
- 第1卷对应「立身」幕：{act_descriptions['立身']}
- 第2卷对应「立足」幕：{act_descriptions['立足']}
- 第3卷对应「立势」幕：{act_descriptions['立势']}
- 第4卷对应「立威」幕：{act_descriptions['立威']}
- 第5卷对应「立命」幕：{act_descriptions['立命']}
（若卷数超过5卷，按进度比例映射到五幕；每卷 main_plot 中需标注所属幕）

【内容容量铁律】每卷固定 50 章约 12 万字（50章×2400字）。每卷 main_plot 必须足够丰满，能支撑 50 章 12 万字的内容容量，不得过于单薄；核心冲突、关键事件要充实，为后续节点设计留足展开空间。

【卷间衔接铁律】（多卷必须保持连贯）
- 第N卷的 ending_hook 必须与第N+1卷开头场景直接衔接
- 卷间不得出现剧情断裂或矛盾

【输入上下文】
{context}

【输出要求】严格输出 JSON 数组（不要包裹在 markdown 代码块中），{count_hint}。
每卷结构如下（nodes 必须为空数组，由后续节点设计填充）：
{{
  "volume_id": "1",
  "volume": "第1卷 卷名",
  "volume_index": 1,
  "act": "立身",
  "main_plot": "本卷主线剧情（150-300字，需标注所属幕，须支撑50章12万字容量）",
  "core_conflict": "本卷核心冲突",
  "emotion_driver": "情感驱动力",
  "key_events": ["关键事件1", "关键事件2", "关键事件3"],
  "turning_points": ["转折点1", "转折点2"],
  "climax": "本卷高潮/BOSS",
  "ending": "本卷结局/卷尾钩子",
  "ending_hook": "本卷卷尾钩子具体内容（下一卷开头需承接此钩子）",
  "foreshadowing": ["新埋伏笔1"],
  "nodes": []
}}

{skill_note}"""

    user_prompt = f'请根据五幕式总纲提取全部卷的主线剧情，{count_hint}。注意：只构建各卷主线，不要生成情节节点（nodes 留空）；各卷必须符合五幕模型定位且卷间 ending_hook 与开头严格衔接；每卷主线须能支撑50章12万字容量。'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=0, temperature=0.7
    )
    if err:
        return jsonify({'error': err}), 500

    import re

    def _safe_volume_index(v, i):
        """安全提取卷号：优先 volume_index，再 volume/volume_id，最后用序号。始终返回 int。"""
        raw = v.get('volume_index')
        if raw is None:
            raw = v.get('volume', v.get('volume_id', ''))
        idx = _extract_volume_index(raw)
        return idx if idx > 0 else (i + 1)

    volumes = None
    parse_error = None

    # 策略1：去除 markdown 代码块围栏后，直接尝试整段解析
    cleaned = content.strip()
    fence_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # 策略2：尝试整段 JSON 解析（数组 或 含 volumes 字段的对象）
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            volumes = parsed
        elif isinstance(parsed, dict):
            # 兼容 {"volumes": [...]} / {"data": [...]} / {"result": [...]} 等包裹
            for k in ('volumes', 'data', 'result', 'items', 'list'):
                if isinstance(parsed.get(k), list):
                    volumes = parsed[k]
                    break
            # 单个对象当一卷处理
            if volumes is None and 'volume' in parsed:
                volumes = [parsed]
    except (json.JSONDecodeError, ValueError) as e:
        parse_error = str(e)

    # 策略3：正则提取最外层数组（非贪婪优先，回退贪婪）
    if not volumes:
        for pattern in (r'\[\s*\{[\s\S]*\}\s*\]', r'\[[\s\S]*\]'):
            m = re.search(pattern, cleaned)
            if m:
                try:
                    cand = json.loads(m.group())
                    if isinstance(cand, list) and cand:
                        volumes = cand
                        break
                except (json.JSONDecodeError, ValueError) as e:
                    parse_error = str(e)

    if not volumes or not isinstance(volumes, list) or len(volumes) == 0:
        return jsonify({'error': 'AI返回格式错误，无法解析为JSON数组', 'raw': content[:800], 'parse_error': parse_error}), 500

    # 过滤非字典项并补全字段
    volumes = [v for v in volumes if isinstance(v, dict)]
    if not volumes:
        return jsonify({'error': 'AI返回的JSON数组中无有效卷数据', 'raw': content[:800]}), 500

    for i, v in enumerate(volumes):
        v['volume_index'] = _safe_volume_index(v, i)
        if 'volume_id' not in v:
            v['volume_id'] = str(v['volume_index'])
        if 'volume' not in v:
            v['volume'] = f'第{v["volume_index"]}卷'

    # 按 volume_index 排序（已确保为 int，不会抛异常）
    volumes.sort(key=lambda v: v['volume_index'])

    # ===== 【P0弊端3修复】写入逻辑改为 upsert 合并，而非整体覆盖 =====
    # 保留用户手工微调的卷数据，只更新本次生成的卷
    # 本次只生成主线（nodes 为空），若已有卷已设计过节点，保留其 nodes 不覆盖
    existing_volumes_list = []
    if bb.timeline:
        try:
            parsed_tl = json.loads(bb.timeline)
            if isinstance(parsed_tl, list):
                existing_volumes_list = [v for v in parsed_tl if isinstance(v, dict)]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 建立已有卷索引 → nodes 的映射，用于回填被保留的节点设计
    existing_nodes_map = {}
    for v in existing_volumes_list:
        idx = v.get('volume_index')
        if idx is not None:
            existing_nodes_map[int(idx)] = v.get('nodes') or []

    # 用本次生成的卷覆盖同 volume_index 的旧卷，保留未涉及的卷
    new_idx_set = {v['volume_index'] for v in volumes}
    merged_volumes = [v for v in existing_volumes_list if v.get('volume_index') not in new_idx_set]
    # 对本次生成的卷：若 nodes 为空且已有卷存在节点设计，回填保留
    for v in volumes:
        existing_ns = existing_nodes_map.get(int(v['volume_index']))
        if (not v.get('nodes')) and existing_ns:
            v['nodes'] = existing_ns
        merged_volumes.append(v)
    merged_volumes.sort(key=lambda v: int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0))

    bb.timeline = json.dumps(merged_volumes, ensure_ascii=False, indent=2)
    _sync_foreshadowings_to_volumes(bb)  # 【P0修复】自动同步各卷伏笔到 foreshadowing_volumes
    db.session.commit()
    return jsonify({'success': True, 'volumes': merged_volumes, 'bible': bb.to_dict()})


@app.route('/api/books/<book_id>/ai-reverse-generate-outline', methods=['POST'])
def ai_reverse_generate_outline(book_id):
    """反生成五幕式总纲：从已导入的各卷剧情（timeline）反向提炼五幕式总纲，
    自动填入大纲维度（plot_design）。打通「导入剧情大纲 → 大纲总纲」的反哺链路。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    # 必须有各卷剧情才能反推总纲
    volumes_data = []
    if bb.timeline:
        try:
            parsed = json.loads(bb.timeline)
            if isinstance(parsed, list) and parsed:
                volumes_data = [v for v in parsed if isinstance(v, dict)]
        except (json.JSONDecodeError, ValueError):
            pass
    if not volumes_data:
        return jsonify({'error': '请先导入剧情大纲或提取各卷，再反生成总纲'}), 400

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'tomato_outline'], mode='agent')

    # 整理各卷剧情摘要
    vol_summaries = []
    for v in volumes_data:
        idx = v.get('volume_index', '?')
        name = v.get('volume', f'第{idx}卷')
        main_plot = (v.get('main_plot') or '').strip()
        climax = (v.get('climax') or '').strip()
        ending = (v.get('ending') or '').strip()
        key_events = v.get('key_events') or []
        events_str = '；'.join(key_events) if key_events else ''
        parts = [f'第{idx}卷「{name}」']
        if main_plot:
            parts.append(f'主线：{main_plot[:200]}')
        if events_str:
            parts.append(f'关键事件：{events_str[:150]}')
        if climax:
            parts.append(f'高潮：{climax[:100]}')
        if ending:
            parts.append(f'结局/钩子：{ending[:100]}')
        vol_summaries.append(' | '.join(parts))
    volumes_text = '\n'.join(vol_summaries)

    # 五幕式总纲 = 立身卷/立足卷/立势卷/破局卷/收束卷 的全书结构
    existing_master = (bb.plot_design or '').strip()
    worldbuilding_ctx = (bb.worldbuilding or '')[:800]
    characters_ctx = (bb.character_profiles or '')[:800]

    system_prompt = f"""你是番茄小说金番作者级别的剧情架构师。
任务：根据已有的各卷剧情，反向提炼生成「五幕式总纲」，写入大纲维度。

【已有各卷剧情】
{volumes_text}

{f"【已有总纲（参考，可在其基础上完善）】{chr(10)}{existing_master[:1500]}" if existing_master else "【已有总纲】（暂无，需全新生成）"}

【世界观设定】
{worldbuilding_ctx or '（暂无）'}

【人物档案】
{characters_ctx or '（暂无）'}

【五幕式总纲格式要求】
严格按五幕结构输出，每幕对应一卷或多卷：
1. 立身卷（开局）：主角起点、金手指觉醒、核心矛盾引入
2. 立足卷（发展）：主角站稳脚跟、势力初成、第一波爽点
3. 立势卷（升级）：格局打开、对手升级、伏笔展开
4. 破局卷（高潮）：终极对决、伏笔回收、真相揭露
5. 收束卷（结局）：收尾、升华、留白/续集钩子

每幕需包含：本卷目标、核心冲突、关键转折、爽点设计、卷尾钩子。
总纲长度 800-1500 字，要能统领全书各卷。

{skill_note}"""

    user_prompt = '请根据上述各卷剧情，反向生成完整的五幕式总纲。直接输出总纲文本，不要包裹在代码块中。'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=0, temperature=0.6
    )
    if err:
        return jsonify({'error': err}), 500

    master_outline = content.strip()
    # 去除可能的代码围栏
    import re as _re3
    fence = _re3.search(r'```(?:markdown|text)?\s*([\s\S]*?)\s*```', master_outline)
    if fence:
        master_outline = fence.group(1).strip()

    # 写入 plot_design（大纲维度）
    bb.plot_design = master_outline
    db.session.commit()

    return jsonify({'success': True, 'master_outline': master_outline, 'bible': bb.to_dict()})


@app.route('/api/books/<book_id>/ai-import-plot-outline', methods=['POST'])
def ai_import_plot_outline(book_id):
    """导入剧情大纲文本，自动识别拆分到各卷（正则优先，AI兜底）。
    支持格式：第X卷/卷X/Volume X 等开头的卷标题 + 后续内容。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    data = request.json or {}
    outline_text = (data.get('outline_text') or '').strip()
    skill_pack_ids = data.get('skill_pack_ids', [])
    if not outline_text:
        return jsonify({'error': '请输入大纲文本'}), 400

    import re

    # ===== 第一步：增强正则匹配卷标题 =====
    # 支持格式：第X卷/卷X/Volume X/第X部/第X篇/第X部分/Chapter X/序章/楔子/卷壹貳叁/# 第X卷 等
    vol_pattern = re.compile(
        r'^(?:'
        r'第\s*([一二三四五六七八九十百零壹貳贰叁肆伍陆陸柒捌玖拾\d]+)\s*[卷部篇部分]'  # 第X卷/部/篇/部分
        r'|卷\s*([一二三四五六七八九十百零壹貳贰叁肆伍陆陸柒捌玖拾\d]+)'  # 卷X
        r'|Volume\s*(\d+)|Chapter\s*(\d+)'  # Volume X / Chapter X
        r'|(序章|楔子|引子|终章|尾声)'  # 序章/楔子等
        r'|#*\s*第\s*([一二三四五六七八九十百零壹貳贰叁肆伍陆陸柒捌玖拾\d]+)\s*卷'
        r')\s*[：:．.、\s\-—]*(.*)$',
        re.MULTILINE
    )
    matches = list(vol_pattern.finditer(outline_text))

    volumes = []
    if matches:
        # 有标准格式，按卷拆分（即使只有 1 个 match 也处理，避免单卷被丢弃）
        for i, m in enumerate(matches):
            # 提取卷号
            vol_num_str = m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(6) or ''
            vol_idx = _extract_volume_index(vol_num_str) or (i + 1)
            # 序章/楔子等特殊章节
            special = m.group(5)
            if special:
                vol_title = special
                vol_idx = 0 if special in ('序章', '楔子', '引子') else 999
            else:
                vol_title = (m.group(7) or '').strip() or f'第{vol_idx}卷'
            # 内容范围：从当前匹配结束到下一个匹配开始（最后一卷取到文末，不丢尾部）
            content_start = m.end()
            content_end = matches[i + 1].start() if i + 1 < len(matches) else len(outline_text)
            vol_content = outline_text[content_start:content_end].strip()

            volumes.append({
                'volume_id': str(vol_idx),
                'volume': f'第{vol_idx}卷 {vol_title}' if vol_title and not vol_title.startswith('第') and not special else vol_title,
                'volume_index': vol_idx,
                'main_plot': vol_content[:500],
                'core_conflict': '',
                'emotion_driver': '',
                'key_events': [l.strip().lstrip('·•*-') for l in vol_content.split('\n') if l.strip() and len(l.strip()) > 5][:8],
                'turning_points': [],
                'climax': '',
                'ending': '',
                'foreshadowing': [],
                'nodes': [],
                'raw_text': vol_content,
            })

    # 【P0修复】正则路径补全：为每卷生成 nodes（章号全书连续）+ ending_hook，
    # 避免后续滚动生成时 start_chapter 解析失败导致章号断裂
    if matches and volumes:
        # 取每卷章数：优先用户大纲隐含的，否则默认50；特殊卷（序章等）1章
        cpc = data.get('chapters_per_volume', 50) or 50
        ch_cursor = 1
        for vi, v in enumerate(volumes):
            special = v.get('volume_index', 0) in (0, 999)  # 序章/终章
            vol_chs = 1 if special else cpc
            start = ch_cursor
            end = ch_cursor + vol_chs - 1
            # 把卷内容按 key_events 等分为最多5个节点
            ke = v.get('key_events') or []
            if not ke:
                ke = ['开局', '发展', '转折', '高潮', '收束'][:5]
            n_nodes = min(5, max(1, len(ke)))
            step = vol_chs / n_nodes
            nodes = []
            for ni in range(n_nodes):
                ns = start + int(step * ni)
                ne = start + int(step * (ni + 1)) - 1 if ni < n_nodes - 1 else end
                if ne < ns:
                    ne = ns
                ntype = ['M', 'D', 'T', 'C', 'F'][ni] if ni < 5 else 'M'  # 开局/发展/转折/高潮/收束
                nodes.append({
                    'chapters': f'{ns}-{ne}',
                    'type': ntype,
                    'title': ke[ni] if ni < len(ke) else f'节点{ni+1}',
                    'summary': (ke[ni] if ni < len(ke) else '')[:80],
                })
            v['nodes'] = nodes
            # ending_hook：取关键事件最后一条或 main_plot 末句
            last_ke = ke[-1] if ke else ''
            v['ending'] = (last_ke or v.get('main_plot', '')[-100:])[:150]
            if not v.get('core_conflict'):
                v['core_conflict'] = (ke[0] if ke else '')[:100]
            ch_cursor = end + 1

    # 如果正则匹配到第一个 match 之前还有内容，作为"开篇/引言"归入第一卷或单独成卷
    if matches and matches[0].start() > 0:
        head_content = outline_text[:matches[0].start()].strip()
        if head_content and len(head_content) > 20:
            # 归入第一卷的 main_plot 前置
            if volumes:
                volumes[0]['main_plot'] = head_content[:300] + '\n' + volumes[0]['main_plot']

    # ===== 第二步：正则匹配失败或卷数<1，调用 AI 智能拆卷（改进版） =====
    if len(volumes) < 1:
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'chapter_plan', 'tomato_outline'], mode='agent')
        # 上下文增强：注入总纲、规则、已有卷、世界观、人物
        ctx_parts = []
        if bb.plot_design:
            ctx_parts.append(f'【五幕式总纲（重要：决定全书卷数和卷目标）】\n{bb.plot_design[:2500]}')
        if bb.key_rules:
            ctx_parts.append(f'【核心规则】\n{bb.key_rules[:800]}')
        if bb.worldbuilding:
            ctx_parts.append(f'【世界观】\n{bb.worldbuilding[:800]}')
        if bb.character_profiles:
            ctx_parts.append(f'【人物档案】\n{bb.character_profiles[:800]}')
        if bb.timeline:
            try:
                existing_vols = json.loads(bb.timeline)
                if isinstance(existing_vols, list) and existing_vols:
                    existing_summary = '；'.join([f"第{v.get('volume_index','?')}卷:{v.get('main_plot','')[:30]}" for v in existing_vols[:10]])
                    ctx_parts.append(f'【已有卷剧情（避免冲突，可按volume_index合并或追加）】\n{existing_summary}')
            except (json.JSONDecodeError, ValueError):
                pass
        extra_ctx = '\n\n'.join(ctx_parts)

        system_prompt = f"""你是番茄小说金番作者级别的剧情架构师。
任务：将用户粘贴的大纲文本**严格按原文的卷划分**拆分为 JSON 数组。

【已有设定参考】
{extra_ctx or '（暂无）'}

【拆卷铁律】
1. **必须严格按原文的卷划分提取**，不得合并、拆分、重排原文卷
2. 原文有明确的卷标题（如"第X卷/卷X/Volume X/序章/楔子"）时，每个标题对应一个卷对象
3. 原文没有明确卷划分时，才可按剧情自然分段（每卷对应一个完整的故事弧线）
4. **不得遗漏任何卷**：从原文开头到结尾，每个段落都必须归属到某个卷
5. main_plot 必须从原文对应段落提取或概括，不得凭空捏造
6. 卷数应与【五幕式总纲】中暗示的卷数一致（若总纲存在）

【输出要求】严格输出 JSON 数组（不要包裹在 markdown 代码块中）。
每卷结构：
{{
  "volume_id": "1",
  "volume": "第1卷 卷名",
  "volume_index": 1,
  "main_plot": "本卷主线剧情（100-300字，从原文提取）",
  "core_conflict": "核心冲突",
  "emotion_driver": "情感驱动",
  "key_events": ["关键事件1", "关键事件2"],
  "turning_points": ["转折点1"],
  "climax": "高潮",
  "ending": "结局/钩子",
  "foreshadowing": ["伏笔1"],
  "nodes": [
    {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "爽点类型"}}
  ]
}}

【情节节点设计要求】
- 每卷生成 5-8 个情节节点
- 章型配额：M主线50%/C角色10%/W世界观10%/D日常20%/F伏笔10%
- 相邻节点章型不同，每卷覆盖整卷章节范围
- 小故事闭环：新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子（5-8章）

{skill_note}"""

        # 取消 6000 字截断，支持超长大纲（分块处理）
        max_input = 12000
        outline_chunk = outline_text[:max_input]
        if len(outline_text) > max_input:
            outline_chunk += f'\n\n[注：原文共 {len(outline_text)} 字，已截取前 {max_input} 字，请确保覆盖全部卷]'

        user_prompt = f'请严格按原文卷划分拆分以下大纲文本，不得遗漏任何卷：\n\n{outline_chunk}'

        content, err = _call_llm(
            [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
            max_tokens=0, temperature=0.2
        )
        if err:
            return jsonify({'error': err}), 500

        # 稳健解析 AI 返回（三策略：整段→对象包裹→正则数组），与 ai_extract_volumes_from_outline 一致
        import re as _re2
        cleaned = content.strip()
        fence_match = _re2.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        ai_volumes = None
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                ai_volumes = parsed
            elif isinstance(parsed, dict):
                for k in ('volumes', 'data', 'result', 'items', 'list'):
                    if isinstance(parsed.get(k), list):
                        ai_volumes = parsed[k]
                        break
                if ai_volumes is None and 'volume' in parsed:
                    ai_volumes = [parsed]
        except (json.JSONDecodeError, ValueError):
            pass

        if not ai_volumes:
            for pattern in (r'\[\s*\{[\s\S]*\}\s*\]', r'\[[\s\S]*\]'):
                m = _re2.search(pattern, cleaned)
                if m:
                    try:
                        cand = json.loads(m.group())
                        if isinstance(cand, list) and cand:
                            ai_volumes = cand
                            break
                    except (json.JSONDecodeError, ValueError):
                        pass

        if not ai_volumes:
            return jsonify({'error': 'AI返回格式错误，无法解析为JSON数组', 'raw': content[:500]}), 500

        volumes = ai_volumes

        # 补全字段 + 安全 volume_index（避免 int() 异常）
        def _safe_idx(v, i):
            raw = v.get('volume_index')
            if raw is None:
                raw = v.get('volume', v.get('volume_id', ''))
            idx = _extract_volume_index(raw)
            return idx if idx > 0 else (i + 1)

        for i, v in enumerate(volumes):
            v['volume_index'] = _safe_idx(v, i)
            if 'volume_id' not in v:
                v['volume_id'] = str(v['volume_index'])
            if 'volume' not in v:
                v['volume'] = f'第{v["volume_index"]}卷'
            if 'nodes' not in v:
                v['nodes'] = []

    # 合并到已有 timeline：按 volume_index(int) 精确替换或追加，避免空 index 互相覆盖
    existing_volumes = []
    if bb.timeline:
        try:
            parsed = json.loads(bb.timeline)
            if isinstance(parsed, list):
                existing_volumes = [v for v in parsed if isinstance(v, dict)]
        except (json.JSONDecodeError, ValueError):
            existing_volumes = []

    def _vol_int_idx(v, fallback=0):
        """安全提取 int 卷号，失败回退 fallback。"""
        raw = v.get('volume_index')
        if raw is None:
            raw = v.get('volume', v.get('volume_id', ''))
        idx = _extract_volume_index(raw)
        return idx if idx > 0 else fallback

    # 建立 existing 的 index→位置 映射（用 int 精确匹配，不再用 str 比对）
    existing_idx_map = {}
    for i, ev in enumerate(existing_volumes):
        ei = _vol_int_idx(ev)
        if ei > 0 and ei not in existing_idx_map:
            existing_idx_map[ei] = i

    for new_v in volumes:
        ni = _vol_int_idx(new_v)
        if ni > 0 and ni in existing_idx_map:
            existing_volumes[existing_idx_map[ni]] = new_v
        else:
            existing_volumes.append(new_v)

    # 按 int 卷号稳定排序（相同 index 保持原顺序，不会前后颠倒）
    existing_volumes.sort(key=lambda v: _vol_int_idx(v, 9999))

    bb.timeline = json.dumps(existing_volumes, ensure_ascii=False, indent=2)
    _sync_foreshadowings_to_volumes(bb)  # 【P0修复】导入剧情后同步各卷伏笔到 foreshadowing_volumes
    db.session.commit()

    return jsonify({'success': True, 'volumes': existing_volumes, 'imported_count': len(volumes), 'bible': bb.to_dict()})


# ==== 总 AI 创作：总览全局各维度，用户确认后填入 ====
@app.route('/api/books/<book_id>/ai-master-create', methods=['POST'])
def ai_master_create(book_id):
    """总AI创作：按番茄金番工作流串行协同，本轮产出回流给下一轮作为上下文。
    维度依赖图（agent 协同）：concept → key_rules → worldbuilding → character_profiles → plot_design → timeline
    每个维度都能看到本轮已生成的所有上游维度产物。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()
    # P0-3修复：首次创建 bible 时同步 book 的 total_volumes/novel_styles，
    # 否则首次总创作时风格流派注入为空、卷数约束失效
    _sync_book_meta_to_bible(book, bb)

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    dimensions = data.get('dimensions', ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design'])
    instruction = data.get('instruction', '')
    # 本轮会话已生成但未确认的内容（跨维度实时互通）
    session_outputs = data.get('session_outputs') or {}

    DIM_MAP = MASTER_DIM_MAP  # 复用公共常量
    DIM_ORDER = MASTER_DIM_ORDER
    ordered_dims = [d for d in DIM_ORDER if d in dimensions]

    # 上下文字典：本轮已生成内容(session_outputs)优先，回退 bible 已有内容
    ctx = _build_master_ctx(bb, session_outputs)

    # 防遗忘检查诊断回注（让维度创作也规避已诊断出的问题）
    af_alerts_master = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
    af_block_master = ''
    if af_alerts_master:
        af_block_master = f"""
【防遗忘检查诊断】（最近检查发现的问题，本维度创作必须保持一致、主动规避）
{af_alerts_master}
"""

    # 已写剧情上下文（章节摘要+动态报告+伏笔图+关系图），让维度创作感知已写正文
    storyline_ctx = _build_master_storyline_ctx(book_id, bb)
    storyline_block = ''
    if storyline_ctx:
        storyline_block = f"""
【已写剧情参照】（已写章节的实际剧情，本维度创作须与之保持一致，不可矛盾）
{storyline_ctx}
"""

    results = []
    for dim in ordered_dims:
        info = DIM_MAP[dim]
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', info['keys'], mode='agent')
        # 【P1修复】格式整合铁律：技能包内容是创作方法论，输出必须严格按平台维度的格式骨架
        # 即使技能包自带独立格式要求（如CDL档案/五不妥协原则），模型也要按平台格式整合映射
        format_integration_note = ''
        if skill_note:
            format_integration_note = '\n\n【格式整合铁律·必读】上方「技能包内容」是创作方法论（指导原则），不是输出格式模板。必须严格按下方任务的「输出格式」骨架输出，技能包的要求整合映射到对应字段。例如：技能包要求"CDL角色档案"中的"外貌特征/战斗风格"应并入角色卡对应字段；技能包要求"五不妥协原则"应整合到对应小节内。不要把技能包字段原样搬出来，要按平台格式重新组织。'

        # 按 DAG 依赖图只注入该维度的上游维度产物（不再无差别全注入）
        upstream_ctx = _build_master_upstream_ctx(dim, ctx)

        # 标注当前维度在 workflow 中的位置
        dim_idx = DIM_ORDER.index(dim)
        upstream_names = [DIM_MAP[d]['label'] for d in DIM_ORDER[:dim_idx] if ctx[d].strip()]
        downstream_names = [DIM_MAP[d]['label'] for d in DIM_ORDER[dim_idx+1:] if d in ordered_dims]
        position_note = f'你正在执行第 {dim_idx+1}/{len(DIM_ORDER)} 步：{info["label"]}设计'
        if upstream_names:
            position_note += f'（上游已完成：{"→".join(upstream_names)}）'
        if downstream_names:
            position_note += f'（下游将基于你的产出继续：{"→".join(downstream_names)}）'

        # ===== 【P0弊端4修复】timeline 维度输出 JSON 数组格式，与 _get_volume_outline 兼容 =====
        # 【P1修复】硬性铁律：卷数严格按用户选择的总卷数，卷序号连续，chapters 编号全书连续
        is_timeline_dim = (dim == 'timeline')
        core_params_block = _build_core_params_block(bb, book)
        if is_timeline_dim:
            tv_for_timeline = _get_total_volumes(bb, book)
            chapters_per_vol = _get_chapters_per_volume(bb, book)
            total_chapters = tv_for_timeline * chapters_per_vol
            system_prompt = f"""你是番茄小说金番作者级别的{info['label']}设计师，正在与其他维度设计师协同创作。
{position_note}

任务：{info['prompt']}

书名：{book.title}

{core_params_block}

【已确认的上游维度产物】（必须在你的产出中保持一致，不可与上游矛盾）
{upstream_ctx}
{storyline_block}{af_block_master}
【五幕模型对齐】各卷对应五幕：立身→立足→立势→立威→立命
【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；各卷 nodes.chapters 全书连续编号。

【分卷铁律·必读】**全书共 {tv_for_timeline} 卷，每卷约 {chapters_per_vol} 章，全书约 {total_chapters} 章**。卷序号从 1 开始连续递增到 {tv_for_timeline}。卷名格式"第N卷 副标题"。必须覆盖全部 {tv_for_timeline} 卷，不得多不得少。

{skill_note}{format_integration_note}

【输出格式铁律】严格输出 JSON 数组（不要包裹在 markdown 代码块中），每卷结构如下：
[
  {{
    "volume_id": "1",
    "volume": "第1卷 副标题",
    "volume_index": 1,
    "act": "立身",
    "main_plot": "本卷主线剧情（100-200字）",
    "core_conflict": "本卷核心冲突",
    "ending_hook": "本卷卷尾钩子具体内容",
    "nodes": [
      {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "实力碾压"}}
    ]
  }}
]

【分卷章节分配】全书 {total_chapters} 章 → {tv_for_timeline} 卷（每卷 {chapters_per_vol} 章）：第1卷 1-{chapters_per_vol}、第2卷 {chapters_per_vol+1}-{chapters_per_vol*2}、... 第{tv_for_timeline}卷 {(tv_for_timeline-1)*chapters_per_vol+1}-{total_chapters}；每卷 nodes 章节连续不重叠。
直接输出 JSON 数组，不要任何解释性文字。"""
            user_prompt = instruction or f'请为这本小说生成分卷剧情JSON数组，**共 {tv_for_timeline} 卷，每卷约 {chapters_per_vol} 章**，与已确认的上游维度保持一致，各卷符合五幕模型且卷间严格衔接。'
        else:
            system_prompt = f"""你是番茄小说金番作者级别的{info['label']}设计师，正在与其他维度设计师协同创作。
{position_note}

任务：{info['prompt']}

书名：{book.title}

{core_params_block}

【已确认的上游维度产物】（必须在你的产出中保持一致，不可与上游矛盾）
{upstream_ctx}
{storyline_block}{af_block_master}
{skill_note}{format_integration_note}

直接输出{info['label']}内容（严格按任务要求的格式铁律输出）。确保与上游维度衔接一致。"""
            user_prompt = instruction or f'请为这本小说生成{info["label"]}，与已确认的上游维度保持一致，并严格按输出格式铁律输出。'

        # P0-1修复：各维度创作不限制输出 token，避免 timeline 等大维度 JSON 被截断导致"只能生成几卷"
        content, err = _call_llm(
            [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
            max_tokens=0, temperature=0.7
        )
        if err:
            results.append({'dimension': dim, 'label': info['label'], 'field': info['field'], 'error': err})
        else:
            # timeline 维度：校验 JSON 格式，失败则保留原文但标记警告
            if is_timeline_dim:
                import re as _re_mc
                cleaned_mc = content.strip()
                fence_mc = _re_mc.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned_mc)
                if fence_mc:
                    cleaned_mc = fence_mc.group(1).strip()
                parsed_tl_mc = None
                try:
                    parsed_tl_mc = json.loads(cleaned_mc)
                    if isinstance(parsed_tl_mc, dict):
                        for k in ('volumes', 'data', 'result', 'items', 'list'):
                            if isinstance(parsed_tl_mc.get(k), list):
                                parsed_tl_mc = parsed_tl_mc[k]
                                break
                except (json.JSONDecodeError, ValueError):
                    # 尝试正则提取数组
                    m_mc = _re_mc.search(r'\[\s*\{[\s\S]*\}\s*\]', cleaned_mc) or _re_mc.search(r'\[[\s\S]*\]', cleaned_mc)
                    if m_mc:
                        try:
                            parsed_tl_mc = json.loads(m_mc.group())
                        except (json.JSONDecodeError, ValueError):
                            parsed_tl_mc = None
                if isinstance(parsed_tl_mc, list) and parsed_tl_mc:
                    content = json.dumps(parsed_tl_mc, ensure_ascii=False, indent=2)
                # 若解析失败，content 保留原文（纯文本），_get_volume_outline 会走纯文本回退分支
            results.append({'dimension': dim, 'label': info['label'], 'field': info['field'], 'content': content})
            # 关键：本轮产出回流到 ctx，供下一轮维度作为上游上下文
            ctx[dim] = content
            # P1-9: 直接落库，避免前端未回流导致协同结果丢失
            setattr(bb, info['field'], content)
            # P0-2：伏笔维度生成后自动构建 DAG（与文本字段并存）
            if dim == 'foreshadowing' and parse_text_to_dag:
                try:
                    graph = parse_text_to_dag(content)
                    errors = graph.validate()
                    if not errors:
                        bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
                except Exception:
                    pass  # DAG 构建失败不影响文本落库
            # P1-4：timeline 维度生成后自动构建四级大纲层级
            if dim == 'timeline' and build_outline_hierarchy:
                try:
                    hierarchy = build_outline_hierarchy(content, bb.plot_design or '')
                    if hierarchy.get('chapters'):
                        bb.outline_hierarchy = json.dumps(hierarchy, ensure_ascii=False)
                except Exception:
                    pass

    # P1-9: 串行生成完成后统一提交事务
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'落库失败: {str(e)}', 'results': results}), 500

    # 返回最新 bible 供前端同步状态
    return jsonify({'results': results, 'bible': bb.to_dict()})


@app.route('/api/books/<book_id>/ai-master-create/stream', methods=['POST'])
@login_required
def ai_master_create_stream(book_id):
    """总AI创作流式版（SSE）：按番茄金番工作流串行协同，逐维度流式输出。
    每个维度生成时注入已有bible维度+本轮上游已生成维度作为上下文，保证一致性。
    SSE 格式：
      data: {"dim":"concept","label":"构思","start":true}\n\n
      data: {"choices":[{"delta":{"content":"..."}}]}\n\n  (流式chunk)
      data: {"dim":"concept","done":true}\n\n
      ... 下一维度 ...
    """
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()
    # P0-3修复：首次创建 bible 时同步 book 的 total_volumes/novel_styles
    _sync_book_meta_to_bible(book, bb)

    data = request.json or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    dimensions = data.get('dimensions', ['concept', 'key_rules', 'worldbuilding', 'character_profiles', 'plot_design'])
    instruction = data.get('instruction', '')
    # 本轮会话已生成但尚未"确认填入"的维度内容（前端传入，用于跨维度实时互通）
    # 优先级：本轮已生成 > bible 已有内容（本轮产出是最新的协作基线）
    session_outputs = data.get('session_outputs') or {}

    # 复用公共常量
    DIM_MAP = MASTER_DIM_MAP
    DIM_ORDER = MASTER_DIM_ORDER
    ordered_dims = [d for d in DIM_ORDER if d in dimensions]

    # 上下文：本轮已生成(session_outputs)优先，回退 bible 已有内容
    ctx = _build_master_ctx(bb, session_outputs)

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('creation') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 防遗忘检查诊断回注（让维度创作也规避已诊断出的问题）
    af_alerts_master = _collect_anti_forget_alerts(bb, max_reports=2, max_alerts=8)
    af_block_master = ''
    if af_alerts_master:
        af_block_master = f"""
【防遗忘检查诊断】（最近检查发现的问题，本维度创作必须保持一致、主动规避）
{af_alerts_master}
"""

    # 已写剧情上下文（章节摘要+动态报告+伏笔图+关系图），让维度创作感知已写正文
    storyline_ctx = _build_master_storyline_ctx(book_id, bb)
    storyline_block = ''
    if storyline_ctx:
        storyline_block = f"""
【已写剧情参照】（已写章节的实际剧情，本维度创作须与之保持一致，不可矛盾）
{storyline_ctx}
"""

    def generate():
        try:
            for dim in ordered_dims:
                info = DIM_MAP[dim]
                # 推送维度开始信号
                yield f'data: {json.dumps({"dim": dim, "label": info["label"], "start": True}, ensure_ascii=False)}\n\n'

                skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', info['keys'], mode='agent')
                format_integration_note = ''
                if skill_note:
                    format_integration_note = '\n\n【格式整合铁律·必读】上方「技能包内容」是创作方法论（指导原则），不是输出格式模板。必须严格按下方任务的「输出格式」骨架输出，技能包的要求整合映射到对应字段。'

                # 按 DAG 依赖图只注入该维度的上游维度产物（不再无差别全注入）
                upstream_ctx = _build_master_upstream_ctx(dim, ctx)

                dim_idx = DIM_ORDER.index(dim)
                upstream_names = [DIM_MAP[d]['label'] for d in DIM_ORDER[:dim_idx] if ctx[d].strip()]
                downstream_names = [DIM_MAP[d]['label'] for d in DIM_ORDER[dim_idx+1:] if d in ordered_dims]
                position_note = f'你正在执行第 {dim_idx+1}/{len(DIM_ORDER)} 步：{info["label"]}设计'
                if upstream_names:
                    position_note += f'（上游已完成：{"→".join(upstream_names)}）'
                if downstream_names:
                    position_note += f'（下游将基于你的产出继续：{"→".join(downstream_names)}）'

                is_timeline_dim = (dim == 'timeline')
                core_params_block = _build_core_params_block(bb, book)
                if is_timeline_dim:
                    tv_for_timeline = _get_total_volumes(bb, book)
                    chapters_per_vol = _get_chapters_per_volume(bb, book)
                    total_chapters = tv_for_timeline * chapters_per_vol
                    system_prompt = f"""你是番茄小说金番作者级别的{info['label']}设计师，正在与其他维度设计师协同创作。
{position_note}

任务：{info['prompt']}

书名：{book.title}

{core_params_block}

【已确认的上游维度产物】（必须在你的产出中保持一致，不可与上游矛盾）
{upstream_ctx}
{storyline_block}{af_block_master}
【五幕模型对齐】各卷对应五幕：立身→立足→立势→立威→立命
【卷间衔接铁律】第N卷 ending_hook 必须与第N+1卷开头严格衔接；各卷 nodes.chapters 全书连续编号。

【分卷铁律·必读】**全书共 {tv_for_timeline} 卷，每卷约 {chapters_per_vol} 章，全书约 {total_chapters} 章**。卷序号从 1 开始连续递增到 {tv_for_timeline}。卷名格式"第N卷 副标题"。必须覆盖全部 {tv_for_timeline} 卷，不得多不得少。

{skill_note}{format_integration_note}

【输出格式铁律】严格输出 JSON 数组（不要包裹在 markdown 代码块中）。直接输出 JSON 数组，不要任何解释性文字。"""
                    user_prompt = instruction or f'请为这本小说生成分卷剧情JSON数组，**共 {tv_for_timeline} 卷，每卷约 {chapters_per_vol} 章**，与已确认的上游维度保持一致，各卷符合五幕模型且卷间严格衔接。'
                else:
                    system_prompt = f"""你是番茄小说金番作者级别的{info['label']}设计师，正在与其他维度设计师协同创作。
{position_note}

任务：{info['prompt']}

书名：{book.title}

{core_params_block}

【已确认的上游维度产物】（必须在你的产出中保持一致，不可与上游矛盾）
{upstream_ctx}
{storyline_block}{af_block_master}
{skill_note}{format_integration_note}

直接输出{info['label']}内容（严格按任务要求的格式铁律输出）。确保与上游维度衔接一致。"""
                    user_prompt = instruction or f'请为这本小说生成{info["label"]}，与已确认的上游维度保持一致，并严格按输出格式铁律输出。'

                # P0-1修复：流式维度创作不限制输出 token，避免大维度 JSON 被截断
                # （不下发 max_tokens 字段，让模型用自身默认输出上限）

                # 流式调用 LLM
                base = base_url.rstrip('/')
                if not base.endswith('/v1'):
                    base += '/v1'
                resp = requests.post(f'{base}/chat/completions',
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json={'model': model,
                          'messages': [{'role': 'system', 'content': system_prompt},
                                       {'role': 'user', 'content': user_prompt}],
                          'temperature': 0.7,
                          'stream': True},
                    stream=True, timeout=180)
                full_content = ''
                for line in resp.iter_lines():
                    if line:
                        line = line.decode('utf-8')
                        if line.startswith('data: '):
                            chunk = line[6:]
                            if chunk == '[DONE]':
                                break
                            # 透传 chunk 给前端
                            yield f'data: {chunk}\n\n'
                            try:
                                parsed = json.loads(chunk)
                                delta = parsed.get('choices', [{}])[0].get('delta', {}).get('content', '')
                                full_content += delta
                            except:
                                pass

                # 维度完成信号
                # timeline 维度：校验 JSON
                if is_timeline_dim and full_content:
                    import re as _re_s
                    cleaned = full_content.strip()
                    fence = _re_s.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned)
                    if fence:
                        cleaned = fence.group(1).strip()
                    try:
                        parsed_tl = json.loads(cleaned)
                        if isinstance(parsed_tl, dict):
                            for k in ('volumes', 'data', 'result', 'items', 'list'):
                                if isinstance(parsed_tl.get(k), list):
                                    parsed_tl = parsed_tl[k]
                                    break
                        if isinstance(parsed_tl, list) and parsed_tl:
                            full_content = json.dumps(parsed_tl, ensure_ascii=False, indent=2)
                    except:
                        pass

                # 回流到 ctx 供下一维度使用
                ctx[dim] = full_content
                # P3-11：事务性落库 —— 只 setattr 到 session，不立即 commit，
                # 全部维度完成后统一提交，避免中途失败/用户取消导致部分维度落库、bible 状态不一致
                try:
                    setattr(bb, info['field'], full_content)
                    # P0-2：伏笔维度生成后自动构建 DAG（与文本字段并存）
                    if dim == 'foreshadowing' and parse_text_to_dag:
                        try:
                            graph = parse_text_to_dag(full_content)
                            errors = graph.validate()
                            if not errors:
                                bb.foreshadowing_graph = json.dumps(graph.to_dict(), ensure_ascii=False)
                        except Exception:
                            pass  # DAG 构建失败不影响文本落库
                    # P1-4：timeline 维度生成后自动构建四级大纲层级
                    if dim == 'timeline' and build_outline_hierarchy:
                        try:
                            hierarchy = build_outline_hierarchy(full_content, bb.plot_design or '')
                            if hierarchy.get('chapters'):
                                bb.outline_hierarchy = json.dumps(hierarchy, ensure_ascii=False)
                        except Exception:
                            pass  # 层级构建失败不影响文本落库
                    # 注意：此处不再立即 commit，延迟到所有维度完成后统一提交
                except Exception:
                    db.session.rollback()

                yield f'data: {json.dumps({"dim": dim, "done": True}, ensure_ascii=False)}\n\n'

            # P3-11：所有维度生成完成后统一提交事务（事务性落库）
            try:
                db.session.commit()
            except Exception as commit_err:
                db.session.rollback()
                yield f'data: {json.dumps({"error": f"统一落库失败: {str(commit_err)[:200]}"}, ensure_ascii=False)}\n\n'

            yield 'data: [DONE]\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"error": str(e)[:300]}, ensure_ascii=False)}\n\n'

    # stream_with_context 保持请求/应用上下文，避免生成器在 yield 后恢复时
    # 触发 "Working outside of application context"（DB 落库需要上下文）
    # 响应头修复：禁止 Cloudflare/Render 代理缓冲 SSE 流（同 ai_continue_batch_stream）
    resp = app.response_class(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp


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
    skill_pack_ids = request.json.get('skill_pack_ids', [])  # 支持技能包注入

    if not concept.strip():
        return jsonify({'error': '请输入一句话构思'}), 400

    bb = BookBible.query.filter_by(book_id=book_id).first()
    existing_context = ''
    if bb and bb.generated_summary:
        existing_context = bb.generated_summary[:2000]

    # 注入技能包提示词（brainstorm 相关）
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['tomato_plan', 'one_line_concept', 'brainstorm', 'master_outline'])

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

    system_prompt = f"""你是资深网文创意策划师，服务于番茄小说/起点中文网。用户正在创作一部{book.book_type}，题材为{_get_genre_label(book)}。

用户的一句话构思：{concept}

{f'已有设定上下文：{existing_context}' if existing_context else '这是全新创作，暂无已有设定。'}

{skill_note}

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

    # agent 协同：读取 bible 其他维度作为已知上下文，让识别结果与已确认维度保持一致
    bb = BookBible.query.filter_by(book_id=book_id).first()
    known_ctx_parts = []
    if bb:
        dim_label_map = {'concept': '构思', 'key_rules': '设定/规则', 'worldbuilding': '世界观',
                         'character_profiles': '人物', 'plot_design': '大纲', 'timeline': '剧情',
                         'foreshadowing': '伏笔', 'locations': '地点'}
        for f, lbl in dim_label_map.items():
            v = getattr(bb, f, '') or ''
            if v.strip() and f != field:  # 排除当前维度自身
                known_ctx_parts.append(f'【{lbl}（已确认）】\n{v[:600]}')
    known_ctx = '\n\n'.join(known_ctx_parts) if known_ctx_parts else '（暂无其他维度参考）'

    system_prompt = f"""你是专业的小说分析师，正在与其他维度分析师协同工作。
请分析以下小说内容，提取并归纳「{dim_label}」维度的设定信息。

【已确认的其他维度设定】（识别结果必须与这些维度保持一致，不可矛盾）
{known_ctx}

严格按JSON格式输出，不要任何其他文字：
{{
  "{field}": "{dim_prompts.get(field, dim_label)}"
}}"""

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'

        # 构建请求体（response_format 某些LLM不支持，捕获后重试）
        req_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f'作品标题：{book.title}\n\n以下是作品内容：\n\n{full_text}'}
            ],
            'temperature': 0.3,
            'max_tokens': 2000,
            'response_format': {'type': 'json_object'}
        }

        # 第一次尝试带 response_format；不支持则去掉重试
        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json=req_body,
            timeout=90)

        # 若返回400且与response_format相关，去掉该参数重试
        if resp.status_code == 400:
            err_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
            err_msg = str(err_body.get('error', '')).lower()
            if 'response_format' in err_msg or 'json_object' in err_msg or 'unrecognized' in err_msg:
                req_body.pop('response_format', None)
                resp = requests.post(f'{base}/chat/completions',
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json=req_body,
                    timeout=90)

        if resp.status_code != 200:
            try:
                err_detail = resp.json().get('error', {}).get('message', '') or resp.text[:300]
            except Exception:
                err_detail = resp.text[:300]
            return jsonify({'error': f'AI调用失败({resp.status_code}): {err_detail}'}), 500

        result = resp.json()
        if 'choices' not in result or not result['choices']:
            return jsonify({'error': f'AI返回异常: {str(result)[:300]}'}), 500
        content = result['choices'][0]['message']['content']

        # JSON 解析容错：提取第一个 {...} 块
        try:
            analysis = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            import re as _re_json
            m = _re_json.search(r'\{[\s\S]*\}', content)
            if m:
                analysis = json.loads(m.group(0))
            else:
                return jsonify({'error': f'AI返回非JSON格式: {content[:200]}'}), 500

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
    """AI识别指定卷的剧情大纲。
    优化版：数据源从单一章节内容扩展为 设定+大纲+人物+规则+章节+动态文件，
    输出增加 nodes 情节节点字段，与 ai_outline_volume 输出结构一致。
    这是相互提供资料数据的过程：识别结果会回流到 timeline 供其他维度使用。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # ===== 1. 收集该卷章节内容 =====
    all_chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    volume_chapters = []
    if volume_id:
        # 优先：parent_id 关联（标准结构）
        volume_chapters = [c for c in all_chapters if not c.is_volume and c.parent_id == volume_id]
        # 回退：顺序遍历法（兼容卷紧挨子章节之前的旧数据）
        if not volume_chapters:
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

    # 章节内容组装（优先用 summary，其次前 800 字 + 末 200 字）
    chapter_text = ''
    max_chars = 12000
    for ch in volume_chapters:
        ch_content = (ch.content or '')
        # 优先用章节摘要
        if getattr(ch, 'summary', None) and ch.summary:
            segment = f'【{ch.title}】{ch.summary[:500]}\n'
        elif len(ch_content) > 1000:
            segment = f'【{ch.title}】{ch_content[:800]}…{ch_content[-200:]}\n'
        else:
            segment = f'【{ch.title}】{ch_content}\n'
        if len(chapter_text) + len(segment) > max_chars:
            remaining = max_chars - len(chapter_text)
            if remaining > 200:
                chapter_text += segment[:remaining]
            break
        chapter_text += segment

    # ===== 2. 收集多维度上下文（相互提供资料数据） =====
    ctx_parts = []
    if bb:
        # 大纲维度（五幕式总纲）
        if bb.plot_design:
            ctx_parts.append(f'【五幕式总纲（本卷应在此弧线内）】\n{bb.plot_design[:2000]}')
        # 设定维度（世界观+规则）
        if bb.worldbuilding:
            ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:1000]}')
        if bb.key_rules:
            ctx_parts.append(f'【核心规则（金手指/能力限制，识别时不可违反）】\n{bb.key_rules[:800]}')
        # 人物及关系维度
        if bb.character_profiles:
            ctx_parts.append(f'【人物档案】\n{bb.character_profiles[:1000]}')
        # 已有该卷剧情（若有，作为参考而非覆盖）
        if bb.timeline:
            try:
                existing_vols = json.loads(bb.timeline)
                if isinstance(existing_vols, list):
                    # 找到该卷的已有数据
                    for ev in existing_vols:
                        ev_vid = str(ev.get('volume_id', ''))
                        ev_vol = str(ev.get('volume', ''))
                        if (volume_id and ev_vid == str(volume_id)) or (volume_title and ev_vol == volume_title):
                            ctx_parts.append(f'【该卷已有剧情（参考，可补充完善）】\n{json.dumps(ev, ensure_ascii=False)[:600]}')
                            break
            except (json.JSONDecodeError, ValueError):
                pass

    # ===== 3. 从动态文件补充数据 =====
    dyn_memories = DynamicMemory.query.filter_by(book_id=book_id).all()
    for dm in dyn_memories:
        if dm.category in ('narrative_engine', 'plot_progress', 'timeline') and dm.content:
            ctx_parts.append(f'【动态文件-{dm.category}】\n{dm.content[:800]}')
            break  # 只取一份，避免过多

    extra_ctx = '\n\n'.join(ctx_parts[-5:])  # 最多 5 块上下文，避免超长

    # 技能包提示
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['volume_breakdown', 'chapter_plan', 'tomato_outline'], mode='agent')

    if not volume_chapters and not extra_ctx:
        return jsonify({'error': '该卷没有章节内容，也没有可参考的设定'}), 400

    vol_label = volume_title or '全部章节'

    system_prompt = f"""你是番茄小说金番作者级别的剧情分析师。请综合【设定/大纲/人物/规则/章节内容/动态文件】多维度数据，识别「{vol_label}」的剧情大纲和情节节点。

【多维度上下文（相互提供资料数据）】
{extra_ctx or '（暂无设定参考，仅依据章节内容识别）'}

【识别要求】
1. 识别出的剧情必须与【五幕式总纲】中该卷的弧线一致，若有偏差在 main_plot 中标注
2. 识别人物互动时参考【人物档案】，确保角色名字和行为准确
3. 识别金手指/能力使用时参考【核心规则】，违反规则的标注为"待修正"
4. 结合【动态文件】中的叙事记录，补充章节内容未体现的关键事件和伏笔

严格按JSON格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "main_plot": "该卷主线剧情概述（100-200字，标注与总纲的偏差）",
  "core_conflict": "核心冲突",
  "emotion_driver": "情感驱动",
  "key_events": ["关键事件1", "关键事件2", "关键事件3"],
  "turning_points": ["转折点1", "转折点2"],
  "climax": "高潮场景描述",
  "ending": "该卷结尾状态/钩子",
  "foreshadowing": ["埋设的伏笔"],
  "nodes": [
    {{"title": "节点1", "chapters": "1-10", "type": "M", "summary": "概要", "cool_type": "爽点类型"}}
  ]
}}

【情节节点识别要求】
- 每卷识别 5-8 个情节节点
- 章型：M主线/C角色/W世界观/D日常/F伏笔
- 章型配额参考：M主线50%/C角色10%/W世界观10%/D日常20%/F伏笔10%
- 节点章节范围不重叠，覆盖整卷
- 小故事闭环：新事件→困难→金手指破局→暴露新信息→打脸收尾→钩子（5-8章）

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n\n以下是该卷章节内容：\n\n{chapter_text or "（无章节内容，请根据设定推断）"}'

    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'

        # 构建请求体（response_format 某些LLM不支持，捕获后重试）
        req_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': 0.3,
            'max_tokens': 4000,
            'response_format': {'type': 'json_object'}
        }

        resp = requests.post(f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json=req_body,
            timeout=90)

        # 若返回400且与response_format相关，去掉该参数重试
        if resp.status_code == 400:
            err_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
            err_msg = str(err_body.get('error', '')).lower()
            if 'response_format' in err_msg or 'json_object' in err_msg or 'unrecognized' in err_msg:
                req_body.pop('response_format', None)
                resp = requests.post(f'{base}/chat/completions',
                    headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                    json=req_body,
                    timeout=90)

        if resp.status_code != 200:
            try:
                err_detail = resp.json().get('error', {}).get('message', '') or resp.text[:300]
            except Exception:
                err_detail = resp.text[:300]
            return jsonify({'error': f'AI调用失败({resp.status_code}): {err_detail}'}), 500

        result = resp.json()
        if 'choices' not in result or not result['choices']:
            return jsonify({'error': f'AI返回异常: {str(result)[:300]}'}), 500
        content = result['choices'][0]['message']['content']

        # JSON 解析容错：使用健壮解析函数提取对象
        analysis, parse_err = _extract_json_from_llm(content, expect='object')
        if analysis is None:
            return jsonify({'error': f'AI返回非JSON格式: {content[:200]}', 'parse_error': parse_err}), 500

        # 存储到 timeline 字段（深度合并：保留人工编辑字段，更新 AI 识别字段）
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

        # 补全 volume_id 和 volume
        vol_data = analysis
        if volume_id:
            vol_data['volume_id'] = volume_id
        vol_data['volume'] = vol_label
        # 补全 volume_index
        if 'volume_index' not in vol_data:
            vol_data['volume_index'] = _extract_volume_index(vol_label) or (len(volumes_data) + 1)

        # 深度合并：找到已有卷，保留人工编辑的 nodes（如果新数据没有 nodes），其他字段用新数据
        found_idx = -1
        for i, v in enumerate(volumes_data):
            if not isinstance(v, dict):
                continue
            ev_vid = str(v.get('volume_id', ''))
            ev_vol = str(v.get('volume', ''))
            if (volume_id and ev_vid == str(volume_id)) or (volume_title and ev_vol == volume_title):
                found_idx = i
                break
        if found_idx >= 0:
            existing = volumes_data[found_idx]
            # 保留人工编辑的 nodes（新数据 nodes 为空或缺失时）
            if not vol_data.get('nodes') and existing.get('nodes'):
                vol_data['nodes'] = existing['nodes']
            # 保留人工编辑的字段（raw_text 等）
            for k in ('raw_text',):
                if k in existing and k not in vol_data:
                    vol_data[k] = existing[k]
            volumes_data[found_idx] = {**existing, **vol_data}
        else:
            volumes_data.append(vol_data)

        # 按 volume_index 排序
        volumes_data.sort(key=lambda v: int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0))

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


def _get_volume_chapters_ordered(book_id, volume_id):
    """返回指定卷下的非卷章节列表（按 order_index 排序）。
    优先用 parent_id 关联（标准结构）；若该卷无任何 parent_id 指向它的章节，
    回退到顺序遍历法（兼容旧数据：卷记录紧挨在其子章节之前）。
    volume_id 为空则返回全部非卷章节。"""
    all_chapters = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    if not volume_id:
        return [c for c in all_chapters if not c.is_volume]
    # 优先：parent_id 关联（标准结构）
    vol_chs = [c for c in all_chapters if not c.is_volume and c.parent_id == volume_id]
    if vol_chs:
        return vol_chs
    # 回退：顺序遍历法（兼容卷紧挨子章节之前的旧数据）
    collecting = False
    for ch in all_chapters:
        if ch.id == volume_id:
            collecting = True
            continue
        if collecting:
            if ch.is_volume:
                break
            vol_chs.append(ch)
    return vol_chs


def _collect_volume_chapters(book_id, volume_id):
    """收集指定卷的章节内容文本。volume_id 为空则取全部非卷章节。
    章节检索逻辑见 _get_volume_chapters_ordered（parent_id 优先，回退顺序遍历）。"""
    volume_chapters = _get_volume_chapters_ordered(book_id, volume_id)

    chapter_text = ''
    max_chars = 12000
    for ch in volume_chapters:
        ch_content = (ch.content or '')
        if getattr(ch, 'summary', None) and ch.summary:
            segment = f'【{ch.title}】{ch.summary[:500]}\n'
        elif len(ch_content) > 1000:
            segment = f'【{ch.title}】{ch_content[:800]}…{ch_content[-200:]}\n'
        else:
            segment = f'【{ch.title}】{ch_content}\n'
        if len(chapter_text) + len(segment) > max_chars:
            remaining = max_chars - len(chapter_text)
            if remaining > 200:
                chapter_text += segment[:remaining]
            break
        chapter_text += segment
    return chapter_text, len(volume_chapters)


def _collect_dimension_source(bb, volume_title=''):
    """当无章节时，从设定/大纲/剧情等主要维度收集基础数据作为识别来源。
    返回 (source_text, source_label)。source_label 标识数据来源（用于提示AI）。"""
    if not bb:
        return '', ''
    parts = []
    if bb.concept and bb.concept.strip():
        parts.append(f'【构思】\n{bb.concept.strip()[:600]}')
    if bb.key_rules and bb.key_rules.strip():
        parts.append(f'【设定/核心规则】\n{bb.key_rules.strip()[:1200]}')
    if bb.worldbuilding and bb.worldbuilding.strip():
        parts.append(f'【世界观】\n{bb.worldbuilding.strip()[:800]}')
    if bb.plot_design and bb.plot_design.strip():
        parts.append(f'【大纲/总纲】\n{bb.plot_design.strip()[:1500]}')
    if bb.timeline and bb.timeline.strip():
        # timeline 可能是 JSON（卷列表）或纯文本
        tl_text = bb.timeline.strip()
        try:
            tl_parsed = json.loads(tl_text)
            if isinstance(tl_parsed, list):
                tl_lines = []
                for v in tl_parsed:
                    if isinstance(v, dict):
                        vol_name = v.get('volume', '')
                        vol_content = v.get('content', '') or v.get('outline', '') or v.get('plot', '')
                        if vol_content:
                            tl_lines.append(f'卷「{vol_name}」: {str(vol_content)[:300]}')
                if tl_lines:
                    tl_text = '\n'.join(tl_lines)
        except (json.JSONDecodeError, ValueError):
            pass
        parts.append(f'【剧情/时间线】\n{tl_text[:1500]}')
    if bb.character_profiles and bb.character_profiles.strip():
        parts.append(f'【人物档案】\n{bb.character_profiles.strip()[:800]}')

    source_text = '\n\n'.join(parts)
    label = '设定/大纲/剧情维度' if source_text else ''
    if volume_title and source_text:
        label = f'设定/大纲/剧情维度（针对「{volume_title}」）'
    return source_text, label


def _get_volume_list(bb):
    """从 bible.timeline 解析卷列表，返回 [{volume_id, volume, volume_index}]"""
    if not bb or not bb.timeline:
        return []
    try:
        parsed = json.loads(bb.timeline)
        if isinstance(parsed, list):
            return [v for v in parsed if isinstance(v, dict)]
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _upsert_volume_entry(bb, field_name, entry):
    """在 bible.<field_name> 的 JSON 数组中按 volume_id/volume upsert 一条记录。
    P1-5: 对 inventory 字段，若旧值是纯文本（非JSON），先迁移为 [{volume:'', data:旧文本}] 再 upsert。"""
    data_list = []
    raw = getattr(bb, field_name, '') or ''
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            data_list = parsed
        elif isinstance(parsed, str) and parsed.strip():
            # 旧纯文本：迁移为单元素数组
            data_list = [{'volume': '历史数据', 'volume_id': '', 'data': parsed}]
        else:
            data_list = []
    except (json.JSONDecodeError, ValueError):
        # P1-5: 纯文本兜底——把旧文本包成单元素数组，避免静默丢弃
        if raw.strip():
            data_list = [{'volume': '历史数据', 'volume_id': '', 'data': raw}]
        else:
            data_list = []

    vid = str(entry.get('volume_id', ''))
    vname = str(entry.get('volume', ''))
    found_idx = -1
    for i, v in enumerate(data_list):
        if not isinstance(v, dict):
            continue
        ev_vid = str(v.get('volume_id', ''))
        ev_vol = str(v.get('volume', ''))
        if (vid and ev_vid == vid) or (vname and ev_vol == vname):
            found_idx = i
            break
    if found_idx >= 0:
        data_list[found_idx] = {**data_list[found_idx], **entry}
    else:
        data_list.append(entry)
    data_list.sort(key=lambda v: int(v.get('volume_index', 0) or _extract_volume_index(v.get('volume', v.get('volume_id', '0'))) or 0))
    setattr(bb, field_name, json.dumps(data_list, ensure_ascii=False, indent=2))
    return data_list


@app.route('/api/books/<book_id>/ai-analyze-character-volume', methods=['POST'])
@login_required
def ai_analyze_character_volume(book_id):
    """AI识别指定卷的人物档案。按卷分析章节内容，识别人物并写入 character_volumes。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别人物。请先填写设定、大纲或剧情维度。'}), 400

    # 上下文：全局人物档案 + 设定 + 该卷剧情
    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【全局人物档案（参考，避免重复识别）】\n{bb.character_profiles[:1000]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:600]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:600]}')
    # 该卷剧情
    if bb.timeline:
        try:
            vols = json.loads(bb.timeline)
            if isinstance(vols, list):
                for v in vols:
                    if isinstance(v, dict) and (str(v.get('volume_id', '')) == str(volume_id) or v.get('volume') == volume_title):
                        ctx_parts.append(f'【该卷剧情（参考）】\n{(v.get("main_plot") or "")[:500]}')
                        break
        except (json.JSONDecodeError, ValueError):
            pass
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['character_cognition', 'tomato_character'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说分析师。请从以下「{vol_label}」的章节内容中，识别本卷出现的所有重要角色（出现2次以上或有台词的角色）。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "characters": [
    {{
      "name": "角色名",
      "role": "主角/配角/反派/路人",
      "identity": "身份职业",
      "personality": "性格特征（1-2句）",
      "motivation": "本卷中的动机",
      "relationships": "本卷中与其他角色的关系",
      "abilities": "本卷中使用的能力/功法",
      "items": "本卷中持有的重要物品",
      "arc": "本卷中的角色弧线/变化"
    }}
  ]
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3000, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_cv
        m = _re_cv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    chars = analysis.get('characters', []) if isinstance(analysis, dict) else []

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'characters': chars,
    }
    data_list = _upsert_volume_entry(bb, 'character_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'character_volumes': data_list,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/ai-analyze-inventory-volume', methods=['POST'])
@login_required
def ai_analyze_inventory_volume(book_id):
    """AI识别指定卷的物资库。按卷分析章节内容，识别势力/角色拥有的物品、功法、法宝、境界等。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别物资。请先填写设定、大纲或剧情维度。'}), 400

    # 上下文
    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【人物档案（识别持有者）】\n{bb.character_profiles[:1000]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则（能力体系/境界划分）】\n{bb.key_rules[:800]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定（势力格局）】\n{bb.worldbuilding[:600]}')
    if bb.timeline:
        try:
            vols = json.loads(bb.timeline)
            if isinstance(vols, list):
                for v in vols:
                    if isinstance(v, dict) and (str(v.get('volume_id', '')) == str(volume_id) or v.get('volume') == volume_title):
                        ctx_parts.append(f'【该卷剧情（参考）】\n{(v.get("main_plot") or "")[:500]}')
                        break
        except (json.JSONDecodeError, ValueError):
            pass
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'tomato_setting'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说世界观分析师。请从以下「{vol_label}」的章节内容中，识别本卷出现的所有势力及角色拥有的物资。
物资类型包括：物品、功法、法宝、境界、灵宠、领地、资源等。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "items": [
    {{
      "owner": "持有者（角色名/势力名）",
      "owner_type": "角色/势力",
      "name": "物资名称",
      "category": "物品/功法/法宝/境界/灵宠/领地/资源/其他",
      "description": "描述（来源、能力、效果）",
      "status": "获得/持有/失去/消耗",
      "chapter": "首次出现章节"
    }}
  ],
  "realms": [
    {{
      "character": "角色名",
      "realm": "当前境界",
      "progress": "修炼进度/突破节点"
    }}
  ]
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=3000, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_iv
        m = _re_iv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    items = analysis.get('items', []) if isinstance(analysis, dict) else []
    realms = analysis.get('realms', []) if isinstance(analysis, dict) else []

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'items': items,
        'realms': realms,
    }
    data_list = _upsert_volume_entry(bb, 'inventory', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'inventory': data_list,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/ai-analyze-dynamic-volume', methods=['POST'])
@login_required
def ai_analyze_dynamic_volume(book_id):
    """AI识别指定卷的动态文件分类。按卷汇总章节内容，生成该卷的动态摘要（人物/事件/时间/地点/势力/伏笔/境界/关系）。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别动态文件。请先填写设定、大纲或剧情维度。'}), 400

    # 收集该卷区间内的已有动态报告（5章一份）
    dyn_reports = DynamicReport.query.filter_by(book_id=book_id).order_by(DynamicReport.chapter_start).all()
    relevant_reports = []
    # 计算该卷的起止章号（与 _collect_volume_chapters 一致：parent_id 优先，回退顺序遍历）
    all_chs = Chapter.query.filter_by(book_id=book_id).order_by(Chapter.order_index).all()
    if volume_id:
        vol_chs_in_order = _get_volume_chapters_ordered(book_id, volume_id)
        # 计算该卷章节在全章节序列中的序号（1-based）
        non_vol_chs = [c for c in all_chs if not c.is_volume]
        vol_ch_idx = [non_vol_chs.index(c) for c in vol_chs_in_order if c in non_vol_chs]
    else:
        vol_ch_idx = list(range(len([c for c in all_chs if not c.is_volume])))

    if vol_ch_idx:
        # 章节序号从1开始
        ch_start = vol_ch_idx[0] + 1
        ch_end = vol_ch_idx[-1] + 1
        for r in dyn_reports:
            if r.chapter_end >= ch_start and r.chapter_start <= ch_end:
                relevant_reports.append(r)

    reports_text = '\n\n'.join([f'【{r.title}】\n{(r.content or "")[:500]}' for r in relevant_reports]) if relevant_reports else '（无已生成报告）'

    ctx_parts = []
    if bb.character_profiles:
        ctx_parts.append(f'【人物档案】\n{bb.character_profiles[:600]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:600]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'narrative_debt', 'foreshadow_register'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说防遗忘系统分析师。请从以下「{vol_label}」的章节内容及已有动态报告中，生成本卷的动态分类摘要。

【已有动态报告（5章一份）】
{reports_text}

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "characters": "本卷登场人物及状态变化（200字内）",
  "events": "本卷关键事件脉络（200字内）",
  "timeline": "本卷时间线要点（150字内）",
  "locations": "本卷涉及地点（100字内）",
  "factions": "本卷势力动态（100字内）",
  "foreshadowing": "本卷埋设/回收的伏笔（150字内）",
  "realms": "本卷境界/能力变化（100字内）",
  "relationships": "本卷人物关系变化（100字内）",
  "summary": "本卷综合动态摘要（300字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无章节内容，依据报告生成）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    analysis, parse_err = _extract_json_from_llm(content, expect='object')
    if analysis is None:
        return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300], 'parse_error': parse_err}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'dynamic_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'dynamic_volumes': data_list,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/ai-analyze-foreshadowing-volume', methods=['POST'])
@login_required
def ai_analyze_foreshadowing_volume(book_id):
    """AI识别指定卷的伏笔。按卷分析章节内容，识别本卷埋设/回收的伏笔并写入 foreshadowing_volumes。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别伏笔。请先填写设定、大纲或剧情维度。'}), 400

    ctx_parts = []
    if bb.foreshadowing:
        ctx_parts.append(f'【全局伏笔档案（参考，避免重复）】\n{bb.foreshadowing[:800]}')
    if bb.key_rules:
        ctx_parts.append(f'【核心规则】\n{bb.key_rules[:400]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['foreshadow_register', 'narrative_debt'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说伏笔分析师。请从以下「{vol_label}」的章节内容中，识别本卷埋设的伏笔、回收的伏笔、以及尚未回收的悬念。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "planted": [
    {{"content": "伏笔内容", "chapter": "埋设章节/位置", "purpose": "埋设目的", "status": "待回收"}}
  ],
  "resolved": [
    {{"content": "伏笔内容", "planted_at": "埋设位置", "resolved_at": "回收位置", "effect": "回收效果"}}
  ],
  "pending": [
    {{"content": "未回收悬念", "planted_at": "埋设位置", "importance": "高/中/低"}}
  ],
  "summary": "本卷伏笔综述（150字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_fv
        m = _re_fv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'foreshadowing_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'foreshadowing_volumes': data_list,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/ai-analyze-locations-volume', methods=['POST'])
@login_required
def ai_analyze_locations_volume(book_id):
    """AI识别指定卷的地点/地图。按卷分析章节内容，识别本卷涉及的地点并写入 locations_volumes。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])

    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
        db.session.commit()

    config = AIConfig.query.first()
    api_key = config.api_key if config and config.api_key else os.environ.get('USER_LLM_API_KEY', '')
    base_url = config.base_url if config else os.environ.get('USER_LLM_BASE_URL', 'https://api.deepseek.com/v1')
    model = config.get_model_for_task('recognition') if config else os.environ.get('USER_LLM_MODEL', 'deepseek-chat')

    if not api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    chapter_text, ch_count = _collect_volume_chapters(book_id, volume_id)
    source_label = ''
    if not chapter_text or ch_count == 0:
        # 无章节时，从设定/大纲/剧情维度提取基础数据
        chapter_text, source_label = _collect_dimension_source(bb, volume_title or '全部章节')
        if not chapter_text:
            return jsonify({'error': '该卷暂无章节，且设定/大纲/剧情维度也为空，无法识别地点。请先填写设定、大纲或剧情维度。'}), 400

    ctx_parts = []
    if bb.locations:
        ctx_parts.append(f'【全局地点档案（参考）】\n{bb.locations[:800]}')
    if bb.worldbuilding:
        ctx_parts.append(f'【世界观设定】\n{bb.worldbuilding[:500]}')
    extra_ctx = '\n\n'.join(ctx_parts)

    # P2-10: 'world_setting' 是幽灵key，替换为 'tomato_setting'
    skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['lock_facts', 'tomato_setting'], mode='agent')

    vol_label = volume_title or '全部章节'
    system_prompt = f"""你是专业的小说地图分析师。请从以下「{vol_label}」的章节内容中，识别本卷涉及的所有地点、场景、地理信息。

{extra_ctx and f"【已有参考】{chr(10)}{extra_ctx}" or ""}

严格按JSON对象格式输出（不要任何其他文字）：
{{
  "volume": "{vol_label}",
  "locations": [
    {{"name": "地点名", "type": "城市/山脉/秘境/建筑/其它", "description": "地点描述", "events": "该地点发生的重要事件", "importance": "高/中/低"}}
  ],
  "regions": [
    {{"name": "区域名", "scope": "范围描述", "feature": "区域特征"}}
  ],
  "summary": "本卷地理概况（150字内）"
}}

{skill_note}"""

    user_prompt = f'作品标题：{book.title}\n卷名：{vol_label}\n{source_label and f"（数据来源：{source_label}）" or ""}\n\n以下是该卷内容：\n\n{chapter_text or "（无内容，请根据设定推断）"}'

    content, err = _call_llm(
        [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
        max_tokens=2500, temperature=0.3, task_type='recognition'
    )
    if err:
        return jsonify({'error': err}), 500

    try:
        analysis = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        import re as _re_lv
        m = _re_lv.search(r'\{[\s\S]*\}', content)
        if m:
            analysis = json.loads(m.group())
        else:
            return jsonify({'error': 'AI返回格式无法解析', 'raw': content[:300]}), 500

    entry = {
        'volume_id': volume_id,
        'volume': vol_label,
        'volume_index': _extract_volume_index(vol_label) or 0,
        'data': analysis if isinstance(analysis, dict) else {},
    }
    data_list = _upsert_volume_entry(bb, 'locations_volumes', entry)
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        'success': True,
        'volume_data': entry,
        'locations_volumes': data_list,
        'bible': bb.to_dict()
    })


@app.route('/api/books/<book_id>/clear-timeline', methods=['POST'])
@login_required
def clear_timeline(book_id):
    """一键清空剧情分卷大纲（timeline 字段），不影响章节表。"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        bb = BookBible(book_id=book_id)
        db.session.add(bb)
    bb.timeline = ''
    bb.last_synced_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({'success': True, 'bible': bb.to_dict()})


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
题材：{_get_genre_label(book)}
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


def _generate_dynamic_report_content(book_id, chapter_start, chapter_end, skill_pack_ids=None):
    """内部函数：调用AI生成动态报告内容。
    skill_pack_ids: 可选，技能包ID列表，用于注入提示词（P0-2修复）"""
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

    # P0-2: 注入技能包提示词（narrative_debt/foreshadow_register 用于动态摘要生成）
    skill_note = ''
    if skill_pack_ids:
        skill_note = _get_skill_prompts_by_category(skill_pack_ids, 'master', ['narrative_debt', 'foreshadow_register', 'lock_facts'], mode='agent')

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
- 直接输出报告内容，不要加标题和前后缀

{skill_note}"""

    user_content = f"""作品：{book.title}
题材：{_get_genre_label(book)}

第{chapter_start}章到第{chapter_end}章内容：
{chapters_content}

请生成动态报告（≤500字）："""

    # P2-8：动态报告失败重试（指数退避，最多2次重试），全失败则降级为章节摘要拼接
    import time as _time
    max_retries = 2
    last_error = None
    try:
        base = base_url.rstrip('/')
        if not base.endswith('/v1'):
            base += '/v1'
        for attempt in range(max_retries + 1):
            try:
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
                if content and len(content) > 50:
                    return content, None
                last_error = f'LLM 返回内容过短（{len(content)}字）'
            except Exception as e:
                last_error = str(e)
            # 指数退避：1s, 2s
            if attempt < max_retries:
                _time.sleep(2 ** attempt)
        # 全部重试失败，降级为章节摘要拼接
        fallback_parts = [f'【降级摘要·第{chapter_start}-{chapter_end}章】（LLM生成失败，自动拼接）']
        for ch in target_chapters:
            snippet = (ch.content or '')[:120].replace('\n', ' ').strip()
            if snippet:
                fallback_parts.append(f'- {ch.title}：{snippet}')
        fallback_content = '\n'.join(fallback_parts)[:800]
        return fallback_content, None
    except Exception as e:
        # 连降级都失败的极端情况
        return None, f'{str(e)} | last_error={last_error}'


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

    # 借鉴 PlotPilot 检查点快照：每5章自动备份 BookBible + DynamicMemory 关键状态
    # 用户可回滚到某章节点重新创作，避免"写崩了无法恢复"
    try:
        _create_state_snapshot(book_id, current_end)
    except Exception as snap_err:
        try:
            app.logger.warning(f'快照创建失败（不影响主流程）: {snap_err}')
        except Exception:
            pass

    return {'report': report.to_dict()}


def _create_state_snapshot(book_id, chapter_end):
    """创建叙事状态检查点快照（借鉴 PlotPilot checkpoint）。
    备份 BookBible 关键字段 + DynamicMemory 5文件，存入 bb.state_snapshots。
    每个快照含：snapshot_id / chapter_end / created_at / bible_fields / dynamic_memory。
    最多保留 20 个快照（超出按时间淘汰最旧的）。"""
    bb = BookBible.query.filter_by(book_id=book_id).first()
    if not bb:
        return
    # 备份 BookBible 关键叙事字段（不含快照自身，避免递归膨胀）
    bible_fields = {}
    for field in ['worldbuilding', 'character_profiles', 'timeline', 'foreshadowing',
                  'style_guide', 'key_rules', 'locations', 'concept', 'plot_design',
                  'relation_graph', 'inventory', 'character_volumes', 'dynamic_volumes',
                  'foreshadowing_volumes', 'locations_volumes', 'foreshadowing_graph',
                  'outline_hierarchy', 'chapter_changes_log']:
        bible_fields[field] = getattr(bb, field, '') or ''
    # 备份 DynamicMemory 5文件
    dm_data = {}
    try:
        dm = DynamicMemory.query.filter_by(book_id=book_id).first()
        if dm:
            for key in ['narrative_engine', 'foreshadowing_tracker', 'character_ecosystem',
                         'ability_world', 'health_dashboard']:
                dm_data[key] = getattr(dm, key, '') or ''
    except Exception:
        pass
    snapshot = {
        'snapshot_id': str(uuid.uuid4())[:8],
        'chapter_end': chapter_end,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'bible_fields': bible_fields,
        'dynamic_memory': dm_data,
    }
    existing = []
    try:
        existing = json.loads(bb.state_snapshots or '[]') if bb.state_snapshots else []
    except Exception:
        existing = []
    existing.append(snapshot)
    # 最多保留 20 个，淘汰最旧
    if len(existing) > 20:
        existing = existing[-20:]
    bb.state_snapshots = json.dumps(existing, ensure_ascii=False)
    db.session.commit()


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


@app.route('/api/books/<book_id>/dynamic-reports/batch-generate', methods=['POST'])
@login_required
def batch_generate_dynamic_reports(book_id):
    """按卷批量生成动态报告（每5章一份）。AI识别按钮选择某卷后，自动生成该卷内所有
    尚未生成的5章区间动态报告，减少一个个手动添加的麻烦。
    参数：volume_id（可选，空则全卷）、volume_title、skill_pack_ids、overwrite（是否覆盖已存在，默认false）"""
    book = Book.query.get(book_id)
    if not book:
        return jsonify({'error': 'Book not found'}), 404

    data = request.get_json() or {}
    volume_id = data.get('volume_id', '')
    volume_title = data.get('volume_title', '')
    skill_pack_ids = data.get('skill_pack_ids', [])
    overwrite = data.get('overwrite', False)

    config = AIConfig.query.first()
    if not config or not config.api_key:
        return jsonify({'error': '请先配置 AI 模型 API Key'}), 400

    # 收集全作品非卷章节（按 order_index，用于计算全局 1-based 章号）
    all_chs = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if not all_chs:
        return jsonify({'error': '该作品暂无章节，无法批量生成动态报告。'}), 400

    # 确定该卷章节的全局1-based起止章号
    if volume_id:
        # 用共享助手取该卷章节：parent_id 优先，回退顺序遍历（兼容未设置 parent_id 的旧数据）
        vol_chs = _get_volume_chapters_ordered(book_id, volume_id)
        if not vol_chs:
            return jsonify({'error': f'卷「{volume_title or volume_id}」内暂无章节（请确认章节已归入该卷）'}), 400
        # 计算这些章节在 all_chs 中的全局序号（1-based）
        ch_id_to_idx = {c.id: i for i, c in enumerate(all_chs)}
        vol_ch_idx = [ch_id_to_idx[c.id] for c in vol_chs if c.id in ch_id_to_idx]
        if not vol_ch_idx:
            return jsonify({'error': f'卷「{volume_title or volume_id}」内暂无章节'}), 400
        global_start = min(vol_ch_idx) + 1
        global_end = max(vol_ch_idx) + 1
    else:
        global_start = 1
        global_end = len(all_chs)

    # 按5章一份切分区间
    intervals = []
    s = global_start
    while s <= global_end:
        e = min(s + DYNAMIC_REPORT_INTERVAL - 1, global_end)
        intervals.append((s, e))
        s = e + 1

    if not intervals:
        return jsonify({'error': '该卷章节范围无效'}), 400

    # 查询已存在的报告（按chapter_start/chapter_end匹配），决定是否跳过/覆盖
    existing = DynamicReport.query.filter_by(book_id=book_id).all()
    existing_map = {(r.chapter_start, r.chapter_end): r for r in existing}

    generated = []
    skipped = []
    errors = []
    for (cs, ce) in intervals:
        key = (cs, ce)
        if key in existing_map and not overwrite:
            skipped.append({'chapter_start': cs, 'chapter_end': ce, 'reason': '已存在'})
            continue
        # 调用AI生成内容（P0-2: 传入 skill_pack_ids）
        content, err = _generate_dynamic_report_content(book_id, cs, ce, skill_pack_ids=skill_pack_ids)
        if err:
            errors.append({'chapter_start': cs, 'chapter_end': ce, 'error': err})
            continue
        if key in existing_map and overwrite:
            # 覆盖已存在报告
            r = existing_map[key]
            r.content = content
            r.title = f'动态-({cs}-{ce}章)'
            r.auto_generated = False
        else:
            # 新建报告
            r = DynamicReport(
                book_id=book_id, title=f'动态-({cs}-{ce}章)', content=content,
                chapter_start=cs, chapter_end=ce, auto_generated=False
            )
            db.session.add(r)
        db.session.commit()
        generated.append(r.to_dict())

    return jsonify({
        'success': True,
        'volume_title': volume_title or '全部章节',
        'chapter_range': [global_start, global_end],
        'total_intervals': len(intervals),
        'generated_count': len(generated),
        'skipped_count': len(skipped),
        'error_count': len(errors),
        'generated': generated,
        'skipped': skipped,
        'errors': errors,
    })


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


@app.route('/api/books/<book_id>/dynamic-reports/batch-delete', methods=['POST'])
@login_required
def batch_delete_dynamic_reports(book_id):
    """批量删除动态报告"""
    data = request.get_json() or {}
    report_ids = data.get('report_ids') or []
    if not isinstance(report_ids, list) or not report_ids:
        return jsonify({'error': '请提供要删除的报告ID列表'}), 400

    deleted = DynamicReport.query.filter(
        DynamicReport.book_id == book_id,
        DynamicReport.id.in_(report_ids)
    ).all(synchronize_session=False)
    deleted_ids = [r.id for r in deleted]
    for r in deleted:
        db.session.delete(r)
    db.session.commit()
    return jsonify({'success': True, 'deleted_count': len(deleted_ids), 'deleted_ids': deleted_ids})


@app.route('/api/books/<book_id>/dynamic-reports/<report_id>/regenerate', methods=['POST'])
@login_required
def regenerate_dynamic_report(book_id, report_id):
    """重新生成动态报告内容（AI）。支持可选 skill_pack_ids 注入提示词。"""
    report = DynamicReport.query.filter_by(id=report_id, book_id=book_id).first()
    if not report:
        return jsonify({'error': 'Report not found'}), 404

    data = request.get_json(silent=True) or {}
    skill_pack_ids = data.get('skill_pack_ids', [])
    content, error = _generate_dynamic_report_content(book_id, report.chapter_start, report.chapter_end, skill_pack_ids=skill_pack_ids)
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
    # 返回最近10份报告（覆盖更长前文记忆，保证剧情连贯）
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start.desc()
    ).limit(10).all()
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
        'relationGraph': 'relation_graph',
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

    # 获取已有的bible字段内容
    bible = BookBible.query.filter_by(book_id=book_id).first()
    existing_value = getattr(bible, field, '') if bible else ''

    # 按维度组装数据源（用户要求：不同图谱从不同维度读取数据供AI识别）
    # 1. 关系图谱：从「人物及关系」+「剧情」维度读取，再补充动态文件
    # 2. 地点图谱：首先从「设定」+「大纲」维度读取，再从动态文件补充
    # 3. 境界图谱：首先从「设定」+「大纲」维度读取，再从动态文件补充
    # 4. 地图(locations)：保持动态文件优先，回退章节内容

    def _bible_val(attr):
        return getattr(bible, attr, '') if bible else ''

    # 动态文件报告（补充数据源）
    reports = DynamicReport.query.filter_by(book_id=book_id).order_by(
        DynamicReport.chapter_start
    ).all()
    dynamic_text = '\n\n'.join([f'【{r.title}】\n{r.content}' for r in reports if r.content]) if reports else ''

    # 章节内容（最终回退）
    chapter_text = ''
    chapters = Chapter.query.filter_by(book_id=book_id, is_volume=False).order_by(Chapter.order_index).all()
    if chapters:
        max_chars = 8000
        for ch in chapters:
            segment = f'【{ch.title}】\n{(ch.content or "")[:800]}\n\n'
            if len(chapter_text) + len(segment) > max_chars:
                break
            chapter_text += segment

    source_parts = []  # [(标签, 内容)]
    if dimension == 'relationGraph':
        # 关系图谱：人物及关系 + 剧情 + 动态文件
        cp = _bible_val('character_profiles')
        tl = _bible_val('timeline')
        if cp.strip():
            source_parts.append(('人物及关系维度', cp[:3000]))
        if tl.strip():
            source_parts.append(('剧情维度', tl[:3000]))
        if dynamic_text.strip():
            source_parts.append(('动态文件补充', dynamic_text[:3000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容补充', chapter_text[:2000]))
    elif dimension in ('locationGraph', 'realmGraph'):
        # 地点图谱/境界图谱：设定 + 大纲 + 动态文件
        wb = _bible_val('worldbuilding')
        kr = _bible_val('key_rules')
        pd = _bible_val('plot_design')
        if wb.strip():
            source_parts.append(('设定维度(世界观)', wb[:3000]))
        if kr.strip():
            source_parts.append(('设定维度(核心规则)', kr[:2000]))
        if pd.strip():
            source_parts.append(('大纲维度', pd[:3000]))
        if dynamic_text.strip():
            source_parts.append(('动态文件补充', dynamic_text[:3000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容补充', chapter_text[:2000]))
    else:
        # locations 及其他：动态文件优先，回退章节内容
        if dynamic_text.strip():
            source_parts.append(('动态文件报告', dynamic_text[:8000]))
        elif chapter_text.strip():
            source_parts.append(('章节内容', chapter_text[:8000]))
        else:
            # 没有任何数据源时，尝试从设定/大纲补充
            for attr, label in [('worldbuilding', '世界观设定'), ('key_rules', '核心规则'), ('plot_design', '大纲'), ('character_profiles', '人物及关系'), ('timeline', '剧情')]:
                v = _bible_val(attr)
                if v.strip():
                    source_parts.append((label, v[:3000]))

    if not source_parts:
        return jsonify({'error': '没有可用的数据源，请先在相关维度填写内容或生成动态文件'}), 400

    source_type = '、'.join([p[0] for p in source_parts])
    source_text = '\n\n'.join([f'--- {label} ---\n{content}' for label, content in source_parts])

    # 不同维度的提取提示
    dim_prompts = {
        'locations': """请从以下内容中提取所有地点信息，按三级分类整理（大区域/城市/场景）。
输出JSON格式：
{"locations": [{"name":"大区域名","desc":"描述","children":[{"name":"城市名","desc":"描述","children":[{"name":"场景名","desc":"描述"}]}]}]}
如果没有明确地点信息，输出空数组。""",

        'relationGraph': """请从以下内容中提取所有人物及其关系，整理为关系图谱数据。
重要规则：
- 只提取真实的人物姓名作为节点，绝对不要把"关系"、"好友"、"敌人"、"师徒"等关系类型词当作人物节点。
- 每个人物用姓名开头，关系单独列为"关系: A与B-关系类型"。

输出JSON格式：
{"relation_graph": "人物1: 姓名|身份|性格|动机\\n人物2: 姓名|身份|性格|动机\\n关系: A与B-关系类型"}
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
    elif ext in ('md', 'markdown', 'json'):
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
                    if inner_ext in ('txt', 'md', 'markdown', 'json'):
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
        # Migration: 逐条独立提交，避免 PostgreSQL 事务污染
        # （PG 中一条 ALTER 失败会使整个事务 aborted，后续语句全失败）
        # 使用 ADD COLUMN IF NOT EXISTS（PostgreSQL 9.6+ / SQLite 3.35+ 均支持，老版本 SQLite 走 except 兜底）
        def _add_column(table, col_def):
            sql = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_def}'
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()

        _add_column('book_bible', 'locations TEXT')
        _add_column('book_bible', 'concept TEXT')
        _add_column('book_bible', 'plot_design TEXT')
        # Migration: 关系图谱专用字段（解耦 character_profiles，避免角色丢失）
        _add_column('book_bible', 'relation_graph TEXT')
        # Migration: 物资库、人物按卷、动态文件按卷
        _add_column('book_bible', 'inventory TEXT')
        _add_column('book_bible', 'character_volumes TEXT')
        _add_column('book_bible', 'dynamic_volumes TEXT')
        # Migration: 伏笔按卷、地图按卷
        _add_column('book_bible', 'foreshadowing_volumes TEXT')
        _add_column('book_bible', 'locations_volumes TEXT')
        _add_column('ai_config', 'recognition_model TEXT')
        # Migration: skill_packs 添加 github_source 和 github_synced_at 字段
        _add_column('skill_packs', "github_source VARCHAR(500) DEFAULT ''")
        _add_column('skill_packs', 'github_synced_at TIMESTAMP')
        # Migration: 防遗忘检查报告历史
        _add_column('book_bible', 'anti_forget_reports TEXT')
        # Migration P0-2: 伏笔 DAG
        _add_column('book_bible', 'foreshadowing_graph TEXT')
        # Migration P1-4: 四级大纲层级
        _add_column('book_bible', 'outline_hierarchy TEXT')
        # Migration P1-6: 章级变更日志
        _add_column('book_bible', 'chapter_changes_log TEXT')
        # Migration: 检查点快照（借鉴 PlotPilot，每5章自动备份叙事状态）
        _add_column('book_bible', 'state_snapshots TEXT')
        # Migration: 技能包三类划分（master/style/review）+ 题材目标 + 优先级
        _add_column('skill_packs', "category VARCHAR(20) DEFAULT 'master'")
        _add_column('skill_packs', "genre_target VARCHAR(50) DEFAULT ''")
        _add_column('skill_packs', 'priority INTEGER DEFAULT 100')
        # Migration: Book 表技能包三类字段（构思/文风/审查 无污染隔离）
        _add_column('books', "master_skill_ids TEXT DEFAULT '[]'")
        _add_column('books', "style_skill_ids TEXT DEFAULT '[]'")
        _add_column('books', "review_skill_ids TEXT DEFAULT '[]'")
        # Migration: 总卷数 + 风格流派（Book 与 BookBible 双写）
        _add_column('books', 'total_volumes INTEGER DEFAULT 10')
        _add_column('books', "novel_styles TEXT DEFAULT '[]'")
        _add_column('book_bible', 'total_volumes INTEGER DEFAULT 10')
        _add_column('book_bible', "novel_styles TEXT DEFAULT '[]'")
        # Migration P1-5: 章节审计-修订闭环快照（修复保存章节 500 错误：老库缺此列）
        _add_column('chapters', 'review_snapshots TEXT')
        # Migration: 章节摘要（修复 'Chapter' object has no attribute 'summary'：老库缺此列）
        _add_column('chapters', 'summary VARCHAR(500)')
        seed_builtin_templates()
        seed_prompt_templates()
        seed_skill_packs()
        # 铁律诊断：每次启动打印数据库状态，确认用户数据持久化
        try:
            user_count = User.query.count()
            book_count = Book.query.count()
            uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            is_pg = uri.startswith('postgresql')
            db_label = 'PostgreSQL ✅' if is_pg else 'SQLite ⚠️'
            print(f'[铁律] 用户数据持久化检查：{db_label} | users={user_count} | books={book_count}', flush=True)
            if is_pg:
                print(f'[铁律] ✅ 已连接 PostgreSQL，用户数据将持久化，部署/重启不丢失', flush=True)
            else:
                print(f'[铁律] ❌ 检测到 SQLite！本地开发可用，但生产环境会拒绝启动。请配置 DATABASE_URL', flush=True)
            if user_count == 0:
                print(f'[铁律] ℹ️ 用户表为空（新数据库正常；若之前注册过账号说明数据未持久化）', flush=True)
        except Exception as e:
            print(f'[铁律] 诊断失败: {e}', flush=True)


# ==== 前端静态文件托管（生产环境）====
# 当后端直接提供服务时，托管前端构建产物，避免前后端分离部署导致的 /api 请求失败

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """托管前端 SPA：所有非 /api 请求都返回静态文件或 index.html"""
    # 前端构建产物目录：优先 frontend/dist，其次 backend/static
    dist_dir = FRONTEND_DIST
    if not dist_dir.exists() or not (dist_dir / 'index.html').exists():
        # fallback: backend/static（仓库中提交的预构建产物）
        static_dir = Path(__file__).parent / 'static'
        if (static_dir / 'index.html').exists():
            dist_dir = static_dir
        else:
            # 再 fallback: 项目根目录
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
    # threaded=True 必须：SSE 流式端点（ai_continue_batch_stream / ai_continue_stream）会长时间占用
    # 工作线程。单线程模式下（默认），SSE 连接期间所有其他请求（health/列表/保存等）会被阻塞。
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
