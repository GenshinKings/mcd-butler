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


def cart_fingerprint(items: Sequence[CartItem]) -> tuple:
    """购物车指纹：商品码 + 数量 + 单价。

    为什么缓存键里必须有它：`evaluate(cart, assignment)` 的价格同时对购物车
    和挂券方案两者敏感，而 `Assignment.signature()` 只编码了后者。
    目前每个求解器恰好只服务一个购物车所以没出事，但只要有人把
    `CouponOptimizer` 提到循环外复用（很自然的重构），下一个购物车就会静默地
    用上一个购物车的价格 —— 查错成本极高。
    """
    return tuple(
        (
            it.product_code,
            int(it.quantity or 1),
            it.unit_price_fen,
            it.list_price_fen,
            it.needs_with_order,
        )
        for it in items
    )


def cached_oracle(oracle: PriceOracle) -> PriceOracle:
    """给算价器套一层跨调用共享的记忆化。

    为什么必须在这一层：`_cache` 挂在 CouponOptimizer 实例上时，一次 `advise`
    会新建十几个求解器（求边际成本每件一个、拆单建议每件两个），每个带一份
    互不相通的缓存。实测 `--budget 500` 时 181 次网络请求只对应 70 个不同的
    方案，六成在重复计算 —— 而这六成全是付费的网络往返。

    纯记忆化：**不增删任何候选、不改任何一步的搜索轨迹**，结果 100% 一致。
    省下来的调用额度还给搜索，所以质量只会变好，不会变坏。
    """
    cache: dict[tuple, PriceResult] = {}
    stats = {"calls": 0, "hits": 0}

    def wrapped(items: list[CartItem], assignment: Assignment) -> PriceResult:
        key = (cart_fingerprint(items), assignment.signature())
        hit = cache.get(key)
        if hit is not None:
            stats["hits"] += 1
            return hit
        result = oracle(items, assignment)
        stats["calls"] += 1
        cache[key] = result
        return result

    # 这两个属性给调用方做「这次到底有没有发请求」的判断，
    # 预算要按真实网络请求计，不能按 evaluate 次数计。
    wrapped.calls = stats  # type: ignore[attr-defined]
    wrapped.cache = cache  # type: ignore[attr-defined]
    return wrapped


def real_calls(oracle: PriceOracle) -> int:
    """取出一个算价器实际发出的网络请求数（被 `cached_oracle` 包过才>0 有意义）。"""
    stats = getattr(oracle, "calls", None)
    return int(stats["calls"]) if isinstance(stats, dict) else -1


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
        payload_items = _expand(items, assignment, coupons)

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
            # type().__name__ 是 Python 类型名（list/NoneType），只在内部有意义。
            raise McpError("麦当劳算价接口这次返回的数据格式不对，请稍后再试。", data)

        # price 缺失时必须报错，不能静默当 0 分 ——
        # 0 是最小值，优化器会把「没算出来」当成「最便宜」而选中它。
        if data.get("price") is None or data.get("price") == "":
            raise McpError("麦当劳算价接口这次没返回价格，请稍后再试。", data)

        _assert_quantity_kept(payload_items, data)
        _assert_price_scale(data, items)

        return PriceResult(
            price_fen=as_fen(data.get("price")),
            original_fen=as_fen(data.get("originalPrice")),
            discount_fen=as_fen(data.get("discount")),
            delivery_fen=as_fen(data.get("deliveryPrice")),
            packing_fen=as_fen(data.get("packingPrice")),
        )

    # 套一层记忆化：一次命令行会跑好几路分析（寻优 / 边际成本 / 拆单 / 冗余券），
    # 彼此的候选高度重叠。没有它，同样的 arrignment 会被反复付费重算。
    return cached_oracle(oracle)


def _expand(
    items: list[CartItem], assignment: Assignment, coupons: Sequence[Coupon]
) -> list[dict]:
    """把 quantity>1 的商品**拆成 quantity 行、每行 quantity=1**。

    这不是洁癖，是实测出来的真金白银的坑（麦咖啡冰美式中杯 507411 +
    券「9.9元中杯冰美式」，门店 1450713）：

        单行 quantity=2 挂券 → price=990，productList.quantity=[1]
        单行 quantity=2 无券 → price=2380，productList.quantity=[2]
        拆两行各 1 件、首行挂券 → price=2180，productList.quantity=[1,1]

    也就是说：**一旦挂券，接口会把这一行的数量静默重置成 1**，
    于是「买 2 杯挂 1 张券」被算成只收 1 杯的钱 ——
    引擎会非常自信地报「省了 ¥13.90」，而真实只省 ¥2.00。
    `optimize --items "薯条x2"` 正好踩在这条线上。

    拆行之后，券只挂在**第一行**上：一张券只能用一次，
    买两件时真实结算就是「一件用券 + 一件原价」。
    """
    payload_items: list[dict] = []
    for idx, item in enumerate(items):
        ci = assignment.mapping.get(idx)
        if ci is not None and not (0 <= ci < len(coupons)):
            # mapping 的值越界说明上游装配错了券下标。静默跳过会让这一行
            # 「假装挂了券却没挂」，算出一个比真实更贵的价，
            # 优化器还会把它当成「挂券方案」采纳 —— 必须报错。
            raise McpError(
                "这张券挂不上去，已跳过。"
                f"（第 {idx + 1} 件商品挂了第 {ci + 1} 张券，但这次只取到 {len(coupons)} 张）",
                {"idx": idx, "coupon_index": ci, "pool": len(coupons)},
            )
        qty = max(1, int(item.quantity or 1))
        for k in range(qty):
            entry: dict = {"productCode": item.product_code, "quantity": 1}
            # 券只挂第一行：一张券只能用一次，买两件时真实结算就是
            # 「一件用券 + 一件原价」。
            if ci is not None and k == 0:
                entry["couponId"] = coupons[ci].coupon_id
                entry["couponCode"] = coupons[ci].coupon_code
            payload_items.append(entry)
    return payload_items


