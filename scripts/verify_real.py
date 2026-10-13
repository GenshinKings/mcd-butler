"""拿真实接口审自己：数据对不对、钱算错没、给的是不是最优。

和 `bench.py` 的区别：bench 用的是**假**算价器（我们自己写的模型，永远自洽），
这里用的是**麦当劳真实接口**。假算价器测不出「接口的行为和我们想的不一样」——
而那恰恰是最会让人多付钱的一类错。

只做只读调用（query-* / calculate-price）：不下单、不领券、不改任何状态。

用法：
    python scripts/verify_real.py                 # 默认门店
    python scripts/verify_real.py --store 1450713
    python scripts/verify_real.py --skip-brute    # 只想看数据体检
"""

from __future__ import annotations

import argparse
import itertools
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

from src.cli import (  # noqa: E402
    _build_cart,
    _norm_name,
    _parse_coupons,
    _parse_menu,
    _with_order_options,
)
from src.coupon_engine import Assignment, CouponOptimizer, suggest_budget  # noqa: E402
from src.mcd_client import McdMcpClient, load_dotenv  # noqa: E402
from src.oracle import make_price_oracle, real_calls  # noqa: E402

PROBLEMS: list[str] = []
# 「接口就是这个口径、而本项目已经绕开」的事实 —— 不是缺陷，但必须记下来，
# 否则下一个人读到接口文档会以为 discount 就是省下的钱。
FACTS: list[str] = []


def flag(msg: str) -> None:
    PROBLEMS.append(msg)
    print(f"   !! {msg}")


def note(msg: str) -> None:
    FACTS.append(msg)
    print(f"   ·  {msg}")


def ok(msg: str) -> None:
    print(f"   ok {msg}")


