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


def _days(n: int) -> str:
    """相对今天的到期日 —— 演示里必须能稳定出现「明天到期」这类提示。"""
    from datetime import date, timedelta

    return (date.today() + timedelta(days=n)).isoformat()


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
        valid_until=_days(20),
    ),
    Coupon(
        coupon_id="C2",
        coupon_code="DEMO-C2",
        title="汉堡类任选 -4 元",
        product_codes=frozenset({"D001", "D002"}),
        valid_until=_days(20),
    ),
    Coupon(
        coupon_id="C3",
        coupon_code="DEMO-C3",
        title="小食买一送一（薯条）",
        product_codes=frozenset({"D003"}),
        valid_until=_days(20),
    ),
    Coupon(
        coupon_id="C4",
        coupon_code="DEMO-C4",
        title="中杯可乐立减 3 元",
        product_codes=frozenset({"D005"}),
        valid_until=_days(20),
    ),
    # 陷阱券：挂上后反而占用商品、挡掉更优组合
    Coupon(
        coupon_id="C5",
        coupon_code="DEMO-C5",
        title="麦旋风满 20 减 1 元",
        product_codes=frozenset({"D006"}),
        valid_until=_days(20),
    ),
]

# 各券在演示世界的真实减免（仅离线求值使用）
_DEMO_DISCOUNT = {"C1": 500, "C2": 400, "C3": 1200, "C4": 300, "C5": -100}

# 随单购（演示）
# 演示档位也用正式的 WithOrderOption —— payload 和 label 分开。
# 演示里 payload 没有真实含义，但保持跟线上同构，离线才能测到线上会走的代码路径。
DEMO_WITH_ORDER_OPTIONS = [
    None,
    WithOrderOption(payload={"membershipCode": "DEMO-GOLD"}, label="麦金卡（随单购）"),
]
_DEMO_WITH_ORDER_DISCOUNT = {"": 0, "麦金卡（随单购）": 600}


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
        # 演示世界也必须讲有效期。原先这里完全不看 valid_until，
        # 于是只要有人把 _days(20) 手滑写成 _days(-1)，或者系统时钟往前跳，
        # 演示就会 **平静地** 印出「用一张昨天就过期的券省了 ¥20」——
        # 一份算错的账，而且没有任何提示，看着跟真的一样。
        if _expired(DEMO_COUPONS[coupon_idx].valid_until):
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


def _expired(valid_until: str) -> bool:
    """这张券到期了吗。没写到期日的按「不过期」处理（真实券里大量不写）。"""
    if not valid_until:
        return False
    from datetime import date

    try:
        return date.fromisoformat(valid_until[:10]) < date.today()
    except ValueError:
        # 解析不出来的日期按「不知道」处理，不能反过来当成已过期把券全废掉
        return False


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


# ------------------------------------------------------- 券包体检演示数据
#
# 体检要演示的是三件真实存在、但演示券池堆不出来的事：
#   门槛券（便宜的挂不上）、废券（挂着一分不减）、死券（这家店没得挂）。
# 所以这里单独配一套小菜单 + 小券池，规则明确、可离线复现。

AUDIT_MENU: list[CartItem] = [
    CartItem("A1", "麦香鱼", 1, 1000),
    CartItem("A2", "麦旋风", 1, 1000),
    CartItem("A3", "板烧鸡腿堡", 1, 1800),
    # 故意不复用 DEMO_MENU 的商品名：两段演示用同一家店名、同一件「巨无霸」、
    # 却一个是「立减 5 元」一个是「立减 8 元」，读者会以为首页在编数据。
    CartItem("A4", "安格斯厚牛堡", 1, 2500),
]

AUDIT_COUPONS: list[Coupon] = [
    Coupon("K1", "KA1", "板烧鸡腿堡立减 4 元", frozenset({"A3"}), valid_until=_days(1)),
    Coupon("K2", "KA2", "满 20 减 3 元", frozenset({"A2", "A4"}), valid_until=_days(30)),
    Coupon("K3", "KA3", "小食满 15 减 3 元", frozenset({"A1", "A2"}), valid_until=_days(2)),
    # 面额 8 元、实测只减 5 元 —— 券印多少和真减多少从来不是一回事
    Coupon("K4", "KA4", "安格斯厚牛堡立减 8 元", frozenset({"A4"}), valid_until=_days(7)),
    # 这家店不卖咖啡：一张看着好好的死券
    Coupon("K5", "KA5", "麦咖啡新品立减 3 元", frozenset({"X9"}), valid_until=_days(5)),
]

# coupon_id -> (门槛分, 实减分)。门槛是演示世界自己定的，接口不会告诉你。
_AUDIT_RULES: dict[str, tuple[int, int]] = {
    "K1": (0, 400),
    "K2": (2000, 300),
    "K3": (1500, 300),
    "K4": (0, 500),  # 印着 8 元，只减 5 元
}

# 体检演示不引入随单购：它跟券叠加会把价格打到 0，
# 那是演示世界的假象，不该混进「这张券值多少钱」的判断里。
AUDIT_WITH_ORDER_OPTIONS = [None]


def audit_oracle(items: list[CartItem], assignment) -> PriceResult:
    """体检演示用求价器：带门槛规则，够不到门槛就一分不减。

    真实接口正是这样 —— 不满足门槛时它不报错、不提示，就是不给你减。
    """
    subtotal = sum(i.effective_price_fen() * i.quantity for i in items)
    discount = 0
    for idx, coupon_idx in assignment.mapping.items():
        if idx >= len(items):
            continue
        cid = AUDIT_COUPONS[coupon_idx].coupon_id
        threshold, off = _AUDIT_RULES.get(cid, (0, 0))
        spend = items[idx].effective_price_fen() * items[idx].quantity
        if spend >= threshold:
            discount += off
    return PriceResult(
        price_fen=max(subtotal - discount, 0),
        original_fen=subtotal,
        discount_fen=discount,
    )


# ------------------------------------------------------- 活动日历演示报文
#
# `campaign` 是唯一一个**只能联网才能看到**的功能 —— 没这条离线演示，
# 路人 clone 下来跑 `demo` 根本不知道还有择日这回事。
#
# 构造器复用 `campaign.build_markdown`（不在这里另写一份）：
# 曾经自检和演示各写了一份，一个月内就漂了（一份「+3 天」一份「+4 天」），
# 而演示那份坏了，129 条自检一条都不会红 —— 因为没人测它。

def campaign_demo_markdown() -> str:
    """活动日历的离线演示报文（结构真实，内容是演示）。"""
    from datetime import date, timedelta

    from .campaign import build_markdown

    t0 = date.today()
    breakfast = ("超值\U0001d7e1.\U0001d7e1元早餐两件套陪你开工啦", "早八的快乐，一堡一咖已就位")
    return build_markdown(
        [
            (
                t0,
                " 今日",
                [
                    breakfast,
                    ("学生专属！49元150天麦金学期卡", "每天低至 0.33 元，一卡在手"),
                ],
            ),
            # 明天跟今天完全重复 —— 用来演示「重复的活动不算值得等」
            (t0 + timedelta(days=1), "", [breakfast]),
            (t0 + timedelta(days=3), "", [("麦麦夜市回归", "限时 12.9 元起，售完即止")]),
        ]
    )
