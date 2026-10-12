"""麦门黄历：把「今天该怎么点」写成一张能晒的宜忌表。

为什么做这个
------------
市面上的麦当劳趣味项目，黄历是编的、性格是抽的、运是算着玩的。
我们不编 —— 这里的每一条「宜」「忌」，背后都真的打过一次算价接口：

    宜 = 这张券挂上去，接口真的给我便宜了这么多
    忌 = 要么这单品压根没券，要么挂上去反而更贵（随单购陷阱）

梗是外壳，账是真的。这才是这个项目该有的样子：
**别人的黄历是编出来的，我们的黄历是算出来的。**

设计约束
--------
1. 纯函数式：吃 menu / coupons / oracle 三个参数，不认识网络，
   所以能离线自测，也能在真实接口上一模一样地跑。
2. 预算自觉：每张券最多 2 次算价（裸买 + 用券），裸买价按商品缓存，
   预算见底立刻收工，绝不偷偷超支。
3. 绝不为了凑齐"宜忌各三条"而编造条目 —— 算不出就是算不出，宁缺毋滥。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .coupon_engine import Assignment, Budget, CartItem, Coupon, PriceResult
from .mcd_client import McpFatalError

# ------------------------------------------------------------------ 时段
# 麦当劳的券大量是时段券（早餐/夜宵），黄历必须先把"现在几点"说清楚，
# 否则推荐一张 10:31 就失效的早餐券，等于骗人。
# 边界用「当天 0 点起的分钟数」，因为早餐不是整点结束的。
# 麦当劳早餐 10:30 收摊：早先这里写的是 (5, 10) 按整点切，
# 于是 10:15 时黄历第一行已经印「午市」，而早餐券其实还能再用 15 分钟 ——
# 一张还能用的券被说成过了时段，比不提醒更坑人。
def _m(h: int, minute: int = 0) -> int:
    return h * 60 + minute


PERIODS: list[tuple[int, int, str, str]] = [
    (_m(5), _m(10, 30), "早市", "早餐券在这个点还能用，过 10:30 就作废"),
    (_m(10, 30), _m(14), "午市", "正餐时段，套餐券最全"),
    (_m(14), _m(17), "下午茶", "甜品饮料券的主场，正餐券多半歇着"),
    (_m(17), _m(21), "晚市", "全天券都能用，人最多"),
    (_m(21), _m(24), "夜宵", "夜宵券专场，白天的好券反而用不了"),
    (_m(0), _m(5), "深夜", "多数门店已打烊，先确认营业时间"),
]


def period_of(now: datetime) -> tuple[str, str]:
    """返回（时段名，提示语）。"""
    mins = now.hour * 60 + now.minute
    for start, end, name, tip in PERIODS:
        if start <= mins < end:
            return name, tip
    return "深夜", PERIODS[-1][3]


# ------------------------------------------------------------------ 到期
def days_left(valid_until: str, now: datetime) -> int | None:
    """券还剩几天到期。解析不出来返回 None（绝不当成"今天到期"吓唬人）。"""
    if not valid_until:
        return None
    text = valid_until.strip()
    # 常见格式：2026-10-11 / 2026-10-11 23:59 / 2026/10/11
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(text[: len(fmt) + 4].strip(), fmt)
        except ValueError:
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
        return (dt.date() - now.date()).days
    return None


def expiry_note(valid_until: str, now: datetime) -> str:
    d = days_left(valid_until, now)
    if d is None:
        return ""
    if d < 0:
        return "已过期"
    if d == 0:
        return "今日到期，不用就作废"
    if d == 1:
        return "明天到期"
    if d <= 3:
        return f"{d} 天后到期"
    return ""


# ------------------------------------------------------------------ 条目
@dataclass
class Reading:
    """黄历上的一行。kind 只有三种：宜 / 忌 / 平。"""

    kind: str
    product_name: str
    coupon_title: str = ""
    pay_fen: int = 0
    base_fen: int = 0
    saved_fen: int = 0
    note: str = ""

    @property
    def rate(self) -> float:
        """折扣力度（省下的占裸买价的比例）。"""
        if self.base_fen <= 0:
            return 0.0
        return self.saved_fen / self.base_fen


def _match_products(menu: list[CartItem], coupon: Coupon) -> list[CartItem]:
    """券能挂在哪些商品上。先按编码精确，再按名称归一化后包含匹配。

    宁可少推，不可错推：匹配不上就返回空，绝不拿不相干的商品凑数。
    """
    if coupon.product_codes:
        hits = [i for i in menu if i.product_code in coupon.product_codes]
        if hits:
            return hits
    names = [_norm(n) for n in coupon.product_names if n]
    title = _norm(coupon.title)
    out: list[CartItem] = []
    for item in menu:
        iname = _norm(item.name or "")
        if not iname:
            continue
        if any(n and (n == iname or n in iname or iname in n) for n in names):
            out.append(item)
        elif title and (title in iname or iname in title):
            out.append(item)
    return out


def _norm(s: str) -> str:
    for ch in "™®©· 　":
        s = s.replace(ch, "")
    return s.strip()


# ------------------------------------------------------------------ 主流程
def build(
    menu: list[CartItem],
    coupons: list[Coupon],
    oracle,
    budget: Budget,
    now: datetime | None = None,
    max_probes: int = 24,
) -> dict:
    """生成黄历。

    `max_probes` 限制最多试算多少个（商品, 券）组合，防止券多时把预算打爆。
    返回 dict：period / tip / readings / expiry / probes（实际调用次数）。
    """
    now = now or datetime.now()
    name, tip = period_of(now)

    base_cache: dict[str, int] = {}  # 商品裸买价按编码缓存，同商品只问一次
    probes = 0
    positives: list[Reading] = []
    traps: list[Reading] = []  # 挂了反而更贵的，这是最值钱的洞见
    covered: set[str] = set()  # 真正省到钱的（挂上反而更贵的也算进去了）
    probed_codes: set[str] = set()  # 挂上券算过价的（省没省到都算）

    for ci, coupon in enumerate(coupons):
        if budget.remaining <= 1 or probes >= max_probes:
            break
        targets = _match_products(menu, coupon)
        for item in targets[:2]:  # 同一张券最多试两个商品，够用了
            if budget.remaining <= 1 or probes >= max_probes:
                break
            code = item.product_code
            if code not in base_cache:
                base = _call(oracle, budget, [item], Assignment(mapping={}, with_order=None))
                probes += 1
                if base is None:
                    continue
                base_cache[code] = base.price_fen
            base_fen = base_cache[code]

            used = _call(oracle, budget, [item], Assignment(mapping={0: ci}, with_order=None))
            probes += 1
            if used is None:
                continue

            saved = base_fen - used.price_fen
            # 只要这张券真的挂上去算过价，这件商品就是「试过」，
            # 哪怕一分没省。下面「忌：没券可挂的贵价单品」用的是这个集合 ——
            # 用错成「省到钱的」集合，会把一件明明有适用券、只是没减动的东西
            # 说成「没有它能用的券」——把「减不动」说成「没有」。
            probed_codes.add(code)
            note = expiry_note(coupon.valid_until, now)
            reading = Reading(
                kind="宜" if saved > 0 else ("忌" if saved < 0 else "平"),
                product_name=item.name or item.product_code,
                coupon_title=coupon.title,
                pay_fen=used.price_fen,
                base_fen=base_fen,
                saved_fen=saved,
                note=note,
            )
            if saved > 0:
                positives.append(reading)
                covered.add(code)
            elif saved < 0:
                # 挂了反而更贵：随单购把卡费算进去了，用户自己看不出来
                # 不能写「多半是……」。自己都拿不准的措辞，读者只会更不信。
                # 挂券变贵的机制是确定的：这张券没到门槛，券本身不生效，
                # 但"挂券"这个动作会把这件商品从原本更划算的默认折扣里摘出去。
                # 控制台 80 列，一句话塞满会到 89 列直接折行 —— 折行位置不在
                # 句读上，读起来更费劲。所以显式拆成「结果 / 原因」两行。
                reading.note = (
                    f"挂券反而贵 ¥{abs(saved) / 100:.2f}\n"
                    "      这张券没到门槛、不会生效，却把原本的默认折扣顶掉了"
                )
                traps.append(reading)
                covered.add(code)

    positives.sort(key=lambda r: -r.saved_fen)
    traps.sort(key=lambda r: r.saved_fen)

    # 同一件商品可能被多张券命中（巨无霸既能挂立减 5 元也能挂汉堡 -4 元）。
    # 黄历讲的是"今天该吃什么"，不是券清单 —— 一件商品只留最省的那条。
    seen: set[str] = set()
    uniq: list[Reading] = []
    for r in positives:
        if r.product_name in seen:
            continue
        seen.add(r.product_name)
        uniq.append(r)
    positives = uniq

    # 忌的第二类：贵而没有券可挂的单品 —— 今天点它最亏。
    # 但只有在「至少有一张券真的生效过」时才列它：
    # 一家门店一张券都挂不上时，列出"忌吃什么"是拿原价说事，属于编。
    # 条件价陷阱：菜单上那个诱人的低价其实是「随单购麦金卡」价。
    # 这是真实数据（unit_price vs list_price），一次算价都不用打，
    # 却是菜单最容易坑人的地方 —— 黄历里最值钱的一条"忌"。
    condition_traps = [
        i
        for i in menu
        if 0 < i.unit_price_fen < i.list_price_fen
    ]
    condition_traps.sort(key=lambda i: -(i.list_price_fen - i.unit_price_fen))

    cond_codes = {i.product_code for i in condition_traps[:3]}
    orphans: list[CartItem] = []
    if positives or traps:
        orphans = [
            i
            for i in menu
            if i.product_code not in probed_codes
            and i.product_code not in cond_codes  # 已作为条件价陷阱列出过，别重复
            and i.list_price_fen > 0
        ]
        orphans.sort(key=lambda i: -i.list_price_fen)

    readings = list(positives)
    for r in traps[:3]:
        readings.append(r)
    for i in condition_traps[:3]:
        gap = i.list_price_fen - i.unit_price_fen
        cond = i.discount_type or "条件价"
        readings.append(
            Reading(
                kind="忌",
                product_name=i.name or i.product_code,
                pay_fen=i.unit_price_fen,
                base_fen=i.list_price_fen,
                saved_fen=-gap,
                note=(
                    f"标的 ¥{i.unit_price_fen / 100:.2f} 是「{cond}」价\n"
                    f"      不满足条件要按 ¥{i.list_price_fen / 100:.2f} 结账，"
                    f"差 ¥{gap / 100:.2f}"
                ),
            )
        )
    # 无券可挂的贵价单品只在「确实有券生效过」时才列 —— 一家店一张券都挂不上时，
    # 拿"它没券所以别点"当忌，等于拿原价说事，那是编。
    for i in (orphans[:2] if (positives or traps) else []):
        readings.append(
            Reading(
                kind="忌",
                product_name=i.name or i.product_code,
                base_fen=i.list_price_fen,
                pay_fen=i.list_price_fen,
                saved_fen=0,
                note="这张券池里没有它能用的券，今天点它就是原价",
            )
        )

    expiry = []
    for c in coupons:
        note = expiry_note(c.valid_until, now)
        if note in ("今日到期，不用就作废", "明天到期"):
            expiry.append({"title": c.title, "note": note})

    return {
        "period": name,
        "tip": tip,
        "readings": readings,
        "expiry": expiry,
        "probes": probes,
        "now": now,
        # 是否真的有券生效过。没有的话黄历只剩菜单价格陷阱，
        # 必须如实说明，不能让人以为"今天的宜忌就这些"。
        "coupon_signal": bool(positives or traps),
    }


def _call(oracle, budget: Budget, items: list[CartItem], assignment: Assignment):
    """带预算的算价。预算不足或**业务性**算不出都返回 None —— 黄历是锦上添花，
    某一条算不出来不该让整张黄历崩掉。

    但**环境故障必须穿透**（McpFatalError）：Token 失效 / 限流 / 断网时，
    每一条都会返回 None，build() 最终交出一份 `readings=0` 的黄历，
    屏幕上印的是「这家门店当前没有可挂载的券」。用户会照着这句提示换店、
    换时段反复重跑，永远查不出真因 —— 把环境问题说成"这家店没券"，
    比直接崩掉更坏。
    """
    if budget.remaining <= 0:
        return None
    try:
        price = oracle(items, assignment)
    except McpFatalError:
        raise
    except Exception:
        return None
    budget.spend(1)
    return price if isinstance(price, PriceResult) else None


# ------------------------------------------------------------------ 渲染
def render(day: dict, butler_line: str = "") -> str:
    """把黄历渲染成文本。"""
    now: datetime = day["now"]
    week = "一二三四五六日"[now.weekday()]
    lines = [
        "",
        f"  麦门黄历 · {now:%Y-%m-%d} 周{week} · {day['period']}",
        f"  {day['tip']}",
        "",
    ]
    if not day["readings"]:
        lines.append("  今天算不出宜忌 —— 这家门店当前没有可挂载的券。")
        lines.append("  小的不编。换家门店或换个时间再来。")
        if butler_line:
            lines += ["", f"  {butler_line}"]
        return "\n".join(lines)
    if not day.get("coupon_signal"):
        lines.append("  ⚠️ 这家门店当前一张券都没挂上，下面只有菜单本身的价格陷阱，")
        lines.append("     不是完整的宜忌。想看全部券和到期日：`python -m src.cli assets`")
        lines.append("")

    for r in day["readings"]:
        head = f"  {r.kind} · {r.product_name}"
        lines.append(head)
        if r.kind == "宜":
            lines.append(
                f"      挂「{r.coupon_title}」→ 到手 ¥{r.pay_fen / 100:.2f}"
                f"（省 ¥{r.saved_fen / 100:.2f}，力度 {r.rate * 100:.0f}%）"
            )
        elif r.kind == "忌" and r.saved_fen < 0:
            # 条件价陷阱没有券名，别输出一个空的「」
            prefix = f"「{r.coupon_title}」" if r.coupon_title else ""
            lines.append(f"      {prefix}{r.note}")
        else:
            lines.append(f"      ¥{r.base_fen / 100:.2f}　{r.note}")
        if r.note and r.kind == "宜":
            lines.append(f"      ⚠️ {r.note}")
        lines.append("")

    if day["expiry"]:
        total = "、".join(f"{e['title']}" for e in day["expiry"])
        lines.append(f"  冲 · {len(day['expiry'])} 张券临期：{total}")
        lines.append("")

    if butler_line:
        lines.append(f"  {butler_line}")
    return "\n".join(lines)
