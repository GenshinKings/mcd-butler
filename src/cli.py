"""麦门管家 CLI。

    python -m src.cli demo        # 无需 Token，离线跑完整演示
    python -m src.cli optimize    # 真实账号：定位门店 → 算价 → 券价寻优
    python -m src.cli replay      # 历史订单复刻 + 价格变动汇报
    python -m src.cli tools       # 列出 MCP 暴露的全部工具
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from .coupon_engine import (
    Assignment,
    Budget,
    BudgetExhausted,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
    WithOrderOption,
    to_fen,
)
from .mcd_client import McpError, load_dotenv
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
    if best.price is None:
        raise BudgetExhausted("没能算出任何价格")
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
        if best.price is None:
            continue

        paid_before = order["realTotalAmount"]
        paid_now = best.price.price_fen
        diff = paid_now - paid_before
        verdict = "贵了" if diff > 0 else ("便宜了" if diff < 0 else "持平")
        mark = "  ⚠️" if diff > 0 else ""

        print(f"  上次实付 ¥{FEN(paid_before)}  →  今天最优到手 ¥{FEN(paid_now)}")
        print(
            f"  同样的单，{verdict} ¥{FEN(abs(diff))}{mark}"
            f"（已用尽 {len(best.mapping)} 张券"
            f" + {best.with_order.label if best.with_order else '无随单购'}）"
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


def _locate(client, city: str, keyword: str):
    """定位门店 + 载入菜单 + 拉券池。三个真实命令都要用这一段。"""
    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not stores:
        return None
    store = stores[0]
    store_code = store["storeCode"]
    meals = client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
    menu = _parse_menu(meals)
    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": store_code, "orderType": 1, "beType": 1})
    )
    return store, store_code, menu, coupons


def run_advise(city: str, keyword: str, budget: int) -> int:
    """一键顾问：今天该怎么点。

    把几件事合成一条命令 —— 大多数人不会去记八个子命令，
    他们只想问一句「我现在该怎么点最划算」。
    """
    from .assets import fetch_my_coupons
    from .mcd_client import McdMcpClient
    from datetime import datetime

    client = McdMcpClient()
    client.initialize()

    # 1. 先看有没有今天就要作废的券 —— 这个比省几块钱更急
    try:
        mine = fetch_my_coupons(client, now=datetime.now())
    except Exception:
        mine = []
    due = [c for c in mine if c.get("due_today")]
    if due:
        print("🚨 今天内到期，不用就作废：")
        for c in due:
            print(f"   - {c['title']}　{c['valid_text']}")
        print()
    elif mine:
        print(f"卡包 {len(mine)} 张，没有今天到期的。\n")

    # 2. 再算最优一单
    ctx = _locate(client, city, keyword)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx
    print(f"门店：{store['storeName']}　可用券 {len(coupons)} 张")

    if not coupons:
        print("\n当前时段这张门店没有可用券。")
        print("提示：很多券是时段券（早餐/夜宵），换个时间再试；")
        print("      或先跑 `python -m src.cli assets --claim` 把能领的领了。")
        return 0

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
            payload["withOrder"] = assignment.with_order.payload
        d = client.data("calculate-price", payload)
        return PriceResult(
            price_fen=int(d.get("price", 0)),
            original_fen=int(d.get("originalPrice", 0)),
            discount_fen=int(d.get("discount", 0)),
            delivery_fen=int(d.get("deliveryPrice", 0)),
            packing_fen=int(d.get("packingPrice", 0)),
        )

    cart = _build_cart(menu, coupons)
    if not cart:
        print("菜单解析为空。")
        return 1

    print("\n推荐这一单：")
    for item in cart:
        note = item.condition_note()
        suffix = f"   ← {note}" if note else ""
        print(f"   - {item.name or item.product_code:<16} ¥{FEN(item.effective_price_fen())}{suffix}")

    shared = Budget(budget)
    optimizer = CouponOptimizer(oracle, max_calls=budget, budget=shared)
    # 裸买基准必须先算：optimize 会把预算吃光，若放后面 try_evaluate 拿到 None 会崩溃
    baseline = optimizer.try_evaluate(cart, Assignment(mapping={}, with_order=None))
    if baseline is None:
        raise BudgetExhausted("预算不足以计算裸买基准，请调大 --budget")
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    if best.price is None:
        raise BudgetExhausted("没能算出任何价格")

    print("\n最优组合：")
    for line in best.describe(cart, coupons):
        print(f"   {line}")
    print(f"\n裸买 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
    print(f"真实省下 ¥{FEN(best.price.saved_vs(baseline.price_fen))}")

    extra = _print_insights(oracle, cart, coupons, best, shared)
    print(f"\n算价接口共调用 {optimizer.budget_used} 次（上限 {budget}）")
    print("\n想换成自己爱吃的：")
    print('  python -m src.cli optimize --items "板烧鸡腿堡三件套,薯条x2"')
    return 0


def run_menu(city: str, keyword: str, filter_kw: str, limit: int) -> int:
    """列出门店菜单。带券的商品会被标出来——那是真正值得点的。"""
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    ctx = _locate(client, city, keyword)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx

    covered: set[str] = set()
    for c in coupons:
        covered |= set(c.product_codes)

    print(f"门店：{store['storeName']}（storeCode={store_code}）")
    print(f"共 {len(menu)} 个品类，{len(coupons)} 张可用券\n")

    shown = 0
    for item in menu:
        if filter_kw and filter_kw not in (item.name or ""):
            continue
        if shown >= limit:
            print(f"\n（只显示前 {limit} 条，用 --limit 调整或加 --filter 缩小范围）")
            break
        price = item.effective_price_fen()
        note = f"  ← {item.condition_note()}" if item.needs_with_order else ""
        mark = " [有券]" if item.product_code in covered else ""
        print(f"  ¥{FEN(price):>6}  {item.name or item.product_code}{mark}{note}")
        shown += 1
    if shown == 0:
        print(f"没有匹配「{filter_kw}」的餐品。")
    return 0


def run_optimize(
    city: str,
    keyword: str,
    budget: int,
    save: bool = True,
    items: list[str] | None = None,
    insights: bool = True,
) -> int:
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()

    print(f"定位门店：{city} / {keyword}")
    ctx = _locate(client, city, keyword)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx
    print(f"命中：{store['storeName']}（{store['address']}）storeCode={store_code}")
    print(f"菜单载入 {len(menu)} 个品类")

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
            payload["withOrder"] = assignment.with_order.payload
        data = client.data("calculate-price", payload)
        return PriceResult(
            price_fen=int(data.get("price", 0)),
            original_fen=int(data.get("originalPrice", 0)),
            discount_fen=int(data.get("discount", 0)),
            delivery_fen=int(data.get("deliveryPrice", 0)),
            packing_fen=int(data.get("packingPrice", 0)),
        )

    if items:
        cart, missed = _pick_cart(menu, items)
        if missed:
            print("\n⚠️ 下列餐品在菜单里没找到，已跳过：")
            for m in missed:
                print(f"  - {m}")
            print("  用 `python -m src.cli menu --filter 关键词` 看看准确的品名。")
    else:
        cart = _build_cart(menu, coupons)

    if not cart:
        print("购物车为空，无法寻优。")
        return 1

    print("\n购物车：")
    for item in cart:
        note = item.condition_note()
        price = item.effective_price_fen()
        suffix = f"   ← {note}" if note else ""
        qty = f"×{item.quantity} " if item.quantity > 1 else ""
        print(f"  - {item.name or item.product_code:<16} {qty}¥{FEN(price)}{suffix}")

    shared = Budget(budget)
    optimizer = CouponOptimizer(oracle, max_calls=budget, budget=shared)
    # 裸买基准先算（占 1 次调用），否则 optimize 吃光预算后这里拿到 None 会崩溃
    baseline = optimizer.try_evaluate(cart, Assignment(mapping={}, with_order=None))
    if baseline is None:
        raise BudgetExhausted("预算不足以计算裸买基准，请调大 --budget")
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    if best.price is None:
        raise BudgetExhausted("没能算出任何价格")
    print("\n最优组合：")
    for line in best.describe(cart, coupons):
        print(f"  {line}")
    # 真实节省必须以「裸买价」为基准。
    # 不能用接口返回的 discount：随单购会把卡费计入 originalPrice
    # （实测 97.50 → 107.40，多出的 9.90 就是卡费），导致 discount 虚高。
    saved = baseline.price_fen - best.price.price_fen
    print(f"\n裸买价 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
    print(f"真实省下 ¥{FEN(saved)}（接口 discount 报 ¥{FEN(best.price.discount_fen)}，含卡费口径）")

    extra = 0
    if insights:
        extra = _print_insights(oracle, cart, coupons, best, shared)
    print(f"\n算价接口共调用 {optimizer.budget_used} 次（上限 {budget}）")

    if save:
        from .snapshot import entry_from_cart, save as save_snapshot

        label = ""
        if isinstance(best.with_order, dict):
            label = best.with_order.label
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
            paid_fen = to_fen(order.get("realTotalAmount"))
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
            payload["withOrder"] = assignment.with_order.payload
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
    if best.price is None:
        raise BudgetExhausted("没能算出任何价格")
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
        label = best.with_order.label
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

    print(f"\n热量预算 {kcal} kcal 内推荐搭配（按蛋白质降序）：")
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


def _print_insights(oracle, cart, coupons, best, budget) -> int:
    """打印省钱洞察，返回本次洞察消耗的接口调用次数。

    `budget` 是**全局共享**的预算对象，不是数字 ——
    洞察里每一路都会新建求解器，只有共用同一个计数器，
    才能保证「用户设了 --budget 40，就绝不会打出第 41 次」。
    """
    from . import insights as ins

    if best.price is None:
        return 0
    whole = best.price.price_fen
    spent_before = budget.used if hasattr(budget, "used") else 0
    counter = [0]

    print("\n" + "-" * 62)
    print("省钱洞察")
    print("-" * 62)

    # --- 1. 每件的真实成本
    marginals = ins.marginal_costs(
        oracle,
        cart,
        coupons,
        _with_order_options,
        baseline_fen=whole,
        per_call_budget=max(budget.remaining // max(len(cart), 1), 4),
        call_counter=counter,
        budget=budget,
    )
    if marginals:
        print("\n  这一单里，每件东西实际花了你多少：")
        for m in marginals:
            name = m.item.name or m.item.product_code
            qty = f"×{m.item.quantity}" if m.item.quantity > 1 else ""
            if m.delta_fen > 0:
                tag = f"  省 ¥{FEN(m.delta_fen)}"
            elif m.delta_fen < 0:
                tag = "  ⚠️ 反而更贵"
            else:
                tag = "  没享受到优惠"
            print(
                f"    {name:<18}{qty:<4} 标价 ¥{FEN(m.list_fen):>6}"
                f"  →  实际 ¥{FEN(m.marginal_fen):>6}{tag}"
            )

    # --- 2. 挂了等于没挂的券
    redundant = ins.redundant_coupons(oracle, cart, coupons, best, budget=budget)
    counter[0] += len(best.mapping)
    if redundant:
        print("\n  ⚠️ 以下券挂了等于没挂（摘掉它总价一分不变）：")
        for item, coupon in redundant:
            print(f"    - {coupon.title}  ← 挂在「{item.name or item.product_code}」")
        print("    常见成因：随单购已经把这件降到券面价了。")

    # --- 3. 拆单建议
    splits = ins.split_suggestions(
        oracle,
        cart,
        coupons,
        _with_order_options,
        whole_fen=whole,
        per_call_budget=max(budget.remaining // max(len(cart), 1), 4),
        call_counter=counter,
        budget=budget,
    )
    if splits:
        print("\n  💡 分开下单更便宜：")
        for item, total, gain in splits:
            print(
                f"    - 把「{item.name or item.product_code}」单独下一单"
                f"  →  两单合计 ¥{FEN(total)}，省 ¥{FEN(gain)}"
            )

    if not marginals and not redundant and not splits:
        print("\n  （这张单的券结构简单，没有额外的省钱空间）")
    if budget.remaining <= 0:
        print("\n  （算价调用已到 --budget 上限，洞察到此为止；调大 --budget 可看完整分析）")
    return (budget.used - spent_before) if hasattr(budget, "used") else counter[0]


def _parse_menu(raw: dict):
    """把 query-meals 的 meals 映射摊平成 CartItem 列表。

    关键：`currentPrice` 可能是「随单购麦金卡优惠」这类条件价，
    必须把 originalPrice 一起留下来，否则展示会骗人。
    """
    items: list[CartItem] = []
    detail = raw.get("meals", {}) or {}

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


def _pick_cart(menu: list[CartItem], wants: list[str]) -> tuple[list[CartItem], list[str]]:
    """按用户给的餐品名挑购物车。

    支持两种写法：
        --items 巨无霸,中份薯条
        --items "巨无霸x2,中份薯条"      （数量）

    匹配规则：先按商品编码精确匹配，再按名称精确匹配，最后退化成子串包含。
    匹配不上就原样报回来，绝不拿一个不相干的商品凑数。
    """
    by_code = {i.product_code: i for i in menu}
    by_name = {(i.name or "").strip(): i for i in menu}

    cart: list[CartItem] = []
    missed: list[str] = []
    # 先收集「要什么、各要几份」，最后再一次性构造 ——
    # CartItem 是 frozen dataclass，创建之后改不动 quantity。
    qty_by_code: dict[str, int] = {}
    proto: dict[str, CartItem] = {}

    for raw in wants:
        token = (raw or "").strip()
        if not token:
            continue
        qty = 1
        m = re.search(r"[xX*×]\s*(\d+)\s*$", token)
        if m:
            qty = max(int(m.group(1)), 1)
            token = token[: m.start()].strip()
        if not token:
            continue

        item = by_code.get(token) or by_name.get(token)
        if item is None:
            hits = [i for i in menu if token and token in (i.name or "")]
            item = hits[0] if hits else None
        if item is None:
            missed.append(raw)
            continue

        # 同一个商品写了两次就合并数量，而不是加两行
        qty_by_code[item.product_code] = qty_by_code.get(item.product_code, 0) + qty
        proto[item.product_code] = item

    for code, qty in qty_by_code.items():
        base = proto[code]
        cart.append(CartItem(**{**base.__dict__, "quantity": qty}))
    return cart, missed


def _with_order_options(cart: list[CartItem]) -> list:
    """随单购候选档位。

    菜单里带 canWithOrder 的商品，其标价往往挂在「随单购麦金卡/早餐卡」下，
    所以「要不要随单购」本身就是一个需要试算的决策维度。
    """
    options: list[WithOrderOption | None] = [None]
    seen: set[str] = set()
    for item in cart:
        if not item.needs_with_order or not item.membership_code:
            continue
        if item.membership_code in seen:
            continue
        seen.add(item.membership_code)
        # payload 里只放官方字段；展示文案单独放 label。
        # 混在一起的话，官方一旦做严格 schema 校验，整个算价请求都会被拒。
        options.append(
            WithOrderOption(
                payload={
                    "membershipCode": item.membership_code,
                    "membershipSpecId": item.membership_spec_id,
                },
                label=f"{item.discount_type}（membershipCode={item.membership_code}）",
            )
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
    # 先把 .env 读进来，默认门店才能用得上（也避免"照着 README 做却报缺 Token"）
    load_dotenv()
    city_default = os.environ.get("MCD_DEFAULT_CITY", "上海")
    kw_default = os.environ.get("MCD_DEFAULT_KEYWORD", "人民广场")

    parser = argparse.ArgumentParser(prog="mcd-butler", description="麦门管家")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="离线演示，无需 Token")
    sub.add_parser("tools", help="列出 MCP 工具")
    sub.add_parser("selftest", help="离线自检解析器与引擎，无需 Token")

    p_adv = sub.add_parser("advise", help="一键顾问：今天该怎么点最划算")
    p_adv.add_argument("--city", default=city_default)
    p_adv.add_argument("--keyword", default=kw_default)
    p_adv.add_argument("--budget", type=int, default=40, help="算价接口调用上限")

    p_assets = sub.add_parser("assets", help="麦门资产周报（默认只读）")
    p_assets.add_argument(
        "--claim", action="store_true", help="真的去领券（会写入你的真实会员账号）"
    )

    p_menu = sub.add_parser("menu", help="列出门店菜单")
    p_menu.add_argument("--city", default=city_default)
    p_menu.add_argument("--keyword", default=kw_default)
    p_menu.add_argument("--filter", default="", help="只显示名称包含该关键词的餐品")
    p_menu.add_argument("--limit", type=int, default=40, help="最多显示多少条")

    p_opt = sub.add_parser("optimize", help="真实账号券价寻优")
    p_opt.add_argument("--city", default=city_default)
    p_opt.add_argument("--keyword", default=kw_default)
    p_opt.add_argument("--budget", type=int, default=40, help="算价接口调用上限")
    p_opt.add_argument(
        "--items",
        default="",
        help="指定想吃什么，逗号分隔，可带数量：\"巨无霸x2,中份薯条\"；留空则自动挑选",
    )
    p_opt.add_argument(
        "--no-save", action="store_true", help="不存本地快照（默认会存）"
    )
    p_opt.add_argument(
        "--no-insights",
        action="store_true",
        help="跳过省钱洞察（会少算若干次价，快一些）",
    )

    p_rep = sub.add_parser("replay", help="历史订单复刻与价格变动")
    p_rep.add_argument("--store", default=None, help="指定门店编码，默认用最近快照")
    p_rep.add_argument("--list", action="store_true", help="只列出本地快照")

    p_nut = sub.add_parser("nutrition", help="热量预算搭配")
    p_nut.add_argument("--city", default=city_default)
    p_nut.add_argument("--keyword", default=kw_default)
    p_nut.add_argument("--kcal", type=int, default=600, help="热量上限（千卡）")
    p_nut.add_argument("--size", type=int, default=3, help="最多几件")

    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except BudgetExhausted as exc:
        # 预算太小，连一次贪心都没跑完。
        # 这时候不该编一个价格出来，明确告诉用户怎么改。
        print(f"\n{exc}")
        print("算价调用预算太小，没算出结果。把 --budget 调大再试，例如 --budget 80。")
        return 3
    except McpError as exc:
        # Token 失效、限流、网络错误都走这里。
        # 直接抛 traceback 对使用者毫无帮助，给一句能照着做的提示。
        print(f"\n接口调用失败：{exc}")
        print("常见原因：Token 失效（到 https://open.mcd.cn/mcp 重新激活）、")
        print("          网络不通（本机需要代理时请设置 https_proxy）、")
        print("          或触发限流（每 Token 600 次/分钟）。")
        return 2


def _dispatch(args) -> int:
    if args.cmd == "demo":
        return run_demo()
    if args.cmd == "tools":
        return run_tools()
    if args.cmd == "selftest":
        from .selftest import run as run_selftest

        print("离线自检（无需 Token）\n")
        return run_selftest()
    if args.cmd == "advise":
        return run_advise(args.city, args.keyword, args.budget)
    if args.cmd == "menu":
        return run_menu(args.city, args.keyword, args.filter, args.limit)
    if args.cmd == "assets":
        return run_assets(args.claim)
    if args.cmd == "replay":
        return run_replay(args.store, args.list)
    if args.cmd == "nutrition":
        return run_nutrition(args.city, args.keyword, args.kcal, args.size)
    if args.cmd == "optimize":
        picked = [s for s in (args.items or "").split(",") if s.strip()]
        return run_optimize(
            args.city,
            args.keyword,
            args.budget,
            save=not args.no_save,
            items=picked,
            insights=not args.no_insights,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
