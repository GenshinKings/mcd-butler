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


def run_assets(claim: bool) -> int:
    """资产周报。默认只读；--claim 才领券。"""
    from .assets import build_report
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    print(build_report(client, claim=claim))
    return 0


def run_optimize(city: str, keyword: str, budget: int, save: bool = True) -> int:
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

    if save:
        from .snapshot import entry_from_cart, save as save_snapshot

        label = ""
        if isinstance(best.with_order, dict):
            label = str(best.with_order.get("_label", ""))
        path = save_snapshot(
            entry_from_cart(
                cart=cart,
                paid_fen=best.price.price_fen,
                baseline_fen=baseline.price_fen,
                discount_fen=best.price.discount_fen,
                store_code=store_code,
                store_name=store.get("storeName", ""),
                city=city,
                keyword=keyword,
                coupon_titles=[coupons[ci].title for ci in best.mapping.values()],
                with_order_label=label,
            )
        )
        print(f"\n已存快照 → {path}（下次可用 `replay` 对比这单的价格变动）")
    return 0


def run_replay(store_code: str | None, list_only: bool) -> int:
    """历史订单复刻：把这单按今天的菜单和券重算一遍，报价格变动。

    数据源有两条，按优先级：
    1. 真实历史订单（order-list）—— 账号下过单才有，很多账号是空的；
    2. 本地快照 —— optimize 跑完自动存的「上次算过的那单」。

    比较口径统一为「上次实付」vs「今天同款购物车的最优实付」。
    不能直接比今天菜单的标价：那等于拿「用券后的价」比「不用的价」，
    是在骗自己。
    """
    from .mcd_client import McdMcpClient
    from . import snapshot as snap

    if list_only:
        entries = snap.load_all()
        if not entries:
            print("还没有快照。先跑一次 `python -m src.cli optimize` 生成。")
            return 1
        print(f"本地快照 {len(entries)} 条：")
        for i, e in enumerate(entries, 1):
            print(f"  {i}. {snap.describe(e)}")
        return 0

    client = McdMcpClient()
    client.initialize()

    source = ""
    paid_fen: int | None = None
    want: list[tuple[str, int]] = []  # (productCode, quantity)
    target_store = store_code

    # ---- 1. 真实历史订单
    try:
        data = client.data("order-list", {}) or {}
        orders = (data or {}).get("list") or []
    except Exception:
        orders = []

    if orders:
        order = orders[0]
        target_store = target_store or str(order.get("storeCode", ""))
        try:
            paid_fen = int(float(order.get("realTotalAmount") or 0) * 100)
        except (TypeError, ValueError):
            paid_fen = None
        for p in order.get("orderProductList") or []:
            code = str(p.get("productCode", ""))
            if code:
                want.append((code, int(p.get("quantity") or 1)))
        source = f"历史订单 {order.get('orderId', '')} @ {order.get('createTime', '')}"

    # ---- 2. 退回本地快照
    if not want:
        entry = snap.latest(target_store)
        if entry is None:
            print("既没有历史订单，也没有本地快照。")
            print("先跑 `python -m src.cli optimize --city 上海 --keyword 人民广场` 生成一单。")
            print("（离线演示：`python -m src.cli demo`）")
            return 1
        target_store = entry.get("store_code", "")
        paid_fen = entry.get("paid_fen")
        want = [(i["code"], int(i.get("qty") or 1)) for i in entry.get("items") or []]
        source = f"本地快照 {entry.get('saved_at', '')}"

    print(f"复刻来源：{source}")
    print(f"目标门店：{target_store}")

    meals = client.data("query-meals", {"storeCode": target_store, "orderType": 1, "beType": 1})
    menu = _parse_menu(meals)
    by_code = {i.product_code: i for i in menu}
    print(f"今日菜单 {len(menu)} 个品类")

    cart: list[CartItem] = []
    missing: list[str] = []
    for code, qty in want:
        item = by_code.get(code)
        if item is None:
            missing.append(code)
            continue
        cart.append(CartItem(**{**item.__dict__, "quantity": qty}))
    if not cart:
        print("这单里的商品今天一个都点不到了，无法对比。")
        return 1
    if missing:
        print(f"⚠️ {len(missing)} 个商品已从今日菜单下架：{', '.join(missing)}")

    print("\n复刻购物车：")
    for item in cart:
        print(f"  - {item.name or item.product_code:<16} ×{item.quantity}  ¥{FEN(item.effective_price_fen())}")

    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": target_store, "orderType": 1, "beType": 1})
    )
    print(f"\n今日可用券 {len(coupons)} 张")

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
            "storeCode": target_store,
            "orderType": 1,
            "beType": 1,
            "items": payload_items,
        }
        if isinstance(assignment.with_order, dict):
            payload["withOrder"] = assignment.with_order
        d = client.data("calculate-price", payload)
        return PriceResult(
            price_fen=int(d.get("price", 0)),
            original_fen=int(d.get("originalPrice", 0)),
            discount_fen=int(d.get("discount", 0)),
            delivery_fen=int(d.get("deliveryPrice", 0)),
            packing_fen=int(d.get("packingPrice", 0)),
        )

    optimizer = CouponOptimizer(oracle, max_calls=40)
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    assert best.price is not None
    print("\n今日最优组合：")
    for line in best.describe(cart, coupons):
        print(f"  {line}")

    print("\n" + "-" * 62)
    if paid_fen is None:
        print(f"上次实付金额未知，今日最优价 ¥{FEN(best.price.price_fen)}")
    else:
        diff = best.price.price_fen - paid_fen
        verdict = "涨了" if diff > 0 else ("降了" if diff < 0 else "持平")
        mark = " 🔺" if diff > 0 else (" 🔻" if diff < 0 else "")
        print(f"上次实付 ¥{FEN(paid_fen)}  →  今日最优 ¥{FEN(best.price.price_fen)}")
        print(f"同样的单，{verdict} ¥{FEN(abs(diff))}{mark}")
    print("-" * 62)

    # 存一份今天的快照，下次就能比
    from .snapshot import entry_from_cart, save as save_snapshot

    label = ""
    if isinstance(best.with_order, dict):
        label = str(best.with_order.get("_label", ""))
    save_snapshot(
        entry_from_cart(
            cart=cart,
            paid_fen=best.price.price_fen,
            baseline_fen=best.price.original_fen,
            discount_fen=best.price.discount_fen,
            store_code=target_store,
            store_name="",
            city="",
            keyword="",
            coupon_titles=[coupons[ci].title for ci in best.mapping.values()],
            with_order_label=label,
        )
    )
    return 0


