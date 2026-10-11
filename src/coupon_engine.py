"""券价最优组合引擎 —— 麦门管家的核心。

问题建模
--------
麦当劳的 `calculate-price` 允许 items[] 中**每个商品各自携带一张券**，
所以「怎么用券最划算」不是「挑一张最猛的券」，而是一个
**券 → 购物车商品 的二分匹配问题**：

    约束 1：一张券最多用一次
    约束 2：一个商品最多挂一张券
    约束 3：券只能挂到它 products[] 里声明过的商品上

难点在于：接口**不返回券的折扣金额**（只有标题和适用商品），
因此无法离线估算收益，只能把 `calculate-price` 当作黑盒求值器。

求解策略
--------
因为目标函数是黑盒、且每次求值要走网络（限流 600 次/分钟），
这里用「贪心构造 + 局部搜索（爬山）」而不是穷举：

    1. 贪心：按「可挂商品数」升序安排券（最受约束的先占坑）
    2. 爬山：交替执行 删除 / 新增 / 换券 / 挪动 / 对调 五种邻域动作，
       每步都用真实算价接口验证，接受任何变便宜的改动
    3. 预算耗尽或收敛即停

这样既能在有限调用次数内逼近最优，又不会依赖未在文档中承诺的字段。

合规：`calculate-price` 只算价，不产生任何订单。
"""

from __future__ import annotations

import itertools
import json
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence


