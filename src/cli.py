"""麦门管家 CLI。

    python -m src.cli demo        # 无需 Token，离线跑完整演示
    python -m src.cli optimize    # 真实账号：定位门店 → 算价 → 券价寻优
    python -m src.cli replay      # 历史订单复刻 + 价格变动汇报
    python -m src.cli tools       # 列出 MCP 暴露的全部工具
"""

from __future__ import annotations

import argparse
import os
import sys

from .coupon_engine import Assignment, CartItem, Coupon, CouponOptimizer, PriceResult
from . import demo_data as demo


FEN = lambda f: f"{f / 100:.2f}"


# ==================================================================== demo

def run_demo() -> int:
    print("=" * 62)
    print("麦门管家 · 离线演示（无需 Token）")
    print("=" * 62)

    cart = [
        demo.DEMO_MENU[0],  # 巨无霸
        demo.DEMO_MENU[2],  # 中份薯条
        demo.DEMO_MENU[4],  # 中杯可乐
        demo.DEMO_MENU[5],  # 麦旋风
    ]
    coupons = demo.DEMO_COUPONS

    print("\n购物车：")
    for item in cart:
        print(f"  - {item.name:<10} ¥{FEN(item.unit_price_fen)}")
    subtotal = sum(i.unit_price_fen for i in cart)
    print(f"  原价合计：¥{FEN(subtotal)}")

    print(f"\n可用券 {len(coupons)} 张：")
    for c in coupons:
        scope = "、".join(sorted(c.product_codes)) or "通用"
        print(f"  - {c.title:<20} 适用：{scope}")

    optimizer = CouponOptimizer(demo.demo_oracle, max_calls=60)
    best = optimizer.optimize(cart, coupons, demo.DEMO_WITH_ORDER_OPTIONS)

    print("\n" + "-" * 62)
    print("寻优结果")
    print("-" * 62)
    for line in best.describe(cart, coupons):
        print(f"  {line}")
    assert best.price is not None
    print(f"\n  原价 ¥{FEN(best.price.original_fen)} → 到手 ¥{FEN(best.price.price_fen)}")
    print(f"  省下 ¥{FEN(best.price.saved_fen)}    共调用算价接口 {optimizer.calls} 次")

    # 对比：一张券都不用
    naive = Assignment(mapping={}, with_order=None)
    naive.price = optimizer.oracle(cart, naive)
    gap = naive.price.price_fen - best.price.price_fen
    print(f"\n  不用券直接买：¥{FEN(naive.price.price_fen)}（差 ¥{FEN(gap)}）")

    print("\n" + "=" * 62)
    print("历史订单复刻（replay）")
    print("=" * 62)
    _demo_replay()
    return 0


def _demo_replay() -> None:
    """复刻历史订单，并给出「今天再买一次」的到手价对比。

    对比口径必须是同为到手价：
        上次实付（含券）  vs  今天用引擎算出的最优到手价（含券）
    拿「上次实付」比「今天菜单原价」是苹果比橘子，会被一眼看穿。
    """
    optimizer = CouponOptimizer(demo.demo_oracle, max_calls=60)

    for order in demo.DEMO_ORDER_HISTORY:
        date = order["createTime"][:10]
        print(f"\n{order['createTime']}  {order['storeName']}")

        cart: list[CartItem] = []
        for p in order["products"]:
            cur = demo.DEMO_MENU_BY_CODE[p["productCode"]]
            cart.append(
                CartItem(
                    product_code=cur.product_code,
                    name=cur.name,
                    quantity=p["quantity"],
                    unit_price_fen=cur.unit_price_fen,
                )
            )
            snap = demo.DEMO_PRICE_SNAPSHOT.get(p["productCode"], {})
            then = snap.get(date)
            flag = ""
            if then is not None and cur.unit_price_fen != then:
                flag = f"   单价 {(cur.unit_price_fen - then) / 100:+.2f}"
            print(f"  - {cur.name} ×{p['quantity']}{flag}")

        # 今天的最优到手价
        best = optimizer.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
        assert best.price is not None

        paid_before = order["realTotalAmount"]
        paid_now = best.price.price_fen
        diff = paid_now - paid_before
        verdict = "贵了" if diff > 0 else ("便宜了" if diff < 0 else "持平")
        mark = "  ⚠️" if diff > 0 else ""

        print(f"  上次实付 ¥{FEN(paid_before)}  →  今天最优到手 ¥{FEN(paid_now)}")
        print(
            f"  同样的单，{verdict} ¥{FEN(abs(diff))}{mark}"
            f"（已用尽 {len(best.mapping)} 张券 + {best.with_order or '无随单购'}）"
        )
    print("\n（演示数据，不代表真实价格）")


# ==================================================================== real

def run_tools() -> int:
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    tools = client.list_tools()
    print(f"MCP 暴露 {len(tools)} 个工具：\n")
    for t in sorted(tools, key=lambda x: x["name"]):
        print(f"  {t['name']}")
    return 0


