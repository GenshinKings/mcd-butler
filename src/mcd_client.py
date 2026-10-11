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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


def load_dotenv() -> None:
    """读取项目根目录的 `.env`（若存在），填进 os.environ。

    为什么要有这段：仓库里给了 `.env.example`，说明写的是「复制为 .env 后填入」。
    用户照做之后，如果代码只读 `os.environ`，他照样会看到「缺少 MCP Token」——
    因为复制文件不等于 export 环境变量。
    「照着文档一步步做，结果跑不起来」是最劝退的一种失败，
    而且用户根本不会怀疑是项目的问题，只会觉得自己笨。

    规则：已存在的环境变量优先，.env 只补缺；不覆盖、不落盘、不打印。
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

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
        timeout: int = 15,
        max_retries: int = 2,
        backoff_base: float = 0.5,
    ):
        load_dotenv()
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise McpError(
                "缺少 MCP Token。请先到 https://open.mcd.cn/mcp 激活，"
                "然后设置环境变量 MCD_MCP_TOKEN。"
            )
        self.base_url = base_url.rstrip("/")
        # 实测单次算价 < 1 秒。超时设 60 秒只会让 CLI 看起来"卡死了"，
        # 用户等不到结果也等不到报错；15 秒足够，且失败后还能重试。
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
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

        # 只对「值得重试」的失败退避重试：限流 429 和服务端 5xx。
        # 401 是 Token 问题，重试一百次也不会变对，立刻失败让用户去换 Token。
        # 为什么必须退避：麦当劳限流 600 次/分钟，而 compare 一次就能打几十次；
        # 踩到 429 还全速重打，只会把限流窗口越拖越长。
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(self.base_url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    new_sid = resp.headers.get("Mcp-Session-Id")
                    if new_sid:
                        self.session_id = new_sid
                return self._parse(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "ignore")
                if exc.code == 401:
                    raise McpError("Token 无效或已过期（HTTP 401）。", detail) from exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                last_exc = McpError(
                    "触发限流（每 Token 每分钟 600 次）。" if exc.code == 429 else f"HTTP {exc.code}: {detail}",
                    detail,
                )
                if not retryable or attempt >= self.max_retries:
                    raise last_exc from exc
            except urllib.error.URLError as exc:
                last_exc = McpError(f"网络错误: {exc.reason}")
                if attempt >= self.max_retries:
                    raise last_exc from exc

            time.sleep(self.backoff_base * (2**attempt))

        raise last_exc if last_exc else McpError("请求失败")

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
