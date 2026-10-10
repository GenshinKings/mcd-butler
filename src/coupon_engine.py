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
    2. 爬山：交替执行 删除 / 新增 / 交换 三种邻域动作，
       每步都用真实算价接口验证，接受任何变便宜的改动
    3. 预算耗尽或收敛即停

这样既能在有限调用次数内逼近最优，又不会依赖未在文档中承诺的字段。

合规：`calculate-price` 只算价，不产生任何订单。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence


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


@dataclass(frozen=True)
class Coupon:
    """一张可用券。"""

    coupon_id: str
    coupon_code: str
    title: str = ""
    product_codes: frozenset[str] = frozenset()
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


@dataclass
class Assignment:
    """一个候选方案：item_index -> coupon_index。"""

    mapping: dict[int, int] = field(default_factory=dict)
    with_order: object | None = None  # 随单购（早餐卡 / 麦金卡），None 表示不随单购
    price: PriceResult | None = None

    def signature(self) -> tuple:
        return (
            tuple(sorted(self.mapping.items())),
            repr(self.with_order),
        )

    def describe(self, cart: Sequence[CartItem], coupons: Sequence[Coupon]) -> list[str]:
        lines = []
        for item_idx, coupon_idx in sorted(self.mapping.items()):
            item = cart[item_idx]
            coupon = coupons[coupon_idx]
            lines.append(f"{item.name or item.product_code}  ←  {coupon.title}")
        if self.with_order:
            wo = self.with_order
            label = wo.get("_label") if isinstance(wo, dict) else None
            lines.append(f"随单购：{label or wo}")
        return lines


PriceOracle = Callable[[list[CartItem], "Assignment"], PriceResult]


class BudgetExhausted(RuntimeError):
    pass


class CouponOptimizer:
    """券价最优组合求解器。

    oracle 是唯一的真实来源：给定购物车与方案，返回真实价格。
    把它抽象成可注入函数，是为了让 demo 模式能离线复现完整演示。
    """

    def __init__(self, oracle: PriceOracle, max_calls: int = 40, verbose: bool = False):
        self.oracle = oracle
        self.max_calls = max_calls
        self.verbose = verbose
        self.calls = 0
        self.trace: list[int] = []
        # 同一个方案会被不同的邻域动作重复生成（例如先删后加绕回原点），
        # 缓存命中就不消耗调用次数 —— 预算是按次计的，省一次是一次。
        self._cache: dict[tuple, PriceResult] = {}

    # ---------- 求值 ----------

    @property
    def remaining(self) -> int:
        return max(self.max_calls - self.calls, 0)

    def evaluate(self, cart: Sequence[CartItem], assignment: Assignment) -> PriceResult:
        """求值一次；相同方案直接命中缓存，不计入调用次数。"""
        sig = assignment.signature()
        if sig in self._cache:
            assignment.price = self._cache[sig]
            return assignment.price
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"算价调用已达上限 {self.max_calls}")
        self.calls += 1
        result = self.oracle(list(cart), assignment)
        assignment.price = result
        self._cache[sig] = result
        self.trace.append(result.price_fen)
        return result

    def _try_eval(
        self, cart: Sequence[CartItem], candidate: Assignment
    ) -> PriceResult | None:
        """预算耗尽时返回 None，让调用方优雅退出而不是崩掉。"""
        try:
            return self.evaluate(cart, candidate)
        except BudgetExhausted:
            return None

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
        self, cart: Sequence[CartItem], coupons: Sequence[Coupon]
    ) -> Assignment:
        """最受约束优先：可挂商品越少的券越先占坑。"""
        table = self._eligibility(cart, coupons)
        used_items: set[int] = set()
        mapping: dict[int, int] = {}
        for ci in sorted(table, key=lambda c: len(table[c])):
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
        with_order_options: Iterable[object] = (None,),
    ) -> Assignment:
        table = self._eligibility(cart, coupons)
        best: Assignment | None = None

        for with_order in with_order_options:
            current = self.greedy(cart, coupons)
            current.with_order = with_order
            if self._try_eval(cart, current) is None:
                break  # 预算耗尽，后面档位也别试了
            if best is None or current.price.price_fen < best.price.price_fen:
                best = current

            current = self._hill_climb(cart, coupons, table, current)
            if current.price is not None and (
                best is None or current.price.price_fen < best.price.price_fen
            ):
                best = current

        if best is None or best.price is None:
            # 预算连一次贪心都没跑成：退化成裸买方案，至少返回可用结果而不是崩掉
            best = Assignment(mapping={}, with_order=None)
            best.price = self.oracle(list(cart), best)
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
            if self._try_eval(cart, current) is None:
                return current
        current_price = current.price.price_fen

        improved = True
        while improved and self.calls < self.max_calls:
            improved = False

            # 邻域 1：删掉一张已挂的券
            # 这不是多余的 —— 实测券会被随单购作废，挂着反而占坑。
            for item_idx in list(current.mapping):
                candidate = Assignment(
                    mapping={k: v for k, v in current.mapping.items() if k != item_idx},
                    with_order=current.with_order,
                )
                price = self._try_eval(cart, candidate)
                if price is None:
                    return current
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
                    price = self._try_eval(cart, candidate)
                    if price is None:
                        return current
                    if price.price_fen < current_price:
                        current, current_price = candidate, price.price_fen
                        improved = True
                        break
                if improved:
                    break

            if improved:
                continue

            # 邻域 3：把已挂的券挪到另一个它还适用的空闲商品上
            # 只有「删」和「加」的话，单步都不一定变便宜，会卡在局部最优出不来。
            for item_idx, ci in list(current.mapping.items()):
                for target in table.get(ci, []):
                    if target == item_idx or target in current.mapping:
                        continue
                    moved = dict(current.mapping)
                    del moved[item_idx]
                    moved[target] = ci
                    candidate = Assignment(mapping=moved, with_order=current.with_order)
                    price = self._try_eval(cart, candidate)
                    if price is None:
                        return current
                    if price.price_fen < current_price:
                        current, current_price = candidate, price.price_fen
                        improved = True
                        break
                if improved:
                    break

            if improved:
                continue

            # 邻域 4：两张已挂的券对调
            # 对调之后是「商品 i1 挂券 c2、商品 i2 挂券 c1」，
            # 所以要校验的是 c2 适用于 i1、c1 适用于 i2。
            # table 的 key 是券下标、value 是商品下标列表 —— 千万别把两者弄反。
            for (i1, c1), (i2, c2) in itertools.combinations(list(current.mapping.items()), 2):
                if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):
                    continue
                swapped = dict(current.mapping)
                swapped[i1], swapped[i2] = c2, c1
                candidate = Assignment(mapping=swapped, with_order=current.with_order)
                price = self._try_eval(cart, candidate)
                if price is None:
                    return current
                if price.price_fen < current_price:
                    current, current_price = candidate, price.price_fen
                    improved = True
                    break

        return current