def section(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


# ────────────────────────────────────────────────────────────────
# 一、用户自己的数据：菜单 / 券 / 名字
# ────────────────────────────────────────────────────────────────
def audit_data(menu, coupons) -> None:
    section("一、数据体检：菜单与券的接口数据本身可不可信")

    # (1) 归一化后重名 —— 用户 --items 打一个名字，可能命中另一个商品
    name_map: dict[str, list[str]] = {}
    for it in menu:
        name_map.setdefault(_norm_name(it.name or ""), []).append(it.product_code)
    dup = {n: c for n, c in name_map.items() if len(c) > 1}
    if dup:
        flag(f"菜单里有 {len(dup)} 个重名（同一个名字对应多个商品码）：用户点单可能被算成另一个商品")
        for n, codes in list(dup.items())[:5]:
            print(f"        「{n}」-> {codes}")
    else:
        ok("菜单里没有重名，按名字点单不会指错商品")

    # (2) 条件价（标价 < 原价，普通顾客付不出来）
    cond = [it for it in menu if 0 < it.unit_price_fen < it.list_price_fen]
    print(f"\n   · 条件价商品 {len(cond)}/{len(menu)} 件（标价 < 原价，需随单购/会员价才拿得到）")
    for it in cond[:3]:
        print(
            f"        {it.name}: 标价 ¥{it.unit_price_fen / 100:.2f} / "
            f"原价 ¥{it.list_price_fen / 100:.2f}"
        )
    if not cond:
        print("        （本店菜单本次没有条件价商品）")

    # (3) 券的适用商品码能不能在菜单里找到
    codes = {it.product_code for it in menu}
    total_links = 0
    dead_links = 0
    empty_cps = 0
    for c in coupons:
        if not c.product_codes:
            empty_cps += 1
            continue
        for pc in c.product_codes:
            total_links += 1
            if pc not in codes:
                dead_links += 1
    print(f"\n   · 券 {len(coupons)} 张，其中 {empty_cps} 张没写适用商品（视为全场通用）")
    print(f"   · 券→商品的链接 {total_links} 条，其中 {dead_links} 条的商品码在菜单里找不到")
    if total_links and dead_links / total_links > 0.5:
        flag("超过一半的券→商品链接对不上菜单：券的适用性判定不可信，可能漏掉真实可用的挂法")
    elif dead_links:
        ok("少数链接对不上（可能是套餐/时段商品），不影响已匹配的那些")

    # (4) 券名 → 商品码 的回退映射有没有歧义
    by_coupon_name: dict[str, set[str]] = {}
    for c in coupons:
        hit = [pc for pc in c.product_codes if pc in codes]
        if not hit:
            continue
        for pn in c.product_names:
            n = _norm_name(pn)
            if n:
                by_coupon_name.setdefault(n, set()).update(hit)
    amb = {n: s for n, s in by_coupon_name.items() if len(s) > 1}
    if amb:
        flag(f"{len(amb)} 个券名指向多个商品码：照券名点单只会命中第一个，可能是错的那个")
        for n, s in list(amb.items())[:5]:
            print(f"        「{n}」-> {sorted(s)}")
    else:
        ok("券名回退映射没有歧义")

    # (5) 金额量纲
    bad = [
        it.name
        for it in menu
        if not isinstance(it.unit_price_fen, int) or it.unit_price_fen < 0
    ]
    if bad:
        flag(f"{len(bad)} 件商品价格解析后不是合法非负整数分")
    else:
        ok("菜单价格全部解析为整数「分」")


# ────────────────────────────────────────────────────────────────
# 二、算价口径：接口报的数我们理解对了没有
# ────────────────────────────────────────────────────────────────
def audit_pricing(client, store: str, cart, coupons) -> None:
    section("二、算价口径体检：接口报的钱，我们理解对了没有")

    oracle = make_price_oracle(client, store, coupons)

    def price_of(assign: Assignment):
        return oracle(list(cart), assign)

    bare = price_of(Assignment(mapping={}, with_order=None))
    print(f"   裸买：到手 ¥{bare.price_fen / 100:.2f}  原价 ¥{bare.original_fen / 100:.2f}  "
          f"接口声称优惠 ¥{bare.discount_fen / 100:.2f}  "
          f"配送 ¥{bare.delivery_fen / 100:.2f} 打包 ¥{bare.packing_fen / 100:.2f}")

    # (1) 「到手价」里到底含不含配送费/打包费 —— 不含的话用户实际付得更多
    if bare.delivery_fen or bare.packing_fen:
        flag(
            f"接口单独报了配送 ¥{bare.delivery_fen / 100:.2f} / 打包 ¥{bare.packing_fen / 100:.2f}，"
            "而本项目展示的「到手价」用的是 price 字段、没把这两项加进去"
        )
    else:
        ok("到店取餐口径下配送费/打包费均为 0，到手价即实付")

    # (2) 裸买时接口自己报的 discount 应该为 0 —— 不为 0 说明 discount 不是券的优惠
    if bare.discount_fen != 0:
        note(
            f"一件券都没挂，接口却报了优惠 ¥{bare.discount_fen / 100:.2f} —— "
            "discount = 原价 − 到手，含菜单自身条件价，不是券的优惠"
        )
    else:
        ok("不挂券时接口报的优惠为 0，price 就是实付")

    # (3) 单挂每张券：接口报的 discount 是否等于「裸买 − 到手」
    print("\n   逐张单挂核对（接口声称的优惠 vs 我们算的真实优惠）：")
    eligible = [
        (ci, c) for ci, c in enumerate(coupons)
        if any(not c.product_codes or it.product_code in c.product_codes for it in cart)
    ][:6]
    mismatch = 0
    for ci, c in eligible:
        for ii, it in enumerate(cart):
            if c.product_codes and it.product_code not in c.product_codes:
                continue
            got = price_of(Assignment(mapping={ii: ci}, with_order=None))
            real = bare.price_fen - got.price_fen
            claimed = got.discount_fen
            mark = "ok" if real == claimed else "!!"
            if real != claimed:
                mismatch += 1
            print(
                f"      {mark} 「{c.title}」挂「{it.name}」: "
                f"真实省 ¥{real / 100:.2f} / 接口报 ¥{claimed / 100:.2f}"
            )
            break
    if mismatch:
        note(
            f"{mismatch}/{len(eligible)} 张券的接口 discount 与「裸买−到手」对不上 —— "
            "本项目一律用「裸买 − 到手」算省额，不用 discount，正是为了躲开这个坑"
        )
    elif eligible:
        ok("逐张核对一致：discount 口径 = 裸买价 − 到手价")

    # 静态守卫：源码里不许出现「拿 discount 当省额」的写法
    cli_src = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")
    misuses = re.findall(r"saved\w*\s*=\s*[^=\n]*discount_fen", cli_src)
    if misuses:
        flag(f"源码里有拿 discount 当省额的写法：{misuses[:2]}")
    else:
        ok("源码里没有任何一处拿 discount 当省额（省额一律是裸买 − 到手）")

    # (4) 数量：买 2 件挂 1 张券，接口会不会静默把数量重置成 1
    print("\n   数量核对（买 2 件只挂 1 张券）：")
    if cart:
        two = [cart[0], cart[0]]
        try:
            p2 = oracle(two, Assignment(mapping={0: eligible[0][0]}, with_order=None)) if eligible \
                else oracle(two, Assignment(mapping={}, with_order=None))
            p1 = oracle([cart[0]], Assignment(mapping={}, with_order=None))
            print(
                f"      2 件（首件挂券）到手 ¥{p2.price_fen / 100:.2f}；"
                f"1 件裸买 ¥{p1.price_fen / 100:.2f}"
            )
            if p2.price_fen <= p1.price_fen:
                flag("2 件的到手价不高于 1 件：接口极可能把数量吞了，继续算会报出虚假优惠")
            else:
                ok("2 件确实比 1 件贵，数量没有被吞")
        except Exception as exc:  # noqa: BLE001
            print(f"      数量核对没跑成：{exc}")

    return oracle


# ────────────────────────────────────────────────────────────────
# 三、最优性：真实穷举 vs 引擎
# ────────────────────────────────────────────────────────────────
def audit_optimality(client, store: str, cart, coupons) -> None:
    section("三、最优性体检：引擎给的是不是真最优（真实接口穷举对照）")

    options = _with_order_options(cart)
    print(f"   购物车 {len(cart)} 件，券池 {len(coupons)} 张，随单购档位 {len(options)} 个")

    # 只在真实可用范围内穷举：券 c 只能挂在它适用的商品上
    table: dict[int, list[int]] = {}
    for ci, c in enumerate(coupons):
        hits = [
            ii for ii, it in enumerate(cart)
            if not c.product_codes or it.product_code in c.product_codes
        ]
        if hits:
            table[ci] = hits
    usable = sorted(table)[:5]  # 控制调用量
    table = {ci: table[ci] for ci in usable}
    print(f"   可用券 {len(usable)} 张（已按适用性过滤，上限 5 张以控制调用次数）")

    oracle = make_price_oracle(client, store, coupons)
    n_items = len(cart)

    # 穷举：给每个商品分配一张互不相同的可用券（或不挂）× 每个随单购档位
    best_fen = None
    best_desc = ""
    combos = 0
    for wo in options:
        choices = [([None] + usable) for _ in range(n_items)]
        for combo in itertools.product(*choices):
            picked = [x for x in combo if x is not None]
            if len(set(picked)) != len(picked):
                continue  # 一张券只能用一次
            if any(x is not None and ii not in table[x] for ii, x in enumerate(combo)):
                continue  # 这张券不适用于这件商品
            combos += 1
            mapping = {ii: x for ii, x in enumerate(combo) if x is not None}
            try:
                r = oracle(list(cart), Assignment(mapping=mapping, with_order=wo))
            except Exception as exc:  # noqa: BLE001
                print(f"      穷举时有个方案算价失败，跳过：{exc}")
                continue
            if best_fen is None or r.price_fen < best_fen:
                best_fen = r.price_fen
                desc = "、".join(
                    f"{cart[ii].name}←{coupons[x].title}" for ii, x in sorted(mapping.items())
                ) or "不挂券"
                best_desc = f"{desc}｜随单购={wo.label if wo else '无'}"
    print(f"\n   真实穷举：试了 {combos} 个方案，最优到手 ¥{(best_fen or 0) / 100:.2f}")
    print(f"              {best_desc}")

    for budget in (40, 200):
        fresh = make_price_oracle(client, store, coupons)
        opt = CouponOptimizer(fresh, max_calls=budget)
        try:
            got = opt.optimize(cart, coupons, options)
        except Exception as exc:  # noqa: BLE001
            flag(f"预算 {budget} 时引擎抛异常：{exc}")
            continue
        if got.price is None:
            flag(f"预算 {budget} 时引擎没算出价")
            continue
        gap = got.price.price_fen - (best_fen or 0)
        print(
            f"\n   引擎 --budget {budget}: 到手 ¥{got.price.price_fen / 100:.2f} "
            f"（真实网络请求 {real_calls(fresh)} 次）"
        )
        if gap == 0:
            ok("与真实穷举一致 —— 这一单给的就是最优解")
        else:
            flag(f"比真实最优贵 ¥{gap / 100:.2f} —— 这一单给的**不是**最优解")

    tbl_probe = CouponOptimizer(oracle, max_calls=1 << 30)._eligibility(cart, coupons)
    rec = suggest_budget(tbl_probe, len(cart), len(options), 40)
    print(f"\n   引擎自荐预算：--budget {rec}")


def audit_single_item(client, store: str, menu, coupons) -> None:
    """单件对账：菜单上写的价，和接口真正收的价，是不是同一个数。

    用户是拿菜单上的价自己加一遍来核账的。两边一旦不一致，
    「省下多少」无论怎么算都跟他的预期对不上 —— 那不是算错，是口径没说清。
    """
    section("四、单件对账：菜单标价 vs 接口实收")
    oracle = make_price_oracle(client, store, coupons)
    cond = sorted(
        [i for i in menu if 0 < i.unit_price_fen < i.list_price_fen],
        key=lambda i: i.list_price_fen - i.unit_price_fen,
        reverse=True,
    )[:4]
    plain = [i for i in menu if i.unit_price_fen >= i.list_price_fen > 0][:2]
    print(f"{'商品':<26}{'菜单标价':>9}{'接口实收':>9}{'菜单原价':>9}{'接口原价':>9}  判定")
    print("-" * 76)
    worse = 0
    for it in cond + plain:
        r = oracle([it], Assignment(mapping={}, with_order=None))
        tag = "一致"
        if r.price_fen != it.unit_price_fen:
            tag = f"实收≠标价（{(r.price_fen - it.unit_price_fen) / 100:+.2f}）"
            if r.price_fen > it.unit_price_fen:
                worse += 1
        print(
            f"{(it.name or it.product_code)[:24]:<26}{it.unit_price_fen / 100:>9.2f}"
            f"{r.price_fen / 100:>9.2f}{it.list_price_fen / 100:>9.2f}"
            f"{r.original_fen / 100:>9.2f}  {tag}"
        )
    print("-" * 76)
    if worse:
        print(
            f"   · {worse}/{len(cond) + len(plain)} 件接口实收高于菜单标价 —— "
            "这些都是条件价商品，不随单购就按原价收。"
        )
        print("     CLI 里已按「结账口径」显示（取原价），并有 ⚠️ 提示；这里只是把它量出来。")
    else:
        ok("菜单标价与接口实收一致")


def audit_with_order(client, store: str, menu, coupons) -> None:
    """随单购档位到底有没有用。

    这个项目在 withOrder 上栽过一次大的：判断写成 isinstance(..., dict)，
    而 with_order 早不是 dict，恒为 False —— withOrder 一次都没发出去过，
    屏幕上照样印「随单购：麦金卡」。那次是编出来的建议。所以每次都要对账。
    """
    section("五、随单购档位：不发 / 发，真实各收多少")
    oracle = make_price_oracle(client, store, coupons)
    cond = sorted(
        [i for i in menu if 0 < i.unit_price_fen < i.list_price_fen],
        key=lambda i: i.list_price_fen - i.unit_price_fen,
        reverse=True,
    )[:4]
    if not cond:
        print("   本店没有条件价商品，档位无从验证。")
        return
    options = _with_order_options(cond)
    if len(options) < 2:
        print("   菜单没给出可试算的随单购档位。")
        return
    wo = options[1]
    print(f"   档位：{wo.label}")
    print(f"\n{'商品':<26}{'菜单标价':>9}{'不带卡':>9}{'带卡':>9}{'带卡省':>9}  判定")
    print("-" * 76)
    dead = 0
    above_ad = 0
    for it in cond:
        bare = oracle([it], Assignment(mapping={}, with_order=None))
        got = oracle([it], Assignment(mapping={}, with_order=wo))
        delta = bare.price_fen - got.price_fen
        if delta <= 0:
            dead += 1
        if got.price_fen > it.unit_price_fen:
            above_ad += 1
        print(
            f"{(it.name or it.product_code)[:24]:<26}{it.unit_price_fen / 100:>9.2f}"
            f"{bare.price_fen / 100:>9.2f}{got.price_fen / 100:>9.2f}{delta / 100:>9.2f}"
            f"  {'档位无效！' if delta <= 0 else '有效'}"
        )
    print("-" * 76)
    if dead == len(cond):
        flag("withOrder 发了但接口不认，屏幕上「随单购再省 X」会是编的")
    elif dead:
        print(f"   · {dead}/{len(cond)} 件档位无效，其余有效。")
    else:
        ok("档位全部有效 —— 随单购这个决策维度是真的")
    if above_ad:
        print(
            f"   · {above_ad}/{len(cond)} 件**带卡之后的实收仍高于菜单标价** —— "
            "菜单上印的那个价你根本付不出来。"
        )
        print("     CLI 里已用 `_card_cost_lines` 把这截差额挑明（多半是卡的钱）。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    ap.add_argument("--skip-brute", action="store_true", help="跳过真实穷举（省调用次数）")
    args = ap.parse_args()

    load_dotenv()
    client = McdMcpClient()
    store = args.store
    if not store:
        import os

        store = os.environ.get("MCD_DEFAULT_STORE") or "1450713"
    print(f"门店 {store}")

    menu = _parse_menu(
        client.data("query-meals", {"storeCode": store, "orderType": 1, "beType": 1})
    )
    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": store, "orderType": 1, "beType": 1})
    )
    if not menu:
        print("这家店取不到菜单，换个 --store 试试。")
        return 1
    print(f"菜单 {len(menu)} 件，券池 {len(coupons)} 张")

    audit_data(menu, coupons)

    cart = _build_cart(menu, coupons, size=2)
    if not cart:
        print("构造不出购物车，结束。")
        return 1
    print(f"\n核对用购物车：{'、'.join(i.name for i in cart)}")

    audit_pricing(client, store, cart, coupons)
    audit_single_item(client, store, menu, coupons)
    audit_with_order(client, store, menu, coupons)
    if not args.skip_brute:
        audit_optimality(client, store, cart, coupons)

    section("结论")
    if FACTS:
        print(f"接口口径事实 {len(FACTS)} 条（本项目已规避，记录以免后人踩回去）：")
        for i, f in enumerate(FACTS, 1):
            print(f"  {i}. {f}")
        print()
    if PROBLEMS:
        print(f"发现 {len(PROBLEMS)} 个问题：")
        for i, p in enumerate(PROBLEMS, 1):
            print(f"  {i}. {p}")
        return 1
    print("这一轮真实数据体检没查出问题。")
    print("注意：没查出 ≠ 保证正确 —— 接口随时可能变，且只验证了这一家店这几件商品。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
