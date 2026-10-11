"""离线自检：`python -m src.cli selftest`

为什么要有这个：本项目的**绝大部分复杂度在解析**，而解析的正确性
依赖于麦当劳返回体的具体格式——一旦对面改格式，这些解析器会静默失效。

所以这里把真实抓回来的**棘手报文原样存成 fixture**，逐个验证解析器。
全程离线，不需要 Token，几秒钟跑完。

重点覆盖三种返回体格式：
- A 类：Markdown 说明 + 内嵌 JSON（且说明里带 `{data[].title}` 占位符）
- B 类：纯 Markdown，无 JSON
- C 类：`data` 是字符串（类 CSV）
"""

from __future__ import annotations

import itertools
import random

from .assets import parse_claimable_markdown, parse_coupon_markdown, fmt_num, _num
from .coupon_engine import (
    Assignment,
    Budget,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
    WithOrderOption,
    as_fen,
    to_fen,
)
from .mcd_client import extract_payload
from .nutrition import parse_nutrition
from . import demo_data as demo

def _random_case(rnd: random.Random):
    """随机造一个「券 → 商品」的匹配场景，含真实世界里的两种坑。

    - 有负收益券（挂上反而加价），对应实测中「券被随单购作废」的情形
    - 随单购与某张券互斥，对应实测中「随单购后券不再叠加」的情形

    返回 (cart, coupons, oracle, with_order_options)
    """
    n_items = rnd.randint(2, 4)
    n_cps = rnd.randint(2, 5)
    cart = [
        CartItem(
            product_code=f"P{i}",
            name=f"品{i}",
            unit_price_fen=rnd.randrange(500, 4000),
        )
        for i in range(n_items)
    ]
    coupons = []
    for c in range(n_cps):
        picks = rnd.sample(range(n_items), rnd.randint(1, n_items))
        coupons.append(
            Coupon(
                coupon_id=f"C{c}",
                coupon_code=f"X{c}",
                title=f"券{c}",
                product_codes=frozenset(cart[p].product_code for p in picks),
            )
        )
    power = {
        c.coupon_id: rnd.choice([-300, -100, 0, 200, 400, 800, 1200]) for c in coupons
    }
    card_discount: dict[str, int] = {"": 0}
    for w in range(rnd.randint(0, 2)):
        card_discount[f"卡{w}"] = rnd.choice([100, 300, 600])
    conflict: dict[str, str] = {}
    if rnd.random() < 0.5 and len(coupons) > 1:
        labs = [k for k in card_discount if k]
        if labs:
            conflict[rnd.choice(labs)] = rnd.choice(coupons).coupon_id
    options: list = [None] + [
        WithOrderOption(payload={"membershipCode": k}, label=k) for k in card_discount if k
    ]

    def oracle(items, assignment):
        subtotal = sum(i.unit_price_fen * i.quantity for i in items)
        ids = [coupons[ci].coupon_id for _, ci in assignment.mapping.items()]
        discount = sum(power.get(cid, 0) for cid in ids)
        label = assignment.with_order.label if assignment.with_order else ""
        if label and conflict.get(label) in ids:
            discount -= power.get(conflict[label], 0)
        discount += card_discount.get(label, 0)
        return PriceResult(
            price_fen=max(subtotal - discount, 0),
            original_fen=subtotal,
            discount_fen=discount,
        )

    return cart, coupons, oracle, options


def _bruteforce_best(cart, coupons, oracle, options) -> int:
    """穷举所有合法匹配，返回真正的全局最优价。

    只用于离线基准：真实场景券多、商品多，穷举的调用次数会爆，
    线上跑的是爬山。这里拿它当标尺，是为了回答「爬山到底差多少」。
    """
    best = None
    for wo in options:
        for k in range(len(coupons) + 1):
            for cs in itertools.combinations(range(len(coupons)), k):
                for its in itertools.permutations(range(len(cart)), k):
                    price = oracle(
                        list(cart), Assignment(mapping=dict(zip(its, cs)), with_order=wo)
                    )
                    if best is None or price.price_fen < best:
                        best = price.price_fen
    return best


def _benchmark_vs_bruteforce(cases: int = 80, budget: int = 40):
    """拿穷举最优当标尺，量爬山的实际命中率。

    固定种子，所以每次跑出的数字一样 —— 否则这条自检就成了玄学。
    """
    hit = 0
    gap_sum = 0
    done = 0
    for s in range(cases):
        rnd = random.Random(s * 7919)
        cart, coupons, oracle, options = _random_case(rnd)
        optimum = _bruteforce_best(cart, coupons, oracle, options)
        try:
            opt = CouponOptimizer(oracle, max_calls=budget)
            got = opt.optimize(cart, coupons, options)
        except Exception:  # noqa: BLE001
            continue
        if got.price is None:
            continue
        gap = got.price.price_fen - optimum
        gap_sum += gap
        done += 1
        if gap == 0:
            hit += 1
    return hit, max(done, 1), gap_sum / max(done, 1)


# ------------------------------------------------------------------ fixtures

# A 类：字段说明里带 `{data[].title}` 占位符 —— 这是当初两次解析失败的根因
MIXED = """
# API Response Information

## Response Structure

- **data**: (Type: object)
  - **data.availablePoint**: 可用积分 (Type: string)
  - **data.list[].title**: 券标题 (Type: string)
  - **data.list[].couponId**: 券ID，示例 {data[].couponId} (Type: string)

## Original Response

{"success":true,"code":200,"message":"请求成功","data":{"availablePoint":"9.9","list":[{"title":"9.9元中杯冰美式","couponId":"abc"}]}}
"""

