"""麦当劳 MCP 客户端（Streamable HTTP，零第三方依赖）

设计取舍：
- 只用标准库 urllib，评委/路人 clone 下来无需 pip install 即可跑通。
- Token 只从环境变量读取，绝不落盘、绝不进仓库。
- MCP 返回体可能是纯 JSON，也可能是 SSE（event:/data:）流，两种都兼容。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"

class McpError(RuntimeError):
    """MCP 调用失败。"""

    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


def _iter_json_objects(text: str):
    """扫描出文本中所有括号配平的顶层 JSON 对象。

    不能直接用 `\\{.*\\}` 贪婪正则：麦当劳的返回体在字段说明里就带 `{...}`
    占位示例，贪婪匹配会把说明文字和真正的报文粘在一起，解析必然失败。
    """
    start = None
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : i + 1]
                    start = None


def extract_payload(text: str) -> Any:
    """从工具返回的 Markdown 文本中抽出内嵌的 JSON 业务报文。

    麦当劳 MCP 的返回体是「字段说明 + Markdown + 业务报文」的混合体，
    而且报文位置不固定——有时在末尾，有时夹在两段 Markdown 中间。
    所以判据不能是位置，只能是内容：找出带 `success` 键的那个对象。

    另外不能用括号计数：字段说明里的占位符（如 `{data[].title}`）没有引号包裹，
    会把计数带偏，导致真正的报文被吞进一个永远不闭合的对象里。
    """
    decoder = json.JSONDecoder()
    found: Any = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        # 快速过滤：真正的 JSON 键一定带引号，占位符 `data[].title` 不带
        if '"' not in text[i : i + 12]:
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "success" in obj:
            found = obj  # 取最后一个，最贴近真实报文
    return found


class McdMcpClient:
    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 60,
    ):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请先到 https://open.mcd.cn/mcp 激活，"
                "然后设置环境变量 MCD_MCP_TOKEN。"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_id: str | None = None
        self._seq = 0
        self._initialized = False

    # ---------- 传输层 ----------

    def _post(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        req = urllib.request.Request(self.base_url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                new_sid = resp.headers.get("Mcp-Session-Id")
                if new_sid:
                    self.session_id = new_sid
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            if exc.code == 401:
                raise McpError("Token 无效或已过期（HTTP 401）。", detail) from exc
            if exc.code == 429:
                raise McpError("触发限流（每 Token 每分钟 600 次）。", detail) from exc
            raise McpError(f"HTTP {exc.code}: {detail}", detail) from exc
        except urllib.error.URLError as exc:
            raise McpError(f"网络错误: {exc.reason}") from exc

        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> dict:
        raw = raw.strip()
        if not raw:
            return {}
        # SSE 流：取最后一条 data: 行
        if raw.startswith("event:") or raw.lstrip().startswith("data:"):
            payloads = [
                line[len("data:"):].strip()
                for line in raw.splitlines()
                if line.strip().startswith("data:")
            ]
            if payloads:
                raw = payloads[-1]
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError(f"无法解析 MCP 响应: {raw[:200]}") from exc

    # ---------- 协议层 ----------

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def initialize(self) -> dict:
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-butler", "version": "0.1.0"},
                },
            }
        )
        if "error" in resp:
            raise McpError(f"initialize 失败: {resp['error']}", resp)
        self._initialized = True
        return resp["result"]

    def list_tools(self) -> list[dict]:
        if not self._initialized:
            self.initialize()
        resp = self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        )
        return resp.get("result", {}).get("tools", [])

    def _rpc(self, method: str, params: dict) -> dict:
        if not self._initialized:
            self.initialize()
        resp = self._post(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        )
        if "error" in resp:
            raise McpError(f"{method} 失败: {resp['error']}", resp)
        return resp.get("result", {})

    # ---------- 业务层 ----------

    def call(self, tool: str, arguments: dict | None = None) -> str:
        """调用工具，返回拼接后的文本内容。"""
        result = self._rpc(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )
        parts = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(parts)

    def call_json(self, tool: str, arguments: dict | None = None) -> dict:
        """调用工具并直接抽出业务报文（data 字段已提升到顶层）。"""
        text = self.call(tool, arguments)
        payload = extract_payload(text)
        if payload is None:
            return {"success": False, "message": "无 JSON 报文", "raw": text}
        return payload

    def data(self, tool: str, arguments: dict | None = None) -> Any:
        """调用工具并返回 data 字段；失败抛异常。"""
        payload = self.call_json(tool, arguments)
        if not payload.get("success"):
            raise McpError(
                f"{tool} 调用失败: {payload.get('message')} (code={payload.get('code')})",
                payload,
            )
        return payload.get("data")
