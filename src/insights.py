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

from .coupon_engine import Assignment, CartItem, Coupon, CouponOptimizer, PriceResult


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
) -> list[Marginal]:
    """逐件剔除，反推每件的真实成本。

    注意：被剔除后剩下的商品会**重新寻优**，
    因为少了一件之后，最优的券搭配可能变了。
    只把券原样搬过去会算错。
    """
    out: list[Marginal] = []
    for i, item in enumerate(cart):
        rest = [c for j, c in enumerate(cart) if j != i]
        if not rest:
            continue
        opt = CouponOptimizer(oracle, max_calls=per_call_budget)
        best = opt.optimize(rest, coupons, with_order_options(rest))
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
) -> list[tuple[CartItem, Coupon]]:
    """找出「挂了等于没挂」的券。

    判据很直白：把这张券摘掉，总价一分不变 —— 那它就是摆设。
    实测里最典型的成因是随单购已经把商品降到券面价了。
    """
    if best.price is None:
        return []
    out: list[tuple[CartItem, Coupon]] = []
    for item_idx, ci in sorted(best.mapping.items()):
        reduced = {k: v for k, v in best.mapping.items() if k != item_idx}
        cand = Assignment(mapping=reduced, with_order=best.with_order)
        try:
            price = oracle(cart, cand)
        except Exception:
            continue
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
) -> list[tuple[CartItem, int, int]]:
    """试试把某一件单独拆成一单，看会不会更便宜。

    为什么值得试：一张券只能用一次，整单结算时可能被迫二选一。
    这里只试「单件独立成单」这一种拆法 —— 完整枚举所有分割是 2^n，
    调用次数会爆；而单件拆出的情形已经覆盖了绝大多数真实受益场景。
    """
    out: list[tuple[CartItem, int, int]] = []
    for i, item in enumerate(cart):
        rest = [c for j, c in enumerate(cart) if j != i]
        if not rest:
            continue

        o_solo = CouponOptimizer(oracle, max_calls=per_call_budget)
        b_solo = o_solo.optimize([item], coupons, with_order_options([item]))
        o_rest = CouponOptimizer(oracle, max_calls=per_call_budget)
        b_rest = o_rest.optimize(rest, coupons, with_order_options(rest))
        if call_counter is not None:
            call_counter[0] += o_solo.calls + o_rest.calls
        if b_solo.price is None or b_rest.price is None:
            continue

        total = b_solo.price.price_fen + b_rest.price.price_fen
        if total < whole_fen:
            out.append((item, total, whole_fen - total))
    return out
