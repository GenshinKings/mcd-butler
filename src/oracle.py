"""算价 oracle 的**唯一**构造处。

为什么要有这个文件
------------------
`calculate-price` 的调用原先在 `cli.py` 里被复制了五份
（advise / almanac / optimize / compare / replay），
差异全是复制时无意造成的 drift：almanac 那份丢了配送费与打包费。

更严重的是，五份里都有一行：

    if isinstance(assignment.with_order, dict):
        payload["withOrder"] = assignment.with_order.payload

而 `with_order` 早就从裸 dict 改成了 `WithOrderOption`
（`frozen dataclass`，**不是 dict 子类**），这个判断恒为 False ——
真实账号上 `withOrder` **一次都没发出去过**。

实测同一件商品（人气超值四件套随心选）：
    不带 withOrder → ¥57.00
    带 withOrder    → ¥37.90
差 ¥19.10。引擎以为自己在逐档试算随单购，其实算了几个一模一样的价，
最后还照样打印「随单购：麦金卡（随单购）」—— 用户拿到的是编出来的建议。

合并到一处，这种 bug 才可能只犯一次、也才可能被一条断言抓住。
"""

from __future__ import annotations

from typing import Callable, Sequence

from .coupon_engine import Assignment, CartItem, Coupon, PriceResult, as_fen
from .mcd_client import McpError

PriceOracle = Callable[[list[CartItem], Assignment], PriceResult]


def make_price_oracle(
    client,
    store_code: str,
    coupons: Sequence[Coupon],
    order_type: int = 1,
    be_type: int = 1,
) -> PriceOracle:
    """构造一个 `calculate-price` 的黑盒求值器。

    参数说明：
        `order_type` / `be_type`
            到店取餐固定 1/1。外卖（麦乐送）的取价路径目前拿不到真值，
            所以这里不做外卖档位 —— 宁可少一块功能，也不拿假数字冒充。
    """

    def oracle(items: list[CartItem], assignment: Assignment) -> PriceResult:
        payload_items = []
        for idx, item in enumerate(items):
            entry = {"productCode": item.product_code, "quantity": item.quantity}
            ci = assignment.mapping.get(idx)
            if ci is not None:
                entry["couponId"] = coupons[ci].coupon_id
                entry["couponCode"] = coupons[ci].coupon_code
            payload_items.append(entry)

        payload: dict = {
            "storeCode": store_code,
            "orderType": order_type,
            "beType": be_type,
            "items": payload_items,
        }
        # 守卫必须判 None，不能判 dict：WithOrderOption 是 frozen dataclass。
        # 判 dict 会让随单购静默失效（实测单品差 ¥19.10），且离线永远测不出来。
        if assignment.with_order is not None:
            payload["withOrder"] = assignment.with_order.payload

        data = client.data("calculate-price", payload)
        if not isinstance(data, dict):
            raise McpError(f"算价接口返回了非对象结构：{type(data).__name__}")

        # price 缺失时必须报错，不能静默当 0 分 ——
        # 0 是最小值，优化器会把「没算出来」当成「最便宜」而选中它。
        if data.get("price") is None or data.get("price") == "":
            raise McpError("算价接口未返回 price 字段，无法判断这一单的价格")

        return PriceResult(
            price_fen=as_fen(data.get("price")),
            original_fen=as_fen(data.get("originalPrice")),
            discount_fen=as_fen(data.get("discount")),
            delivery_fen=as_fen(data.get("deliveryPrice")),
            packing_fen=as_fen(data.get("packingPrice")),
        )

    return oracle
