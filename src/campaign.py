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

# 日期标题。级别用 `#{2,6}` 而不是写死 `####`：
# 同一份报文里「### 当前时间」「### 活动列表」已经是三级标题了，
# 官方把日期降一级是非常现实的一次改动，写死四级会让整天静默消失。
# 行尾也不能锚死：实测过「#### 2026年10月20日 星期六」这种带星期后缀的写法。
# 尾缀只吃空格和制表符，**不能吃换行**（`\s*` 会把下一行也吞进日期标题，
# 实测直接导致后续分块整体错位）。星期、今日标记一律当作行尾文本收进来。
_DAY_RE = re.compile(r"^#{2,6}[ \t]*(\d{4})年(\d{1,2})月(\d{1,2})日([^\n]*)$", re.M)
# 「-   **活动标题**：xxx」之后到「**活动内容介绍**」之前
_TITLE_RE = re.compile(r"\*\*活动标题\*\*\s*[：:]\s*(.+?)\s*\n")
_BODY_RE = re.compile(
    r"\*\*活动内容介绍\*\*\s*[：:]\s*(.*?)\s*\n\s*\*\*活动图片介绍\*\*", re.S
)
# 活动文案里自己写出来的价格：「9.9元」「14.9元起」「两件套只要9.9元」
_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")

# 「减/省/满」后面的数字是**优惠额或门槛**，不是你要付的钱。
# 必须先把它们从文本里挖掉再找卖价，否则取 min 会稳定取到优惠额 ——
# 实测「满 20 减 3 元，仅需 9.9 元」被取成 ¥3.00（真卖价 ¥9.9），
# 「买一送一（省 12 元）」被当成 ¥12.00 的活动价。
# 而 min() 天然偏爱小数字，所以这个错是系统性的、不是偶发的。
_DISCOUNT_RE = re.compile(
    r"满\s*\d+(?:\.\d+)?\s*元?"  # 「满 20」「满20元」——门槛
    r"|"
    r"(?:立减|满减|减|省|优惠|原价|便宜)\s*\d+(?:\.\d+)?\s*元"  # 优惠额/原价
)


def _strip_discounts(text: str) -> str:
    """把文本里的门槛和优惠额挖掉，只留下可能是卖价的数字。"""
    return _DISCOUNT_RE.sub(" ", text or "")

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
        # **只看日期，不看 `is_today` 标记**。
        # 早先这里写 `if self.is_today or delta == 0`，于是接口给哪天标了
        # 「今日」就把哪天叫今天 —— 实测把 10-13 直接印成「（今天）」，
        # 跟 `today_campaigns` 那个 bug 是同一个错，只是挪了个地方。
        # 说「3 天后」永远比说错「今天」安全。
        if delta == 0:
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


# 正文里的价格只在「明确是卖价」的写法下才认：
# 「仅需15.9元起」「特价 9.9 元/份」。像「每天低至 0.33 元」「原价 18 元」
# 「任意消费满 6 元即可加购」都不会命中——它们分别是摊销价、原价和门槛，
# 拿任何一个当活动价都是骗人。
_BODY_PRICE_RE = re.compile(
    r"(?:仅需|只要|特价|售价|只需|低至)?\s*(\d+(?:\.\d+)?)\s*元\s*(?:起|/份|/件|起售)?"
)


def _first_price(title: str, body: str = "") -> float | None:
    """取活动里真实的价格。

    三道必须守的规矩，都是从实测报文里踩出来的：

    1. **标题优先，正文只在强模式下兜底。**
       标题写「49元150天麦金学期卡」，正文写「每天低至 0.33 元」——
       那是**摊销价**，不是你结账要付的钱。
       正文还常出现「原价 18 元」「任意消费满 6 元即可加购」这类
       原价/门槛数字，一律不认。

    2. **别用 ¥1 当硬下限。** 「0.1 元抢甜筒」是真活动，砍掉它是误伤；
       真正的摊销价靠第 1 条的强模式挡掉就够了。

    3. **别用 ¥500 当上限。** 麦当劳没有 500 元的单品，但有团餐套餐，
       拍脑袋的上限会把真活动变成「未标价」。

    4. **取价之前先把门槛和优惠额挖掉。** 「满 20 减 3 元，仅需 9.9 元」
       里有两个数字，取 `min` 会稳定拿到优惠额 3 —— 而优惠额和门槛
       通常都比卖价小，所以这是个**系统性偏向**，不是偶发误差。
    """
    vals = [float(m.group(1)) for m in _PRICE_RE.finditer(_strip_discounts(title))]
    vals = [v for v in vals if 0.1 <= v <= 10000]
    if vals:
        return min(vals)
    vals = [
        float(m.group(1))
        for m in _BODY_PRICE_RE.finditer(_strip_discounts(body))
    ]
    vals = [v for v in vals if 0.1 <= v <= 10000]
    return min(vals) if vals else None


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
        for j, t in enumerate(titles):
            # 标题里可能自带 markdown 强调（实测出现过 `**超值**𝟗.𝟗元…`），
            # 不剥掉会原样打进终端。
            title = t.group(1).strip().strip("*").strip()
            # 正文必须**在本活动自己的范围内**找，不能跨活动按序号配对：
            # 老活动缺「活动内容介绍」是常态，按下标取会把下一个活动的
            # 文案和价格安到它头上（实测把夜市的 ¥12.9 安到了早餐两件套上）。
            seg_end = titles[j + 1].start() if j + 1 < len(titles) else len(block)
            seg = block[t.end() : seg_end]
            m = _BODY_RE.search(seg)
            body = m.group(1).strip() if m else ""
            items.append(
                Campaign(
                    title=title,
                    summary=_condense(body),
                    price_yuan=_first_price(title, body),
                )
            )
        tail = head.group(4) or ""
        days.append(
            CampaignDay(
                day=day,
                is_today=bool(re.search(r"今日|今天", tail)),
                campaigns=tuple(items),
            )
        )
    return days


