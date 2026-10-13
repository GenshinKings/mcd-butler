"""麦门资产管理：领券巡检 + 卡包到期预警 + 积分过期提醒。

设计原则：**默认只读**。
巡检（查可领券、查积分、查卡包）不产生任何副作用；
只有显式传 `--claim` 才会真的调 `auto-bind-coupons` 领券。
领券会写进用户的真实会员账号，不能默认执行。

这里最难的部分不是调接口，而是**解析**。麦当劳 MCP 的三个资产接口
返回格式各不相同，而且没有一个是规整的 JSON 数组：

- `query-my-account`  → Markdown 说明 + 一段 JSON，`availablePoint` 是
  字符串小数 `"9.9"`，直接 `int()` 会抛异常（这是第一版把积分全算成 0 的原因）。
- `query-my-coupons`  → 纯 Markdown 文档：`## 券名` + `- **有效期**: ...`，
  没有内嵌 JSON，`extract_payload` 必然返回 None。
- `available-coupons` → 另一种 Markdown：列表项 `- 优惠券标题：X \\ 状态：已领取`。

所以下面三个函数各写一套解析，全部容错返回空值，绝不因为格式变了就崩。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .mcd_client import McpError, McdMcpClient

# `2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29` —— 只取前两个时间点，
# 后面的「周日 05:00-10:29」是重复的可用餐时段说明，正则不碰它。
_RANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
)
_HEAD_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*张")
_BENEFIT_RE = re.compile(r"^-\s*\*\*优惠\*\*:\s*(.+?)\s*$", re.M)
_VALID_RE = re.compile(r"^-\s*\*\*有效期\*\*:\s*(.+?)\s*$", re.M)
_RECEIVED_RE = re.compile(r"^-\s*\*\*领取时间\*\*:\s*(.+?)\s*$", re.M)
_TAGS_RE = re.compile(r"^-\s*\*\*标签\*\*:\s*(.+?)\s*$", re.M)

# `### 麦麦省优惠券列表：` 里的条目
_CLAIM_TITLE_RE = re.compile(r"^-?\s*优惠券标题[：:]\s*(.+?)\s*\\?\s*$", re.M)
_CLAIM_STATUS_RE = re.compile(r"状态[：:]\s*(.+?)\s*\\?\s*$", re.M)


def _num(value: Any) -> float:
    """积分字段是字符串小数（"9.9"），int() 会炸，必须走 float。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fmt_num(n: float) -> str:
    """9.9 → "9.9"；10.0 → "10"。避免无意义的 .0 尾巴。"""
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.1f}"


def _parse_end(text: str) -> datetime | None:
    """从有效期文本里抽出截止时刻。解析不了就返回 None，不猜。"""
    m = _RANGE_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%Y-%m-%d %H:%M")
    except ValueError:
        return None


# ---------------------------------------------------------------- 积分

def fetch_points(client: McdMcpClient) -> dict:
    """积分账户。任何字段缺失都返回 0，不抛异常。"""
    try:
        payload = client.call_json("query-my-account")
    except McpError:
        # Token 失效 / 限流 / 断网（McpFatalError）必须穿透，
        # **业务性 McpError 同样必须穿透**：吞掉它就会打印
        # 「卡包（0 张）／券池为空」，用户以为自己一张券都没有，
        # 实际是这一次根本没问到服务器 —— 「没查到」被说成了「没有」。
        raise
    except Exception:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    return {
        "currency": data.get("currency") or "积分",
        "available": _num(data.get("availablePoint")),
        "accumulative": _num(data.get("accumulativePoint")),
        "used": _num(data.get("usedPoint")),
        "frozen": _num(data.get("frozenPoint")),
        "expired": _num(data.get("expiredPoint")),
        "expire_this_month": _num(data.get("currentMouthExpirePoint")),
        "expire_next_month": _num(data.get("nextMouthExpirePoint")),
        "expire_last_month": _num(data.get("lastMouthExpirePoint")),
    }


# ------------------------------------------------------- 积分能干什么

