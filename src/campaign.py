"""麦门活动日历 —— 回答一个黄历回答不了的问题：**要不要改天再来**。

为什么要有这一层
----------------
黄历告诉你「今天宜吃什么」，但它不知道**明天会不会更划算**。
`campaign-calendar` 会把当月所有活动按日列出来 —— 哪个活动哪天开始、
大概什么价，官方自己是有的，只是没人把它跟「我今天要不要吃」放在一起看。

于是有了「择日」：

    今天这单能省 ¥6.00；
    10-15 有「麦麦夜市」（¥14.90 起），还有 4 天。
    不急的话，等那天更划算。

这里只做**如实转述**：活动标题、日期、活动里自己写出来的价格，
一个字都不加工。没写价格的活动就显示「未标价」——
绝不拿历史价或猜的数字冒充活动价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# 「#### 2026年10月11日 今日」
_DAY_RE = re.compile(r"^####\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(今日)?\s*$", re.M)
# 「-   **活动标题**：xxx」之后到「**活动内容介绍**」之前
_TITLE_RE = re.compile(r"\*\*活动标题\*\*\s*[：:]\s*(.+?)\s*\n")
_BODY_RE = re.compile(
    r"\*\*活动内容介绍\*\*\s*[：:]\s*(.*?)\s*\n\s*\*\*活动图片介绍\*\*", re.S
)
# 活动文案里自己写出来的价格：「9.9元」「14.9元起」「两件套只要9.9元」
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class Campaign:
    """一个活动。"""

    title: str
    summary: str = ""
    price_yuan: float | None = None

    def price_text(self) -> str:
        if self.price_yuan is None:
            return "未标价"
        return f"¥{self.price_yuan:.2f}"


@dataclass(frozen=True)
class CampaignDay:
    """某一天的全部活动。"""

    day: date
    is_today: bool = False
    campaigns: tuple[Campaign, ...] = ()

    def label(self, today: date | None = None) -> str:
        base = today or date.today()
        delta = (self.day - base).days
        week = _WEEKDAYS[self.day.weekday()]
        if self.is_today or delta == 0:
            return "今天"
        if delta == 1:
            return "明天"
        if delta == 2:
            return "后天"
        if delta < 0:
            return f"{abs(delta)} 天前"
        return f"{delta} 天后"

    def cheapest(self) -> float | None:
        prices = [c.price_yuan for c in self.campaigns if c.price_yuan is not None]
        return min(prices) if prices else None


def _first_price(title: str, body: str = "") -> float | None:
    """取活动里真实的价格。

    两道必须守的规矩，都是从实测报文里踩出来的：

    1. **标题优先，正文兜底。**
       标题写「49元150天麦金学期卡」，正文写「每天低至 0.33 元」。
       正文里那个 0.33 是**摊销价**，不是你结账要付的钱；
       拿它当活动价，等于告诉用户「今天有 ¥0.33 的活动」—— 纯属误导。

    2. **低于 ¥1 的一律不算。**
       麦当劳没有 ¥0.33 的单品或套餐，这种数字只会来自摊销、单价换算
       或者年份一类的噪声。
    """
    for text in (title, body):
        vals = [float(m.group(1)) for m in _PRICE_RE.finditer(text or "")]
        vals = [v for v in vals if 1.0 <= v < 500]
        if vals:
            return min(vals)
    return None


def parse_campaign_markdown(text: str) -> list[CampaignDay]:
    """解析 `campaign-calendar` 的 Markdown。

    结构是「`#### 日期`」分块，块内每个活动是
    `- **活动标题**：…` + `**活动内容介绍**：…` + `**活动图片介绍**：…`。

    解析不出来就返回空列表，而不是抛异常 ——
    活动日历是锦上添花的东西，它挂了不该连累黄历和顾问。
    """
    if not text:
        return []

    heads = list(_DAY_RE.finditer(text))
    days: list[CampaignDay] = []

    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[head.end() : end]
        try:
            day = date(int(head.group(1)), int(head.group(2)), int(head.group(3)))
        except ValueError:
            continue

        items: list[Campaign] = []
        titles = list(_TITLE_RE.finditer(block))
        bodies = list(_BODY_RE.finditer(block))
        for j, t in enumerate(titles):
            title = t.group(1).strip()
            body = bodies[j].group(1).strip() if j < len(bodies) else ""
            items.append(
                Campaign(
                    title=title,
                    summary=_condense(body),
                    price_yuan=_first_price(title, body),
                )
            )
        days.append(
            CampaignDay(
                day=day, is_today=bool(head.group(4)), campaigns=tuple(items)
            )
        )
    return days


def _condense(body: str, limit: int = 40) -> str:
    """活动文案塞满了 emoji 和换行，取第一行能读的就行。"""
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def fetch(client, specified_date: str | None = None) -> list[CampaignDay]:
    """拉活动日历。失败返回空列表（锦上添花的东西不该让主流程崩）。"""
    args = {"specifiedDate": specified_date} if specified_date else {}
    try:
        text = client.call("campaign-calendar", args)
    except Exception:  # noqa: BLE001 - 活动日历不可用时静默降级
        return []
    return parse_campaign_markdown(text)


def upcoming(
    days: list[CampaignDay], today: date | None = None, within: int = 7
) -> list[CampaignDay]:
    """今天之后 `within` 天内还有活动的日子。"""
    base = today or date.today()
    out = []
    for d in days:
        delta = (d.day - base).days
        if 0 < delta <= within and d.campaigns:
            out.append(d)
    return sorted(out, key=lambda d: d.day)


def fresh_campaigns(day: CampaignDay, today: list[Campaign]) -> list[Campaign]:
    """这一天里**今天还没有**的活动。

    「等几天更划算」这句话成立的前提，是那天有今天吃不到的东西。
    把今天已经在跑的活动也算进去，等于劝用户为一个他现在就能买的东西干等 ——
    那是把「择日」做成了拖延。
    """
    known = {c.title for c in today}
    return [c for c in day.campaigns if c.title not in known]


def today_campaigns(days: list[CampaignDay], today: date | None = None) -> list[Campaign]:
    base = today or date.today()
    for d in days:
        if d.day == base or (d.is_today and not any(x.day == base for x in days)):
            return list(d.campaigns)
    return []


def render(
    days: list[CampaignDay],
    today: date | None = None,
    within: int = 7,
    max_per_day: int = 3,
) -> str:
    """渲染活动日历。"""
    base = today or date.today()
    lines: list[str] = []
    lines.append("═" * 58)
    lines.append("  麦门活动日历")
    lines.append("═" * 58)
    lines.append(f"  今天 {base.isoformat()} {_WEEKDAYS[base.weekday()]}")
    lines.append("")

    if not days:
        lines.append("  活动日历接口这次没返回内容（不影响其它功能）。")
        return "\n".join(lines)

    tdy = today_campaigns(days, base)
    if tdy:
        lines.append(f"  今天有 {len(tdy)} 个活动")
        for c in tdy[:max_per_day]:
            lines.append(f"    · {c.title}　{c.price_text()}")
            if c.summary:
                lines.append(f"      {c.summary}")
        if len(tdy) > max_per_day:
            lines.append(f"    …还有 {len(tdy) - max_per_day} 个")
    else:
        lines.append("  今天没有活动。")
    lines.append("")

    # 只列「今天吃不到的新活动」—— 今天就能买的东西不值得让人干等。
    fresh_days = [(d, fresh_campaigns(d, tdy)) for d in upcoming(days, base, within)]
    fresh_days = [(d, f) for d, f in fresh_days if f]
    if fresh_days:
        lines.append(f"  接下来 {within} 天，{len(fresh_days)} 天有今天吃不到的新活动：")
        for d, pool in fresh_days:
            names = "、".join(c.title for c in pool[:2])
            if len(pool) > 2:
                names += f" 等 {len(pool)} 个"
            prices = [c.price_yuan for c in pool if c.price_yuan is not None]
            price = f"　最低 ¥{min(prices):.2f}" if prices else ""
            lines.append(
                f"    🆕 {d.day.strftime('%m-%d')} {_WEEKDAYS[d.day.weekday()]}"
                f"（{d.label(base)}）{price}"
            )
            lines.append(f"      {names}")
    else:
        lines.append(
            f"  接下来 {within} 天没有「今天吃不到」的新活动 —— 想吃就今天去，不用等。"
        )
    lines.append("")
    lines.append("  价格取自活动文案自己写的数字；没写的显示「未标价」，不猜。")
    return "\n".join(lines)


def advise_line(days: list[CampaignDay], today: date | None = None) -> list[str]:
    """给 `advise` 用的一小段：今天有什么活动、最近哪天更值得等。"""
    base = today or date.today()
    out: list[str] = []
    tdy = today_campaigns(days, base)
    if tdy:
        cheap = min(
            (c.price_yuan for c in tdy if c.price_yuan is not None), default=None
        )
        tail = f"，最低 ¥{cheap:.2f}" if cheap is not None else ""
        out.append(f"今天 {len(tdy)} 个活动{tail}：{tdy[0].title}")
    # 只提「今天没有的新活动」：今天就能买的东西不值得让人干等。
    for d in upcoming(days, base, 7):
        fresh = fresh_campaigns(d, tdy)
        if not fresh:
            continue
        prices = [c.price_yuan for c in fresh if c.price_yuan is not None]
        tail = f"（最低 ¥{min(prices):.2f}）" if prices else "（未标价）"
        out.append(
            f"　{d.day.strftime('%m-%d')}（{d.label(base)}）"
            f"「{fresh[0].title}」{tail} —— 今天没有，不急可以等这天"
        )
        if len(out) >= 3:
            break
    return out