def _condense(body: str, limit: int = 40) -> str:
    """活动文案塞满了 emoji 和换行，取第一行能读的就行。

    必须跳过 HTML 标签行：`<img>` 有时就排在「活动内容介绍」之后、
    真正文案之前，直接取第一行会把一串 `<img src=... height="300"`
    当成活动说明打进终端。
    """
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("<") or line.startswith("!["):
            continue
        return line[:limit]
    return ""


def build_markdown(day_blocks: Sequence[tuple[date, str, Sequence[tuple[str, str]]]]) -> str:
    """拼一份 `campaign-calendar` 格式的报文。

    放在这里而不是各用各的：**自检和演示曾经各写了一份构造器**，
    结果一个月内就漂了（一份写「+3 天」一份写「+4 天」），
    而演示那份坏了 129 条自检一条都不会红 —— 因为没人测它。
    两个调用方共用同一个构造器，演示报文的可用性才能被自检覆盖到。
    """
    out: list[str] = [f"### 当前时间：{date.today().isoformat()} 14:25:13", "", "### 活动列表：", ""]
    for day, tail, items in day_blocks:
        out.append(f"#### {day.year}年{day.month}月{day.day}日{tail}")
        out.append("")
        for title, body in items:
            out += [
                "",
                f"-   **活动标题**：{title}",
                f"    **活动内容介绍**：{body}",
                "    **活动图片介绍**：",
                '    <img src="https://example.com/x.jpg" alt="" height="300" width="auto">',
                "",
            ]
    return "\n".join(out)


def fetch(client, specified_date: str | None = None) -> list[CampaignDay]:
    """拉活动日历。失败返回空列表（锦上添花的东西不该让主流程崩）。

    注意 `specifiedDate` **不要用**：schema 说它会返回「当天及前后各一天」，
    实测只返回 2 天（当天 + 次日），接上去会让「择日」从看整月退化成看明天。
    这个参数目前没有调用方，留着只是为了让接口事实有处可查。
    """
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
    """这一天里**今天还没有、或今天买不到这个价的**活动。

    「等几天更划算」这句话成立的前提，是那天有今天吃不到的东西。
    把今天已经在跑的活动也算进去，等于劝用户为一个他现在就能买的东西干等 ——
    那是把「择日」做成了拖延。

    但**同名不等于同价**：「麦麦夜市回归」今天 ¥12.9、三天后 ¥9.9，
    只看标题会判成「今天已有」而从名单里消失 —— 那恰好漏掉了
    择日最该告诉用户的一类信息。所以标题相同但更便宜的，也算新的。
    """
    known: dict[str, float | None] = {}
    for c in today:
        if c.title not in known:
            known[c.title] = c.price_yuan
        elif c.price_yuan is not None:
            old = known[c.title]
            known[c.title] = c.price_yuan if old is None else min(old, c.price_yuan)
    out = []
    for c in day.campaigns:
        if c.title not in known:
            out.append(c)
            continue
        base = known[c.title]
        # 今天更便宜（或一样便宜）→ 不值得为它等
        if base is not None and c.price_yuan is not None and c.price_yuan < base:
            out.append(c)
    return out


def today_campaigns(days: list[CampaignDay], today: date | None = None) -> list[Campaign]:
    """今天的活动。**只认日期，不认「今日」标记。**

    早先版本有一句 `d.is_today and not any(x.day == base for x in days)`，
    意思是"接口标了今日、但今天不在返回范围内时，就采信那个标记"。
    实测后果：本机日期是 2027-01-01 时，2026-10-11 那天的活动会被当成
    "今天"的活动输出 —— 把别的日子说成今天，跟这项目的立身之本正面冲突。

    宁可说"今天没有活动"，也不能拿标记去猜。
    """
    base = today or date.today()
    for d in days:
        if d.day == base:
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


def advise_line(
    days: list[CampaignDay], today: date | None = None, within: int = 7
) -> list[str]:
    """给 `advise` 用的一小段：今天有什么活动、最近哪天更值得等。

    `within` 必须能传进来：早先这里写死 7，跟 `campaign --days` 不是同一个数，
    用户在命令行改了窗口，顾问里还是老样子。
    """
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
    for d in upcoming(days, base, within):
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
