"""省钱洞察：不只要给出最优价，还要说清楚**钱是怎么省下来的**。

一个只会说「到手 ¥70.30」的工具，跟 App 的结算页没区别。
真正让人觉得「早该有这东西」的，是回答三个没人回答过的问题：

1. **这单里每件东西，我实际花了多少？**（不是标价）
   标价 ¥34.50 的套餐，因为整单用了券，它可能只花掉你 ¥26.00；
   而标价 ¥8.50 的薯饼，可能一分没减。不拆开看，你永远不知道券到底
   补贴到了哪一件上。

2. **哪张券挂了等于没挂？**
   实测发现券会被随单购作废：随单购之后商品已经降到券面价，
   此时再挂券一分钱也不会再减。这种券就是摆设，得指出来。

3. **分开下单会不会更便宜？**
   一张券通常只能用一次，整单结算时可能被迫「二选一」；
   拆成两单说不定两张都能用上。这个没人会替你试。

每一项都靠**真调算价接口**验证，不靠估算——因为接口不返回券的折扣金额，
离线算不出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .coupon_engine import (
    Assignment,
    Budget,
    BudgetExhausted,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
)


@dataclass
class Marginal:
    """单件商品在本单中的真实成本。"""

    item: CartItem
    list_fen: int  # 标价 × 数量
    marginal_fen: int  # 把它拿掉，总价会降多少 —— 这才是它真实花掉的钱

    @property
    def delta_fen(self) -> int:
        """标价与实际花费之差。正数＝这件东西享受到了优惠。"""
        return self.list_fen - self.marginal_fen

    @property
    def ratio(self) -> float:
        """实际花费 / 标价。0 表示白送，1 表示一分没减。"""
        if self.list_fen <= 0:
            return 1.0
        return self.marginal_fen / self.list_fen


def marginal_costs(
    oracle: Callable[[Sequence[CartItem], Assignment], PriceResult],
    cart: Sequence[CartItem],
    coupons: Sequence[Coupon],
    with_order_options: Callable[[Sequence[CartItem]], Sequence],
    baseline_fen: int,
    per_call_budget: int = 12,
    call_counter: list[int] | None = None,
    budget: Budget | None = None,
) -> list[Marginal]:
    """逐件剔除，反推每件的真实成本。

    注意：被剔除后剩下的商品会**重新寻优**，
    因为少了一件之后，最优的券搭配可能变了。
    只把券原样搬过去会算错。
    """
    out: list[Marginal] = []
    for i, item in enumerate(cart):
        # 预算见底就停：这一项洞察做不全可以不做，但不能把 --budget 撑爆
        if budget is not None and budget.remaining <= 0:
            break
        rest = [c for j, c in enumerate(cart) if j != i]
        if not rest:
            continue
        opt = CouponOptimizer(oracle, max_calls=per_call_budget, budget=budget)
        try:
            best = opt.optimize(rest, coupons, with_order_options(rest))
        except BudgetExhausted:
            break
        if call_counter is not None:
            call_counter[0] += opt.calls
        if best.price is None:
            continue
        out.append(
            Marginal(
                item=item,
                list_fen=item.effective_price_fen() * item.quantity,
                marginal_fen=baseline_fen - best.price.price_fen,
            )
        )
    return out


def redundant_coupons(
    oracle: Callable[[Sequence[CartItem], Assignment], PriceResult],
    cart: Sequence[CartItem],
    coupons: Sequence[Coupon],
    best: Assignment,
    budget: Budget | None = None,
) -> list[tuple[CartItem, Coupon]]:
    """找出「挂了等于没挂」的券。

    判据很直白：把这张券摘掉，总价一分不变 —— 那它就是摆设。
    实测里最典型的成因是随单购已经把商品降到券面价了。
    """
    if best.price is None:
        return []
    out: list[tuple[CartItem, Coupon]] = []
    for item_idx, ci in sorted(best.mapping.items()):
        if budget is not None and budget.remaining <= 0:
            break  # 全局预算见底，剩下的券这次不查了
        reduced = {k: v for k, v in best.mapping.items() if k != item_idx}
        cand = Assignment(mapping=reduced, with_order=best.with_order)
        _stats = getattr(oracle, "calls", None)
        _before = _stats["calls"] if isinstance(_stats, dict) else None
        try:
            price = oracle(cart, cand)
        except (TypeError, ValueError, KeyError):
            # 只吞「解析不出价格」这类数据问题；
            # 网络/鉴权故障必须往上抛，否则会把故障伪装成「没有冗余券」。
            continue
        # 先算价、成功后才扣预算：失败时不会白白吃掉一次调用额度。
        # 命中共享缓存时同样不扣 —— 那一趟只是查表，没有真的打扰麦当劳。
        # 「已挂的最优解去掉某张券」这个候选，爬山过程的邻域 1（删除）极可能
        # 已经算过，这一步是纯粹的白捡。
        if budget is not None and (not isinstance(_stats, dict) or _before != _stats["calls"]):
            budget.spend()
        if price.price_fen == best.price.price_fen:
            out.append((cart[item_idx], coupons[ci]))
    return out


def split_suggestions(
    oracle: Callable[[Sequence[CartItem], Assignment], PriceResult],
    cart: Sequence[CartItem],
    coupons: Sequence[Coupon],
    with_order_options: Callable[[Sequence[CartItem]], Sequence],
    whole_fen: int,
    per_call_budget: int = 12,
    call_counter: list[int] | None = None,
    budget: Budget | None = None,
) -> list[tuple[CartItem, int, int]]:
    """试试把某一件单独拆成一单，看会不会更便宜。

    为什么值得试：一张券只能用一次，整单结算时可能被迫二选一。
    这里只试「单件独立成单」这一种拆法 —— 完整枚举所有分割是 2^n，
    调用次数会爆；而单件拆出的情形已经覆盖了绝大多数真实受益场景。

    两个必须守住的规矩，破了就会报出一个用户根本拿不到的价格：

    1. **同一张券不能在两单里各用一次。**
       早先的写法把完整券池分别交给两单各跑一遍，两边都挑到同一张最肥的券，
       于是报出「拆开省 ¥5.00」——而那 ¥5.00 需要同一张券核销两次。
       用户的实际体验是：照着做，第二单的券根本选不上，价格比整单还贵。
       现在的做法是让两单**依次**挑券：先跑的那单挑走之后，
       后跑的那单只能在剩下的券里选。这样报出来的合计是客人真能买到的。

    2. **两单都随单购麦金卡的不算数。**
       随单购是在这一单里买一张卡。拆成两单还各自随单购，就是买两张卡，
       多付一份卡费。这种「便宜」是账面上便宜、付款时更贵。

    两个方向都试（先给单件挑 / 先给其余挑），取真能买到的那个更低的。
    """
    out: list[tuple[CartItem, int, int]] = []
    for i, item in enumerate(cart):
        if budget is not None and budget.remaining <= 0:
            break
        rest = [c for j, c in enumerate(cart) if j != i]
        if not rest:
            continue

        calls = 0
        best_total: int | None = None
        try:
            # 两个方向都试：先给「这一件」挑券，以及先给「其余」挑券。
            for first, second in (([item], rest), (rest, [item])):
                o1 = CouponOptimizer(oracle, max_calls=per_call_budget, budget=budget)
                b1 = o1.optimize(first, coupons, with_order_options(first))
                calls += o1.calls
                if b1.price is None:
                    continue
                # 第二单只能用第一单挑剩下的券
                taken = set(b1.mapping.values())
                left = [c for k, c in enumerate(coupons) if k not in taken]
                o2 = CouponOptimizer(oracle, max_calls=per_call_budget, budget=budget)
                b2 = o2.optimize(second, left, with_order_options(second))
                calls += o2.calls
                if b2.price is None:
                    continue
                # 两单都随单购 = 买两张卡，这个价拿不到
                if b1.with_order and b2.with_order:
                    continue
                total = b1.price.price_fen + b2.price.price_fen
                if best_total is None or total < best_total:
                    best_total = total
        except BudgetExhausted:
            break
        if call_counter is not None:
            call_counter[0] += calls
        if best_total is None or best_total >= whole_fen:
            continue
        out.append((item, best_total, whole_fen - best_total))
    return out