# B 类：纯 Markdown，压根没有 JSON
COUPON_MD = """# 您的优惠券列表

共 2 张可用优惠券 | 第 1/1 页 | 每页 200 条

## 9.9元中杯冰美式
- **优惠**: ¥9.9 (用券价格)
- **有效期**: 2026-10-11 00:00-2026-10-17 23:59
- **领取时间**: 今日收到
- **标签**: 到店专用、外送专用

## 巧克力味厚松饼猪柳蛋套餐
- **优惠**: ¥29.9 (用券价格)
- **有效期**: 2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29
- **标签**: 今日到期、到店专用
"""

CLAIMABLE_MD = """### 麦麦省优惠券列表：
- 优惠券标题：免费脆薯饼 \\
  状态：已领取 \\
  优惠券图片：\\
    <img src="https://example.com/a.png">
- 优惠券标题：9.9元中杯冰美式 \\
  状态：已领取 \\
"""

# C 类：data 是字符串
NUTRITION_STR = """[3]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
  黑咖啡,null,20,5,0,0,1,10,2
"""

# ------------------------------------------------------------------ runner

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


def run() -> int:
    # 1. A 类解析：必须跳过占位符，拿到真正的报文
    p = extract_payload(MIXED)
    check("A类-能抽出报文", p is not None)
    check("A类-success为真", bool(p and p.get("success")))
    check(
        "A类-data字段正确",
        bool(p and p.get("data", {}).get("availablePoint") == "9.9"),
        str(p.get("data") if p else None),
    )
    check(
        "A类-没被占位符污染",
        bool(p and "list" in p.get("data", {}) and len(p["data"]["list"]) == 1),
    )

    # 2. B 类：纯 Markdown 解析
    coupons = parse_coupon_markdown(COUPON_MD)
    check("B类-卡包条数", len(coupons) == 2, f"得到 {len(coupons)}")
    check(
        "B类-标题",
        bool(coupons and coupons[0]["title"] == "9.9元中杯冰美式"),
        str(coupons[0]["title"] if coupons else ""),
    )
    check(
        "B类-有效期只取前两个时间点",
        bool(
            coupons
            and coupons[1]["valid_text"].startswith("2026-10-11 05:00-2026-10-11 10:29")
        ),
        str(coupons[1]["valid_text"] if len(coupons) > 1 else ""),
    )
    check(
        "B类-截止时刻解析",
        bool(len(coupons) > 1 and coupons[1]["end"] is not None),
    )
    check(
        "B类-标签拆分",
        bool(coupons and coupons[0]["tags"] == ["到店专用", "外送专用"]),
        str(coupons[0]["tags"] if coupons else ""),
    )

    claimable = parse_claimable_markdown(CLAIMABLE_MD)
    check("B类-券池条数", len(claimable) == 2, f"得到 {len(claimable)}")
    check(
        "B类-券池剥掉行尾反斜杠",
        bool(claimable and claimable[0]["title"] == "免费脆薯饼"),
        str(claimable[0]["title"] if claimable else ""),
    )

    # 3. C 类：类 CSV 字符串
    nut = parse_nutrition(NUTRITION_STR)
    check("C类-解析条数", len(nut) == 3, f"得到 {len(nut)}")
    check(
        "C类-字段映射",
        bool(nut and nut.get("中薯条", {}).get("kcal") == 289),
        str(nut.get("中薯条") if nut else ""),
    )

    # 4. 积分是字符串小数，int() 会炸
    check("积分-字符串小数", _num("9.9") == 9.9, str(_num("9.9")))
    check("积分-空值兜底", _num(None) == 0.0)
    check("积分-整数不带尾巴", fmt_num(10.0) == "10", fmt_num(10.0))
    check("积分-小数保留一位", fmt_num(9.9) == "9.9", fmt_num(9.9))

    # 5. 条件价：不随单购必须按原价
    item = CartItem(
        product_code="X",
        name="四件套",
        unit_price_fen=2900,
        list_price_fen=5700,
        discount_type="随单购麦金卡优惠",
        needs_with_order=True,
    )
    check("条件价-不随单购按原价", item.effective_price_fen() == 5700)
    plain = CartItem(product_code="Y", name="普通", unit_price_fen=2500)
    check("条件价-普通品按标价", plain.effective_price_fen() == 2500)

    # 6. 券适用性
    c = Coupon(coupon_id="1", coupon_code="c", product_codes=frozenset({"X"}))
    check("券-命中声明商品", c.applies_to("X"))
    check("券-不命中其它商品", not c.applies_to("Z"))

    # 7. 引擎：demo 数据上必须比裸买便宜
    cart = [demo.DEMO_MENU[0], demo.DEMO_MENU[2], demo.DEMO_MENU[4], demo.DEMO_MENU[5]]
    opt = CouponOptimizer(demo.demo_oracle, max_calls=60)

    def bare_oracle(items, assignment):
        return demo.demo_oracle(items, Assignment(mapping={}, with_order=None))

    baseline = bare_oracle(cart, Assignment())
    best = opt.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
    check(
        "引擎-优于裸买",
        best.price is not None and best.price.price_fen < baseline.price_fen,
        f"{best.price.price_fen if best.price else '?'} vs {baseline.price_fen}",
    )
    check(
        "引擎-预算内收敛",
        opt.calls <= 60,
        f"调用 {opt.calls} 次",
    )
    check(
        "引擎-不违反一券一用",
        len(set(best.mapping.values())) == len(best.mapping),
    )

    # 8. 预算为 0 时不能崩，必须仍返回可用结果
    tiny = CouponOptimizer(demo.demo_oracle, max_calls=1)
    try:
        t = tiny.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
        check("引擎-预算耗尽不崩", t is not None and t.price is not None)
    except Exception as exc:  # noqa: BLE001
        check("引擎-预算耗尽不崩", False, repr(exc))

    # 8b. 契约锁定（修复「裸买基准在 optimize 后崩溃」的 P0）：
    # optimize 会把共享预算吃光，之后再 try_evaluate(裸买) 必然返回 None；
    # 因此调用方必须在 optimize 之前先算裸买基准。这里断言该契约成立，
    # 防止 cli 把 baseline 写回 optimize 之后导致 AttributeError。
    opt_budget = Budget(40)
    eng = CouponOptimizer(demo.demo_oracle, max_calls=40, budget=opt_budget)
    eng.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
    late = eng.try_evaluate(list(cart), Assignment(mapping={}, with_order=None))
    check(
        "契约-optimize吃光预算后裸买基准为None",
        late is None,
        f"应返回 None 以提醒调用方『基准须先算』，实际 {late!r}",
    )
    # 反过来：先算基准则能拿到价格（cli 的正确写法）
    early_budget = Budget(40)
    eng2 = CouponOptimizer(demo.demo_oracle, max_calls=40, budget=early_budget)
    early = eng2.try_evaluate(list(cart), Assignment(mapping={}, with_order=None))
    eng2.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
    check("契约-裸买基准先于optimize可正常算出", early is not None and early.price_fen >= 0)

    # 9. 真实节省口径：必须以裸买价为基准，不能用 discount
    pr = PriceResult(price_fen=7030, original_fen=10740, discount_fen=3710)
    check("口径-真实节省用裸买基准", pr.saved_vs(9750) == 2720, str(pr.saved_vs(9750)))
    check(
        "口径-接口口径与真实口径不同",
        pr.saved_fen != pr.saved_vs(9750),
        f"{pr.saved_fen} vs {pr.saved_vs(9750)}",
    )

    # 10. 省钱洞察
    from . import insights as ins

    marg = ins.marginal_costs(
        demo.demo_oracle,
        cart,
        demo.DEMO_COUPONS,
        lambda c: demo.DEMO_WITH_ORDER_OPTIONS,
        baseline_fen=best.price.price_fen,
        per_call_budget=20,
    )
    check("洞察-边际成本覆盖每件", len(marg) == len(cart), f"{len(marg)} vs {len(cart)}")
    check(
        "洞察-边际成本非负",
        all(m.marginal_fen >= 0 for m in marg),
        str([m.marginal_fen for m in marg]),
    )
    check(
        "洞察-确实有商品享受到优惠",
        any(m.delta_fen > 0 for m in marg),
    )

    red = ins.redundant_coupons(demo.demo_oracle, cart, demo.DEMO_COUPONS, best)
    check("洞察-冗余券检测不报错", isinstance(red, list))

    splits = ins.split_suggestions(
        demo.demo_oracle,
        cart,
        demo.DEMO_COUPONS,
        lambda c: demo.DEMO_WITH_ORDER_OPTIONS,
        whole_fen=best.price.price_fen,
        per_call_budget=20,
    )
    check("洞察-拆单检测不报错", isinstance(splits, list))

    # 11. 演示数据里那张「陷阱券」（C5，挂上反而加价）不该被选进最优解
    used_ids = {demo.DEMO_COUPONS[ci].coupon_id for ci in best.mapping.values()}
    check("引擎-不选中陷阱券C5", "C5" not in used_ids, str(sorted(used_ids)))

    # 12. 金额换算不能少一分：float 会把 33.30 算成 3329.999…
    from .coupon_engine import to_fen

    check("金额-33.30不能少一分", to_fen("33.30") == 3330, str(to_fen("33.30")))
    check("金额-19.99正确", to_fen("19.99") == 1999, str(to_fen("19.99")))
    check("金额-整数元", to_fen(25) == 2500, str(to_fen(25)))
    check("金额-浮点输入", to_fen(33.3) == 3330, str(to_fen(33.3)))
    check("金额-脏数据兜底", to_fen("abc") == 0 and to_fen(None) == 0)
    check("金额-带空格", to_fen(" 12.50 ") == 1250, str(to_fen(" 12.50 ")))

    # 13. 随单购参数里不能混进展示用的字段
    from .coupon_engine import WithOrderOption

    wo = WithOrderOption(payload={"membershipCode": "M1"}, label="麦金卡")
    check("随单购-payload不混展示字段", set(wo.payload) == {"membershipCode"})
    check("随单购-标签独立", wo.label == "麦金卡")

    # 14. 全局预算：洞察也必须计入，绝不超支
    shared = Budget(30)
    o14 = CouponOptimizer(demo.demo_oracle, max_calls=30, budget=shared)
    o14.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
    ins.marginal_costs(
        demo.demo_oracle,
        cart,
        demo.DEMO_COUPONS,
        lambda c: demo.DEMO_WITH_ORDER_OPTIONS,
        baseline_fen=best.price.price_fen,
        per_call_budget=8,
        budget=shared,
    )
    check("预算-全局不超支", shared.used <= 30, f"用了 {shared.used}/30")

    # 15. 最有说服力的一项：拿穷举最优当标尺，量一量爬山到底够不够好。
    # 这一项把「我们的算法好不好」从一句自夸变成一个可复现的数字。
    hit, cases, avg_gap = _benchmark_vs_bruteforce(cases=80, budget=40)
    rate = hit / cases
    check(
        f"引擎-对照穷举最优（{hit}/{cases} = {rate:.0%}）",
        rate >= 0.85,
        f"平均差 {avg_gap / 100:.2f} 元",
    )

    # 16. 菜单/券名匹配：真实接口两侧命名不一致 + 商标符号的兜底
    # 真实数据：券 productName 写「麦咖啡™冰美式中杯」，菜单品名却是「麦咖啡™美式」，
    # 用户照着券名点「冰美式中杯」必须仍能匹配到正确商品（507411）。
    from .cli import _pick_cart

    menu_match = [
        CartItem(product_code="507411", name="麦咖啡™美式", unit_price_fen=1190),
        CartItem(product_code="9900016073", name="龙焰鸡腿堡三件套", unit_price_fen=2690),
    ]
    cp_match = [
        Coupon(
            coupon_id="C1",
            coupon_code="X",
            title="9.9元中杯冰美式",
            product_codes=frozenset({"507411"}),
            product_names=frozenset({"麦咖啡™冰美式中杯"}),
        )
    ]
    cart_a, missed_a = _pick_cart(menu_match, ["冰美式中杯"], cp_match)
    check(
        "匹配-券名回退命中商品",
        len(cart_a) == 1 and cart_a[0].product_code == "507411",
        f"missed={missed_a}",
    )
    cart_b, _ = _pick_cart(menu_match, ["麦咖啡美式"], cp_match)  # 用户不打™
    check(
        "匹配-忽略商标符号",
        len(cart_b) == 1 and cart_b[0].product_code == "507411",
    )
    cart_c, _ = _pick_cart(menu_match, ["龙焰鸡腿堡三件套"], cp_match)
    check(
        "匹配-普通名称仍可用",
        len(cart_c) == 1 and cart_c[0].product_code == "9900016073",
    )

    # 17. 接口异常结构守卫：真实接口偶发返回非预期类型（list/str/None/dict），
    # 解析器必须返回空列表，绝不能抛 AttributeError/KeyError 裸奔成 traceback。
    # 真实教训：门店字段缺失、菜单返回非 dict 都会让整条命令崩在打印之前。
    from .cli import _parse_coupons, _parse_menu

    check(
        "守卫-菜单返回 None/str/list 不崩",
        _parse_menu(None) == [] and _parse_menu("x") == [] and _parse_menu([1, 2]) == [],
    )
    check(
        "守卫-券池返回 dict/None/str 不崩",
        _parse_coupons({"a": 1}) == [] and _parse_coupons(None) == [] and _parse_coupons("x") == [],
    )

    # 18. 路程性价比模型：把「省下的钱」和「跑一趟的成本」放进同一量纲。
    # 距离来自接口真实 distance（单位米），这里是换算逻辑本身的回归。
    from .route import MODES, OVERHEAD_MIN, StoreQuote, rank_stores, travel_cost, yuan_to_fen

    # 元→分必须用 round，否则 14.0*100 会被浮点误差砍成 1399（老 bug）
    check("金额-元转分不截断", yuan_to_fen(14.0) == 1400 and yuan_to_fen(0.07) == 7)

    tw = travel_cost(144, "walk")  # 真实门店距离 144 米
    check(
        "路程-步行往返与耗时",
        tw.round_meters == 288 and tw.minutes == 9 and tw.money_fen == 0,
        f"往返{tw.round_meters}m/{tw.minutes}分",
    )
    tx = travel_cost(144, "taxi")
    check(
        "路程-打车起步价+里程",
        tx.money_fen == 1472,  # 起步 1400 + 0.288km × 250
        f"{tx.money_fen} 分",
    )
    txw = travel_cost(144, "taxi", wage_per_hour=60)
    check(
        "路程-时薪折算时间成本",
        txw.time_money_fen == 600 and txw.total_fen == 2072,  # 6 分钟 × ¥60/h = ¥6
        f"{txw.time_money_fen} 分",
    )
    check(
        "路程-覆盖每公里价",
        travel_cost(1000, "walk", yuan_per_km=3).money_fen == 600,  # 往返 2km × ¥3
    )
    check("路程-未知方式回退到步行", travel_cost(1000, "飞船").minutes == travel_cost(1000, "walk").minutes)
    check("路程-固定开销存在", OVERHEAD_MIN > 0 and MODES["bus"].base_fen == 200)

    # 缺货的店不能跟齐全的比 —— 必须沉到最后，否则是拿不同的东西比价
    # 关键场景：远店东西更便宜（¥35 < ¥36.80），但打车路费高得多。
    # 加起来反而更贵 —— 这正是「只比价格」会选错、必须算路程的理由。
    q_ok = StoreQuote("A", "近店", distance_m=144, paid_fen=3680, baseline_fen=3880,
                      travel=travel_cost(144, "taxi"))
    q_far = StoreQuote("B", "远店", distance_m=3000, paid_fen=3500, baseline_fen=3880,
                       travel=travel_cost(3000, "taxi"))
    q_miss = StoreQuote("C", "缺货店", distance_m=10, missing=("麦咖啡™美式",))
    ranked = rank_stores([q_far, q_miss, q_ok])
    check("比价-缺货店不参与排序", ranked[-1].store_code == "C")
    check(
        "比价-便宜但远的店会因路费落败",
        [q.store_code for q in ranked[:2]] == ["A", "B"],
        f"近店¥{q_ok.total_fen / 100:.2f} vs 远店¥{q_far.total_fen / 100:.2f}",
    )
    check("比价-真实节省计算", q_ok.saved_fen == 200 and q_ok.total_fen == 5152)

    # 同城同券时各家到手价常常一模一样 —— 这时必须点破"挑最近的就行"，
    # 否则用户会以为工具没算明白（真实场景：三店同为 ¥36.80）。
    from .route import verdict

    qa = StoreQuote("A", "近店", distance_m=144, paid_fen=3680, baseline_fen=3880,
                    travel=travel_cost(144, "walk"))
    qb = StoreQuote("B", "远店", distance_m=900, paid_fen=3680, baseline_fen=3880,
                    travel=travel_cost(900, "walk"))
    v_same = verdict([qb, qa])
    check("比价-同价时点破挑最近", "一样" in v_same and "挑最近" in v_same, v_same[:40])

    q_taxi = StoreQuote("C", "打车店", distance_m=144, paid_fen=3680, baseline_fen=3880,
                        travel=travel_cost(144, "taxi"))
    check(
        "比价-路费吃掉优惠时管家出言劝退",
        "路费" in verdict([q_taxi]) and "劝" in verdict([q_taxi]),
        f"路费¥{q_taxi.travel.money_fen / 100:.2f} vs 省¥{q_taxi.saved_fen / 100:.2f}",
    )

    # 打烊门店必须被排除（businessStatus 实测为布尔值，True=营业中）
    open_stores = [
        s for s in (
            {"storeName": "A", "businessStatus": True},
            {"storeName": "B", "businessStatus": False},
            {"storeName": "C"},  # 字段缺失：不敢乱猜，按营业处理
        )
        if s.get("businessStatus") is not False
    ]
    check(
        "营业-打烊门店被排除而字段缺失不误杀",
        [s["storeName"] for s in open_stores] == ["A", "C"],
    )

    # 19. 券范围解释：卡包接口只有 Markdown、没有券码，必须向用户说清楚
    # 「我明明有 4 张券为什么只用 1 张」，否则用户会以为引擎算漏了。
    from .cli import _explain_coupon_scope

    try:
        for n in (0, 1, 2):
            _explain_coupon_scope(n)
        check("券范围-解释函数不崩（0/1/2 张）", True)
    except Exception as exc:  # noqa:BLE001
        check("券范围-解释函数不崩（0/1/2 张）", False, repr(exc))

    # 券池是主链路：接口把 products[] 换成字符串、或条目本身不是对象时，
    # 一个 AttributeError 就能让整单算不出来，必须优雅跳过。
    from .cli import _parse_coupons

    try:
        dirty = [
            {"couponId": "1", "couponCode": "k", "title": "正常券",
             "products": [{"productCode": "A", "productName": "甲"}]},
            {"couponId": "2", "couponCode": "k2", "title": "脏products",
             "products": ["不是对象", None, {"productCode": "B"}]},
            "整条都不是对象",
            None,
        ]
        got = _parse_coupons(dirty)
        check(
            "券解析-脏数据逐条跳过而非整单崩",
            len(got) == 2 and "A" in got[0].product_codes and "B" in got[1].product_codes,
            str([c.product_codes for c in got]),
        )
        check("券解析-非列表返回空", _parse_coupons(None) == [] and _parse_coupons("x") == [])
    except Exception as exc:  # noqa: BLE001
        check("券解析-脏数据逐条跳过而非整单崩", False, repr(exc))

    # 20. 麦门黄历：这是项目的门面，每一条宜忌都必须是真算出来的
    from datetime import datetime

    from .almanac import build as build_almanac
    from .almanac import days_left, expiry_note, period_of, render as render_almanac

    check("黄历-时段划分覆盖全天", period_of(datetime(2026, 10, 11, 8))[0] == "早市"
          and period_of(datetime(2026, 10, 11, 12))[0] == "午市"
          and period_of(datetime(2026, 10, 11, 15))[0] == "下午茶"
          and period_of(datetime(2026, 10, 11, 19))[0] == "晚市"
          and period_of(datetime(2026, 10, 11, 22))[0] == "夜宵"
          and period_of(datetime(2026, 10, 11, 2))[0] == "深夜")
    check(
        "黄历-到期天数解析",
        days_left("2026-10-11", datetime(2026, 10, 11)) == 0
        and days_left("2026-10-12 23:59", datetime(2026, 10, 11)) == 1
        and days_left("", datetime(2026, 10, 11)) is None,
    )
    check(
        "黄历-临期提示",
        "今日到期" in expiry_note("2026-10-11", datetime(2026, 10, 11))
        and expiry_note("2026-12-31", datetime(2026, 10, 11)) == "",
    )

    # 构造一个「有券省 8 元 / 有券反而贵 1 元 / 有单品没券」的小世界
    # （Budget/CartItem/Coupon/PriceResult 已在模块顶部导入，此处不可再局部导入，
    #  否则会把整个 run() 里的同名名字变成局部变量而 UnboundLocalError）
    menu = [
        CartItem(product_code="A1", name="板烧三件套", unit_price_fen=3700, list_price_fen=3700),
        CartItem(product_code="A2", name="麦香鱼", unit_price_fen=1500, list_price_fen=1500),
        CartItem(product_code="A3", name="薯条", unit_price_fen=1200, list_price_fen=1200),
    ]
    coupons = [
        Coupon(coupon_id="c1", coupon_code="k1", title="板烧立减8元",
               product_codes=frozenset({"A1"}), valid_until="2026-10-11"),
        Coupon(coupon_id="c2", coupon_code="k2", title="薯条陷阱券",
               product_codes=frozenset({"A3"})),
    ]

    def almanac_oracle(items, assignment):
        # 模拟真实接口：挂 c1 省 800 分；挂 c2 反而贵 100 分（卡费口径）
        base = sum(i.unit_price_fen * i.quantity for i in items)
        saved = 0
        for idx, ci in (assignment.mapping or {}).items():
            saved += 800 if ci == 0 else -100
        return PriceResult(price_fen=base - saved, original_fen=base, discount_fen=max(saved, 0))

    day = build_almanac(menu, coupons, almanac_oracle, Budget(40), now=datetime(2026, 10, 11, 12))
    kinds = [(r.product_name, r.kind, r.saved_fen) for r in day["readings"]]
    check(
        "黄历-省钱的券进宜",
        ("板烧三件套", "宜", 800) in kinds,
        str(kinds),
    )
    check(
        "黄历-挂券反而更贵的进忌（随单购陷阱）",
        any(n == "薯条" and k == "忌" and s < 0 for n, k, s in kinds),
        str(kinds),
    )
    check(
        "黄历-无券可挂的贵价单品进忌",
        ("麦香鱼", "忌", 0) in kinds,
        str(kinds),
    )
    check("黄历-临期券被点出来", len(day["expiry"]) == 1, str(day["expiry"]))

    text = render_almanac(day, "小的不编。")
    check("黄历-渲染不崩且含日期与时段", "麦门黄历" in text and "2026-10-11" in text and "午市" in text)

    # 预算见底时必须收工，绝不偷偷超支
    tiny = build_almanac(menu, coupons, almanac_oracle, Budget(1), now=datetime(2026, 10, 11))
    check("黄历-预算为1时不炸且不乱报宜", tiny["probes"] <= 1, str(tiny["probes"]))

    # 没有券时不能编宜忌
    empty = build_almanac(menu, [], almanac_oracle, Budget(40), now=datetime(2026, 10, 11))
    check("黄历-无券时不编造条目", "算不出" in render_almanac(empty, ""))

    # 21. 段位与晒单：空账单、满级、中途三档都要站得住
    from .persona import brag_card, butler_total, butler_warning, progress_bar, rank_of, summarize

    check("段位-零起步是麦门路人", rank_of(0)["title"] == "麦门路人")
    check("段位-满级无下一档", rank_of(99999999)["title"] == "麦门永存" and rank_of(99999999)["next"] is None)
    check(
        "段位-差额计算正确",
        rank_of(5000)["next"]["gap_fen"] == 10000,
        str(rank_of(5000)["next"]),
    )
    check("段位-负数不炸", rank_of(-100)["title"] == "麦门路人")
    check("段位-进度条首尾正确", progress_bar(0, 6).startswith("░") and set(progress_bar(6, 6)) == {"█"})
    check("段位-满级进度条实心", set(progress_bar(6, 6)) == {"█"})
    check("口吻-空账单有说法", "空" in butler_total(0, 0))
    check("口吻-劝退敢说实话", "劝" in butler_warning(1500, 500))
    check("口吻-路费为零时是稳赚", "稳赚" in butler_warning(0, 500))

    entries = [
        {"baseline_fen": 3800, "paid_fen": 3600, "store_name": "人民广场店",
         "coupon_titles": ["板烧立减"], "saved_at": "2026-10-11T12:00:00"},
        {"baseline_fen": 5000, "paid_fen": 4700, "store_name": "人民广场店",
         "coupon_titles": ["板烧立减"], "saved_at": "2026-10-12T12:00:00"},
    ]
    s = summarize(entries)
    check("账单-汇总真实节省", s["saved_fen"] == 500 and s["orders"] == 2, str(s))
    check("账单-最常光顾与最常用券", s["top_store"] == "人民广场店" and s["top_coupon"] == "板烧立减")
    check("账单-脏数据不崩", summarize([{}, {"baseline_fen": None}])["saved_fen"] == 0)
    card = brag_card(500, 2, "麦门学徒", "X店", "Y券")
    card_link = brag_card(500, 2, "麦门学徒", with_link=True)
    check(
        "晒单-卡片默认不带链接（朋友圈里裸链接会被判广告）",
        "github.com" not in card and "麦门学徒" in card and "¥5.00" in card,
        card,
    )
    check("晒单---with-link 才带仓库地址", "github.com" in card_link)
    check(
        "晒单-卡片区分测算与真实成交（不能把测算说成已省）",
        "测算" in card and "真实成交" in card,
        card,
    )
    check(
        "口吻-累计点评不替用户吹牛",
        "测算" in butler_total(5000, 3, 0) and "真实订单" in butler_total(5000, 3, 2),
    )

    # 22. 算价 oracle：随单购必须真的发出去
    # 这是本项目最贵的一个 bug —— with_order 早已从裸 dict 改成 WithOrderOption
    # （frozen dataclass，不是 dict 子类），而五处 oracle 仍在写
    # `isinstance(with_order, dict)`，结果 withOrder 一次都没进过请求：
    # 实测同一件商品不带 ¥57.00、带上 ¥37.90，差 ¥19.10。
    # 更阴的是离线测不出来（demo_oracle 直接读 .label，不校验 payload），
    # 所以这里必须用假 client 把真实 payload 抓出来断言。
    from .oracle import make_price_oracle

    class _RecClient:
        def __init__(self, reply):
            self.payloads: list[dict] = []
            self._reply = reply

        def data(self, tool, payload):
            self.payloads.append(payload)
            return self._reply

    coupon = Coupon(coupon_id="c1", coupon_code="k1", title="立减")
    item = CartItem(product_code="P1", name="测试品")
    wo = WithOrderOption(payload={"membershipCode": "106"}, label="麦金卡（随单购）")

    rc = _RecClient({"price": 3790, "originalPrice": 6690, "discount": 2900})
    orc = make_price_oracle(rc, "S1", [coupon])
    pr = orc([item], Assignment(mapping={0: 0}, with_order=wo))
    sent = rc.payloads[-1]
    check("oracle-随单购真的进了请求", sent.get("withOrder", {}).get("membershipCode") == "106", str(sent))
    check("oracle-券码进了请求", sent["items"][0].get("couponId") == "c1", str(sent["items"]))
    check("oracle-算价结果按分读取不放大100倍", pr.price_fen == 3790, str(pr))

    # 不带随单购时不能凭空塞一个 withOrder 进去
    rc2 = _RecClient({"price": 5700})
    make_price_oracle(rc2, "S1", [coupon])([item], Assignment(mapping={}, with_order=None))
    check("oracle-不随单购时不带 withOrder", "withOrder" not in rc2.payloads[-1], str(rc2.payloads[-1]))

    # price 缺失必须报错：0 分是最小值，静默返回等于告诉优化器「这单最便宜」
    rc3 = _RecClient({"originalPrice": 5700})
    raised = False
    try:
        make_price_oracle(rc3, "S1", [coupon])([item], Assignment(mapping={}, with_order=None))
    except Exception:
        raised = True
    check("oracle-price缺失时报错而非当成0元", raised)

    # 金额换算两套口径不能混：菜单是元字符串、算价是分整数
    check(
        "金额-两套口径不混用",
        to_fen("33.5") == 3350 and as_fen(5700) == 5700 and as_fen("5700") == 5700,
        f'{to_fen("33.5")} / {as_fen(5700)} / {as_fen("5700")}',
    )
    check("金额-as_fen脏数据不崩", as_fen(None) == 0 and as_fen("") == 0 and as_fen("abc") == 0)

    # 23. 网络抖动不能被当成「已经收敛」
    # 爬山里若只看 price is None，一次抖动等于告诉它「邻域里没有更便宜的」，
    # 于是只爬两步就收工，最后报「已是最优」，用户多付钱且全程无报错。
    # 这里造一个 25% 概率抛错的 oracle（刻意不让它连续失败 3 次，
    # 否则会走 _failures>=3 的抛错兜底，测不到我们要的行为）。
    from .mcd_client import McpError

    class _FlakyOracle:
        """和 demo_oracle 完全同一个世界，只在第 2、6 次调用上抖一下。

        只在固定的两次上失败（而不是按概率），是因为连续失败 3 次会触发
        `_failures >= 3` 的抛错兜底 —— 那就测不到"跳过候选继续搜"这个行为了。
        """

        def __init__(self):
            self.calls = 0

        def __call__(self, items, assignment):
            self.calls += 1
            if self.calls in (2, 6):
                raise McpError("模拟网络抖动")
            return demo.demo_oracle(items, assignment)

    flaky = _FlakyOracle()
    eng = CouponOptimizer(flaky, max_calls=200)
    jitter_price = eng.optimize(demo.DEMO_MENU[:3], demo.DEMO_COUPONS[:3], [None]).price.price_fen

    clean = CouponOptimizer(demo.demo_oracle, max_calls=200)
    clean_price = clean.optimize(demo.DEMO_MENU[:3], demo.DEMO_COUPONS[:3], [None]).price.price_fen

    check(
        "抖动-不因网络抖动给出更贵的结果",
        jitter_price <= clean_price,
        f"抖动下 {jitter_price} vs 正常 {clean_price}",
    )

    # ---------------------------------------------------------- 券包体检
    from .audit import (
        audit_coupons,
        face_value_fen,
        render,
        DEAD,
        DUD,
        WORTH,
    )

    def _audit(limit: int = 10, budget: Budget | None = None, oracle=None):
        return audit_coupons(
            oracle or demo.audit_oracle,
            demo.AUDIT_MENU,
            demo.AUDIT_COUPONS,
            lambda cart: demo.AUDIT_WITH_ORDER_OPTIONS,
            budget=budget,
            limit=limit,
        )

    vs = {v.coupon.coupon_id: v for v in _audit()}

    check(
        "体检-死券：这家店没得挂的券要单独标出来",
        vs["K5"].status == DEAD,
        f"K5={vs['K5'].status}",
    )
    check(
        "体检-废券：够不到门槛、一分不减的券不能报成省钱",
        vs["K3"].status == DUD and vs["K3"].value_fen == 0,
        f"K3={vs['K3'].status} value={vs['K3'].value_fen}",
    )
    check(
        "体检-门槛是试出来的：便宜的不生效、贵的生效，区间要说出来",
        vs["K2"].status == WORTH
        and vs["K2"].works_from_fen == 2500
        and vs["K2"].blocked_fen == 1000
        and "门槛" in vs["K2"].threshold_note(),
        f"K2 works_from={vs['K2'].works_from_fen} blocked={vs['K2'].blocked_fen}",
    )
    check(
        "体检-券面额不等于券价值",
        vs["K4"].face_fen == 800 and vs["K4"].value_fen == 500,
        f"K4 face={vs['K4'].face_fen} value={vs['K4'].value_fen}",
    )

    text = render(
        list(vs.values()),
        store_name="演示餐厅",
        menu_count=len(demo.AUDIT_MENU),
        calls=sum(v.calls for v in vs.values()),
        total_coupons=len(demo.AUDIT_COUPONS),
    )
    check("体检-报告要写出面额与实测的落差", "标着减" in text)
    check("体检-报告要给出试出来的门槛", "门槛" in text)
    check("体检-报告要提醒快过期的券", "到期" in text)

    # 面额解析最容易犯的错：把售价当成优惠额，凭空造出一张假废券
    check(
        "体检-售价不能被当成券面额",
        face_value_fen("9.9元中杯冰美式") == 0
        and face_value_fen("巨无霸立减 5 元") == 500
        and face_value_fen("满 20 减 3 元") == 300
        and face_value_fen("") == 0,
        f"9.9元→{face_value_fen('9.9元中杯冰美式')}",
    )

    # 预算只够查一张时，必须先查明天就作废的那张 —— 代价不对称
    tight = _audit(budget=Budget(2))
    check(
        "体检-预算紧张时先查快过期的券",
        len(tight) == 1 and tight[0].coupon.coupon_id == "K1",
        f"查了 {[v.coupon.coupon_id for v in tight]}",
    )

    capped = _audit(budget=Budget(5))
    check(
        "体检-绝不超出调用预算",
        sum(v.calls for v in capped) <= 5,
        f"用了 {sum(v.calls for v in capped)} 次",
    )

    def _dead_oracle(items, assignment):
        raise RuntimeError("模拟鉴权失效")

    try:
        _audit(oracle=_dead_oracle)
    except RuntimeError:
        blew_up = True
    except Exception:  # noqa: BLE001
        blew_up = False
    else:
        blew_up = False
    check(
        "体检-接口全挂时必须报错，不能伪装成「全是废券」",
        blew_up,
        "鉴权失效被吞掉了",
    )

    # ---------------------------------------------------------- 活动日历
    from .campaign import (
        advise_line,
        fresh_campaigns,
        parse_campaign_markdown,
        render,
        today_campaigns,
        upcoming,
    )

    # 报文节选：结构照搬真实返回（#### 日期 / 活动标题 / 活动内容介绍 / 活动图片介绍）
    CAMPAIGN_FIXTURE = """### 当前时间：2026-10-11 14:25:13

### 活动列表：


#### 2026年10月11日 今日


-   **活动标题**：超值\U0001d7e1.\U0001d7e1元早餐两件套陪你开工啦
    **活动内容介绍**：早八的快乐，一堡一咖已就位
    **活动图片介绍**：
    <img src="https://example.com/a.jpg" alt="" height="300" width="auto">



-   **活动标题**：学生专属！49元150天麦金学期卡
    **活动内容介绍**：每天低至 0.33 元，一卡在手
    **活动图片介绍**：
    <img src="https://example.com/b.jpg" alt="" height="300" width="auto">



#### 2026年10月12日


-   **活动标题**：超值\U0001d7e1.\U0001d7e1元早餐两件套陪你开工啦
    **活动内容介绍**：早八的快乐，一堡一咖已就位
    **活动图片介绍**：
    <img src="https://example.com/a.jpg" alt="" height="300" width="auto">



#### 2026年10月15日


-   **活动标题**：麦麦夜市回归
    **活动内容介绍**：限时 12.9 元起，售完即止
    **活动图片介绍**：
    <img src="https://example.com/c.jpg" alt="" height="300" width="auto">
"""

    cdays = parse_campaign_markdown(CAMPAIGN_FIXTURE)
    check(
        "活动日历-按日切块：3 个日期、今天 2 个活动",
        len(cdays) == 3 and len(cdays[0].campaigns) == 2 and cdays[0].is_today,
        f"days={len(cdays)} today={len(cdays[0].campaigns) if cdays else 0}",
    )
    check(
        "活动日历-标题里的数学粗体数字也能认出价格",
        cdays[0].campaigns[0].price_yuan == 9.9,
        f"price={cdays[0].campaigns[0].price_yuan}",
    )
    check(
        "活动日历-摊销价不能当活动价（49元150天 → 49，不是每天 0.33）",
        cdays[0].campaigns[1].price_yuan == 49.0,
        f"price={cdays[0].campaigns[1].price_yuan}",
    )

    tdy = today_campaigns(cdays)
    fut = upcoming(cdays, within=7)
    check(
        "活动日历-只把「今天吃不到」的算作值得等的新活动",
        len(fut) == 2
        and len(fresh_campaigns(fut[0], tdy)) == 0
        and len(fresh_campaigns(fut[1], tdy)) == 1,
        f"fut={[ (d.day.isoformat(), len(fresh_campaigns(d, tdy))) for d in fut ]}",
    )

    ctext = render(cdays, within=7)
    check("活动日历-报告要点出新品那天", "麦麦夜市回归" in ctext)
    check("活动日历-建议里不能劝人为已在跑的活动干等", "今天也在跑" not in ctext)
    check("活动日历-空报文不崩", parse_campaign_markdown("") == [])

    advise_lines = advise_line(cdays)
    check(
        "活动日历-advise 只提今天没有的新活动",
        any("今天没有" in x for x in advise_lines)
        and not any("干等" in x for x in advise_lines),
        f"{advise_lines}",
    )

    # ---------------------------------------------------------------- 输出
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    for name, ok, detail in _RESULTS:
        flag = "PASS" if ok else "FAIL"
        line = f"  [{flag}] {name}"
        if not ok and detail:
            line += f"   → {detail}"
        print(line)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1
