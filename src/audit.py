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

from .coupon_engine import (
    Assignment,
    Budget,
    BudgetExhausted,
    CartItem,
    Coupon,
    PriceResult,
    to_fen,
)
from .mcd_client import McpFatalError

# 除了 McpFatalError，这些也按「环境/代码坏了」处理 —— 它们几乎不可能
# 是麦当劳的业务答案，而是我们自己踩到了 bug 或本机出了问题。
_FATAL_TYPES = (TypeError, AttributeError, KeyError, IndexError, OSError)


def _is_fatal(exc: BaseException) -> bool:
    """这个错是「环境/代码坏了」还是「这件商品用不上这张券」。

    必须分清楚，因为两种错的处置完全相反：

    - **环境坏了**（Token 失效、断网、限流、返回体变了）：
      继续跑下去只会得到一份写满「没查出来」的假报告，必须立刻抛出去，
      让用户去换 Token / 调预算，而不是让他以为自己的券都是废纸。
    - **业务性拒绝**（实测 `calculate-price` 会回「促销规则不支持该商品」）：
      这是这张券跟这几件商品不匹配的**正常答案**，换一件商品照样能查，
      为它放弃整张券是误伤 —— 一张「麦咖啡」券挂到汉堡上就是这个错。

    判定**只看类型，不看错误文案**。早先这里用正则匹配「401 / token /
    鉴权 / 授权 / 超时 / 限流」这些关键词，实测 15 条真实文案错 12 条：
    「该优惠券仅限授权门店使用」「订单超时未支付」是业务拒绝却被判死，
    「502 Bad Gateway」「Connection reset」「登录已失效」是环境故障却被放过。
    自然语言猜不出错误的性质 —— 那是抛错的地方才知道的事，
    所以改由 `McpFatalError` 这个类型在 raise 处显式标注。
    """
    return isinstance(exc, (McpFatalError, *_FATAL_TYPES))

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


# 券面写的「满 X」门槛。读不出来返回 0（表示「券面没写」）。
# 两种写法都要认：「满 20 元」「满 20 减 3 元」。
# 但单位必须锁死在「元」或「减」上 —— 「满 2 件减 5 元」是件数门槛，
# 拿 2 当金额会报出一个 ¥2.00 的假门槛。
_THRESHOLD_RES = (
    re.compile(r"满\s*(\d+(?:\.\d+)?)\s*元"),
    re.compile(r"满\s*(\d+(?:\.\d+)?)\s*减"),
)


def threshold_on_face(title: str) -> int:
    """从券标题读门槛（分）。读不出返回 0 —— 0 表示「券面没写」，不是「无门槛」。"""
    for rx in _THRESHOLD_RES:
        m = rx.search(title or "")
        if m:
            return to_fen(m.group(1))
    return 0