def to_fen(value: Any) -> int:
    """任意金额写法 → 分（整数）。

    为什么不能用 `int(float(x) * 100)`：
    二进制浮点表示不了十进制小数，`33.30 * 100` 实际是 `3329.9999...`，
    直接截断就变成 3329 分 —— 每件商品少一分，一整单就差出好几毛，
    而且这种错误极难发现，因为它只在特定数字上出现。

    `Decimal` 按十进制算，再四舍五入，才是金额该有的处理方式。
    """
    if value is None or value == "":
        return 0
    try:
        return int((Decimal(str(value).strip()) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return 0


def as_fen(value: Any) -> int:
    """接口**已经是「分」**的字段 → 整数分。

    和 `to_fen` 的区别必须说清楚，混用会差 100 倍：
        `to_fen`  元 → 分（乘 100），用于菜单价（实测 `currentPrice` 是元字符串）
        `as_fen`  分 → 分（不乘），用于算价结果（实测 `price` 是分整数）

    两者都要容忍脏数据：同一个字段在不同工具里可能返回
    `5700` / `"5700"` / `None` / `""`，任何一种都不该让整条链路崩掉。
    """
    if value is None or value == "":
        return 0
    try:
        return int(
            Decimal(str(value).strip()).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return 0


@dataclass(frozen=True)
class CartItem:
    """购物车条目。

    注意 `unit_price_fen`（菜单标价）**可能是条件价**：
    实测发现四件套一类商品的 `currentPrice` 标 29 元，
    而 `discountType` 是「随单购麦金卡优惠」，`originalPrice` 是 57 元 ——
    不随单购麦金卡就只能按 57 元结算。
    因此必须同时保留 `list_price_fen`（无门槛价），
    否则展示出来的价格会骗人。
    """

    product_code: str
    name: str = ""
    quantity: int = 1
    unit_price_fen: int = 0  # 菜单标价（可能是条件价）
    list_price_fen: int = 0  # 无门槛价（originalPrice）
    discount_type: str = ""
    needs_with_order: bool = False

    # 随单购所需参数（来自 query-meals 的 withOrder 字段）
    membership_code: str = ""
    membership_spec_id: str = ""

    def base_total_fen(self) -> int:
        """不随单购时应付的金额。"""
        return self.effective_price_fen() * self.quantity

    def effective_price_fen(self) -> int:
        """当前条件下真正拿得到的单价。"""
        if self.needs_with_order:
            return self.list_price_fen or self.unit_price_fen
        return self.unit_price_fen

    def condition_note(self) -> str:
        if not self.needs_with_order:
            return ""
        return (
            f"标价 ¥{self.unit_price_fen / 100:.2f} 需{discount_label(self.discount_type)}"
            f"，否则按 ¥{self.list_price_fen / 100:.2f}"
        )


def discount_label(discount_type: str) -> str:
    return discount_type or "无条件优惠"


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def valid_until_end(text: Any) -> str:
    """从有效期区间里取**结束日**。

    `query-store-coupons` 的 `tradeDateTime` 给的是一整串区间：

        "2026-10-11 00:00:00-2026-10-17 23:59:59"

    早先版本把这个字符串整个塞进 `Coupon.valid_until`，下游再截前 10 位，
    拿到的是**开始日** —— 于是「还有 6 天才过期」的券被报成「今天到期」，
    用户被吓得当天就跑去消费，还可能白跑一趟。

    取不到任何日期就返回空串，让下游按「不知道到期日」处理；
    绝不能拿开始日冒充到期日。
    """
    if not text:
        return ""
    hits = _DATE_RE.findall(str(text))
    return hits[-1] if hits else ""


@dataclass(frozen=True)
class Coupon:
    """一张可用券。"""

    coupon_id: str
    coupon_code: str
    title: str = ""
    product_codes: frozenset[str] = frozenset()
    product_names: frozenset[str] = frozenset()
    valid_until: str = ""

    def applies_to(self, product_code: str) -> bool:
        # products 为空表示文档未给出限制，此时视作通用券
        return not self.product_codes or product_code in self.product_codes


@dataclass(frozen=True)
class PriceResult:
    """一次算价结果，金额单位统一为「分」。"""

    price_fen: int
    original_fen: int = 0
    discount_fen: int = 0
    delivery_fen: int = 0
    packing_fen: int = 0

    @property
    def saved_fen(self) -> int:
        """按接口 `originalPrice` 口径算的节省额。

        注意：随单购会把卡费计入 originalPrice，所以这个值可能是虚高的。
        想知道「真实省了多少」，请用 `saved_vs(裸买价)`。
        """
        return self.original_fen - self.price_fen

    def saved_vs(self, baseline_fen: int) -> int:
        """相对裸买价的真实节省。这才是用户关心的数字。"""
        return baseline_fen - self.price_fen

    def yuan(self, fen: int) -> str:
        return f"{fen / 100:.2f}"


@dataclass(frozen=True)
class WithOrderOption:
    """一个「随单购」档位。

    `payload` 和 `label` 必须分开 —— 这不是洁癖，是踩过坑的：

    早先版本把展示用的 `_label` 直接塞进同一个 dict 发给官方接口。
    官方当前没做严格 schema 校验所以没出事，但只要哪天它开始校验未知字段，
    整个算价请求会被拒，而这个 bug 在离线测试里根本测不出来
    （离线 oracle 不校验 payload）。

    所以这里定死：**payload 里只有官方字段，一个多余的键都不能有。**
    """

    payload: dict
    label: str = ""

    def signature(self) -> str:
        # 用 json 而不是 repr：dict 的 repr 依赖插入顺序，
        # 两个字段相同、顺序不同的 dict 会被当成两个方案，白白多调接口。
        return json.dumps(
            {"p": self.payload, "l": self.label}, sort_keys=True, ensure_ascii=False
        )


@dataclass
class Assignment:
    """一个候选方案：item_index -> coupon_index。"""

    mapping: dict[int, int] = field(default_factory=dict)
    with_order: WithOrderOption | None = None  # None 表示不随单购
    price: PriceResult | None = None

    def signature(self) -> tuple:
        wo = self.with_order
        if wo is None:
            wo_sig = ""
        elif isinstance(wo, WithOrderOption):
            wo_sig = wo.signature()
        else:
            # 兼容离线演示里直接传字符串档位的写法，别让它悄悄崩在这里
            wo_sig = str(wo)
        return (tuple(sorted(self.mapping.items())), wo_sig)

    def describe(self, cart: Sequence[CartItem], coupons: Sequence[Coupon]) -> list[str]:
        lines = []
        for item_idx, coupon_idx in sorted(self.mapping.items()):
            item = cart[item_idx]
            coupon = coupons[coupon_idx]
            lines.append(f"{item.name or item.product_code}  ←  {coupon.title}")
        if self.with_order:
            lines.append(f"随单购：{self.with_order.label}")
        return lines


PriceOracle = Callable[[list[CartItem], "Assignment"], PriceResult]


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    """跨求解器共享的算价调用预算。

    为什么需要它：`optimize` 跑完还会跑洞察（边际成本要逐件剔除重算、
    拆单建议要各拆一遍），每一路都会新建一个 CouponOptimizer。
    各记各的账的话，用户设 `--budget 40`，实际能打出 79 次 ——
    麦当劳限流 600 次/分钟不会当场报错，但用户看到「预算 40 用了 79 次」
    只会得出一个结论：这工具不守规矩。

    所以全局只认这一个计数器，谁调接口谁先问它。
    """

    def __init__(self, total: int):
        self.total = max(int(total), 0)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(self.total - self.used, 0)

    def spend(self, n: int = 1) -> None:
        self.used += n

    def __repr__(self) -> str:
        return f"Budget({self.used}/{self.total})"


class CouponOptimizer:
    """券价最优组合求解器。

    oracle 是唯一的真实来源：给定购物车与方案，返回真实价格。
    把它抽象成可注入函数，是为了让 demo 模式能离线复现完整演示。
    """

    def __init__(
        self,
        oracle: PriceOracle,
        max_calls: int = 40,
        verbose: bool = False,
        max_restarts: int = 2,
        restart_seed: int = 20261011,
        budget: Budget | None = None,
    ):
        """参数说明：

        `max_calls`  本求解器自己的上限
        `budget`     全局共享预算；不传就退化为「只受 max_calls 约束」
        `max_restarts`
            爬山收敛后换起点重来的轮数。实测同样 400 组随机用例、预算 40 次：
            不重启命中穷举最优 92.8%，重启 2 轮命中 94.0%，代价约多 2 次调用。
            重启只在还有预算时才发生，收敛快的场景不会被拖慢。
        """
        self.oracle = oracle
        self.max_calls = max(int(max_calls), 0)
        self.verbose = verbose
        self._budget = budget or Budget(self.max_calls)
        # 爬山收敛后再换起点重来的轮数。0 = 不做重启。
        self.max_restarts = max(0, max_restarts)
        self.restart_seed = restart_seed
        self.calls = 0
        # 同一个方案会被不同的邻域动作重复生成（例如先删后加绕回原点），
        # 缓存命中就不消耗调用次数 —— 预算是按次计的，省一次是一次。
        self._cache: dict[tuple, PriceResult] = {}
        self._failures = 0

    # ---------- 求值 ----------

    @property
    def remaining(self) -> int:
        """还能调几次：全局预算和本求解器上限，取更紧的那个。"""
        return min(self._budget.remaining, max(self.max_calls - self.calls, 0))

    @property
    def budget_used(self) -> int:
        """全局预算已用次数（含别的求解器花掉的）。"""
        return self._budget.used

    def evaluate(self, cart: Sequence[CartItem], assignment: Assignment) -> PriceResult:
        """求值一次；相同方案直接命中缓存，不计入调用次数。"""
        sig = assignment.signature()
        if sig in self._cache:
            assignment.price = self._cache[sig]
            return assignment.price
        if self._budget.remaining <= 0:
            raise BudgetExhausted(f"算价调用已达上限 {self._budget.total}")
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"本轮求解已达上限 {self.max_calls}")
        self._budget.spend()
        self.calls += 1
        result = self.oracle(list(cart), assignment)
        assignment.price = result
        self._cache[sig] = result
        self._failures = 0
        return result

    def try_evaluate(
        self, cart: Sequence[CartItem], candidate: Assignment
    ) -> PriceResult | None:
        """算不出来就返回 None，让搜索继续试别的邻域，而不是整轮崩掉。

        但**不能无脑吞掉所有异常**：如果 Token 失效或触发限流，
        每一次求值都会失败，全返回 None 的话搜索会静默跑完，
        最后报出一个「没找到更好的组合」——把真正的故障伪装成正常结果。
        所以连续失败到一定次数就原样抛出，让上层看见真实原因。
        """
        price, _ = self._try_evaluate(cart, candidate)
        return price

    def _try_evaluate(
        self, cart: Sequence[CartItem], candidate: Assignment
    ) -> tuple[PriceResult | None, str]:
        """返回 `(价格, 原因)`，原因三种：

            "ok"      算出来了
            "budget"  预算耗尽 —— 必须停止搜索，这时收工是诚实的
            "error"   单次失败（网络抖动 / 接口报错）—— 该**跳过这个候选继续试**

        为什么非得分开：爬山里如果只看 `price is None`，
        一次网络抖动就会被当成「邻域内没有更便宜的」，于是只爬两步就收工，
        最后报一个「已是最优」，实际上用户多付了钱，而且全程没有任何报错。
        """
        from .mcd_client import McpError  # 局部导入，避免模块级循环依赖

        try:
            return self.evaluate(cart, candidate), "ok"
        except BudgetExhausted:
            return None, "budget"
        except (McpError, TimeoutError, OSError) as exc:
            self._failures += 1
            if self._failures >= 3:
                raise
            if self.verbose:
                print(f"  （一次算价失败，跳过该候选：{exc}）")
            return None, "error"

    # ---------- 构造 ----------

    def _eligibility(
        self, cart: Sequence[CartItem], coupons: Sequence[Coupon]
    ) -> dict[int, list[int]]:
        """coupon_index -> 可挂的商品下标列表。"""
        table: dict[int, list[int]] = {}
        for ci, coupon in enumerate(coupons):
            hits = [
                ii for ii, item in enumerate(cart) if coupon.applies_to(item.product_code)
            ]
            if hits:
                table[ci] = hits
        return table

    def greedy(
        self, cart: Sequence[CartItem], coupons: Sequence[Coupon], shuffle: int = 0
    ) -> Assignment:
        """最受约束优先：可挂商品越少的券越先占坑。

        `shuffle` > 0 时打乱同长度券的先后顺序（`shuffle` 用作随机种子）。
        贪心的起点不同，落到的局部最优也不同 —— 这是扰动重启的多样性来源。
        种子固定，所以同一 seed 结果可复现，演示不会每次跑出不同数字。
        """
        table = self._eligibility(cart, coupons)
        used_items: set[int] = set()
        mapping: dict[int, int] = {}
        order = sorted(table, key=lambda c: len(table[c]))
        if shuffle:
            rng = random.Random(shuffle)
            # 只在「可挂商品数相同」的券之间换顺序，尽量不破坏最受约束优先的初衷
            buckets: dict[int, list[int]] = {}
            for ci in order:
                buckets.setdefault(len(table[ci]), []).append(ci)
            order = []
            for key in sorted(buckets):
                group = buckets[key][:]
                rng.shuffle(group)
                order.extend(group)
        for ci in order:
            for ii in table[ci]:
                if ii not in used_items:
                    mapping[ii] = ci
                    used_items.add(ii)
                    break
        return Assignment(mapping=mapping)

    # ---------- 搜索 ----------

    def optimize(
        self,
        cart: Sequence[CartItem],
        coupons: Sequence[Coupon],
        with_order_options: Iterable[WithOrderOption | None] = (None,),
    ) -> Assignment:
        table = self._eligibility(cart, coupons)
        best: Assignment | None = None

        # ---- 第一步：每个随单购档位先用贪心各试一次（每档只花 1 次调用）
        #
        # 为什么不能「一个档位爬完山再试下一个」：
        # 第一个档位的爬山会把预算吃干，轮到后面的档位时 try_evaluate 返回 None，
        # 直接 break —— 于是那些档位连一次都没被算过。
        # 实测 400 组随机用例里，这种写法让 22 组拿到了更差的结果，
        # 而且错得很隐蔽：mapping 一模一样，只是随单购档位选错了。
        # 「要不要随单购」往往值好几块钱，比在单一档位里多爬两步重要得多。
        screened: list[Assignment] = []
        for with_order in with_order_options:
            start = self.greedy(cart, coupons)
            start.with_order = with_order
            if self.try_evaluate(cart, start) is None:
                # 区分「预算见底」和「瞬时网络抖动」：
                # 只有前者才真正停；后者跳到下一个随单购档位再试，
                # 否则一次超时就会静默跳过「要不要随单购」这个最值钱的决策。
                if self.remaining <= 0:
                    break
                continue
            screened.append(start)
            if best is None or start.price.price_fen < best.price.price_fen:
                best = start

        # ---- 第二步：按贪心价从低到高分配剩下的预算，好钢用在刀刃上
        screened.sort(key=lambda a: a.price.price_fen)
        for start in screened:
            if self.remaining <= 0:
                break
            climbed = self._hill_climb(cart, coupons, table, start)
            if climbed.price is not None and (
                best is None or climbed.price.price_fen < best.price.price_fen
            ):
                best = climbed

            # 迭代局部搜索：收敛后换个贪心起点再来一轮。
            # 爬山只能保证「邻域内没有更便宜的」，不保证全局最优。
            # 只在预算还有富余时做，且每轮都先看一眼预算，绝不超支。
            for restart in range(1, self.max_restarts + 1):
                if self.remaining <= 0 or best is None or best.price is None:
                    break
                alt = self.greedy(cart, coupons, shuffle=self.restart_seed + restart)
                alt.with_order = start.with_order
                # 起点跟当前最优一样的话，重启没有意义，省下这次调用
                if alt.signature() == best.signature():
                    continue
                if self.try_evaluate(cart, alt) is None:
                    if self.remaining <= 0:
                        break
                    continue
                alt = self._hill_climb(cart, coupons, table, alt)
                if alt.price is not None and alt.price.price_fen < best.price.price_fen:
                    best = alt

        if best is None or best.price is None:
            # 预算连一次贪心都没跑成。
            # 这里**不能**绕过 evaluate 直接调 oracle —— 那会同时绕过预算和缓存，
            # 表面上看是"兜底成功"，实际是静默超支。这里照常记账：
            # 预算真的没了就抛 BudgetExhausted，让上层明确告诉用户调大 --budget。
            best = Assignment(mapping={}, with_order=None)
            best.price = self.evaluate(cart, best)
        return best

    def _hill_climb(
        self,
        cart: Sequence[CartItem],
        coupons: Sequence[Coupon],
        table: dict[int, list[int]],
        start: Assignment,
    ) -> Assignment:
        current = start
        if current.price is None:
            if self.try_evaluate(cart, current) is None:
                return current
        current_price = current.price.price_fen

        improved = True
        while improved and self.remaining > 0:
            improved = False

            # 邻域 1：删掉一张已挂的券
            # 这不是多余的 —— 实测券会被随单购作废，挂着反而占坑。
            for item_idx in list(current.mapping):
                candidate = Assignment(
                    mapping={k: v for k, v in current.mapping.items() if k != item_idx},
                    with_order=current.with_order,
                )
                price, reason = self._try_evaluate(cart, candidate)
                if reason == "budget":
                    return current  # 预算见底，这时收工是诚实的
                if price is None:
                    continue  # 抖动/单次失败：跳过这个候选，继续试下一个
                if price.price_fen < current_price:
                    current, current_price = candidate, price.price_fen
                    improved = True
                    break

            if improved:
                continue

            # 邻域 2：给空闲商品补一张未用的券
            used_coupons = set(current.mapping.values())
            for ci, hits in table.items():
                if ci in used_coupons:
                    continue
                for item_idx in hits:
                    if item_idx in current.mapping:
                        continue
                    candidate = Assignment(
                        mapping={**current.mapping, item_idx: ci},
                        with_order=current.with_order,
                    )
                    price, reason = self._try_evaluate(cart, candidate)
                    if reason == "budget":
                        return current  # 预算见底，这时收工是诚实的
                    if price is None:
                        continue  # 抖动/单次失败：跳过这个候选，继续试下一个
                    if price.price_fen < current_price:
                        current, current_price = candidate, price.price_fen
                        improved = True
                        break
                if improved:
                    break

            if improved:
                continue

            # 邻域 3：把某个**已挂商品**上的券，换成另一张还没用、且也适用它的券
            #
            # 这是最容易被漏掉的一个邻域，也是实测里最值钱的一个。
            # 贪心是「最受约束的券先占坑」，于是那张只能用在 1 个商品上的券
            # 会第一个抢到位置 —— 但它未必是那件商品上最猛的一张。
            # 此时「删」会让总价变贵（不通过）、「加」无处可加（商品已占），
            # 两个邻域都动不了，就卡死在次优解上。
            #
            # 实测（400 组随机券表，预算 40 次调用，以穷举最优为基准）：
            #   只有 删/加/挪/对调  →  命中 76.2%，平均差 ¥1.785
            #   补上这个邻域        →  命中 92.8%，平均差 ¥0.315
            # 这组对照就是 `selftest` 里的「引擎-对照穷举最优」一项，可复现。
            used_coupons = set(current.mapping.values())
            for item_idx, ci in list(current.mapping.items()):
                for cj, hits in table.items():
                    if cj == ci or cj in used_coupons:
                        continue
                    if item_idx not in hits:
                        continue
                    swapped = dict(current.mapping)
                    swapped[item_idx] = cj
                    candidate = Assignment(mapping=swapped, with_order=current.with_order)
                    price, reason = self._try_evaluate(cart, candidate)
                    if reason == "budget":
                        return current  # 预算见底，这时收工是诚实的
                    if price is None:
                        continue  # 抖动/单次失败：跳过这个候选，继续试下一个
                    if price.price_fen < current_price:
                        current, current_price = candidate, price.price_fen
                        improved = True
                        break
                if improved:
                    break

            if improved:
                continue

            # 邻域 4：把已挂的券挪到另一个它还适用的空闲商品上
            # 只有「删」和「加」的话，单步都不一定变便宜，会卡在局部最优出不来。
            for item_idx, ci in list(current.mapping.items()):
                for target in table.get(ci, []):
                    if target == item_idx or target in current.mapping:
                        continue
                    moved = dict(current.mapping)
                    del moved[item_idx]
                    moved[target] = ci
                    candidate = Assignment(mapping=moved, with_order=current.with_order)
                    price, reason = self._try_evaluate(cart, candidate)
                    if reason == "budget":
                        return current  # 预算见底，这时收工是诚实的
                    if price is None:
                        continue  # 抖动/单次失败：跳过这个候选，继续试下一个
                    if price.price_fen < current_price:
                        current, current_price = candidate, price.price_fen
                        improved = True
                        break
                if improved:
                    break

            if improved:
                continue

            # 邻域 5：两张已挂的券对调
            # 对调之后是「商品 i1 挂券 c2、商品 i2 挂券 c1」，
            # 所以要校验的是 c2 适用于 i1、c1 适用于 i2。
            # table 的 key 是券下标、value 是商品下标列表 —— 千万别把两者弄反。
            for (i1, c1), (i2, c2) in itertools.combinations(list(current.mapping.items()), 2):
                if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):
                    continue
                swapped = dict(current.mapping)
                swapped[i1], swapped[i2] = c2, c1
                candidate = Assignment(mapping=swapped, with_order=current.with_order)
                price, reason = self._try_evaluate(cart, candidate)
                if reason == "budget":
                    return current  # 预算见底，这时收工是诚实的
                if price is None:
                    continue  # 抖动/单次失败：跳过这个候选，继续试下一个
                if price.price_fen < current_price:
                    current, current_price = candidate, price.price_fen
                    improved = True
                    break

        return current