def run_nutrition(city: str, keyword: str, kcal: int, size: int) -> int:
    """热量预算搭配：给定热量上限，挑出蛋白质最高的合法组合。"""
    from .mcd_client import McdMcpClient, extract_payload
    from . import nutrition as nut

    client = McdMcpClient()
    client.initialize()

    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not stores:
        print("未找到门店，请换一个关键词。")
        return 1
    store = stores[0]
    store_code = store["storeCode"]
    print(f"门店：{store['storeName']}（storeCode={store_code}）")

    menu = _parse_menu(
        client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
    )
    print(f"菜单 {len(menu)} 个品类")

    raw = extract_payload(client.call("list-nutrition-foods", {}))
    table = nut.parse_nutrition((raw or {}).get("data"))
    print(f"营养表 {len(table)} 条（接口返回的是字符串，需自行解析）")

    pairs = nut.match_menu(menu, table)
    print(f"能对上营养数据的餐品 {len(pairs)} 个")

    if not pairs:
        print("菜单与营养表一个都对不上，无法给出建议。")
        return 1

    combos = nut.plan(pairs, kcal_budget=kcal, size=size)
    if not combos:
        print(f"在 {kcal} kcal 预算内凑不出任何组合，试试提高预算。")
        return 1

    print(f"\n热量预算 {kcal} kcal 内的最优搭配（按蛋白质降序）：")
    for i, combo in enumerate(combos, 1):
        s = nut.summarize(combo)
        names = " + ".join(p[0].name or p[0].product_code for p in combo)
        print(f"\n  {i}. {names}")
        print(
            f"     {s['kcal']} kcal　蛋白 {s['protein']}g　脂肪 {s['fat']}g　"
            f"碳水 {s['carb']}g　钠 {s['sodium']}mg"
        )
        print(f"     合计 ¥{FEN(s['price_fen'])}")
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

    p_assets = sub.add_parser("assets", help="麦门资产周报（默认只读）")
    p_assets.add_argument(
        "--claim", action="store_true", help="真的去领券（会写入你的真实会员账号）"
    )

    p_opt = sub.add_parser("optimize", help="真实账号券价寻优")
    p_opt.add_argument("--city", default="上海")
    p_opt.add_argument("--keyword", default="人民广场")
    p_opt.add_argument("--budget", type=int, default=40, help="算价接口调用上限")
    p_opt.add_argument(
        "--no-save", action="store_true", help="不存本地快照（默认会存）"
    )

    p_rep = sub.add_parser("replay", help="历史订单复刻与价格变动")
    p_rep.add_argument("--store", default=None, help="指定门店编码，默认用最近快照")
    p_rep.add_argument("--list", action="store_true", help="只列出本地快照")

    p_nut = sub.add_parser("nutrition", help="热量预算搭配")
    p_nut.add_argument("--city", default="上海")
    p_nut.add_argument("--keyword", default="人民广场")
    p_nut.add_argument("--kcal", type=int, default=600, help="热量上限（千卡）")
    p_nut.add_argument("--size", type=int, default=3, help="最多几件")

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return run_demo()
    if args.cmd == "tools":
        return run_tools()
    if args.cmd == "assets":
        return run_assets(args.claim)
    if args.cmd == "replay":
        return run_replay(args.store, args.list)
    if args.cmd == "nutrition":
        return run_nutrition(args.city, args.keyword, args.kcal, args.size)
    if args.cmd == "optimize":
        return run_optimize(args.city, args.keyword, args.budget, save=not args.no_save)
    return 1


if __name__ == "__main__":
    sys.exit(main())