def run_optimize(city: str, keyword: str, budget: int) -> int:
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()

    print(f"定位门店：{city} / {keyword}")
    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not stores:
        print("未找到门店，请换一个关键词。")
        return 1
    store = stores[0]
    store_code = store["storeCode"]
    print(f"命中：{store['storeName']}（{store['address']}）storeCode={store_code}")

    meals = client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
    menu = _parse_menu(meals)
    print(f"菜单载入 {len(menu)} 个品类")

    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": store_code, "orderType": 1, "beType": 1})
    )
    print(f"门店可用券 {len(coupons)} 张")
    if not coupons:
        print("提示：卡包为空时先执行领券，或改用 --demo 查看离线演示。")

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
            "orderType": 1,
            "beType": 1,
            "items": payload_items,
        }
        if isinstance(assignment.with_order, dict):
            payload["withOrder"] = assignment.with_order
        data = client.data("calculate-price", payload)
        return PriceResult(
            price_fen=int(data.get("price", 0)),
            original_fen=int(data.get("originalPrice", 0)),
            discount_fen=int(data.get("discount", 0)),
            delivery_fen=int(data.get("deliveryPrice", 0)),
            packing_fen=int(data.get("packingPrice", 0)),
        )

    cart = _build_cart(menu, coupons)
    if not cart:
        print("菜单解析为空，暂无法寻优。")
        return 1

    print("\n购物车（优先纳入券覆盖到的商品）：")
    for item in cart:
        note = item.condition_note()
        price = item.effective_price_fen()
        suffix = f"   ← {note}" if note else ""
        print(f"  - {item.name or item.product_code:<16} ¥{FEN(price)}{suffix}")

    optimizer = CouponOptimizer(oracle, max_calls=budget)
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    print("\n最优组合：")
    for line in best.describe(cart, coupons):
        print(f"  {line}")
    assert best.price is not None

    # 真实节省必须以「裸买价」为基准。
    # 不能用接口返回的 discount：随单购会把卡费计入 originalPrice
    # （实测 97.50 → 107.40，多出的 9.90 就是卡费），导致 discount 虚高。
    baseline = optimizer.oracle(cart, Assignment(mapping={}, with_order=None))
    saved = baseline.price_fen - best.price.price_fen
    print(f"\n裸买价 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
    print(f"真实省下 ¥{FEN(saved)}（接口 discount 报 ¥{FEN(best.price.discount_fen)}，含卡费口径）")
    print(f"算价接口共调用 {optimizer.calls} 次")
    return 0


def _parse_menu(raw: dict):
    """把 query-meals 的 meals 映射摊平成 CartItem 列表。

    关键：`currentPrice` 可能是「随单购麦金卡优惠」这类条件价，
    必须把 originalPrice 一起留下来，否则展示会骗人。
    """
    items: list[CartItem] = []
    detail = raw.get("meals", {}) or {}

    def to_fen(value) -> int:
        try:
            return int(float(value or 0) * 100)
        except (TypeError, ValueError):
            return 0

    for code, info in detail.items():
        if not isinstance(info, dict):
            continue
        discount_type = info.get("discountType") or ""
        needs = bool(info.get("canWithOrder")) and "随单购" in discount_type
        with_order = info.get("withOrder") or {}
        items.append(
            CartItem(
                product_code=code,
                name=info.get("name", ""),
                quantity=1,
                unit_price_fen=to_fen(info.get("currentPrice")),
                list_price_fen=to_fen(info.get("originalPrice")),
                discount_type=discount_type,
                needs_with_order=needs,
                membership_code=str(with_order.get("membershipCode", "")),
                membership_spec_id=str(with_order.get("specId", "")),
            )
        )
    return items


def _build_cart(menu: list[CartItem], coupons: list[Coupon], size: int = 4) -> list[CartItem]:
    """构造演示购物车：优先纳入被券覆盖的商品。

    随机取前 N 个餐品的话，购物车大概率一张券都用不上，
    引擎就退化成「啥也没优化」，演示没有说服力。
    """
    covered = {code for c in coupons for code in c.product_codes}
    picked: list[CartItem] = []
    seen: set[str] = set()

    for item in menu:
        if len(picked) >= size:
            break
        if item.product_code in covered and item.product_code not in seen:
            picked.append(item)
            seen.add(item.product_code)

    for item in menu:
        if len(picked) >= size:
            break
        if item.product_code not in seen:
            picked.append(item)
            seen.add(item.product_code)
    return picked


def _with_order_options(cart: list[CartItem]) -> list:
    """随单购候选档位。

    菜单里带 canWithOrder 的商品，其标价往往挂在「随单购麦金卡/早餐卡」下，
    所以「要不要随单购」本身就是一个需要试算的决策维度。
    """
    options: list = [None]
    seen: set[str] = set()
    for item in cart:
        if not item.needs_with_order or not item.membership_code:
            continue
        if item.membership_code in seen:
            continue
        seen.add(item.membership_code)
        options.append(
            {
                "membershipCode": item.membership_code,
                "membershipSpecId": item.membership_spec_id,
                "_label": f"{item.discount_type}（membershipCode={item.membership_code}）",
            }
        )
    return options


def _parse_coupons(raw) -> list[Coupon]:
    if not raw:
        return []
    out: list[Coupon] = []
    for entry in raw:
        products = entry.get("products") or []
        codes = {p.get("productCode") for p in products if p.get("productCode")}
        out.append(
            Coupon(
                coupon_id=str(entry.get("couponId", "")),
                coupon_code=str(entry.get("couponCode", "")),
                title=entry.get("title", ""),
                product_codes=frozenset(codes),
                valid_until=str(entry.get("tradeDateTime", "")),
            )
        )
    return out


# ==================================================================== main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcd-butler", description="麦门管家")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="离线演示，无需 Token")
    sub.add_parser("tools", help="列出 MCP 工具")

    p_opt = sub.add_parser("optimize", help="真实账号券价寻优")
    p_opt.add_argument("--city", default="上海")
    p_opt.add_argument("--keyword", default="人民广场")
    p_opt.add_argument("--budget", type=int, default=40, help="算价接口调用上限")

    sub.add_parser("replay", help="历史订单复刻与价格变动")

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return run_demo()
    if args.cmd == "tools":
        return run_tools()
    if args.cmd == "replay":
        print("（replay 真实模式开发中，先看离线演示）\n")
        _demo_replay()
        return 0
    if args.cmd == "optimize":
        return run_optimize(args.city, args.keyword, args.budget)
    return 1


if __name__ == "__main__":
    sys.exit(main())