def fetch_lottery(client: McdMcpClient) -> dict:
    """积分抽奖：抽一次要多少分、现在够不够抽。

    为什么要查它：只报「本月将过期 N 分」是在制造焦虑 ——
    用户看完不知道这点分能干嘛。抽奖是积分最现实的去处，
    而「抽一次要多少分」接口是明确给的（实测 drawPoint=24）。
    """
    try:
        payload = client.call_json("query-lottery-info")
    except McpError:
        # 业务性失败也必须穿透：返回空等于把「没查到」说成「没有」。
        raise
    except Exception:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    dec = data.get("drawDecision") or {}
    if not isinstance(dec, dict):
        dec = {}
    return {
        "name": data.get("activityName") or "积分抽奖",
        "status": data.get("activityStatusText") or "",
        "draw_point": _num(data.get("drawPoint")),
        "available": _num(data.get("availablePoint")),
        "eligible": bool(dec.get("resourceEligible")),
        "reason": str(dec.get("reason") or ""),
    }


def fetch_points_mall(client: McdMcpClient) -> dict:
    """麦麦商城里**真正能用积分换**的商品。

    实测踩过的坑：这个接口返回的 50 件商品里 `point` 全是 "0"、带 `price` ——
    那是**现金购买**的派对/读书会，不是积分兑换。
    拿它当「您的积分能换这些」，等于凭空造出一个不存在的兑换清单。
    所以只把 `point > 0` 的算作兑换项，一件都没有就如实说一件都没有。
    """
    try:
        payload = client.call_json("mall-points-products")
    except McpError:
        # 业务性失败也必须穿透：返回空等于把「没查到」说成「没有」。
        raise
    except Exception:
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    items = []
    for it in data:
        if not isinstance(it, dict):
            continue
        point = _num(it.get("point"))
        if point <= 0:
            continue
        items.append(
            {
                "name": str(it.get("spuName") or "未命名商品"),
                "point": point,
                "price": _num(it.get("price")),
                "cat": str(it.get("catName") or ""),
            }
        )
    items.sort(key=lambda x: x["point"])
    return {"total": len(data), "exchangeable": items}


