"""离线演示数据层（--demo 模式）。

为什么需要这一层
----------------
真实账号常有「零历史订单 / 无可用券 / 无配送地址」的情况，
新人 clone 仓库后直接跑会得到一个空演示 —— 这是演示级硬伤。

本层提供一份脱敏的固定数据集，让任何人无需 Token、
无需真实账号，也能看到完整的引擎搜索过程和对比结论。

所有数据均为构造数据，不代表麦当劳真实菜单或价格。
"""

from __future__ import annotations

from .coupon_engine import CartItem, Coupon, PriceResult, WithOrderOption


# ---------------------------------------------------------------- 演示门店

DEMO_STORE = {
    "storeCode": "0000000",
    "storeName": "麦门管家·演示餐厅",
    "address": "示例市示例区示例路 1 号",
    "beType": 1,
    "orderType": 1,
}

# ---------------------------------------------------------------- 演示菜单

DEMO_MENU: list[CartItem] = [
    CartItem("D001", "巨无霸", 1, 2500),
    CartItem("D002", "麦辣鸡腿堡", 1, 2200),
    CartItem("D003", "中份薯条", 1, 1200),
    CartItem("D004", "麦香鸡", 1, 1300),
    CartItem("D005", "中杯可乐", 1, 900),
    CartItem("D006", "麦旋风", 1, 1000),
    CartItem("D007", "脆薯饼", 1, 800),
    # 条件价商品：菜单上标 62.90 是「随单购麦金卡」价，不办卡按 122.50 结。
    # 真实菜单里这种商品不少（四件套、八件套都这样），演示里必须留一个，
    # 否则新人看不出「菜单标价可能骗人」这件最关键的事。
    CartItem(
        "D008",
        "BFF四宫格小食盘双人餐",
        1,
        6290,
        list_price_fen=12250,
        discount_type="随单购麦金卡优惠",
        needs_with_order=True,
    ),
]

DEMO_MENU_BY_CODE: dict[str, CartItem] = {m.product_code: m for m in DEMO_MENU}

# ---------------------------------------------------------------- 演示券池

# demo_discount_fen 仅用于离线演示求值，真实券不提供该字段
DEMO_COUPONS: list[Coupon] = [
    Coupon(
        coupon_id="C1",
        coupon_code="DEMO-C1",
        title="巨无霸立减 5 元",
        product_codes=frozenset({"D001"}),
        valid_until="2026-10-31",
    ),
    Coupon(
        coupon_id="C2",
        coupon_code="DEMO-C2",
        title="汉堡类任选 -4 元",
        product_codes=frozenset({"D001", "D002"}),
        valid_until="2026-10-31",
    ),
    Coupon(
        coupon_id="C3",
        coupon_code="DEMO-C3",
        title="小食买一送一（薯条）",
        product_codes=frozenset({"D003"}),
        valid_until="2026-10-31",
    ),
    Coupon(
        coupon_id="C4",
        coupon_code="DEMO-C4",
        title="饮料免费升杯",
        product_codes=frozenset({"D005"}),
        valid_until="2026-10-31",
    ),
    # 陷阱券：挂上后反而占用商品、挡掉更优组合
    Coupon(
        coupon_id="C5",
        coupon_code="DEMO-C5",
        title="麦旋风满 20 减 1 元",
        product_codes=frozenset({"D006"}),
        valid_until="2026-10-31",
    ),
]

# 各券在演示世界的真实减免（仅离线求值使用）
_DEMO_DISCOUNT = {"C1": 500, "C2": 400, "C3": 1200, "C4": 300, "C5": -100}

# 随单购（演示）
# 演示档位也用正式的 WithOrderOption —— payload 和 label 分开。
# 演示里 payload 没有真实含义，但保持跟线上同构，离线才能测到线上会走的代码路径。
DEMO_WITH_ORDER_OPTIONS = [
    None,
    WithOrderOption(payload={"membershipCode": "DEMO-BREAKFAST"}, label="早餐卡（随单购）"),
    WithOrderOption(payload={"membershipCode": "DEMO-GOLD"}, label="麦金卡（随单购）"),
]
_DEMO_WITH_ORDER_DISCOUNT = {"": 0, "早餐卡（随单购）": 200, "麦金卡（随单购）": 600}


# ---------------------------------------------------------------- 演示算价器

def demo_oracle(items: list[CartItem], assignment) -> PriceResult:
    """离线求价器：模拟真实 calculate-price 的黑盒行为。

    故意保留两个真实世界的「坑」，以展示局部搜索的价值：
      1. C5 是负收益券，贪心会误挂，靠删除动作纠正
      2. 麦金卡随单购与部分券互斥，只有换档才能拿到最优
    """
    subtotal = sum(i.unit_price_fen * i.quantity for i in items)

    coupon_ids = [_cid(coupon_idx) for _, coupon_idx in assignment.mapping.items()]

    discount = 0
    for idx, coupon_idx in assignment.mapping.items():
        cid = _cid(coupon_idx)
        if not cid:
            continue
        unit = _DEMO_DISCOUNT.get(cid, 0)
        # 「买一送一」这类券按件数生效：买 1 件省半价、买 2 件省一件的钱。
        # 不按件数算会出现「一件商品挂买一送一券 → 到手 ¥0」这种假得离谱的演示。
        if cid == "C3":
            qty = items[idx].quantity if idx < len(items) else 1
            unit = (unit // 2) * qty
        discount += unit

    wo_label = assignment.with_order.label if assignment.with_order else ""

    # 互斥规则：麦金卡随单购不能与 C2 同享
    if wo_label == "麦金卡（随单购）" and "C2" in coupon_ids:
        discount -= _DEMO_DISCOUNT["C2"]

    discount += _DEMO_WITH_ORDER_DISCOUNT.get(wo_label, 0)

    price = max(subtotal - discount, 0)
    return PriceResult(
        price_fen=price,
        original_fen=subtotal,
        discount_fen=discount,
        delivery_fen=0,
        packing_fen=0,
    )


def _cid(coupon_idx: int) -> str:
    return DEMO_COUPONS[coupon_idx].coupon_id


# ---------------------------------------------------------------- 演示历史订单（replay 用）

DEMO_ORDER_HISTORY = [
    {
        "orderId": "DEMO-20260714-001",
        "createTime": "2026-07-14 12:31:05",
        "storeName": "麦门管家·演示餐厅",
        "beType": 1,
        "products": [
            {"productCode": "D001", "productName": "巨无霸", "quantity": 1},
            {"productCode": "D003", "productName": "中份薯条", "quantity": 1},
            {"productCode": "D005", "productName": "中杯可乐", "quantity": 1},
        ],
        "realTotalAmount": 3300,  # 当时到手价：33.00 元
    },
    {
        "orderId": "DEMO-20260902-002",
        "createTime": "2026-09-02 19:02:44",
        "storeName": "麦门管家·演示餐厅",
        "beType": 1,
        "products": [
            {"productCode": "D002", "productName": "麦辣鸡腿堡", "quantity": 1},
            {"productCode": "D006", "productName": "麦旋风", "quantity": 1},
        ],
        "realTotalAmount": 3000,
    },
]

# 演示价格快照：今天的菜单价与历史单价的差异（用于「比上次贵了多少」）
DEMO_PRICE_SNAPSHOT = {
    "D001": {"2026-07-14": 2400, "now": 2500},
    "D003": {"2026-07-14": 1200, "now": 1200},
    "D005": {"2026-07-14": 900, "now": 900},
    "D002": {"2026-09-02": 2200, "now": 2200},
    "D006": {"2026-09-02": 1000, "now": 1000},
}
