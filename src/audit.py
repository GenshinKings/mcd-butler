"""券包体检 —— 回答一个从来没人回答过的问题：**你手里这张券，到底值多少钱**。

为什么必须有这个功能
--------------------
麦当劳的券只印「减 5 元」，它不会告诉你另外三件事，
而这三件事恰恰决定了这张券是钱还是废纸：

1. **它在这家店到底减不减得动。**
   实测发现：商品一旦走了「随单购麦金卡」价，价格已经被压到券面价附近，
   这时候再挂券，接口一分钱都不减。券还在，钱没了。

2. **它的门槛藏在哪。**
   「满 20 减 1」你拿一件 ¥10 的东西去挂，接口不会报错、不会提示，
   就是不给你减。用户只会觉得「这券怎么没用」，不知道是自己没凑够。

3. **它还有没有东西可挂。**
   券印着「麦咖啡」，而这家店根本不卖咖啡 —— 那它在这儿就是一张死券。

这三点，`query-store-coupons` 一个字都不说，`calculate-price` 也不解释。
**唯一的办法是一件一件试出来**：同一件商品，挂券算一次、不挂券算一次，
差价就是这张券的真实价值。

所以体检报告里每一个数字背后都是真实的算价调用，没有一行是估的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from .coupon_engine import Assignment, Budget, CartItem, Coupon, PriceResult

# 三种判决。注意「废券」和「死券」必须分开：
# 废券是「能挂但没用」，死券是「压根没地方挂」，
# 前者说明这家店的价盘吃掉了你的券，后者说明你走错店了。
WORTH = "worth"    # 真能省钱
DUD = "dud"        # 能挂上去，但一分没省
DEAD = "dead"      # 这家店的在售菜单里没有它能挂的商品
UNKNOWN = "unknown"  # 接口没算出来，不乱下结论

_STATUS_LABEL = {
    WORTH: "真能省钱",
    DUD: "挂了白挂",
    DEAD: "无处可挂",
    UNKNOWN: "没查出来",
}

# 券标题里的「面额」。只认最直白的几种写法，读不出来就当 0 ——
# 宁可不说，也绝不能拿猜出来的数字去打脸。
_FACE_RE = re.compile(r"(?:立减|满减|减|省|-)\s*(\d+(?:\.\d+)?)\s*元")


def face_value_fen(title: str) -> int:
    """从券标题读面额（分）。

    像「9.9元中杯冰美式」这种**没有减字**的不会被误读成面额，
    因为那 ¥9.9 是售价不是优惠额 —— 把售价当面额，会凭空造出一张假废券。
    """
    m = _FACE_RE.search(title or "")
    if not m:
        return 0
    try:
        return int(round(float(m.group(1)) * 100))
    except (TypeError, ValueError):
        return 0


def yuan(fen: int) -> str:
    return f"{fen / 100:.2f}"


@dataclass
class Verdict:
    """一张券的体检结论。"""

    coupon: Coupon
    status: str = DEAD
    value_fen: int = 0        # 真实价值：挂券比不挂券便宜了多少
    face_fen: int = 0         # 券面额（标题里读出来的，读不出是 0）
    best_item: CartItem | None = None
    baseline_fen: int = 0     # 这件商品不挂券时的最优价
    best_price_fen: int = 0   # 挂上这张券之后的最优价
    probed: int = 0           # 试了几件适用商品
    works_from_fen: int = 0   # 试出来的「生效价」下界
    blocked_fen: int = 0      # 试出来的「不生效价」上界（比 works_from 便宜）
    calls: int = 0
    note: str = ""

    @property
    def gap_fen(self) -> int:
        """面额与真实价值的落差。正数＝这张券在吹牛。"""
        if self.face_fen <= 0:
            return 0
        return self.face_fen - self.value_fen

    def threshold_note(self) -> str:
        """把试出来的门槛区间说成人话。

        接口从不告诉你门槛是多少，但**在什么价位生效、在什么价位不生效**
        是可以试出来的：拿便宜的挂一次、拿贵的挂一次，中间就是门槛所在的区间。
        """
        if self.works_from_fen <= 0 or self.blocked_fen <= 0:
            return ""
        if self.blocked_fen >= self.works_from_fen:
            return ""
        return (
            f"¥{yuan(self.blocked_fen)} 的商品上不生效，"
            f"¥{yuan(self.works_from_fen)} 的才生效 —— 门槛大概卡在中间"
        )


def _candidates(menu: Sequence[CartItem], coupon: Coupon, probe: int) -> list[CartItem]:
    """挑几件「这张券能挂」的商品去试。

    为什么要便宜/中间/最贵各来一件，而不是只挑第一件：
    满减券只在贵的那件上生效，只试便宜的那件，
    会把一张好券误判成废券 —— 这是体检最容易犯、也最伤人的错。
    """
    hits = [it for it in menu if coupon.applies_to(it.product_code)]
    if not hits:
        return []
    hits.sort(key=lambda i: i.effective_price_fen())
    if len(hits) <= probe:
        return hits
    picks = [hits[0], hits[len(hits) // 2], hits[-1]]
    out: list[CartItem] = []
    seen: set[str] = set()
    for it in picks:
        if it.product_code in seen:
            continue
        seen.add(it.product_code)
        out.append(it)
    return out


def _best(
    oracle: Callable[[list[CartItem], Assignment], PriceResult],
    cart: list[CartItem],
    options: Sequence[Any],
    mapping: dict[int, int],
    budget: Budget | None,
    counter: list[int],
) -> PriceResult | None:
    """在几个随单购档位里挑最便宜的那个。

    单件商品 + 单张券的组合空间小到可以直接穷举档位，
    不需要爬山 —— 这里求的是**精确值**，不是近似值。
    """
    best: PriceResult | None = None
    for opt in options:
        if budget is not None and budget.remaining <= 0:
            break
        res = oracle(cart, Assignment(mapping=dict(mapping), with_order=opt))
        counter[0] += 1
        if budget is not None:
            budget.spend()
        if best is None or res.price_fen < best.price_fen:
            best = res
    return best


def _sort_key(pair: tuple[int, Coupon]) -> tuple[int, str, int]:
    """快过期的先查。

    预算有限时，先保证「明天就作废」的券有结论；
    一年后才到期的券查不到可以下次再查，代价不对称。
    """
    idx, coupon = pair
    until = (coupon.valid_until or "").strip()
    return (0, until, idx) if until else (1, "", idx)


def audit_coupons(
    oracle: Callable[[list[CartItem], Assignment], PriceResult],
    menu: Sequence[CartItem],
    coupons: Sequence[Coupon],
    with_order_options: Callable[[list[CartItem]], Sequence[Any]],
    budget: Budget | None = None,
    limit: int = 10,
    probe: int = 3,
    min_worth_fen: int = 30,
    universal_probe: int = 3,
) -> list[Verdict]:
    """给券池做体检。

    对每张券：找它能挂的商品 → 挂券算一次、不挂券算一次 → 差价就是它的真实价值。
    `min_worth_fen` 是「算不算真省了钱」的门槛，默认 ¥0.30：
    低于这个数，用户根本感知不到，报成「省钱」是自欺欺人。
    """
    counter = [0]
    base_cache: dict[str, int] = {}
    verdicts: list[Verdict] = []
    errors: list[BaseException] = []

    ordered = sorted(enumerate(coupons), key=_sort_key)

    for orig_idx, coupon in ordered[:limit]:
        if budget is not None and budget.remaining <= 0:
            break

        # 通用券（没声明适用商品）能挂在全菜单上，
        # 一件件试会把预算烧穿，所以只抽几件代表商品，并在结论里说明。
        is_universal = not coupon.product_codes
        cands = _candidates(menu, coupon, universal_probe if is_universal else probe)

        if not cands:
            verdicts.append(
                Verdict(
                    coupon=coupon,
                    status=DEAD,
                    face_fen=face_value_fen(coupon.title),
                    note="这家店的在售菜单里没有它能挂的商品",
                )
            )
            continue

        v = Verdict(
            coupon=coupon,
            status=DUD,
            face_fen=face_value_fen(coupon.title),
            probed=len(cands),
        )
        if is_universal:
            v.note = f"通用券，只在 {len(cands)} 件代表商品上试"

        before = counter[0]
        probed_ok = 0
        for item in cands:
            if budget is not None and budget.remaining <= 0:
                break
            cart = [item]
            options = with_order_options(cart)
            if not options:
                options = [None]

            baseline = base_cache.get(item.product_code)
            if baseline is None:
                try:
                    b = _best(oracle, cart, options, {}, budget, counter)
                except Exception as exc:  # noqa: BLE001 - 见下方「全部失败则抛出」
                    errors.append(exc)
                    continue
                if b is None:
                    continue
                baseline = b.price_fen
                base_cache[item.product_code] = baseline

            try:
                w = _best(oracle, cart, options, {0: orig_idx}, budget, counter)
            except Exception as exc:  # noqa: BLE001 - 见下方「全部失败则抛出」
                # 单次失败只记下来、跳过这张券。
                # 但绝不允许「全都失败」还输出一份全是废券的报告 ——
                # 那是把鉴权失效/断网伪装成了业务结论，比报错坏得多。
                errors.append(exc)
                break
            if w is None:
                continue
            probed_ok += 1

            value = baseline - w.price_fen
            price = item.effective_price_fen()
            if value > 0:
                if v.works_from_fen == 0 or price < v.works_from_fen:
                    v.works_from_fen = price
            else:
                if price > v.blocked_fen:
                    v.blocked_fen = price

            if value > v.value_fen:
                v.value_fen = value
                v.best_item = item
                v.baseline_fen = baseline
                v.best_price_fen = w.price_fen

        v.calls = counter[0] - before

        if probed_ok == 0:
            v.status = UNKNOWN
            v.note = "算价接口这件商品没返回价格"
        elif v.value_fen >= min_worth_fen:
            v.status = WORTH
        else:
            v.status = DUD
        verdicts.append(v)

    # 一次都没算成功、还报错了 → 这是鉴权/网络故障，不能伪装成「全是废券」。
    # 判据必须看「成功调用次数」而不是「是不是全 UNKNOWN」：
    # 券池里本来就可能有死券（status=DEAD），那样永远凑不出「全 UNKNOWN」，
    # 一个 Token 失效就会被静默吞掉。
    if counter[0] == 0 and errors:
        raise errors[0]

    return verdicts


def health_score(verdicts: Sequence[Verdict]) -> tuple[int, int, int]:
    """返回 (真能省钱, 挂了白挂, 无处可挂)。"""
    worth = sum(1 for v in verdicts if v.status == WORTH)
    dud = sum(1 for v in verdicts if v.status == DUD)
    dead = sum(1 for v in verdicts if v.status == DEAD)
    return worth, dud, dead


def _bar(ratio: float, width: int = 20) -> str:
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _due_note(coupon: Coupon, now: datetime | None) -> tuple[int, str]:
    """距离到期还有几天。返回 (天数, 文案)；解析不出来返回 (99, "")。"""
    until = (coupon.valid_until or "").strip()
    if not until:
        return 99, ""
    try:
        end = datetime.fromisoformat(until[:10])
    except ValueError:
        return 99, ""
    base = now or datetime.now()
    days = (end.date() - base.date()).days
    if days < 0:
        return days, "已过期"
    if days == 0:
        return 0, "今天到期"
    if days == 1:
        return 1, "明天到期"
    if days <= 3:
        return days, f"{days} 天后到期"
    return days, ""


def render(
    verdicts: Sequence[Verdict],
    store_name: str = "",
    menu_count: int = 0,
    calls: int = 0,
    total_coupons: int = 0,
    now: datetime | None = None,
) -> str:
    """把体检结论渲染成能直接截图发出去的样子。"""
    lines: list[str] = []
    head = "券包体检"
    if store_name:
        head += f" · {store_name}"
    lines.append("═" * 58)
    lines.append(f"  {head}")
    lines.append("═" * 58)

    scope = f"在售 {menu_count} 件 · 券池 {total_coupons or len(verdicts)} 张"
    lines.append(f"  {scope} · 本次体检 {len(verdicts)} 张（真打 {calls} 次算价接口）")
    lines.append("")

    worth, dud, dead = health_score(verdicts)
    unknown = sum(1 for v in verdicts if v.status == UNKNOWN)
    lines.append(f"  真能省钱   {worth} 张")
    lines.append(f"  挂了白挂   {dud} 张")
    if dead:
        lines.append(f"  无处可挂   {dead} 张")
    if unknown:
        lines.append(f"  没查出来   {unknown} 张")
    lines.append("")

    counted = worth + dud + dead
    ratio = (worth / counted) if counted else 0.0
    lines.append(f"  健康度 {_bar(ratio)} {ratio * 100:.0f}%")
    lines.append("")

    # ── 最值钱的几张
    good = sorted(
        [v for v in verdicts if v.status == WORTH], key=lambda v: -v.value_fen
    )[:3]
    if good:
        lines.append("── 最值钱的券 " + "─" * 44)
        for v in good:
            lines.append(f"  ¥{yuan(v.value_fen)}  {v.coupon.title}")
            if v.best_item is not None:
                lines.append(
                    f"        挂「{v.best_item.name}」"
                    f" ¥{yuan(v.baseline_fen)} → ¥{yuan(v.best_price_fen)}"
                )
        lines.append("")

    # ── 试出来的门槛：接口从不说门槛在哪，只能靠「便宜的挂不上、贵的挂得上」反推
    thresholds = [v for v in verdicts if v.threshold_note()]
    if thresholds:
        lines.append("── 试出来的门槛 " + "─" * 43)
        for v in thresholds:
            lines.append(f"  ? {v.coupon.title}　{v.threshold_note()}")
        lines.append("")

    # ── 面额 vs 真实价值：这条最容易让人「哦原来如此」
    boasters = [
        v
        for v in verdicts
        if v.status == WORTH and v.face_fen > 0 and v.gap_fen >= 100
    ]
    if boasters:
        lines.append("── 券面额 ≠ 券价值 " + "─" * 40)
        for v in sorted(boasters, key=lambda v: -v.gap_fen):
            lines.append(
                f"  ! {v.coupon.title}　标着减 ¥{yuan(v.face_fen)}，"
                f"实测只省 ¥{yuan(v.value_fen)}"
            )
        lines.append("")

    # ── 废券
    duds = [v for v in verdicts if v.status == DUD]
    if duds:
        lines.append("── 看着挺猛，其实没用 " + "─" * 37)
        for v in duds:
            face = f"（标着减 ¥{yuan(v.face_fen)}）" if v.face_fen > 0 else ""
            lines.append(f"  × {v.coupon.title}{face}")
            th = v.threshold_note()
            if th:
                lines.append(f"    {th}，便宜的东西够不到")
            elif v.probed:
                lines.append(f"    试了 {v.probed} 件适用商品，接口一分没减")
        lines.append("")

    # ── 死券
    deads = [v for v in verdicts if v.status == DEAD]
    if deads:
        lines.append("── 这家店用不上 " + "─" * 42)
        for v in deads:
            lines.append(f"  · {v.coupon.title}")
            if v.note:
                lines.append(f"    {v.note}")
        lines.append("")

    # ── 快过期：比省几块钱更急
    due: list[tuple[int, Verdict]] = []
    for v in verdicts:
        if v.status == DEAD:
            continue
        days, label = _due_note(v.coupon, now)
        if label:
            due.append((days, v))
    if due:
        lines.append("── 快过期 " + "─" * 48)
        for days, v in sorted(due, key=lambda x: x[0]):
            _, label = _due_note(v.coupon, now)
            worth_txt = (
                f"　还能省 ¥{yuan(v.value_fen)}"
                if v.status == WORTH
                else "　但这张在这家店省不了钱"
            )
            lines.append(f"  ⏳ {label}　{v.coupon.title}{worth_txt}")
        lines.append("")

    if not verdicts:
        lines.append("  （没有可体检的券）")
    return "\n".join(lines)