def points_advice(points: dict, lottery: dict, mall: dict) -> list[str]:
    """积分去向的结论。**宁可说「什么都换不了」，也不编一份兑换清单。**

    管家的分寸：报完「您有 9.9 分」就闭嘴，是最敷衍的；
    但为了显得有用而列出一堆其实要用现金买的商品，是骗人。
    """
    if not points:
        return []
    unit = points["currency"]
    avail = points["available"]
    lines: list[str] = []
    expire = points["expire_this_month"] + points["expire_next_month"]

    if lottery:
        need = lottery["draw_point"]
        if need > 0:
            if avail >= need:
                times = int(avail // need)
                lines.append(
                    f"- 积分抽奖：抽一次 {fmt_num(need)} {unit}，"
                    f"您现在够抽 **{times} 次**"
                    + (f"（{lottery['status']}）" if lottery["status"] else "")
                )
            else:
                lines.append(
                    f"- 积分抽奖：抽一次要 {fmt_num(need)} {unit}，"
                    f"您有 {fmt_num(avail)} —— **还差 {fmt_num(need - avail)}**，先攒着"
                )
        elif lottery["status"]:
            lines.append(f"- 积分抽奖：{lottery['status']}")

    if mall:
        n_ex = len(mall["exchangeable"])
        if n_ex == 0:
            lines.append(
                f"- 麦麦商城：{mall['total']} 件在售，但**没有一件是纯积分兑换**"
                f"（都要另付现金），所以这点分暂时换不了东西"
            )
        else:
            # 分成「现在就换得起」和「还差一点」：混在一起报，用户得自己
            # 拿余额去比，那不叫管家，叫打印接口。
            afford = [it for it in mall["exchangeable"] if it["point"] <= avail]
            lines.append(
                f"- 麦麦商城：{n_ex} 件可用积分兑换，"
                f"按您现在的 {fmt_num(avail)} {unit}，"
                f"**{len(afford)} 件换得起**"
            )
            for it in afford[:3]:
                extra = f" + ¥{fmt_num(it['price'])}" if it["price"] > 0 else ""
                lines.append(f"  - ✅ {it['name']}　{fmt_num(it['point'])} {unit}{extra}")
            if not afford:
                cheap = mall["exchangeable"][0]
                lines.append(
                    f"  - 最便宜的「{cheap['name']}」要 {fmt_num(cheap['point'])} {unit}，"
                    f"还差 {fmt_num(cheap['point'] - avail)}"
                )
            elif len(afford) < n_ex:
                nxt = next((it for it in mall["exchangeable"] if it["point"] > avail), None)
                if nxt:
                    lines.append(
                        f"  - 再攒 {fmt_num(nxt['point'] - avail)} {unit} 就能换「{nxt['name']}」"
                    )

    if expire > 0 and mall and not mall["exchangeable"] and (
        not lottery or avail < lottery["draw_point"]
    ):  # noqa: SIM102
        # 到期了却无处可花 —— 这才是真该说的实话，别让用户白着急
        lines.append(
            f"- ⚠️ 有 {fmt_num(expire)} {unit} 即将过期，"
            f"但按上面的行情，它们**没有可换的东西** —— 不是您没抓紧，是没得换。"
        )
    return lines


# ---------------------------------------------------------------- 卡包

def parse_coupon_markdown(text: str) -> list[dict]:
    """解析 `query-my-coupons` 的 Markdown 文档。

    结构是「一个 `## 券名` 后面跟若干 `- **字段**: 值`」，
    所以做法是按 `##` 切成块，每块内部再用字段正则取值。
    """
    if not text:
        return []

    blocks: list[tuple[str, str]] = []
    starts = [m.start() for m in _HEAD_RE.finditer(text)]
    for i, s in enumerate(starts):
        line_end = text.find("\n", s)
        title = text[s:line_end].lstrip("#").strip() if line_end != -1 else ""
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append((title, text[s:end]))

    out: list[dict] = []
    for title, body in blocks:
        valid_text = ""
        m = _VALID_RE.search(body)
        if m:
            valid_text = m.group(1).strip()
        tags_raw = ""
        m = _TAGS_RE.search(body)
        if m:
            tags_raw = m.group(1).strip()
        tags = [t.strip() for t in re.split(r"[、,，]", tags_raw) if t.strip()]
        benefit = ""
        m = _BENEFIT_RE.search(body)
        if m:
            benefit = m.group(1).strip()
        received = ""
        m = _RECEIVED_RE.search(body)
        if m:
            received = m.group(1).strip()

        out.append(
            {
                "title": title or "未命名券",
                "benefit": benefit,
                "valid_text": valid_text,
                "end": _parse_end(valid_text),
                "tags": tags,
                "received": received,
            }
        )
    return out


def fetch_my_coupons(client: McdMcpClient, now: datetime | None = None) -> list[dict]:
    """卡包内已持有的券，并标注距到期还有多久。"""
    try:
        text = client.call("query-my-coupons", {})
    except McpError:
        # 业务性失败也必须穿透：返回空等于把「没查到」说成「没有」。
        raise
    except Exception:
        return []
    coupons = parse_coupon_markdown(text)
    now = now or datetime.now()
    for c in coupons:
        end = c.get("end")
        if end is None:
            c["expired"] = False
            c["days_left"] = None
            continue
        delta = end - now
        c["expired"] = delta.total_seconds() <= 0
        c["days_left"] = delta.days
        # 「今天之内到期」比「还剩 0 天」更贴近用户体感，单独标出来
        c["due_today"] = (
            not c["expired"] and delta.total_seconds() <= 24 * 3600
        )
    return coupons


# ------------------------------------------------------------ 可领券

def parse_claimable_markdown(text: str) -> list[dict]:
    """解析 `available-coupons` 的 Markdown 列表。

    条目形如：
        - 优惠券标题：免费脆薯饼 \\
          状态：已领取 \\
          优惠券图片：...
    行尾的 `\\` 是 MCP 拼 Markdown 时带的转义，要剥掉。
    """
    if not text:
        return []
    out: list[dict] = []
    for m in _CLAIM_TITLE_RE.finditer(text):
        title = m.group(1).strip()
        seg_end = text.find("优惠券标题", m.end())
        if seg_end == -1:
            seg_end = len(text)
        seg = text[m.end() : seg_end]
        sm = _CLAIM_STATUS_RE.search(seg)
        status = sm.group(1).strip() if sm else ""
        out.append({"title": title, "status": status})
    return out


def fetch_claimable(client: McdMcpClient) -> list[dict]:
    """可领取（或已领取）的券。"""
    try:
        text = client.call("available-coupons", {})
    except McpError:
        # 业务性失败也必须穿透：返回空等于把「没查到」说成「没有」。
        raise
    except Exception:
        return []
    return parse_claimable_markdown(text)


def claim_all(client: McdMcpClient) -> str:
    """一键领取所有可领券。**有副作用**，仅在用户显式要求时调用。"""
    return client.call("auto-bind-coupons", {})


# ---------------------------------------------------------------- 周报

def build_report(client: McdMcpClient, claim: bool = False, now=None) -> str:
    """生成一份 Markdown 资产周报。默认只读。

    `now` 可以外部注入（自检要用固定时间），不传就取服务端时间 ——
    **不能**退回 `datetime.now()` 了事：那会让同一账号在同一时刻
    一边报「今天有 N 张要作废」、一边报「0 张」，而错的方向是漏报。
    """
    from .clock import server_now_with_source

    if now is None:
        now, _src = server_now_with_source(client)
    else:
        _src = "given"
    lines: list[str] = ["# 本周麦门资产", ""]

    # ---- 积分
    points = fetch_points(client)
    if points:
        unit = points["currency"]
        lines.append("## 积分")
        lines.append(f"- 可用：**{fmt_num(points['available'])}** {unit}")
        lines.append(f"- 累计：{fmt_num(points['accumulative'])}　已用：{fmt_num(points['used'])}")
        lines.append(
            f"- 冻结：{fmt_num(points['frozen'])}　已过期：{fmt_num(points['expired'])}"
        )
        for label, key in (
            ("本月将过期", "expire_this_month"),
            ("下月将过期", "expire_next_month"),
            ("上月已过期", "expire_last_month"),
        ):
            v = points[key]
            if v > 0:
                lines.append(f"- ⚠️ **{label} {fmt_num(v)} {unit}**")
        if points["expire_this_month"] == 0 and points["expire_next_month"] == 0:
            lines.append("- 近两月无即将过期积分")
        # 「有分」不等于「有得花」。报完余额就结束是在制造焦虑 ——
        # 得把这点分到底能干嘛（抽奖够不够 / 商城有没有纯兑换）说清楚。
        advice = points_advice(points, fetch_lottery(client), fetch_points_mall(client))
        if advice:
            lines.append("- **这点分能干什么：**")
            lines.extend(advice)
        lines.append("")

    # ---- 卡包 + 到期预警（这一节才是真正的价值点）
    mine = fetch_my_coupons(client, now=now)
    due = [c for c in mine if c.get("due_today")]
    expired = [c for c in mine if c.get("expired")]
    alive = [c for c in mine if not c.get("expired") and not c.get("due_today")]

    lines.append(f"## 卡包（{len(mine)} 张）")
    # 到期作废是不可逆的，用户有权知道这个「今天」是哪个钟给的。
    # 服务端没问到、退回本机时间时，本机时区若不准，漏报就是白扔一张券。
    if _src == "local":
        lines.append(
            "- ⚠️ 服务端时间没问到，**按本机时间判断到期**（本机时钟不准会有偏差）"
        )
    if not mine:
        lines.append("- 卡包为空")
    else:
        if due:
            lines.append(
                f"- 🚨 **{len(due)} 张今天内到期** —— 不用就作废："
            )
            for c in due:
                lines.append(f"  - {c['title']}　{c['valid_text']}")
        if alive:
            lines.append(f"- ✅ {len(alive)} 张仍可用：")
            for c in alive:
                d = c.get("days_left")
                tail = f"　还剩 {d} 天" if d is not None else ""
                tags = "、".join(c["tags"])
                tag_tail = f"　[{tags}]" if tags else ""
                lines.append(f"  - {c['title']}　{c['valid_text']}{tail}{tag_tail}")
        if expired:
            lines.append(f"- ⏳ {len(expired)} 张已过期：")
            for c in expired:
                lines.append(f"  - {c['title']}　{c['valid_text']}")
    lines.append("")

    # ---- 可领券
    claimable = fetch_claimable(client)
    pending = [c for c in claimable if "已领取" not in (c.get("status") or "")]
    lines.append(f"## 麦麦省券池（{len(claimable)} 张）")
    if claimable:
        for c in claimable:
            mark = "· 已领取" if c not in pending else "· 待领取"
            lines.append(f"- {c['title']} {mark}")
    else:
        lines.append("- 券池为空")
    lines.append("")

    # ---- 领券（有副作用，只在明确要求时执行）
    if claim:
        lines.append("## 领券结果")
        if pending:
            lines.append("```")
            lines.append(claim_all(client).strip())
            lines.append("```")
        else:
            lines.append("- 没有待领取的券，跳过")
        lines.append("")

    if not claim:
        lines.append("（只读巡检，未领券。要真领请加 `--claim`）")
    return "\n".join(lines)
