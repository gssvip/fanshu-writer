import sys, os, traceback, json, io
sys.path.insert(0, '/workspace/source/backend')
os.chdir('/workspace/source/backend')

# 用mock把LLM调用替换掉（因为没API key），让我们走完整个 generate() 流程到 except/finally
import unittest.mock as mock

# 初始化 Flask app context
import app as app_module
app = app_module.app

def run():
    with app.app_context():
        # 确保至少有一个书和会话
        from app import db, Book, BookBible
        # 用之前脚本建的 utest3 用户的书
        book = Book.query.first()
        print('book=', book.id, book.title)
        bb = BookBible.query.filter_by(book_id=book.id).first()
        print('bb exists?', bool(bb))
        # 走 smart_generate 全部逻辑直到调用LLM，我们用monkeypatch掉gw.chat_stream
        from blueprints.chat_collab_bp import smart_generate
        from flask import request
        # 伪造 request.json
        sel_preview = "焚书藏道：焚书坑儒被改写成焚仙魔神；秦廷收缴天下功法典籍，主角是奉命抄录焚书的书吏，抄一部记一部，凡人肉身竟成活体道藏。别人靠师承，他靠背下来的五千卷道书。"
        body = {'book_id': str(book.id), 'dimension': 'concept', 'suggestion': sel_preview, 'requirement': '', 'skill_pack_ids': [], 'session_id': None}
        # 造一个内联request对象 用test client
        client = app.test_client()
        # 登录
        from app import User
        u = User.query.first()
        tok = None
        # 直接拿一个token
        try:
            import requests
            r = client.post('/api/auth/login', json={'username':u.username,'password':'12345678'})
            print('login', r.status_code)
            tok = r.get_json().get('access_token') or r.get_json().get('token')
        except Exception as e:
            print('login err', e)
        H = {'Authorization': f'Bearer {tok}'}
        # 用 mock 替换 gateway chat_stream -> 返回假 stream，让我们走完自检/卡片/异常
        import blueprints.chat_collab_bp as bp_mod
        def fake_chat_stream(self, messages, temperature=0.7, max_tokens=2000, **kw):
            # 模拟吐一段构思内容 1200+字（带卡片？）
            out = (
"""核心创意：焚书藏道

一句话梗概：秦法以“焚仙魔神书”立国，书吏沈墨奉令抄录后焚典，日夜过手五千卷却不被允许修炼，遂以凡人脑为书库、心血为墨，把每卷功法一字字印入魂海；别人拜师学艺，他靠背书破境，在禁道时代凭一己之身，活成最后一卷未焚的道藏。

主角：沈墨，二十岁，御史台校书郎最下等的抄书吏。容貌普通、沉默寡言，手上常年沾墨，左手食指因长年按纸磨出茧。性格：极稳极忍，对人不争对错，对书过目不忘，每焚一卷都在心里对自己说“我替你记住”。

核心矛盾：
1. 外压——秦廷焚书是顶层阳谋，帝师要把“道”垄断在皇室手中，民间私藏一经发现满门抄斩；沈墨每多记一卷，就是把脖子往绞索里多伸一寸。
2. 内锁——凡人肉身藏书有极限，记到第三千卷会七窍流血；他必须在暴毙前，把脑中的书“写回”现世，或找到让自己肉身承载真道的破局法。
3. 反派：帝师公孙鞅（借商君之名化用），秦时权相，主持焚书之政；他知道“凡人藏书”这条路可通神，所以焚书是引蛇出洞——引所有藏书的人出头，再一网打尽，把他们脑中的书榨出来炼成帝道丹。
4. 女主/关键对手：公孙鞅的养女，公孙月。她是秦廷最锋利的刀，负责查抄私藏；她查案时与沈墨数次交锋，最后发现她自己才是当年沈家焚书案唯一的幸存者——她的亲生父亲，就是因为把半部《归藏》交给少年沈墨才被公孙鞅满门抄斩。

世界观底色：
- 秦法：“焚书令”下，一切“道”归皇室；民间不得论道、不得藏典、不得私自授受。违者腰斩于市，邻里连坐。
- 境界：凡人—开窍—聚纹—凝符—化篆—载道—立道—合道。每晋一阶要能“背下”对应数目的道书：开窍需百卷，聚纹需千卷，凝符需三千，化篆需五千，载道需万卷，立道需自著一经，合道需让那一经在世间流传。
- 沈墨的破境法：别人引天地灵气入体，他引书中文字入心。每一卷读过并记住的道书，都会在魂海中以墨字凝成一片竹简。竹简积到对应数目，境界自开。
- 代价：多记一卷，墨痕就会向心脏多爬一分。爬到心尖，人即成书——活不过三日，字会从皮肤往外渗。这是沈墨最大的倒计时悬念。

主线推进（前20%）：
- 开篇钩子：沈墨焚完今日的第七十二卷，拍了拍手上的灰，回到值房把今日所焚的书默写在厕所墙砖的夹缝里——每面墙砖里都藏着一卷书。
- 触发事件：焚书令升级，御史台要把所有抄书吏都集中到骊山狱统一“校录”，实则是把这些过目不忘的人做成“活书库”；沈墨必须在被带走之前，把墙砖里的书，连同他脑子里的三千卷，一起挪出咸阳。
- 卷一高潮：沈墨在焚书大典前一夜，把公孙月引到御史台藏书阁地下，让她亲眼看见自己的血是怎么沿着墙砖缝隙渗进去，把那些墨字点亮成金色竹简书——“你爹当年交给我的，不只半部《归藏》，还有你这个女儿，我记了二十年。”
- 卷末钩子：公孙月的刀柄已经架在了沈墨的脖子上，但她身后的黑色宫门外，公孙鞅的禁军已经把藏书阁围得水泄不通。她的袖口沾着和墙砖上一模一样的金色墨光。
"""
            )
            # 按 40 字一块吐
            for i in range(0, len(out), 40):
                yield out[i:i+40]
        # 打补丁：LLMGateway是在函数内部from llm_gateway引入，mock llm_gateway里的类 + get_llm_config
        import llm_gateway as gw_mod
        p1 = mock.patch.object(gw_mod, 'LLMGateway')
        MockGW = p1.start()
        inst = MockGW.return_value
        inst.chat_stream = lambda *a, **kw: fake_chat_stream(inst, *a, **kw)
        p2 = mock.patch.object(gw_mod, 'get_llm_config', return_value=('http://mock', 'x', 'mock-model'))
        p2.start()
        try:
            # 调 smart_generate
            r = client.post('/api/ai/smart/generate', json=body, headers=H, base_url='http://localhost')
            print('HTTP STATUS:', r.status_code)
            print('HEADERS:', dict(r.headers))
            data = b''.join(r.response.iter_encoded()) if hasattr(r.response, 'iter_encoded') else r.data
            print('BODY LEN bytes:', len(data))
            text = data.decode('utf-8', errors='replace')
            print('---BODY FRAMES (last 8000 chars)---')
            print(text[-8000:])
        except Exception as e:
            print('EXCEPTION:', type(e).__name__, e)
            traceback.print_exc()
        finally:
            p1.stop(); p2.stop()

run()
