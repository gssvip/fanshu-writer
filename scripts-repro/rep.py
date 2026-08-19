import requests, json
base = 'http://127.0.0.1:5000'
# 1. 注册/登录（若用户空则注册）
r = requests.post(base+'/api/auth/register', json={'username':'utest3','password':'12345678','email':'u3@t.tt'})
print('register', r.status_code, r.text[:300])
if r.status_code != 200:
    r = requests.post(base+'/api/auth/login', json={'username':'utest3','password':'12345678'})
    print('login', r.status_code, r.text[:300])
tok = (r.json() or {}).get('access_token') or (r.json() or {}).get('token')
H = {'Authorization': f'Bearer {tok}', 'Content-Type':'application/json'}
# 2. 建一本空小说
r = requests.post(base+'/api/books', json={'title':'智驾构思排查本','genre':'玄幻','book_type':'网络小说'}, headers=H)
book_id = r.json().get('id')
print('book_id=', book_id)
# 3. 构思维度提需求 -> 生成5方案
r = requests.post(base+'/api/ai/smart/suggest', headers=H, json={'book_id':book_id,'dimension':'concept','requirement':'写一本修仙文，男主从底层杂役做起，一步步修炼登顶，走杀伐果断路线','skill_pack_ids':[]}, timeout=180)
print('suggest status', r.status_code)
rj = r.json() if r.status_code==200 else None
sugs = (rj or {}).get('suggestions') or []
print('got', len(sugs), 'suggestions')
if not sugs:
    print('ERR suggest body=', (r.text or '')[:1500])
    raise SystemExit
sel = sugs[2] if len(sugs)>=3 else sugs[0]
print('select title=', sel.get('title'))
# 4. 选方案生成（SSE）
resp = requests.post(base+'/api/ai/smart/generate', headers=H, json={'book_id':book_id,'dimension':'concept','suggestion':sel.get('preview',''),'requirement':'','skill_pack_ids':[]}, stream=True, timeout=360)
print('generate status', resp.status_code, 'ct=', resp.headers.get('content-type'))
seen = []
for raw in resp.iter_content(chunk_size=None, decode_unicode=False):
    if not raw: continue
    for piece in raw.decode('utf-8', errors='replace').split('\n'):
        if piece.strip():
            seen.append(piece.strip())
            if len(seen) > 50: seen = seen[-50:]
print('frames received total last 50=', len(seen))
for i,l in enumerate(seen): print(i, l[:420])
