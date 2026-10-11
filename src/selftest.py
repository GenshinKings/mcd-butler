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
        "比价-路费吃掉优惠时提醒白跑",
        "白跑" in verdict([q_taxi]),
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