def _assert_quantity_kept(payload_items: list[dict], data: dict) -> None:
    """核对接口有没有真的按我们要的数量算。

    这套代码里几乎每个字段都做了防御（脏数据、缺字段、换形态），
    唯独「我发了 quantity，接口是不是照做了」从来没人验过。
    而实测证明它就是不照做。所以这里必须断言：
    数量对不上就报错，绝不能让优化器采信一个凭空便宜的价格。
    """
    returned = data.get("productList")
    if not isinstance(returned, list) or not returned:
        return  # 没有明细就没法核对，不强求
    rows = [r for r in returned if isinstance(r, dict)]
    # 明细里根本没有 quantity 字段就没得核对 —— 报「返回 0 件」是冤枉接口，
    # 而且 engine 累计 3 次异常就会让整条链路崩掉，代价太大。
    if not any("quantity" in r for r in rows):
        return
    got = sum(as_fen(r.get("quantity")) for r in rows)
    want = len(payload_items)
    # 只拦「算少了」。算多了**不拦**：套餐会被接口拆成若干子件返回
    # （实测点一个双人餐返回 5 行），那是正常的展开，不是吞数量。
    # 吞数量只会让价格凭空便宜，所以真正的风险是单向的。
    if got < want:
        raise McpError(
            f"算价接口没有按请求的数量结算：要了 {want} 件，只返回 {got} 件。"
            "这种情况常见于「挂券后数量被重置」，继续算下去会报出虚假的优惠额。"
        )


def _assert_price_scale(data: dict, items: Sequence[CartItem]) -> None:
    """核对 `price` 的量纲是「分」，不是「元」。

    这个项目已经在量纲上栽过一次：`query-meals` 返回元字符串、
    `calculate-price` 返回分整数，所以代码里硬编码了 `as_fen`（按分读）。
    但**选哪个口径是硬编码的，没有任何检测** —— 哪天接口把 price 改成
    `"57.00"`，`as_fen("57.00")` 会得到 57，也就是 ¥0.57。
    优化器会把这个假的最低价选中，报出「裸买 ¥97.50 → 到手 ¥0.57，省了 ¥96.93」，
    而且屏幕上看不出任何异常。

    判据：到手价不可能超过裸买价 × 一个宽松倍数，也不可能为负；
    更要紧的是，**带小数点的字符串一定是元**，分口径从来不会写成 "57.00"。
    """
    raw = data.get("price")
    if isinstance(raw, str) and "." in raw:
        # 原来这里写着「请提 issue 或在 src/oracle.py 里切换 as_fen → to_fen」——
        # 源码路径和函数名是给维护者看的，打给消费者只会让人以为自己装坏了。
        raise McpError(
            "麦当劳算价接口这次返回的金额格式变了（带小数点的 ¥xx.xx），"
            "为避免算错已停止本次算价。请稍后再试或反馈给本项目。",
            raw,
        )
    price = as_fen(raw)
    if price < 0:
        raise McpError(f"麦当劳算价接口这次返回的价格是负数（¥{price / 100:.2f}），不予采信。", price)
    # 上界只做很松的兜底（菜单价 ×3 + 100 元），不追求精确：
    # 随单购会把卡费算进原价，精确上界容易误伤。这里的目的是挡住
    # 「量纲错了导致价格离谱」这一类，不是挡住贵的订单。
    # 字段必须写对：这里曾经写成 `getattr(it, "price_fen", 0)`，而 CartItem
    # 根本没有 `price_fen`（它叫 `unit_price_fen` / `list_price_fen`）——
    # getattr 的默认值让菜单合计恒为 0，`menu_total > 0` 永远不成立，
    # 整段守卫变成一次都不会执行的死代码，而且从外面完全看不出来。
    # 所以这里直接用真实属性，写错了就当场 AttributeError，不会假装通过。
    menu_total = sum(it.effective_price_fen() * it.quantity for it in items)
    if menu_total > 0 and price > menu_total * 3 + 10_000:
        raise McpError(
            f"麦当劳算价接口这次报了 ¥{price / 100:.2f}，"
            f"比这单菜单合计 ¥{menu_total / 100:.2f} 高出太多，"
            "担心是金额单位变了，已停止本次算价。请稍后再试。",
            {"price_fen": price, "menu_total_fen": menu_total},
        )
