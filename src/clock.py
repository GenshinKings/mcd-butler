"""服务端时间：全项目**唯一**的「现在几点」基准。

为什么这个模块必须存在（而不是每个命令各自 `datetime.now()`）：
「今天到期，不用就作废」是本项目里**唯一不可逆**的提醒 —— 券过期了就是过期了，
没有任何补救通道。而它完全取决于「现在算作几点」。

曾经 `advise` 取服务端时间、`assets` 取本机时间，于是同一个账号同一时刻，
一个命令报「🚨 今天有 N 张不用就作废」，另一个报「0 张今天内到期」，
而错的方向恰恰是**漏报**。这种不一致靠读代码看不出来，只有把它们
关进同一个文件、让两边都调同一个函数，才不会再分叉。

三个踩过的坑，都写在这里以免重犯：

1. 曾经 `datetime.fromisoformat(str(text)[:26])`。报文带微秒加时区时
   （"2026-10-13T04:24:00.123456+08:00" 有 32 个字符），`[:26]` 会把 `+08:00`
   整段切掉，剩一个 naive 时间 —— 不报错，只是静默算错。
2. 反过来更糟：解析出 aware datetime 后去减券的到期日（naive），
   Python 抛 TypeError；落在外层 `except Exception` 里被吞掉，
   那条最要紧的到期提醒就**静默消失**了。
3. 本机时区设错 / 系统时间不准（换设备、挂 VPN、虚拟机）时，
   整份黄历和到期判断全错，而用户只会觉得"这券明明还没到期"。

所以这里一律返回**墙上时间**（不带时区），与券面到期日口径一致。
"""

from __future__ import annotations

import re
from datetime import datetime

from .mcd_client import McpError

__all__ = ["server_now", "server_now_with_source", "parse_server_datetime"]


_TZ_SUFFIX = re.compile(
    r"\s*(?:Z|[+-]\d{2}:?\d{2}|GMT\s*[+-]?\d{1,2}(?::\d{2})?|UTC\s*[+-]?\d{1,2})\s*$",
    re.IGNORECASE,
)


def parse_server_datetime(text) -> datetime | None:
    """把服务端时间报文解析成**不带时区**的 datetime。解析不了就返回 None。"""
    s = str(text).strip()
    if not s:
        return None
    for cand in (s, _TZ_SUFFIX.sub("", s).strip(), s.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(cand)
        except (ValueError, TypeError):
            continue
        # 万一还是拿到带时区的（3.10 只认 +HH:MM 这一种写法），
        # 直接丢掉 tzinfo 取墙上时间 —— 绝不让 aware 泄漏出去
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    return None


def server_now_with_source(client) -> tuple[datetime, str]:
    """取「现在几点」。返回 `(墙上时间, 来源)`，来源是 `"server"` 或 `"local"`。

    为什么不干脆只返回时间：退回本机时间这件事**必须让调用方知道**。
    到期作废是不可逆的，用户有权知道自己看到的「今天」是哪个钟给的。
    """
    try:
        data = client.data("now-time-info", {})
        text = (data or {}).get("datetime") if isinstance(data, dict) else None
        if text:
            parsed = parse_server_datetime(text)
            if parsed is not None:
                return parsed, "server"
    except McpError:
        # 接口层面的失败（限流/断网）才允许退回本机时间，并让调用方知道
        pass
    return datetime.now(), "local"


def server_now(client) -> datetime:
    """取服务端时间；拿不到就退回本机时间。只关心时间本身时用这个。"""
    return server_now_with_source(client)[0]
