"""P1-5 MCP 客户端：读 MCP_SERVERS_JSON 环境变量 → 把已注册 MCP tools 的
JSON Schema 参数表注入到通用聊天 LLM 的 function_call（tools 参数）。

⚠️ 设计原则（避免多模态/二维码）：
- 只处理 JSON 协议声明的 MCP server（HTTP 调用型 & 本地命令行子进程型）
- 绝不触碰二维码导入、图像/音频识别、文件上传等多模态内容
- LLM 返回 tool_calls 时：我们在此模块里负责按 server 地址发起实际调用，把结果
  再塞回 messages 继续跑 chat_stream（跟 Chat Completions function calling 流程一致）

MCP_SERVERS_JSON 环境变量格式（支持多个服务器）：
[
  {
    "name": "fanshu-tools",            // 唯一 server 名，用作 tool 前缀
    "transport": "stdio",              // "stdio" = 本地命令行启动子进程; "http" = HTTP POST
    "command": "python",               // stdio: 可执行文件路径
    "args": ["-m", "fanshu_mcp_srv"],  // stdio: 参数列表；或 http 留空
    "base_url": "http://127.0.0.1:8787/mcp",  // http: 服务器根 URL
    "api_key": "",                     // http: 可选鉴权 key
    "tools": [                         // 显式声明本 server 可用的 tools（不声明=该 server 不启用）
      {
        "name": "fetch_bookstore_rank",
        "description": "调用指定书店API获取今日销量前N的题材分类榜",
        "input_schema": {"type":"object", "properties":{"n":{"type":"integer", "default":10}}, "required":[]}
      }
    ]
  },
  ...
]

最小使用姿势：在 chat_general 把
  `mcp_tools = MCPToolRegistry().available_tools_for_llm()`
的结果注入到 LLMGateway.chat_stream 的 `tools=` 参数即可。
LLM 触发 function calling 时，调用
  `await MCPToolRegistry().dispatch(tool_name, tool_args)` 拿回结果，
再按标准 function calling 协议 append assistant + tool 两条 message 后继续流。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any, Dict, List, Optional


class MCPToolRegistry:
    """MCP server 注册表（进程内单例，惰性解析 MCP_SERVERS_JSON）。

    线程安全：首次 load_servers() 走锁，之后只读。
    """

    _instance: Optional['MCPToolRegistry'] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                o = super().__new__(cls)
                o._lock = threading.Lock()
                o._servers: List[Dict[str, Any]] = []
                o._loaded = False
                o._procs: Dict[str, subprocess.Popen] = {}
                cls._instance = o
            return cls._instance

    # ------------------------------------------------------------------
    def load_servers(self, force: bool = False) -> List[Dict[str, Any]]:
        """解析 MCP_SERVERS_JSON，失败静默返回空列表（不炸掉主流程）。"""
        with self._lock:
            if self._loaded and not force:
                return self._servers
            raw = (os.environ.get('MCP_SERVERS_JSON') or '').strip()
            servers: List[Dict[str, Any]] = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        servers = [s for s in parsed if isinstance(s, dict)]
                except Exception:
                    servers = []
            self._servers = servers
            self._loaded = True
            return self._servers

    # ------------------------------------------------------------------
    def available_tools_for_llm(self) -> List[Dict[str, Any]]:
        """返回能直接塞进 OpenAI function calling `tools=` 参数的 list。

        为了避免多个 server 之间的 tool 重名，统一按 "server__tool" 下划线前缀命名，
        并在 description 前置「[MCP serverName]」标识来源，方便 LLM 选择。
        """
        result: List[Dict[str, Any]] = []
        for srv in self.load_servers():
            sname = str(srv.get('name') or 'mcp').replace(' ', '_')
            for tool in (srv.get('tools') or []) if isinstance(srv.get('tools'), list) else []:
                if not isinstance(tool, dict) or not tool.get('name'):
                    continue
                tname = f"{sname}__{str(tool['name'])}"
                desc = tool.get('description') or ''
                desc = f"[MCP {sname}] {desc}".strip()
                schema = tool.get('input_schema')
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                result.append({
                    "type": "function",
                    "function": {
                        "name": tname,
                        "description": desc,
                        "parameters": schema,
                    },
                })
        return result

    # ------------------------------------------------------------------
    def _split_tool_name(self, llm_tool_name: str) -> (Optional[Dict[str, Any]], Optional[str]):
        """把 LLM 用的 "server__tool" 格式拆回 (server, original_tool_name)。"""
        if not isinstance(llm_tool_name, str) or '__' not in llm_tool_name:
            return None, None
        sname, _, tname = llm_tool_name.partition('__')
        for s in self.load_servers():
            if str(s.get('name') or '').replace(' ', '_') == sname:
                for t in (s.get('tools') or []) if isinstance(s.get('tools'), list) else []:
                    if isinstance(t, dict) and str(t.get('name')) == tname:
                        return s, tname
        return None, None

    # ------------------------------------------------------------------
    def dispatch(self, llm_tool_name: str, tool_args: Any) -> str:
        """真正调用一次 MCP tool：解析 → 走对应 transport → 拿回结果字符串。

        任何失败都返回可被 LLM 理解的纯文本错误说明（不抛异常，避免流中断）。
        """
        server, tool_name = self._split_tool_name(llm_tool_name)
        if server is None:
            return f"[MCP_ERROR] unknown tool: {llm_tool_name}. Ask user to configure MCP_SERVERS_JSON."
        transport = str(server.get('transport') or '').lower()
        try:
            if transport == 'stdio':
                return self._call_stdio(server, tool_name, tool_args)
            if transport == 'http':
                return self._call_http(server, tool_name, tool_args)
            return f"[MCP_ERROR] unsupported transport: {transport}"
        except Exception as e:  # noqa: BLE001 - 统一转成文本给 LLM 消化
            return f"[MCP_ERROR] {type(e).__name__}: {str(e)[:300]}"

    # ------------------------------------------------------------------
    def _call_http(self, server: Dict[str, Any], tool_name: str, tool_args: Any) -> str:
        import requests as _requests
        base = (server.get('base_url') or '').rstrip('/')
        if not base:
            return "[MCP_ERROR] HTTP transport requires base_url."
        url = f"{base}/tools/{tool_name}/invoke"
        headers = {"Content-Type": "application/json"}
        key = server.get('api_key') or ''
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"arguments": tool_args if isinstance(tool_args, dict) else {"value": tool_args}}
        resp = _requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            return f"[MCP_ERROR] HTTP {resp.status_code}: {resp.text[:300]}"
        try:
            data = resp.json()
        except Exception:
            data = resp.text[:2000]
        if isinstance(data, dict) and 'content' in data:
            blocks = data['content'] if isinstance(data['content'], list) else [data['content']]
            texts = []
            for b in blocks:
                if isinstance(b, dict) and b.get('type') == 'text':
                    texts.append(str(b.get('text', '')))
                elif isinstance(b, str):
                    texts.append(b)
            return '\n'.join(texts)[:4000] or json.dumps(data, ensure_ascii=False)[:4000]
        return json.dumps(data, ensure_ascii=False)[:4000]

    # ------------------------------------------------------------------
    def _call_stdio(self, server: Dict[str, Any], tool_name: str, tool_args: Any) -> str:
        """HTTP 子进程型 MCP server：简单的 JSON-RPC 1 次请求/响应（不保留长连接）。

        跟官方 MCP stdio 协议一致：
        - 发送 {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":...,"arguments":...}}
        - 期待 {"id":1,"result":{"content":[...]}}
        不做 initialize/negotiate：对最简单的 MCP server 够用；复杂 server 建议走 http transport。
        """
        cmd = server.get('command')
        if not cmd:
            return "[MCP_ERROR] stdio transport requires command."
        args = list(server.get('args') or []) if isinstance(server.get('args'), list) else []
        req = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": tool_args if isinstance(tool_args, dict) else {}},
        }, ensure_ascii=False)
        try:
            p = subprocess.run(
                [cmd] + args, input=req, capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except subprocess.TimeoutExpired:
            return "[MCP_ERROR] stdio tool timeout (60s)."
        except FileNotFoundError:
            return f"[MCP_ERROR] stdio command not found: {cmd}"
        out = (p.stdout or '').strip()
        if not out and p.returncode != 0:
            return f"[MCP_ERROR] stdio exit={p.returncode}: {(p.stderr or '')[:300]}"
        # 最后一行 JSON（兼容前面打印 warning 的 server）
        last_json = ''
        for line in out.splitlines():
            s = line.strip()
            if s.startswith('{'):
                last_json = s
        if not last_json:
            return out[:2000]
        try:
            j = json.loads(last_json)
        except Exception:
            return out[:2000]
        if isinstance(j, dict):
            if 'error' in j and isinstance(j['error'], dict):
                return f"[MCP_ERROR] {j['error'].get('message') or j['error']}"
            res = j.get('result')
            if isinstance(res, dict) and 'content' in res:
                blocks = res['content'] if isinstance(res['content'], list) else [res['content']]
                texts = []
                for b in blocks:
                    if isinstance(b, dict) and b.get('type') == 'text':
                        texts.append(str(b.get('text', '')))
                    elif isinstance(b, str):
                        texts.append(b)
                return '\n'.join(texts)[:4000] or json.dumps(res, ensure_ascii=False)[:4000]
            return json.dumps(res, ensure_ascii=False)[:4000] if res is not None else out[:2000]
        return out[:2000]