def face_value_fen(title: str) -> int:
    """从券标题读面额（分）。

    像「9.9元中杯冰美式」这种**没有减字**的不会被误读成面额，
    因为那 ¥9.9 是售价不是优惠额 —— 把售价当面额，会凭空造出一张假废券。
    """
    m = _FACE_RE.search(title or "")
    if not m:
        return 0
    # 必须用项目统一的 to_fen（Decimal + ROUND_HALF_UP），不能自己
    # `int(round(float(x) * 100))` —— 后者是 banker's rounding，
    # 0.125 元会算成 12 分而不是 13 分，跟别处对不上。
    return to_fen(m.group(1))


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
    probed: int = 0           # 真正算出价的商品件数（不是候选件数）
    attempted: int = 0        # 本来打算试几件
    works_from_fen: int = 0   # 试出来的「生效价」下界
    blocked_fen: int = 0      # 试出来的「不生效价」上界（比 works_from 便宜）
    calls: int = 0
    partial: bool = False     # 有商品算价报错、结论可能偏保守
    note: str = ""

    @property
    def gap_fen(self) -> int:
        """面额与真实价值的落差。正数＝这张券在吹牛。"""
        if self.face_fen <= 0:
            return 0
        return self.face_fen - self.value_fen

    def threshold_note(self) -> str:
        """说清这张券的门槛。

        券面写得明白就**直接用券面上的数字**：标题里「满 20 减 3 元」
        已经把门槛印出来了，再花两次算价去"试"出一个更粗的区间
        （¥10 不生效、¥25 才生效 → 门槛在中间），是自嗨 ——
        用户拿到的是一个比券面更模糊的答案，还白等两次接口。

        只有券面上读不出门槛时，才把试出来的区间报出来，
        并且明确标注这是试出来的、不是券面写的。
        """
        wrote = threshold_on_face(self.coupon.title)
        if wrote:
            return f"门槛：满 ¥{yuan(wrote)}（券面写的）"
        if self.works_from_fen <= 0 or self.blocked_fen <= 0:
            return ""
        if self.blocked_fen >= self.works_from_fen:
            return ""
        return (
            f"券面没写门槛，小的试出来：¥{yuan(self.blocked_fen)} 上不生效、"
            f"¥{yuan(self.works_from_fen)} 上才生效 —— 门槛在这两者之间"
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
    # 去重键要带价格：同一个 productCode 若有不同规格，得各试一件；
    # 但完全相同的条目只算一件，否则报告会虚报「试了 N 件」。
    uniq: list[CartItem] = []
    seen: set[tuple] = set()
    for it in hits:
        k = (it.product_code, it.effective_price_fen())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    # probe<=0 是「一件都别试」，直接返回空，不能掉进下面的采样 ——
    # `[:0]` 得到空表之后 `picks[-1] = ...` 会 IndexError。
    if probe <= 0:
        return []
    if len(uniq) <= probe:
        return uniq
    # probe 必须真的生效（早先写了 picks=[首,中,尾] 三件，probe 形同虚设）；
    # 且**最贵那件一定要试** —— 满减券只在贵的那件上生效，
    # 只试便宜的会把一张好券误判成废券。
    step = max(1, (len(uniq) - 1) // max(probe - 1, 1))
    picks = [uniq[i] for i in range(0, len(uniq), step)][:probe]
    if uniq[-1] not in picks:
        picks[-1] = uniq[-1]
    return picks


def _opt_sig(opt: Any) -> str:
    """随单购档位的稳定标识（缓存键用）。"""
    sig = getattr(opt, "signature", None)
    return sig() if callable(sig) else ""


def _probe(
    oracle: Callable[[list[CartItem], Assignment], PriceResult],
    cart: list[CartItem],
    options: Sequence[Any],
    coupon_idx: int,
    budget: Budget | None,
    counter: list[int],
    base_cache: dict[tuple, int],
    key: tuple,
    errors: list[BaseException],
) -> tuple[tuple[int, int, int, Any] | None, bool]:
    """在**同一个随单购档位**下比「挂券 vs 不挂券」，返回差价最大的那一档。

    为什么必须同档位比，不能两边各自取最优档：
    券和随单购可能互斥（实测：麦金卡随单购不能与部分券同享）。
    若 baseline 取"最优档"、挂券价也取"最优档"，两边都会落到麦金卡那一档、
    券被作废，差值恒为 0 —— 于是一张本来能省 ¥4 的券被判成废券。

    而真相是两件不同的事，必须分开说：
      · 这张券**本身**能减 ¥4（不随单购时）
      · 但它配上麦金卡后不再额外省钱，因为麦金卡那一档更便宜
    混成一个"废券"，用户会以为这张券是假的；其实只是有更优的选择。

    返回 (最大值结果, 是否被预算截断, 所有档位里不挂券的最低价)。
    """
    best: tuple[int, int, int, Any] | None = None
    truncated = False
    min_base_any: int | None = None
    for opt in options:
        if budget is not None and budget.remaining <= 0:
            truncated = True
            break
        ck = (key, _opt_sig(opt))

        base = base_cache.get(ck)
        if base is None:
            try:
                base = oracle(
                    cart, Assignment(mapping={}, with_order=opt)
                ).price_fen
            except Exception as exc:  # noqa: BLE001 - 见下方「一次都没成功才抛」
                errors.append(exc)
                continue
            counter[0] += 1
            if budget is not None:
                budget.spend()
            base_cache[ck] = base

        if budget is not None and budget.remaining <= 0:
            truncated = True
            break
        try:
            with_c = oracle(
                cart, Assignment(mapping={0: coupon_idx}, with_order=opt)
            ).price_fen
        except Exception as exc:  # noqa: BLE001 - 见下方「一次都没成功才抛」
            # 单个候选失败只跳过它：实测券挂到不适配的商品上会返回
            # 「促销规则不支持该商品」，换一件商品照样能查，不该整张券放弃。
            errors.append(exc)
            continue
        counter[0] += 1
        if budget is not None:
            budget.spend()

        gap = base - with_c
        if best is None or gap > best[0]:
            best = (gap, base, with_c, opt)
        # 顺手记录「这件商品在所有档位里不挂券能做到多低」。
        # 只有拿到这个数，才能回答「券配上随单购后还额外省钱吗」——
        # 之前拿别的商品的 base 去凑这个最小值，跨商品比价，
        # 结果是一家店压根没有随单购时，一张真省 ¥3 的券被说成
        # 「配上随单购后不再额外省钱」。
        if min_base_any is None or base < min_base_any:
            min_base_any = base
    return best, truncated, min_base_any


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
    if budget is not None and budget.total <= 0:
        # 预算为 0 时不该输出一份「没有可体检的券」——那是把配置错误
        # 伪装成业务结论。直接说清楚要调大 --budget。
        raise BudgetExhausted(
            "算价预算为 0，一张券都查不了；把 --budget 调大（建议 ≥ 40）"
        )

    counter = [0]
    base_cache: dict[tuple, int] = {}
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
            attempted=len(cands),
        )
        if is_universal:
            v.note = f"通用券，只在 {len(cands)} 件代表商品上试"

        before = counter[0]
        probed_ok = 0
        item_failed = 0
        truncated_any = False
        # 「配上随单购后还额外省钱吗」要拿**最值钱那件商品自己**的
        # 各档位最低价来比。三个理由：
        #   1. 跨商品取全局最小值是错的（拿一件便宜货的价格去否定另一件商品上
        #      真能省 ¥3 的券，等于答非所问：用户问的是这张券值多少钱）。
        #   2. 拿「最优档位的 base」也不够：券和随单购可能互斥，
        #      随单购那一档不挂券可能反而更便宜。
        #   3. 这个判据已经蕴含了「这家店得真有随单购档位」——
        #      只有一个档位时 min_base_any 就等于那个档位的 base，
        #      而 best_price_fen = base - value < base，条件天然不成立。
        #      所以不需要再单独判一次 has_card_opt（那会是永远为真的死条件）。
        best_min_base: int | None = None
        for item in cands:
            if budget is not None and budget.remaining <= 0:
                truncated_any = True
                break
            cart = [item]
            try:
                options = list(with_order_options(cart)) or [None]
            except Exception as exc:  # noqa: BLE001 - 档位构造失败不该拖垮整张券
                errors.append(exc)
                options = [None]

            # 缓存键必须带上价格：同一个 productCode 若有不同规格/价格，
            # 只按 code 缓存会串味（实测会把好券判成废券）。
            key = (item.product_code, item.effective_price_fen())
            best, truncated, min_base_any = _probe(
                oracle, cart, options, orig_idx, budget, counter, base_cache, key, errors
            )
            if truncated:
                truncated_any = True
            if best is None:
                # 这件商品在所有随单购档位上都没算出价 —— 记的是「件」
                # 不是「次」：一件商品跨 3 个档位失败会落 3 个 error，
                # 拿 error 数去当件数会打出「6/3 件商品报错」这种鬼话。
                item_failed += 1
                continue
            probed_ok += 1

            value, base_fen, with_fen, _opt = best

            price = item.effective_price_fen()
            if value > 0:
                if v.works_from_fen == 0 or price < v.works_from_fen:
                    v.works_from_fen = price
            elif price > v.blocked_fen:
                v.blocked_fen = price

            if value > v.value_fen:
                v.value_fen = value
                v.best_item = item
                v.baseline_fen = base_fen
                v.best_price_fen = with_fen
                # 记下**这一件**商品在各档位里不挂券的最低价。
                # 不能拿别的商品的价来凑：那件商品可能根本不是你想吃的，
                # 用它当基准等于说「你别吃 A 改吃 B，所以这张券没用」——
                # 可用户问的是「这张券值多少钱」，不是「今天最便宜能花多少」。
                best_min_base = min_base_any

        v.calls = counter[0] - before
        # probed 必须是**成功算出价**的件数，不能是候选件数：
        # 候选 3 件里只成功 1 件却对外说「试了 3 件」，是虚报。
        v.probed = probed_ok
        failed = item_failed

        if probed_ok == 0:
            v.status = UNKNOWN
            if failed and not truncated_any:
                v.note = f"{failed} 件商品算价报错，这张券没查出来"
            else:
                v.note = (
                    "预算不够，这张券没查完（调大 --budget）"
                    if truncated_any
                    else "算价接口这件商品没返回价格"
                )
        elif v.value_fen >= min_worth_fen:
            v.status = WORTH
            # 券本身有效力，但搭上随单购后不再额外省钱 —— 这两件事必须分开说，
            # 否则用户会以为券是假的，其实是随单购那一档更便宜。
            if best_min_base is not None and v.best_price_fen >= best_min_base:
                v.note = "配上随单购后它不再额外省钱（随单购那一档本身就更低）"
        elif v.value_fen > 0:
            # 减了但没到"值得为它凑单"的程度。
            # 早先这里一律打印「一分没减」，那是事实错误 —— 它明明减了 ¥0.25。
            v.status = DUD
            v.note = f"只省 ¥{yuan(v.value_fen)}，不值得专门为它凑单"
        else:
            v.status = DUD

        # 有几件报错、但别的算出来了 —— 这是线上的真实形态，不能装作没发生：
        # 报错的那几件里可能正有关卡价商品，那会让这张券的价值被低估。
        if failed and probed_ok:
            v.partial = True
            v.note = (
                (v.note + "；" if v.note else "")
                + f"{failed}/{len(cands)} 件商品算价报错，这个数可能偏保守"
            )
        verdicts.append(v)

    # 环境级故障绝不能伪装成业务结论。
    #
    # 判据不能只看「一次都没成功」（早先就是这么写的）：
    # Token 跑到一半失效时前半段是成功的，那样照样输出一份看似正常、
    # 实则残缺的报告，用户会以为自己的券都是废纸。
    # 所以只要出现过一条**环境级**错误就抛 —— 业务性拒绝不算，
    # 那是「这张券跟这件商品不匹配」的正常答案。
    fatal = [e for e in errors if _is_fatal(e)]
    if errors and (counter[0] == 0 or fatal):
        raise fatal[0] if fatal else errors[-1]

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
    lines.append(f"  {head}")
    lines.append("")

    scope = f"在售 {menu_count} 件 · 券池 {total_coupons or len(verdicts)} 张"
    lines.append(f"  {scope} · 本次体检 {len(verdicts)} 张")
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

    # 分母必须包含 UNKNOWN：漏掉它时「1 张能省 + 4 张没查出来」会算出
    # 健康度 100%，那是拿未知当好消息，比不报更坏。
    counted = worth + dud + dead + unknown
    ratio = (worth / counted) if counted else 0.0
    suffix = f"（{unknown} 张没查出来，按未通过计入）" if unknown else ""
    lines.append(f"  健康度 {_bar(ratio)} {ratio * 100:.0f}%{suffix}")
    # 分母是什么必须写出来。屏幕上只有一个百分比时，用户只能去猜
    # 「60% 到底是在说谁」——而「这家店压根不卖你这张券能挂的东西」也算它
    # 不健康，这一点没人猜得到。
    lines.append(
        f"  （健康度 = 真能省钱的 {worth} 张 ÷ 体检到的 {counted} 张；"
        f"挂了白挂和在这家店无处可挂都算不健康）"
    )
    if counted and counted < 3:
        # 券池只有 1~2 张时，健康度不是"你的券很健康"，而是"样本太少"。
        # 把接口现状讲明白，它就成了洞察而不是缺陷。
        lines.append(f"  ⚠️ 只体检到 {counted} 张券，样本太小，健康度仅供参考")
    lines.append("")

    # ── 最值钱的几张
    good = sorted(
        [v for v in verdicts if v.status == WORTH], key=lambda v: -v.value_fen
    )[:3]
    if good:
        lines.extend(["", "  ── 最值钱的券"])
        for v in good:
            lines.append(f"  ¥{yuan(v.value_fen)}  {v.coupon.title}")
            if v.best_item is not None:
                lines.append(
                    f"        挂「{v.best_item.name}」"
                    f" ¥{yuan(v.baseline_fen)} → ¥{yuan(v.best_price_fen)}"
                )
        lines.append("")

    # ── 门槛：券面写了就直接念券面，券面没写才把试出来的区间报出来。
    # 标题里「满 20 减 3 元」已经把门槛印在脸上了，再花两次算价去反推一个
    # 更粗的区间（¥10 不生效、¥25 才生效）纯属自嗨 —— 用户拿到的是
    # 一个比券面更模糊的答案，还白等两次接口。
    thresholds = [v for v in verdicts if v.threshold_note()]
    if thresholds:
        lines.extend(["", "  ── 门槛"])
        for v in thresholds:
            lines.append(f"  · {v.coupon.title}\n      {v.threshold_note()}")
        lines.append("")

    # ── 面额 vs 真实价值：这条最容易让人「哦原来如此」
    boasters = [
        v
        for v in verdicts
        if v.status == WORTH and v.face_fen > 0 and v.gap_fen >= 100
    ]
    if boasters:
        lines.extend(["", "  ── 券面额 ≠ 券价值"])
        for v in sorted(boasters, key=lambda v: -v.gap_fen):
            lines.append(
                f"  ! {v.coupon.title}　标着减 ¥{yuan(v.face_fen)}，"
                # 「实测」在**离线演示**里是假的（一个请求都没发过），
                # 而这同一段渲染代码 demo 也在用。改成「实际」，
                # 真跑是算价接口实际算出来的，演示是演示数据上算出来的，都成立。
                f"实际只省 ¥{yuan(v.value_fen)}"
            )
        lines.append("")

    # ── 废券
    duds = [v for v in verdicts if v.status == DUD]
    if duds:
        lines.extend(["", "  ── 看着挺猛，其实没用"])
        for v in duds:
            face = f"（标着减 ¥{yuan(v.face_fen)}）" if v.face_fen > 0 else ""
            lines.append(f"  × {v.coupon.title}{face}")
            th = v.threshold_note()
            if v.value_fen > 0:
                # 减了但不多：不能说「一分没减」，那是事实错误
                lines.append(
                    f"    试了 {v.probed} 件适用商品，最多只省 ¥{yuan(v.value_fen)}"
                    + (f"；{th}" if th else "")
                )
            elif th:
                lines.append(f"    {th}，便宜的东西够不到")
            elif v.probed:
                lines.append(f"    试了 {v.probed} 件适用商品，接口一分没减")
            if v.note:
                lines.append(f"    {v.note}")
        lines.append("")

    # ── 死券
    deads = [v for v in verdicts if v.status == DEAD]
    if deads:
        lines.extend(["", "  ── 这家店用不上"])
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
        lines.extend(["", "  ── 快过期"])
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
