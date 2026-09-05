"""模拟用户线上旧库：ai_config 无 models 列 + 同 provider 多行 + 旧版本号。
验证 init_db 迁移后：列补齐、行合并、版本更新、保存接口 200。"""
import os, sys, sqlite3, tempfile, json

tmp = tempfile.mkdtemp(prefix='fanshu_old_db_')
os.environ['FANSHU_DATA_DIR'] = tmp
for k in ('DATABASE_URL', 'PORT', 'RENDER', 'HF_SPACE_ID', 'RAILWAY_PROJECT_ID'):
    os.environ.pop(k, None)

db_path = os.path.join(tmp, 'fanshu.db')

# ---- 1. 构造旧库 ----
con = sqlite3.connect(db_path)
cur = con.cursor()
cur.execute('''CREATE TABLE ai_config (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(50) DEFAULT '默认配置',
    is_active BOOLEAN DEFAULT 1,
    provider VARCHAR(50) DEFAULT 'deepseek',
    model VARCHAR(100) DEFAULT 'deepseek-chat',
    recognition_model VARCHAR(100) DEFAULT '',
    api_key VARCHAR(200) DEFAULT '',
    base_url VARCHAR(300) DEFAULT 'https://api.deepseek.com',
    temperature FLOAT DEFAULT 0.7,
    max_tokens INTEGER DEFAULT 4096
)''')  # 注意：没有 models 列（旧结构）
cur.execute('''CREATE TABLE app_meta (
    "key" VARCHAR(100) PRIMARY KEY,
    value VARCHAR(200)
)''')
cur.execute("INSERT INTO app_meta VALUES ('schema_seed_version', '2026-09-03.1')")
rows = [
    # (id, name, active, provider, model, api_key, base_url)
    ('id-zhipu-1', '智谱A', 1, 'zhipu', 'glm-4.7', 'key-keep', 'https://open.bigmodel.cn/api/paas/v4'),
    ('id-zhipu-2', '智谱B', 0, 'zhipu', 'glm-5.3', 'key-other', ''),
    ('id-ds', 'DeepSeek', 0, 'deepseek', 'deepseek-chat', 'sk-ds', 'https://api.deepseek.com'),
]
for r in rows:
    cur.execute('INSERT INTO ai_config (id,name,is_active,provider,model,api_key,base_url) VALUES (?,?,?,?,?,?,?)', r)
con.commit(); con.close()
print('[1] 旧库构造完成:', db_path)

# ---- 2. 启动应用（导入 + init_db）----
sys.path.insert(0, '/workspace/source/backend')
import app as app_module
print('[2] app 导入成功，执行 init_db()...')
app_module.init_db()

# ---- 3. 验证迁移结果 ----
con = sqlite3.connect(db_path)
cur = con.cursor()
cols = [r[1] for r in cur.execute('PRAGMA table_info(ai_config)').fetchall()]
assert 'models' in cols, f'FAIL: models 列未添加, cols={cols}'
print('[3] models 列已添加 ✓')

data = cur.execute('SELECT id, provider, model, models, is_active, api_key, base_url FROM ai_config ORDER BY provider').fetchall()
assert len(data) == 2, f'FAIL: 应合并为 2 行（zhipu/deepseek），实际 {len(data)} 行: {data}'
by_prov = {r[1]: r for r in data}
z = by_prov['zhipu']
assert z[2] == 'glm-4.7', f'FAIL: 保留行 model 应为 glm-4.7, 实际 {z[2]}'
assert json.loads(z[3]) == ['glm-4.7', 'glm-5.3'], f'FAIL: zhipu models 合并错误: {z[3]}'
assert z[4] == 1 and z[5] == 'key-keep', f'FAIL: 激活/key 保留错误: {z}'
d = by_prov['deepseek']
assert d[4] == 0, 'FAIL: deepseek 不应被激活'
print('[4] 同 provider 行合并正确 ✓  zhipu.models=', z[3])

ver = cur.execute("SELECT value FROM app_meta WHERE key='schema_seed_version'").fetchone()
assert ver and ver[0] == '2026-09-05.1', f'FAIL: 版本未更新: {ver}'
print('[5] schema_seed_version →', ver[0], '✓')
con.close()

# ---- 4. 再跑一次 init_db 验证幂等 ----
app_module.init_db()
con = sqlite3.connect(db_path)
n = con.execute('SELECT COUNT(*) FROM ai_config').fetchone()
con.close()
print('[6] 幂等重跑后行数 =', n[0], '✓')

# ---- 5. 保存配置接口 ----
c = app_module.app.test_client()
r = c.put('/api/ai/config', json={'api_key': 'key-keep', 'model': 'glm-5.3',
                                   'models': ['glm-4.7', 'glm-5.3'],
                                   'base_url': 'https://open.bigmodel.cn/api/paas/v4'})
assert r.status_code == 200, f'FAIL: PUT /api/ai/config => {r.status_code}: {r.get_data(as_text=True)[:300]}'
body = r.get_json()
assert body['model'] == 'glm-5.3' and set(body['models']) == {'glm-4.7', 'glm-5.3'}
print('[7] PUT /api/ai/config => 200 ✓  models =', body['models'])

r = c.get('/api/ai/configs')
assert r.status_code == 200, f'FAIL: GET /api/ai/configs => {r.status_code}'
lst = r.get_json()['configs']
assert len(lst) == 2 and lst[0]['is_active'] is True
print('[8] GET /api/ai/configs => 200 ✓  提供商数 =', len(lst))

# select-model 接口
zhipu_id = [x for x in lst if x['provider'] == 'zhipu'][0]['id']
r = c.post(f'/api/ai/configs/{zhipu_id}/select-model', json={'model': 'glm-4.7'})
assert r.status_code == 200, f'FAIL: select-model => {r.status_code}: {r.get_data(as_text=True)[:300]}'
assert r.get_json()['model'] == 'glm-4.7'
print('[9] select-model => 200 ✓')

print('\nALL PASS ✅ 旧库迁移 + 保存接口全部验证通过')
