"""EdgeOne Cloud Functions 最小验证版。

验证目标：
1. Flask WSGI 模式能否在 EdgeOne 正常运行
2. SSE 流式响应（text/event-stream）是否支持
3. 长耗时请求（>30s）是否在 maxDuration 内完成
4. 冷启动时 DB 连接 / 文件系统写入是否正常

路由说明：
  EdgeOne 自动剥离 cloud-functions/api/ 的 /api 前缀，
  所以 Flask 内部路由直接用相对路径（如 /health），
  实际访问 URL 为 https://<域名>/api/health
"""
import time
import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# 记录冷启动时间（模块加载即冷启动）
_COLD_START_TS = time.time()


@app.route('/health')
def health():
    """健康检查：返回运行时信息，验证 Flask 能否正常启动。"""
    now = time.time()
    return jsonify({
        'status': 'ok',
        'python': sys.version.split()[0],
        'cwd': os.getcwd(),
        'tmp_writable': os.access('/tmp', os.W_OK),
        'cold_start_age_sec': round(now - _COLD_START_TS, 2),
        'env_sample': {
            'APP_DATA_DIR': os.environ.get('APP_DATA_DIR', '(not set)'),
        },
    })


@app.route('/test-stream')
def test_stream():
    """SSE 流式测试：逐字返回诗句，模拟 AI 生成的流式输出。

    验证 EdgeOne 是否支持 text/event-stream + stream_with_context。
    如果支持，浏览器会看到文字逐字出现；如果不支持，会等全部生成完一次性返回。
    """
    text = "床前明月光，疑是地上霜。举头望明月，低头思故乡。"

    def generate():
        for i, ch in enumerate(text):
            # 每 0.3 秒输出一个字，模拟 AI 逐字生成
            time.sleep(0.3)
            yield f"data: {json.dumps({'i': i, 'char': ch}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    resp = Response(stream_with_context(generate()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp


@app.route('/test-slow')
def test_slow():
    """长耗时测试：sleep 指定秒数后返回，验证 maxDuration 限制。

    查询参数 ?seconds=30 可自定义等待时间。
    """
    seconds = int(request.args.get('seconds', 30))
    seconds = min(seconds, 110)  # 安全上限，避免超过 120s maxDuration
    time.sleep(seconds)
    return jsonify({
        'status': 'ok',
        'slept_sec': seconds,
        'cold_start_age_sec': round(time.time() - _COLD_START_TS, 2),
    })


@app.route('/test-fs')
def test_fs():
    """文件系统写入测试：验证 /tmp 是否可写（boot gate 标记文件需要）。"""
    marker = Path('/tmp') / '.edgeone_test_marker'
    try:
        marker.write_text('ready', encoding='utf-8')
        exists = marker.exists()
        content = marker.read_text(encoding='utf-8') if exists else None
        return jsonify({'writable': True, 'content': content})
    except Exception as e:
        return jsonify({'writable': False, 'error': str(e)}), 500


@app.route('/')
def index():
    """根路径说明。"""
    return jsonify({
        'service': 'edgeone-minimal-validation',
        'endpoints': {
            '/api/health': '健康检查',
            '/api/test-stream': 'SSE 流式测试',
            '/api/test-slow?seconds=30': '长耗时测试',
            '/api/test-fs': '文件系统写入测试',
        },
    })


# EdgeOne 要求入口文件暴露 app 变量（WSGI 模式自动检测）
# 不需要 if __name__ == '__main__'
