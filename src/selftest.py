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
import re

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
    valid_until_end,
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

# 「今天」的运行时缓存：`run()` 每轮开头清空一次。
_TODAY: dict = {}


def _today():
    """这一轮自检里的「今天」，取一次就钉死。

    散在各处各取一次的话，跨零点的那一次运行会前后拿到两个日期，
    于是「演示输出的日期 == 今天」这类断言随机翻红 ——
    一条只在半夜 00:00 前后一秒才红的断言，比没有还糟：它教会人忽略红条。
    """
    from datetime import date

    if "d" not in _TODAY:
        _TODAY["d"] = date.today()
    return _TODAY["d"]


def check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


def _failure_lines(results: list[tuple[str, bool, str]]) -> list[str]:
    """把失败项汇总成几行，供 `run()` 在结尾再印一遍。

    为什么要有这一段：失败项混在两百多行 PASS 里滚过去了，用户只看最后一行
    「201/203 通过」很可能根本没注意到有红的。一份自称「验证你环境没装坏」
    的自检，翻红了必须让人一眼看见 —— 悄悄红了等于没红。

    为什么单独成函数：写成 `run()` 里的一段 `for` 循环的话，删掉它不会有任何
    一条断言变红（自检自己是绿的，永远不会走到这段）。抽出来才能离线断言。
    """
    bad = [(n, d) for n, ok, d in results if not ok]
    if not bad:
        return []
    out = [f"❌ 有 {len(bad)} 条没过："]
    out += [f"   - {n}" + (f"   → {d}" if d else "") for n, d in bad]
    out.append("   自检不过 = 项目当前状态与预期不符。请不要跳过这几条。")
    return out


def _reentrant(totals: list[int]) -> bool:
    """连跑若干轮得到的断言总数是否一致（自检可重入的判据）。

    为什么单独成函数：写死 `stable = True` 之后，`--twice` 会永远打印
    「自检可重入 ✅」，而 `--twice` 是 mutate.py 发现「_RESULTS 没清空」
    这类污染的唯一手段 —— 判据失效等于把守门的换成了个摆设。
    抽成纯函数，这条判据才能被离线断言到。
    """
    return len(set(totals)) == 1


def run_repeat(times: int = 2) -> int:
    """连跑若干轮 `run()`，用来证明自检本身是**可重入**的。

    为什么单跑一轮证明不了：`_RESULTS` 是模块级列表，`run()` 不清空的话
    第二轮会在 190 条后面再追加 190 条，打印出「380/380」并且**全绿** ——
    污染同时放大了分子和分母，光看「N/M 通过」永远看不出来。
    必须连跑两轮、拿两轮的**总数**互相比，这个洞才现形。

    `scripts/mutate.py` 正是靠这个入口跑变异测试的。
    """
    times = max(1, int(times))
    totals: list[int] = []
    rc = 0
    for _ in range(times):
        rc = run()
        totals.append(len(_RESULTS))
    stable = _reentrant(totals)
    if not stable:
        print(f"\n❌ 自检不可重入：连跑 {times} 轮得到的断言数是 {totals}，本应一致。")
        print("   （`_RESULTS` 是模块级列表，`run()` 开头必须清空它。）")
        return 1
    print(f"\n连跑 {times} 轮，每轮 {totals[0]} 条断言 —— 自检可重入 ✅")
    return rc


def run() -> int:
    # 必须清空：_RESULTS 是模块级列表，run() 跑两次会输出「294/294」，
    # 而且上一次的失败记录会被继承。单条 CLI 调用掩盖了这个问题，
    # 任何复用进程的做法（pytest 化、批量校验）都会拿到污染后的分子分母。
    _RESULTS.clear()

    _TODAY.clear()  # 每一轮重新钉一次「今天」
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
    # 预算必须小到「optimize 一定会吃光」。用 40 的话这条断言锁的是
    # 演示数据恰好够费，而不是契约本身 —— 演示数据一改就塌，还查不出原因。
    opt_budget = Budget(3)
    eng = CouponOptimizer(demo.demo_oracle, max_calls=3, budget=opt_budget)
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
    check(
        "契约-裸买基准先于optimize可正常算出",
        # 不能只断言 "is not None and >= 0" —— 价格恒非负，后半句是恒真的，
        # 等于只测了「没崩」。这里钉住真实数值：裸买 = 菜单原价。
        early is not None
        and early.price_fen == sum(i.unit_price_fen * i.quantity for i in cart),
        f"{early.price_fen if early else None}",
    )

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
    # 只断言 isinstance(list) 是注水：返回 [] 也通过。
    # 改为断言**内容**：演示的最优解里不该有白挂的券，有就得点名。
    check(
        "洞察-演示最优解里没有白挂的券",
        not red,
        str([(i.product_code, c.title) for i, c in red]),
    )
    # demo 场景里恰好没有冗余券，返回 [] 是正确的 —— 所以真断言不能指望 demo。
    # 自己造一个：oracle 完全无视券（挂不挂同价），那这张券必然是摆设。
    _flat_item = CartItem(product_code="X1", name="测试品", unit_price_fen=2000)
    _flat_coupon = Coupon(coupon_id="X", coupon_code="x", title="看着能减 5 元",
                          product_codes=frozenset({"X1"}))
    _flat_best = Assignment(mapping={0: 0})
    _flat_best.price = PriceResult(price_fen=2000, original_fen=2000)
    red2 = ins.redundant_coupons(
        lambda items, a: PriceResult(price_fen=2000, original_fen=2000),
        [_flat_item],
        [_flat_coupon],
        _flat_best,
    )
    check(
        "洞察-挂了等于没挂的券必须被点名",
        [(it.product_code, c.coupon_id) for it, c in red2] == [("X1", "X")],
        f"{red2!r}",
    )

    splits = ins.split_suggestions(
        demo.demo_oracle,
        cart,
        demo.DEMO_COUPONS,
        lambda c: demo.DEMO_WITH_ORDER_OPTIONS,
        whole_fen=best.price.price_fen,
        per_call_budget=20,
    )
    # 返回的是 (商品, 拆单后总价, 比整单便宜多少) 三元组。
    # 只断言 isinstance(list) 的话，返回 [] 也通过。
    check(
        "洞察-拆单建议带得出金额（不是只返回个 list）",
        isinstance(splits, list)
        and len(splits) >= 1
        and all(
            len(x) == 3
            and hasattr(x[0], "product_code")
            and isinstance(x[1], int)
            and isinstance(x[2], int)
            for x in splits
        ),
        f"{splits[:1]!r}",
    )

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
    # 早先这两条只是把刚传进去的实参回显一遍，改坏实现也不会红。
    # 真要锁的是：payload 里**没有** label（否则会把展示文案发给接口），
    # 以及 frozen（进了 dict/set 才不会变）。
    check(
        "随单购-payload不混展示字段",
        set(wo.payload) == {"membershipCode"}
        and "label" not in wo.payload
        and len(wo.payload) == 1
        and isinstance(wo.payload.get("membershipCode"), str),
        str(wo.payload),
    )
    wo_mutated = False
    try:
        wo.label = "改了"
    except Exception:
        wo_mutated = True
    check("随单购-标签独立且不可变", wo.label == "麦金卡" and wo_mutated)

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
    # 早先这条叫「路程-固定开销存在」，断言的却是 `OVERHEAD_MIN > 0`
    # 和 `MODES["bus"].base_fen == 200` —— 两句都是把被测模块里的常量原样抄回来，
    # 抄的是数据不是行为：谁改了这两个数会红，可谁改了**换算逻辑**一条都不红。
    # 换成「固定开销必须真的落进耗时」「起步价必须真的落进路费」两件真事。
    check(
        "路程-固定开销计入耗时（0 米也要算进出店那几分钟）",
        travel_cost(0, "walk").minutes == OVERHEAD_MIN > 0,
        f"0 米往返 {travel_cost(0, 'walk').minutes} 分钟",
    )
    check(
        "路程-起步价计入路费且与距离无关（公交每公里价为 0）",
        travel_cost(0, "bus").money_fen == 200
        and travel_cost(5000, "bus").money_fen == 200
        and travel_cost(0, "walk").money_fen == 0,
        f"公交 0 米 {travel_cost(0, 'bus').money_fen} 分 / 5000 米 {travel_cost(5000, 'bus').money_fen} 分",
    )

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

    # 打烊门店必须被排除（businessStatus 实测为布尔值，True=营业中）。
    # 这一条的断言在文末「24.」里 —— 早先它在这里自己又写了一遍同样的列表推导式、
    # 再断言自己算出来的结果：cli.run_compare 那行改坏自检一条都不红。
    # 守不清哪怕一行 Tailor-made 的实现，等于没有这条自检。

    # 19. 券范围解释：卡包接口只有 Markdown、没有券码，必须向用户说清楚
    # 「我明明有 4 张券为什么只用 1 张」，否则用户会以为引擎算漏了。
    from .cli import _explain_coupon_scope

    try:
        for n in (0, 1, 2):
            _explain_coupon_scope(n)
        # 不能写 check(..., True) —— 那是字面量，跟被测代码零耦合。
        # 真要断言的是：三档数给出的解释互不相同（都说了同一句等于没说）。
        import io as _io
        import contextlib as _ctx
        outs = []
        for n in (0, 1, 2):
            buf = _io.StringIO()
            with _ctx.redirect_stdout(buf):
                _explain_coupon_scope(n)
            outs.append(buf.getvalue())
        # 0 张和 1 张必须解释「券码拿不到」这件事；
        # 2 张以上时券已经在用了，多说一句是噪音，故意不说。
        check(
            "券范围-券没用上时要解释清楚，够用时不要啰嗦",
            "券码" in outs[0]
            and "券码" in outs[1]
            and outs[2] == "",
            f"{outs!r}"[:160],
        )
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
    # 早先这条跟上一行的 `set(progress_bar(6, 6)) == {"█"}` 完全重复，是凑数。
    # 换成真正没人守的方向：进度条长度必须等于总格数，实心数等于当前级数。
    check(
        "段位-进度条长度与实心数正确",
        len(progress_bar(3, 6)) == 12
        and progress_bar(3, 6).count("█") == 6
        and progress_bar(6, 6).count("░") == 0
        and progress_bar(0, 6).count("█") == 0,
        f"{progress_bar(3, 6)}|{progress_bar(9, 12)}",
    )
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

    # quantity>1 挂券：接口会把整行数量静默重置成 1（实测麦咖啡冰美式中杯），
    # 于是「买 2 杯挂 1 张券」被算成只收 1 杯的钱，引擎会凭空报出大额优惠。
    # 修法是拆成 quantity 行 quantity=1，券只挂第一行（一张券只能用一次）。
    two = CartItem(product_code="P2", name="双份", quantity=2)
    rc4 = _RecClient(
        {"price": 2180, "productList": [{"quantity": 1}, {"quantity": 1}]}
    )
    make_price_oracle(rc4, "S1", [coupon])([two], Assignment(mapping={0: 0}))
    sent4 = rc4.payloads[-1]["items"]
    check(
        "oracle-多件挂券要拆行，不能让接口吞掉数量",
        len(sent4) == 2
        and all(e["quantity"] == 1 for e in sent4)
        and sent4[0].get("couponCode") == "k1"
        and "couponCode" not in sent4[1],
        str(sent4),
    )
    rc5 = _RecClient({"price": 2380, "productList": [{"quantity": 1}, {"quantity": 1}]})
    make_price_oracle(rc5, "S1", [coupon])([two], Assignment(mapping={}))
    sent5 = rc5.payloads[-1]["items"]
    check(
        "oracle-多件不挂券：拆行且两行都不带券码",
        len(sent5) == 2
        and all("couponCode" not in e for e in sent5),
        str(sent5),
    )

    # 接口返回的数量与请求不符时必须报错，不能让优化器采信虚假的便宜
    rc6 = _RecClient({"price": 990, "productList": [{"quantity": 1}]})
    qty_raised = False
    try:
        make_price_oracle(rc6, "S1", [coupon])([two], Assignment(mapping={0: 0}))
    except Exception:
        qty_raised = True
    check("oracle-接口少算件数时报错而非采信", qty_raised)

    # 反向的两个坑：套餐会被接口拆成子件返回，明细里也可能压根没有
    # quantity 字段。这两种都不是「吞数量」，报错是冤枉接口 ——
    # 而 engine 累计 3 次异常就会让整条链路崩掉，代价太大。
    def _qty_ok(payload_data):
        try:
            rc = _RecClient(payload_data)
            make_price_oracle(rc, "S1", [coupon])([two], Assignment(mapping={0: 0}))
        except Exception:
            return False
        return True

    check(
        "oracle-套餐被拆成子件返回时不能误报「件数被吞」",
        _qty_ok({"price": 6290, "productList": [{"quantity": 1} for _ in range(5)]}),
        "套餐展开被当成数量异常了",
    )
    check(
        "oracle-明细里没有 quantity 字段时不报错（没得核对就不核对）",
        _qty_ok({"price": 6290, "productList": [{"productCode": "D008"}] * 3}),
        "缺字段被当成返回 0 件了",
    )
    # mapping 的值越界：静默跳过会让这一行「假装挂了券却没挂」，
    # 算出一个比真实更贵的价，优化器还会当成挂券方案采纳。
    #
    # 这里早先写的是 `[one]` —— 全模块没有绑定 `one`，于是抛的是 NameError，
    # 照样被 `except Exception` 兜住，断言恒为 True。删掉 _expand 的越界守卫
    # 这条也照样全绿，等于零防护。下面改成真的构造一次越界调用。
    def _raises_with(bad: int) -> bool:
        try:
            make_price_oracle(_RecClient({"price": 2500}), "S1", [coupon])(
                [item], Assignment(mapping={0: bad})
            )
        except Exception:
            return True
        return False

    check("oracle-券下标越界必须报错，不能静默不挂券", _raises_with(7))
    # 负下标才是最阴的那个：它不抛 IndexError， coupons[-1] 会**静默挂上另一张券**。
    # 只有显式的范围守卫挡得住，靠 Python 自己的下标检查挡不住。
    other = Coupon(coupon_id="c2", coupon_code="k2", title="第二张")
    rc_neg = _RecClient({"price": 2500})
    neg_bad = False
    try:
        make_price_oracle(rc_neg, "S1", [coupon, other])([item], Assignment(mapping={0: -1}))
    except Exception:
        # 报错就是对的：越界必须被挡住，不管正负。
        neg_bad = False
    else:
        # 没报错的话，唯一可接受的结果是「压根没把第二张券发出去」。
        # 如果 coupons[-1] 静默生效，用户以为挂的是 A 券，实际挂的是 B 券，
        # 算出来的价是错的，而且屏幕上看不出任何异常。
        sent_items = (rc_neg.payloads[-1] if rc_neg.payloads else {}).get("items") or []
        neg_bad = any(
            isinstance(it, dict) and it.get("couponCode") == "k2" for it in sent_items
        )
    check(
        "oracle-负下标不能静默挂到另一张券上",
        not neg_bad,
        f"payload={rc_neg.payloads[-1] if rc_neg.payloads else None}",
    )

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
        _is_fatal,
        audit_coupons,
        face_value_fen,
        render,
        DEAD,
        DUD,
        WORTH,
    )
    from .mcd_client import McpError, McpFatalError

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

    # ── 部分失败：线上真实形态。三种必须分得清，混淆任何两种都会出事。

    def _reject_some(items, assignment):
        """业务性拒绝：券跟这件商品不匹配（实测「促销规则不支持该商品」）。

        这是**正常答案**，不是故障 —— 换一件商品照样能查。
        为它放弃整张券，等于把「麦咖啡券挂不上汉堡」说成「接口坏了」。
        """
        # 只拒绝巨无霸（A4）。它是「满 20 减 3」唯一够得到门槛的那件，
        # 拒绝它最能暴露问题：剩下的样本算出来一分不减，
        # 若还敢报成「废券」，就是把接口报错当成了券没用。
        if items and items[0].product_code == "A4":
            raise RuntimeError("促销规则不支持该商品")
        return demo.audit_oracle(items, assignment)

    try:
        partial_vs = {v.coupon.coupon_id: v for v in _audit(oracle=_reject_some)}
        partial_ok = True
    except Exception as exc:  # noqa: BLE001
        partial_ok = False
        partial_vs = {}
        partial_err = f"{type(exc).__name__}: {exc}"
    check(
        "体检-业务性拒绝（促销规则不支持该商品）不能让整份报告崩掉",
        partial_ok,
        partial_err if not partial_ok else "",
    )
    check(
        "体检-有商品报错时要明说结论可能偏保守，不能假装样本完整",
        partial_ok
        and partial_vs.get("K2") is not None
        and partial_vs["K2"].partial
        and "偏保守" in partial_vs["K2"].note,
        f"K2={partial_vs.get('K2')}",
    )
    check(
        "体检-样本不全时不能直接判「废券」（报错不等于这张券没用）",
        partial_ok
        and partial_vs.get("K2") is not None
        # 这个字样只在 render() 的输出里，note 字段从来不产出它 ——
        # 守在 note 上对任何实现都恒真。改成直接查渲染出来的文本。
        and "偏保守" in render(list(partial_vs.values())),
        f"K2.note={getattr(partial_vs.get('K2'), 'note', '')}",
    )

    def _dies_after(
        ok_calls: int, exc: BaseException | None = None
    ):  # noqa: B008 - 刻意默认构造，见下
        """造一个「成功 ok_calls 次之后开始报错」的 oracle。

        用工厂函数而不是函数属性计数器：函数属性会跨调用残留，
        第一次跑完 n=7，任何复用它的用例第一条调用就抛；
        而且 `n > 2` 这种写法把 audit 的内部调用次数硬编码进了断言，
        一旦预算/采样逻辑改动，用例会莫名其妙地变绿或变红。
        """
        state = {"n": 0}
        boom = exc if exc is not None else McpFatalError(
            "Token 无效或已过期（HTTP 401）。"
        )

        def oracle(items, assignment):
            state["n"] += 1
            if state["n"] > ok_calls:
                raise boom
            return demo.audit_oracle(items, assignment)

        return oracle

    try:
        _audit(oracle=_dies_after(2))
    except McpFatalError as exc:
        halfway_blew = "Token" in str(exc)
    except Exception:  # noqa: BLE001
        halfway_blew = False
    else:
        halfway_blew = False
    check(
        "体检-跑到一半 Token 失效也必须报错（不能只防「一次都没成功」）",
        halfway_blew,
        "半程鉴权失效被吞掉了",
    )

    # 错误性质的判定**只能看类型，不能看文案**。早先这里用正则匹配
    # 「401 / token / 鉴权 / 授权 / 超时 / 限流」，实测 15 条真实文案错 12 条。
    # 下面两组用例把两个方向的误判都钉死：
    check(
        "体检-文案里带「授权/超时/401」的业务性拒绝不能被当成环境故障",
        not _is_fatal(McpError("该优惠券仅限授权门店使用"))
        and not _is_fatal(McpError("订单超时未支付，请重新下单"))
        and not _is_fatal(McpError("门店 4010233 暂不支持该券"))
        and not _is_fatal(McpError("促销规则不支持该商品")),
        "业务性拒绝被误判成环境故障",
    )
    check(
        "体检-真正的环境故障必须被认出来（哪怕文案里没有关键词）",
        _is_fatal(McpFatalError("502 Bad Gateway"))
        and _is_fatal(McpFatalError("Connection reset by peer"))
        and _is_fatal(McpFatalError("登录已失效，请重新登录"))
        and _is_fatal(McpFatalError("网络错误: timed out")),
        "环境故障被漏掉了",
    )

    try:
        _audit(oracle=_dies_after(2, McpError("门店 4010233 暂不支持该券")))
        biz_ok = True
    except Exception:  # noqa: BLE001
        biz_ok = False
    check(
        "体检-业务性拒绝绝不能让整份报告崩掉（哪怕它长得像错误码）",
        biz_ok,
        "一条业务性拒绝把整份券包体检作废了",
    )

    # ── 两条「改坏了不会有任何用例变红」的核心逻辑，必须单独钉死。
    #    验证时把它们退回旧实现，138 条自检全绿 —— 等于没有任何回归防护。

    from .audit import _candidates, _probe

    class _Opt:
        """随单购档位替身：`_probe` 只用到 `signature()` 做缓存键。"""

        def __init__(self, label: str):
            self.label = label

        def signature(self) -> str:
            return self.label

    def _mutex_oracle(items, assignment):
        """麦金卡那一档更便宜，但**挂券不再额外减**（券与随单购互斥）。"""
        if getattr(assignment.with_order, "label", "") == "麦金卡":
            return PriceResult(price_fen=1500, original_fen=2000, discount_fen=0)
        off = 400 if assignment.mapping else 0
        return PriceResult(price_fen=2000 - off, original_fen=2000, discount_fen=0)

    _gap, _trunc = _probe(
        _mutex_oracle,
        [demo.AUDIT_MENU[3]],
        [None, _Opt("麦金卡")],
        0,
        None,
        [0],
        {},
        ("A4", 2500),
        [],
    )
    check(
        "体检-券与随单购互斥时仍要判出券自身的价值（必须同档位比）",
        _gap is not None and _gap[0] == 400,
        f"gap={_gap[0] if _gap else None}"
        "（旧实现两边各自取最优档，券被作废 → gap=0 → 好券判成废券）",
    )

    _big_menu = [CartItem(f"B{i}", f"商品{i}", 1, 200 + i * 100) for i in range(10)]
    _uni = Coupon("U1", "U1", "通用立减", frozenset())
    _p3 = _candidates(_big_menu, _uni, 3)
    _p1 = _candidates(_big_menu, _uni, 1)
    check(
        "体检-probe 决定试几件，且最贵那件必须在样本里",
        len(_p3) == 3
        and _p3[-1].effective_price_fen() == 1100
        and len(_p1) == 1
        and _p1[0].effective_price_fen() == 1100
        and len(_candidates(_big_menu, _uni, 100)) == 10
        and _candidates(_big_menu, _uni, 0) == [],
        f"probe3={[i.effective_price_fen() for i in _p3]} "
        f"probe1={[i.effective_price_fen() for i in _p1]}",
    )

    # 券有效期是「起-止」区间，取错一头会把"还有 6 天"报成"今天到期"
    check(
        "券-有效期取结束日而不是开始日",
        valid_until_end("2026-10-11 00:00:00-2026-10-17 23:59:59") == "2026-10-17"
        and valid_until_end("2026-10-17") == "2026-10-17"
        and valid_until_end("") == ""
        and valid_until_end(None) == ""
        and valid_until_end("没有日期") == "",
        valid_until_end("2026-10-11 00:00:00-2026-10-17 23:59:59"),
    )

    # ---------------------------------------------------------- 活动日历
    from datetime import date as _date
    from datetime import timedelta as _td

    from .campaign import (
        Campaign,
        CampaignDay,
        advise_line,
        build_markdown,
        fresh_campaigns,
        parse_campaign_markdown,
        render,
        today_campaigns,
        upcoming,
        _first_price,
    )

    # 报文节选：结构照搬真实返回（#### 日期 / 活动标题 / 活动内容介绍 / 活动图片介绍）
    #
    # 日期**必须相对今天生成，不能写死**。写死过一次（fixture 里刻着
    # 2026年10月11日/12日/15日），于是本机时钟越过那几天之后
    # `upcoming()` 直接返回空、三条用例集体变红，而代码一行没错。
    # 自检里写死时间 = 在测日历而不是测代码，是个定时炸弹。
    _T0 = _today()
    _BREAKFAST = "超值\U0001d7e1.\U0001d7e1元早餐两件套陪你开工啦"
    _CARD = "学生专属！49元150天麦金学期卡"
    _NIGHT = "麦麦夜市回归"

    # 构造器复用 `campaign.build_markdown`，跟演示报文**同一个**：
    # 两份拷贝已经漂过一次（一份「+3 天」一份「+4 天」），
    # 而演示那份坏了 129 条自检一条都不会红。
    CAMPAIGN_FIXTURE = build_markdown(
        [
            # 今天：两个活动，其中一个只出现在今天（用来验「今天吃不到的才算新」）
            (
                _T0,
                " 今日",
                [
                    (_BREAKFAST, "早八的快乐，一堡一咖已就位"),
                    (_CARD, "每天低至 0.33 元，一卡在手"),
                ],
            ),
            # 明天：跟今天完全重复 —— 不该被当成「值得等」
            (_T0 + _td(days=1), "", [(_BREAKFAST, "早八的快乐，一堡一咖已就位")]),
            # 四天后：新品，这才是真正该等的那天
            (_T0 + _td(days=4), "", [(_NIGHT, "限时 12.9 元起，售完即止")]),
        ]
    )

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

    tdy = today_campaigns(cdays, _T0)
    fut = upcoming(cdays, _T0, within=7)
    check(
        "活动日历-只把「今天吃不到」的算作值得等的新活动",
        len(fut) == 2
        and len(fresh_campaigns(fut[0], tdy)) == 0
        and len(fresh_campaigns(fut[1], tdy)) == 1,
        f"fut={[ (d.day.isoformat(), len(fresh_campaigns(d, tdy))) for d in fut ]}",
    )

    ctext = render(cdays, _T0, within=7)
    # 只看「接下来」那一段：今天已在跑的活动本来就列在报告上半部分，
    # 要防的是它混进「值得等」的名单里。
    fresh_part = ctext.split("接下来", 1)[1] if "接下来" in ctext else ""
    check("活动日历-报告要点出新品那天", _NIGHT in fresh_part, fresh_part.strip()[:90])
    check(
        "活动日历-今天已在跑的活动不能出现在「值得等」名单里",
        _BREAKFAST not in fresh_part,
        fresh_part.strip()[:90],
    )
    check("活动日历-空报文不崩", parse_campaign_markdown("") == [])
    check(
        "活动日历-解析不出内容要说清楚，不能伪装成「今天没有活动」",
        "没返回内容" in render([], _T0),
        (render([], _T0).strip().splitlines() or [""])[-1],
    )
    check(
        "活动日历-「今日」标记不能盖过真实日期（时钟不在那天就是没有活动）",
        today_campaigns(cdays, _T0 + _td(days=30)) == [],
        f"{[c.title for c in today_campaigns(cdays, _T0 + _td(days=30))]}",
    )

    advise_lines = advise_line(cdays, _T0)
    check(
        "活动日历-advise 只提今天没有的新活动",
        any("今天没有" in x for x in advise_lines)
        and not any(_BREAKFAST in x for x in advise_lines[1:]),
        f"{advise_lines}",
    )

    # 演示报文必须能解析出来 —— 它跟 fixture 用的是同一个构造器，
    # 但**从来没人测过**。坏掉时 demo 会打印一屏空话，而自检全绿。
    ddays = parse_campaign_markdown(demo.campaign_demo_markdown())
    check(
        "活动日历-离线演示报文必须解析得出内容（它坏过，且没人发现）",
        len(ddays) == 3
        and len(today_campaigns(ddays, _T0)) == 2
        and "麦麦夜市回归" in render(ddays, _T0),
        f"解析出 {len(ddays)} 天",
    )

    # 卖价 vs 优惠额/门槛：`min()` 偏爱小数字，不把优惠额先挖掉就会稳定取错
    check(
        "活动日历-「满 20 减 3 元」里的 3 是优惠额不是卖价",
        _first_price("满 20 减 3 元，仅需 9.9 元") == 9.9
        and _first_price("买一送一（省 12 元）") is None
        and _first_price("任意消费满 6 元即可换购 9.9 元麦旋风") == 9.9
        and _first_price("超值\U0001d7e1.\U0001d7e1元早餐两件套") == 9.9,
        f"满20减3→{_first_price('满 20 减 3 元，仅需 9.9 元')} "
        f"省12→{_first_price('买一送一（省 12 元）')}",
    )

    # 同名活动**更便宜**也算新：只看标题会把「今天 12.9 / 三天后 9.9」判成今天已有
    _cheaper_day = CampaignDay(
        day=_T0 + _td(days=2),
        campaigns=(
            Campaign(title=_NIGHT, summary="更便宜了", price_yuan=9.9),
        ),
    )
    check(
        "活动日历-同名但更便宜的活动也算「值得等」",
        len(fresh_campaigns(_cheaper_day, [_c for _c in tdy if _c.title != _NIGHT] + [
            Campaign(title=_NIGHT, summary="今天", price_yuan=12.9)
        ])) == 1,
        "更便宜的同名活动被当成今天已有而漏掉了",
    )

    # ---------------------------------------------------------- 餐品详情
    # 报文是 `query-meal-detail` 的真实返回（门店 1450713，2026-10-11 实测）。
    # 这一层唯一能变成钱的东西是 `diffPrice` 里的**负号** —— 带负号的换项是白捡的钱。
    from .meal_detail import (
        diff_price_fen,
        parse_meal_detail,
        render as render_meal,
    )

    check(
        "餐品详情-diffPrice 的符号不能搞反（「- ¥4」是便宜 4 块）",
        diff_price_fen("+ ¥1.5") == 150
        and diff_price_fen("- ¥4") == -400
        and diff_price_fen("+ ¥0") == 0
        and diff_price_fen("") == 0
        and diff_price_fen(None) == 0
        and diff_price_fen("+￥2.50") == 250,
        f"+1.5→{diff_price_fen('+ ¥1.5')} -4→{diff_price_fen('- ¥4')}",
    )
    check(
        "餐品详情-脏报文不崩（None / 空 / 不是 dict 都返回 None）",
        parse_meal_detail(None) is None
        and parse_meal_detail({}) is None
        and parse_meal_detail("坏了") is None,
    )

    MEAL_FIXTURE = {
        "code": "9900005456",
        "name": "麦辣鸡腿汉堡三件套",
        "rounds": [
            {
                "id": 1,
                "name": "麦辣鸡腿汉堡",
                "choices": [
                    {
                        "code": "521900",
                        "name": "麦辣鸡腿汉堡",
                        "isDefault": 1,
                        "diffPrice": "+ ¥0",
                        "supportModify": True,
                    }
                ],
            },
            {
                "id": 2,
                "name": "选择套餐内小食",
                "choices": [
                    {
                        "code": "4810",
                        "name": "中薯条",
                        "isDefault": 1,
                        "diffPrice": "+ ¥0",
                        "supportModify": False,
                    },
                    {
                        "code": "521901",
                        "name": "月下相思扭扭薯",
                        "isDefault": 0,
                        "diffPrice": "+ ¥1.5",
                        "supportModify": False,
                    },
                    {
                        "code": "4821",
                        "name": "小杯玉米杯",
                        "isDefault": 0,
                        "diffPrice": "- ¥0.5",
                        "supportModify": False,
                    },
                ],
            },
        ],
        "supportModify": False,
    }
    md = parse_meal_detail(MEAL_FIXTURE)
    check(
        "餐品详情-套餐组成与默认项要认出来",
        md is not None
        and md.is_combo
        and len(md.rounds) == 2
        and md.rounds[1].default_choice() is not None
        and md.rounds[1].default_choice().name == "中薯条",
        f"rounds={len(md.rounds) if md else 0}",
    )
    check(
        "餐品详情-换项里「反而更便宜」的那几个必须单独挑出来",
        md is not None
        and [(c.name, c.diff_fen) for _b, c in md.cheaper_swaps()]
        == [("小杯玉米杯", -50)],
        f"{[(c.name, c.diff_fen) for _b, c in md.cheaper_swaps()] if md else None}",
    )
    check(
        "餐品详情-最贵的换项要说出来（换错最多加多少）",
        md is not None
        and md.priciest_swap() is not None
        and md.priciest_swap()[1].diff_fen == 150,
        f"{md.priciest_swap() if md else None}",
    )
    mtext = render_meal(md)
    check(
        "餐品详情-报告里要说清差价来自接口字段",
        "换成这些反而更便宜" in mtext and "diffPrice" in mtext,
        mtext[-60:],
    )
    single = parse_meal_detail(
        {"code": "521954", "name": "麦当劳联名复古棒球帽", "rounds": [], "supportModify": False}
    )
    check(
        "餐品详情-非套餐（rounds 为空）要明说，不能渲染出一片空白",
        single is not None and not single.is_combo and "没有可换的组成部分" in render_meal(single),
    )

    # ============================================================ 回归防护
    # 这一节是「变异测试」逼出来的。
    # 上一轮把 60 处核心逻辑逐个故意改坏，其中 **33 处改坏后自检全绿** ——
    # 那些断言是装饰品。这里补的就是那 33 个洞，外加本轮新修的几处。
    # 判据只有一条：把对应逻辑改坏，这一节必须有一条变红。

    from datetime import date as _date, timedelta as _td

    # ── 1. McpFatalError 必须在**抛错的地方**就被分好类。
    # 之前 selftest 只 import 了 extract_payload，从没碰过 McdMcpClient，
    # 八条 raise 全靠手搓 McpFatalError 来验证，绕开了真实路径。
    import io as _io3
    import urllib.error as _uerr
    import urllib.request as _ureq
    import http.client as _hcli

    from . import mcd_client as _mc

    _real_urlopen = _ureq.urlopen

    def _fatal_type(fake) -> type:
        try:
            _ureq.urlopen = fake
            cli_ = _mc.McdMcpClient("tok", max_retries=0)
            cli_._post({"x": 1})
        except Exception as exc:  # noqa: BLE001
            return type(exc)
        finally:
            _ureq.urlopen = _real_urlopen
        return type(None)

    def _http_error(code):
        def f(req, timeout=None):
            raise _uerr.HTTPError(
                req.full_url, code, "err", {}, _io3.BytesIO(b'{"error":"x"}')
            )
        return f

    def _raise(exc):
        def f(req, timeout=None):
            raise exc
        return f

    check("传输层-401 必须归类为环境故障", _fatal_type(_http_error(401)) is McpFatalError)
    check("传输层-429 必须归类为环境故障", _fatal_type(_http_error(429)) is McpFatalError)
    check("传输层-5xx 必须归类为环境故障", _fatal_type(_http_error(503)) is McpFatalError)
    check("传输层-URLError 必须归类为环境故障",
          _fatal_type(_raise(_uerr.URLError("boom"))) is McpFatalError)
    # 这两个**不是** URLError：urlopen 已经成功返回，错是随后的 read() 抛的。
    # 漏掉这一层，用户看到的是一整屏 traceback 而不是「网络错误，请重试」。
    check("传输层-读到一半断连（RemoteDisconnected）必须归类为环境故障",
          _fatal_type(_raise(_hcli.RemoteDisconnected("reset"))) is McpFatalError)
    check("传输层-读超时（TimeoutError）必须归类为环境故障",
          _fatal_type(_raise(TimeoutError("timeout"))) is McpFatalError)
    check("错误类型-McpFatalError 是 McpError 的子类（老代码才能照常接住）",
          issubclass(McpFatalError, McpError))

    # ── 1b. 429 的退避必须按限流窗口量级来
    # 这段逻辑埋在 _post 的重试循环里时没法断言（要真发一次 HTTP 再挂钟计时），
    # 所以拆成了纯函数 `_backoff_for()`。这里既断言算出来的数，也断言真睡了。
    from .mcd_client import _backoff_for

    _rl = McpFatalError("触发限流（每 Token 每分钟 600 次）。")
    check(
        "传输层-429 退避必须 ≥5 秒（限流窗口 60 秒，退 0.5 秒等于没退）",
        _backoff_for(0, 0.5, _rl) >= 5.0,
        f"{_backoff_for(0, 0.5, _rl)}",
    )
    check(
        "传输层-429 退避要封顶 30 秒（再久命令行在用户眼里就是卡死了）",
        _backoff_for(20, 0.5, _rl) == 30.0,
        f"{_backoff_for(20, 0.5, _rl)}",
    )
    check(
        "传输层-普通失败仍按指数退避（别把 5xx 也一起拖成 5 秒）",
        _backoff_for(0, 0.5, McpFatalError("HTTP 503")) == 0.5,
        f"{_backoff_for(0, 0.5, McpFatalError('HTTP 503'))}",
    )

    _slept: list[float] = []
    _real_sleep = _mc.time.sleep
    _mc.time.sleep = lambda s: _slept.append(s)
    try:
        _ureq.urlopen = _http_error(429)
        try:
            _mc.McdMcpClient("tok", max_retries=1)._post({"x": 1})
        except Exception:  # noqa: BLE001, S110
            pass
    finally:
        _ureq.urlopen = _real_urlopen
        _mc.time.sleep = _real_sleep
    check(
        "传输层-真踩到 429 时，重试前确实等了 ≥5 秒（不是只算出来好看）",
        bool(_slept) and max(_slept) >= 5.0,
        f"slept={_slept}",
    )

    # ── 1c. 非 UTF-8 的响应体不能被 UnicodeDecodeError 穿透成裸 traceback
    # 服务端返回一个 GBK 的错误页、或半截 gzip 时就会遇到。
    class _Gibberish:
        headers: dict = {}

        def read(self):
            return b"\xff\xfe\x00 not utf8 at all"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _gibberish(req, timeout=None):
        return _Gibberish()

    check(
        "传输层-非 UTF-8 响应体必须收成 McpFatalError，不能裸抛 UnicodeDecodeError",
        _fatal_type(_gibberish) is McpFatalError,
    )

    # ── 1d. main() 的两类错误提示必须分开
    from .cli import _error_hint, _replay_baseline_fen, _with_order_label

    _f_rc, _f_lines = _error_hint(McpFatalError("Token 无效或已过期（HTTP 401）。"))
    _b_rc, _b_lines = _error_hint(McpError("促销规则不支持该商品"))
    check(
        "错误提示-环境故障要指到 Token / 网络 / 限流",
        any("Token" in l for l in _f_lines) and any("限流" in l for l in _f_lines),
        str(_f_lines),
    )
    check(
        "错误提示-业务性拒绝不能再让人去重新激活一个好端端的 Token",
        not any("重新激活" in l for l in _b_lines)
        and any("换一家门店" in l for l in _b_lines),
        str(_b_lines),
    )

    # ── 1e. 随单购标签 / 裸买基准（原先埋在 run_replay 里，改坏没人知道）
    _wo = WithOrderOption(payload={"membershipCode": "X"}, label="麦金卡（随单购）")
    check(
        "随单购-标签必须取得到（WithOrderOption 不是 dict，判 dict 恒假）",
        _with_order_label(_wo) == "麦金卡（随单购）",
        repr(_with_order_label(_wo)),
    )
    check("随单购-没选档位时标签是空串，不是 None", _with_order_label(None) == "")

    class _FakeOptimizer:
        """什么都不挂才给真裸买价 2600；一旦挂券/随单购，接口会把卡费算进
        original（3300）—— 这正是当初 brag 账本被虚高的原因。"""

        def evaluate(self, cart, a):
            if a.mapping or a.with_order is not None:
                return PriceResult(price_fen=2000, original_fen=3300)
            return PriceResult(price_fen=2600, original_fen=3300)

    check(
        "复刻-裸买价必须自己重算一遍（拿接口 original 会把卡费算成省下的钱）",
        _replay_baseline_fen(_FakeOptimizer(), [CartItem("Z", "测试")]) == 2600,
        str(_replay_baseline_fen(_FakeOptimizer(), [CartItem("Z", "测试")])),
    )

    # ── 1f. snapshot 不能在 import 阶段就炸（HOME 未设置时 Path.home() 抛错）
    # 在独立命名空间里重跑一遍模块源码，模拟「读不到家目录」的机器。
    import pathlib as _pathlib
    from pathlib import Path as _PathProbe

    from . import snapshot as _snap

    # 让 Path.home() 抛错，模拟「HOME 未设置」的机器（容器 / CI / 精简环境常见）。
    # 注意不能靠往命名空间里塞一个假 Path：模块源码自己 `from pathlib import Path`
    # 会把它覆盖掉。必须改真正的 pathlib.Path。
    def _no_home():
        raise RuntimeError("Can't determine home directory.")

    _real_home = _pathlib.Path.home
    _probe_ns: dict = {"__name__": "snapshot_probe"}
    _probe_ok = False
    _probe_got: object = None
    try:
        _pathlib.Path.home = staticmethod(_no_home)
        exec(
            compile(
                _PathProbe(_snap.__file__).read_text(encoding="utf-8"),
                "snapshot_probe",
                "exec",
            ),
            _probe_ns,
        )
        _probe_got = _probe_ns.get("SNAPSHOT_DIR")
        _probe_ok = _probe_got == _PathProbe.cwd() / ".mcd-butler"
    except Exception as _exc:  # noqa: BLE001
        # 崩在这里正是要防的事：模块级直接 Path.home() 会在 import 阶段炸
        _probe_got = f"{type(_exc).__name__}: {_exc}"
    finally:
        _pathlib.Path.home = _real_home
    check(
        "快照-HOME 读不到时退回当前目录，不能让 import 阶段就把 brag/forget 一起炸掉",
        _probe_ok,
        str(_probe_got),
    )

    # ── 1g. 演示世界也必须讲券有效期
    # 原先 demo_oracle 完全不看 valid_until：时钟一歪（或有人手滑写 _days(-1)），
    # 演示会平静地印出「用一张昨天就过期的券省了 ¥20」—— 假账，且无提示。
    import dataclasses as _dc

    _saved_c1 = demo.DEMO_COUPONS[0]
    _before = demo.demo_oracle([demo.DEMO_MENU[0]], Assignment(mapping={0: 0})).price_fen
    try:
        demo.DEMO_COUPONS[0] = _dc.replace(_saved_c1, valid_until="2020-01-01")
        _after = demo.demo_oracle([demo.DEMO_MENU[0]], Assignment(mapping={0: 0})).price_fen
    finally:
        demo.DEMO_COUPONS[0] = _saved_c1
    check(
        "演示-过期的券一分钱都不能减（演示世界也得讲有效期）",
        _after == demo.DEMO_MENU[0].unit_price_fen,
        f"过期券仍减到了 {_after}",
    )
    check(
        "演示-没过期的券照常生效（上一条不能是靠「永远返回 0」蒙对的）",
        _before == demo.DEMO_MENU[0].unit_price_fen - 500,
        f"{_before}",
    )

    # ── 2. 黄历不能把环境故障吞成「这家店没券」
    from .almanac import _call

    def _fatal_oracle(items, a):
        raise McpFatalError("Token 无效或已过期（HTTP 401）。")

    alm_raised = False
    try:
        _call(_fatal_oracle, Budget(5), [CartItem("Z", "测试")], Assignment(mapping={}))
    except McpFatalError:
        alm_raised = True
    except Exception:
        alm_raised = False
    check("黄历-环境故障必须穿透，不能印成「这家门店没有可挂载的券」", alm_raised)

    # ── 3. 卡包三处查询：半路限流不能印成「你一张券都没有」
    from .assets import fetch_claimable, fetch_my_coupons, fetch_points

    class _DeadClient:
        def call_json(self, *a, **k):
            raise McpFatalError("触发限流（每 Token 每分钟 600 次）。")

        def call(self, *a, **k):
            raise McpFatalError("触发限流（每 Token 每分钟 600 次）。")

    assets_raised = 0
    for fn in (fetch_points, fetch_my_coupons, fetch_claimable):
        try:
            fn(_DeadClient())
        except McpFatalError:
            assets_raised += 1
        except Exception:  # noqa: BLE001, S110
            pass
    check("卡包-限流/断网不能降级成「卡包为空」", assets_raised == 3, f"{assets_raised}/3")

    # ── 4. Verdict 的三个字段：之前一个断言都没有
    check(
        "体检-probed 记的是成功算出价的件数，不是候选件数",
        partial_ok
        and partial_vs.get("K2") is not None
        and partial_vs["K2"].probed == 1
        and partial_vs["K2"].attempted == 2,
        f"probed={getattr(partial_vs.get('K2'), 'probed', None)} "
        f"attempted={getattr(partial_vs.get('K2'), 'attempted', None)}",
    )
    check(
        "体检-全部成功时 probed 等于 attempted",
        vs["K2"].probed == vs["K2"].attempted and vs["K2"].probed > 0,
        f"{vs['K2'].probed}/{vs['K2'].attempted}",
    )

    # ── 5. 健康度的分母、样本告警、文案
    from .audit import UNKNOWN, Verdict, render as render_audit

    def _mk(cid: str, status: str) -> Verdict:
        return Verdict(
            coupon=Coupon(coupon_id=cid, coupon_code=cid, title=cid), status=status
        )

    _t3 = render_audit([_mk("a", WORTH), _mk("b", UNKNOWN), _mk("c", UNKNOWN)])
    check(
        "体检-健康度分母必须含没查出来的券（不能拿未知当好消息）",
        "33%" in _t3 and "没查出来" in _t3,
        _t3[:160],
    )
    check(
        "体检-样本太小时要自己说清楚",
        "样本太小" in render_audit([_mk("a", WORTH), _mk("b", DEAD)]),
    )
    _vd = _mk("d", DUD)
    _vd.probed = 3
    check("体检-确实一分没减时才说「一分没减」", "一分没减" in render_audit([_vd]))

    # ── 6. 活动日历：label() 不接受报文里的「今日」标记
    from .campaign import Campaign, CampaignDay, advise_line, upcoming

    _T1 = _today()
    check(
        "日历-标了「今日」但不是今天就不能叫今天",
        CampaignDay(day=_T1 + _td(days=30), is_today=True).label(_T1) != "今天",
        CampaignDay(day=_T1 + _td(days=30), is_today=True).label(_T1),
    )
    check(
        "日历-真的是今天就要叫今天（不看标记）",
        CampaignDay(day=_T1, is_today=False).label(_T1) == "今天",
    )

    # ── 7. within 参数必须真的接进来（写死 7 天跟 --days 脱钩）
    _d2 = CampaignDay(
        day=_T1 + _td(days=2), campaigns=(Campaign(title="两天后", summary="", price_yuan=9.0),)
    )
    _d4 = CampaignDay(
        day=_T1 + _td(days=4), campaigns=(Campaign(title="四天后", summary="", price_yuan=5.0),)
    )
    check(
        "日历-upcoming 的 within 真的生效（写死 7 天会漏改）",
        [d.day for d in upcoming([_d2, _d4], _T1, within=3)] == [_d2.day],
    )
    check(
        "日历-advise_line 的 within 真的生效",
        "四天后" not in "\n".join(advise_line([_d2, _d4], _T1, within=3)),
    )

    # ── 8. _first_price 的正文兜底分支（之前四次调用都只传 title，从没走到）
    from .campaign import _first_price, build_markdown, parse_campaign_markdown

    check(
        "日历-正文里的价也要先挖掉门槛再取",
        _first_price("标题里没有价", "任意消费满 6 元即可加购 9.9 元麦旋风") == 9.9,
        str(_first_price("标题里没有价", "任意消费满 6 元即可加购 9.9 元麦旋风")),
    )
    check(
        "日历-标题里只有门槛时返回 None，不能把门槛当卖价",
        _first_price("满 20 减 3 元") is None,
        str(_first_price("满 20 减 3 元")),
    )
    _md_rt = build_markdown(
        [(_T1, "标题A", [("活动甲", "满20减3")]), (_T1 + _td(days=1), "", [("活动乙", "")])]
    )
    _parsed_rt = parse_campaign_markdown(_md_rt)
    check(
        "日历-共享构造器产出的报文能原样解析回来",
        len(_parsed_rt) == 2 and _parsed_rt[0].campaigns[0].title == "活动甲",
    )

    # ── 9. CLI 的两处入参：离线也要能断言
    from .cli import _advise_within, _meal_payload, clamp_top

    check("CLI-advise 的 --days 会传进活动日历窗口", _advise_within(14) == 14)
    check("CLI-advise 的 --days 有下限（0 天等于没窗口）", _advise_within(0) == 1)
    _mp = _meal_payload("S1", "D008")
    check(
        "CLI-meal 入参键是 code 且不带 beCode（传错接口直接报错）",
        _mp.get("code") == "D008" and "beCode" not in _mp and "productCode" not in _mp,
        str(_mp),
    )
    check("CLI-compare 的 --top 至少为 1（0 会变成不截断）", clamp_top(0) == 1)
    check("CLI-compare 的 --top 有上限", clamp_top(9999) == 20)

    # ── 10. 营养搭配：枚举量必须算全，不能只看端点
    from .nutrition import MAX_COMBOS, MAX_MEAL_SIZE, _trim_pool, clamp_size

    check("营养-超大 --size 必须被夹住", clamp_size(50) == MAX_MEAL_SIZE and clamp_size(0) == 1)
    _big = [(f"p{i}", {"kcal": 100 + i, "protein": 10}) for i in range(60)]
    _trimmed = _trim_pool(list(_big), MAX_MEAL_SIZE)
    _n = len(_trimmed)
    _total = 0
    _c = 1
    for _i in range(1, MAX_MEAL_SIZE + 1):
        _c = _c * (_n - _i + 1) // _i
        _total += _c
    check(
        "营养-裁剪后的真实枚举量在上限内（判据要算全部档位，不是只看 C(n,size)）",
        _total <= MAX_COMBOS,
        f"n={_n} 枚举量={_total} 上限={MAX_COMBOS}",
    )

    # ── 11. 金额量纲守卫
    from .oracle import _assert_price_scale

    _scale_bad = False
    try:
        _assert_price_scale({"price": "57.00"}, [CartItem("Z", "测试", 1, 5700)])
    except McpError:
        _scale_bad = True
    check("算价-price 变成元字符串时必须报错（否则会读成 ¥0.57）", _scale_bad)
    _scale_ok = True
    try:
        _assert_price_scale({"price": 5700}, [CartItem("Z", "测试", 1, 5700)])
    except McpError:
        _scale_ok = False
    check("算价-正常的分口径不能被量纲守卫误伤", _scale_ok)

    # ── 12. 金额换算不能用 banker's rounding
    from .route import yuan_to_fen

    check("金额-元转分不能用 int(round(x*100))（0.125 必须是 13 分）",
          yuan_to_fen(0.125) == 13, str(yuan_to_fen(0.125)))

    # ── 13. forget --last 0 必须什么都不删
    import tempfile as _tf
    from pathlib import Path as _Path

    from . import snapshot as _snap
    from .cli import run_forget

    _tmpdir = _tf.mkdtemp(prefix="mcdbutler-selftest-")
    _old_snap = _snap.SNAPSHOT_FILE
    _snap.SNAPSHOT_FILE = _Path(_tmpdir) / "snapshots.json"
    try:
        _snap.save(
            _snap.entry_from_cart(
                cart=[CartItem("Z", "测试品", 1, 2000)],
                paid_fen=1500,
                baseline_fen=2000,
                discount_fen=500,
                store_code="S1",
                store_name="测试店",
                city="",
                keyword="",
                coupon_titles=["测试券"],
                with_order_label="",
            )
        )
        _before = len(_snap.load_all())
        run_forget(0, False)
        _after = len(_snap.load_all())
        check(
            "账本-forget --last 0 必须一条都不删（max(last,1) 会静默删 1 条）",
            _before == 1 and _after == 1,
            f"{_before} → {_after}",
        )
    finally:
        _snap.SNAPSHOT_FILE = _old_snap

    # ── 14. 餐品详情：排序 / isDefault 严格判定 / 无默认时的兜底
    from .meal_detail import parse_meal_detail

    _dirty = {
        "code": "X",
        "name": "测试套餐",
        "rounds": [
            {
                "name": "换饮",
                "choices": [
                    {"name": "中杯", "diffPrice": "- ¥1.00", "isDefault": 1},
                    {"name": "小杯", "diffPrice": "- ¥4.00", "isDefault": 1},
                ],
            },
            {
                "name": "换薯",
                "choices": [
                    {"name": "小份", "diffPrice": "- ¥3.00"},
                    # isDefault=2：真值但不是 1。严格判定（== 1）下它**不是**默认项，
                    # 于是默认回落到首个（小份）；放宽成真值判断它会变成默认项（大份）。
                    # 只有把这种值放在**第二个**才区分得出两种实现 ——
                    # 放第一个的话，两种实现都取它。
                    {"name": "大份", "diffPrice": "+ ¥2.00", "isDefault": 2},
                ],
            },
        ],
    }
    _mdx = parse_meal_detail(_dirty)
    check(
        "餐品详情-同一环两个 isDefault=1 时取第一个",
        _mdx is not None and _mdx.rounds[0].default_choice().name == "中杯",
        f"{_mdx.rounds[0].default_choice() if _mdx else None}",
    )
    check(
        "餐品详情-isDefault 只认 1，不认真值（2 不能冒充默认项）",
        _mdx is not None
        and _mdx.rounds[1].default_choice().name == "小份"
        and not any(c.is_default for c in _mdx.rounds[1].choices),
        f"{_mdx.rounds[1].default_choice() if _mdx else None}",
    )
    check(
        "餐品详情-更便宜的换项按差价升序排（最省的排最前）",
        _mdx is not None
        and [c.diff_fen for _b, c in _mdx.cheaper_swaps()] == sorted(
            c.diff_fen for _b, c in _mdx.cheaper_swaps()
        )
        # 严格判定下第二环的默认项是「小份」，它自己不算换项；
        # 放宽成真值判断后默认变成「大份」，小份就会混进省钱换项名单。
        and [c.name for _b, c in _mdx.cheaper_swaps()] == ["小杯"],
        f"{[(c.name, c.diff_fen) for _b, c in _mdx.cheaper_swaps()] if _mdx else None}",
    )

    # ── 15. 演示数据的一致性（写死日期会变成时间炸弹）
    check(
        "演示-券不能已经过期（写死日期迟早会炸）",
        all(
            (valid_until_end(c.valid_until) or "9999-12-31") >= _T1.isoformat()
            for c in demo.DEMO_COUPONS
        ),
        str([c.valid_until for c in demo.DEMO_COUPONS]),
    )
    _menu_codes = {m.product_code for m in demo.DEMO_MENU}
    check(
        "演示-券的适用商品必须真的在演示菜单里",
        all(set(c.product_codes) <= _menu_codes for c in demo.DEMO_COUPONS),
    )

    # ── 16. README 里贴的演示输出，必须跟真的 demo 对得上。
    # 「照着文档跑，跑出不一样的东西」是最劝退的一种失败 ——
    # 读者第一反应是"我是不是跑错了"，而不是"文档过期了"。
    # 这里双向比对：这些句子既要在真实 demo 输出里，也要在 README 里。
    import contextlib as _ctx2
    import io as _io4

    from .cli import run_demo

    _buf = _io4.StringIO()
    with _ctx2.redirect_stdout(_buf):
        _demo_rc = run_demo()
    _demo_out = _buf.getvalue()
    check("演示-离线演示能跑完且正常退出", _demo_rc == 0, f"rc={_demo_rc}")

    _readme = (_Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    _must_appear = [
        "挂券后小计 ¥42.00",
        "随单购「麦金卡（随单购）」再减 ¥6.00",
        "到手 ¥6.00（省 ¥6.00，力度 50%）",
        "菜单标 ¥62.90 是「随单购麦金卡优惠」",
        "标着减 ¥8.00，实测只省 ¥5.00",
        "¥10.00 的商品上不生效，¥25.00 的才生效",
        # 券名也算契约：README 里贴的券名和 demo_data.py 里的必须一致，
        # 否则读者照着 README 跑 demo 会看到另一张券。
        "中杯可乐立减 3 元",
        "巨无霸立减 5 元",
    ]
    _missing_demo = [s for s in _must_appear if s not in _demo_out]
    _missing_doc = [s for s in _must_appear if s not in _readme]
    check(
        "文档-README 贴的演示输出必须是真的（不是手改过的）",
        not _missing_demo,
        f"demo 里没有：{_missing_demo}",
    )
    check(
        "文档-README 没漏掉演示的关键数字",
        not _missing_doc,
        f"README 里没有：{_missing_doc}",
    )

    # ── 17. 演示里不能出现「今天」之类跟真实日期打架的话
    # （黄历标题会印日期，必须等于 date.today()）
    check(
        "演示-黄历标题印的是真今天，不是写死的日期",
        f"{_today().isoformat()}" in _demo_out,
        "README/demo 与真时钟脱节了",
    )

    # ================================================================ 23.
    # 变异猎手（第五轮）补的断言：下面每一条都对应一处「改坏后自检全绿」的变异。
    _fl = _failure_lines([("甲", True, ""), ("乙", False, "差 1")])
    check(
        "自检-失败时必须把失败项汇总再印一遍（几百行 PASS 里悄悄红了等于没红）",
        len(_fl) >= 3 and any("乙" in l for l in _fl) and any("差 1" in l for l in _fl),
        str(_fl),
    )
    check("自检-全绿时不要印失败汇总", _failure_lines([("甲", True, "")]) == [])
    # ── N5：变异脚本必须把「自检崩了」和「改坏没被抓到」分开判
    _mut_src = (_Path(__file__).resolve().parent.parent / "scripts" / "mutate.py").read_text(
        encoding="utf-8"
    )
    check(
        "工具-变异脚本必须区分「自检崩了」与「无防护」（只看变红条数会把崩误报成 P0）",
        'if red == 0 and rc == 0:\n            verdict = "P0 无防护"' in _mut_src,
        "mutate.py 的崩溃判定被改掉了",
    )

    # ── N4：可重入判据本身要站得住（写死 True 会让 --twice 永远绿）
    _self_src = _Path(__file__).read_text(encoding="utf-8")
    # 判据字符串必须拼出来，不能原样写：写原样的话，变异之后它仍存在于这条
    # 断言自己的字面量里，`in` 恒为真 —— 断言就成了装饰品。
    _needle = "stable = _re" + "entrant(totals)"
    check(
        "自检-run_repeat 必须用 _reentrant(totals) 判可重入（写死 True 会让 --twice 白跑）",
        _reentrant([203, 203])
        and not _reentrant([203, 406])
        and _needle in _self_src,
        "run_repeat 的可重入判据被改掉了",
    )

    # ── N6：券的到期日是「含当天」的，今天到期今天还能用
    _saved_c2 = demo.DEMO_COUPONS[0]
    try:
        demo.DEMO_COUPONS[0] = _dc.replace(
            _saved_c2, valid_until=_today().isoformat()
        )
        _due_today = demo.demo_oracle([demo.DEMO_MENU[0]], Assignment(mapping={0: 0})).price_fen
    finally:
        demo.DEMO_COUPONS[0] = _saved_c2
    check(
        "演示-券在到期日当天仍然有效（valid_until 含当天，不能提前一天废掉）",
        _due_today == demo.DEMO_MENU[0].unit_price_fen - 500,
        f"{_due_today}",
    )

    # ── N7 / N8 / N9：本地账本三处零覆盖的逻辑
    _tmpdir2 = _tf.mkdtemp(prefix="mcdbutler-selftest-2-")
    _old_snap2 = _snap.SNAPSHOT_FILE
    _snap.SNAPSHOT_FILE = _Path(_tmpdir2) / "snapshots.json"
    try:
        for _i in range(3):
            _snap.save(
                _snap.entry_from_cart(
                    cart=[CartItem("Z", f"品{_i}", 1, 2000)],
                    paid_fen=1500,
                    baseline_fen=2000,
                    discount_fen=500,
                    store_code="S1",
                    store_name="测试店",
                    city="",
                    keyword="",
                    coupon_titles=["测试券"],
                    with_order_label="",
                )
            )
        check(
            "账本-latest 取的是最近一条，不是最早一条（replay 会复刻错的单）",
            (_snap.latest() or {}).get("items", [{}])[0].get("name") == "品2",
            str(_snap.latest()),
        )
        check(
            "账本-delete_last(0) 一条都不删（entries[:-0] 会清空整本账）",
            _snap.delete_last(0) == 0 and len(_snap.load_all()) == 3,
            f"删了 {3 - len(_snap.load_all())} 条",
        )
    finally:
        _snap.SNAPSHOT_FILE = _old_snap2
    check(
        "账本-摘要必须分清「成交」与「测算」（replay --list 不能说谎）",
        "测算" in _snap.describe({"kind": "estimate", "saved_at": "x", "items": []})
        and "成交" in _snap.describe({"kind": "order", "saved_at": "x", "items": []}),
    )

    # ── N14：晒出去的卡片里，「真实成交笔数」不能把测算数算进去
    check(
        "账单-真实成交只数 kind=order（把测算数当成交数是给用户一张圆不上的成绩单）",
        summarize([{"kind": "order"}, {"kind": "estimate"}, {}])["real_orders"] == 1,
        str(summarize([{"kind": "order"}, {"kind": "estimate"}, {}])),
    )

    # ── N10 / N11：营养搭配的「热量预算」和「蛋白质降序」两条主判据
    from .nutrition import plan as nutrition_plan

    _npairs = [
        (CartItem("N1", "高蛋白", 1, 1500),
         {"kcal": 300, "protein": 30.0, "fat": 5.0, "carb": 1.0, "sodium": 10, "calcium": 1}),
        (CartItem("N2", "低蛋白", 1, 1500),
         {"kcal": 100, "protein": 2.0, "fat": 1.0, "carb": 1.0, "sodium": 10, "calcium": 1}),
        (CartItem("N3", "超预算", 1, 1500),
         {"kcal": 900, "protein": 60.0, "fat": 1.0, "carb": 1.0, "sodium": 10, "calcium": 1}),
    ]
    _nres = nutrition_plan(_npairs, kcal_budget=500, size=2, top=5)
    check(
        "营养-推荐搭配必须真的在热量预算内（超预算的不能推荐）",
        bool(_nres) and all(sum(p[1]["kcal"] for p in c) <= 500 for c in _nres),
        str([sum(p[1]["kcal"] for p in c) for c in _nres]),
    )
    check(
        "营养-推荐按蛋白质降序（最扛饿的排最前，不是最少蛋白的）",
        bool(_nres)
        and sum(p[1]["protein"] for p in _nres[0])
        == max(sum(p[1]["protein"] for p in c) for c in _nres),
        str([sum(p[1]["protein"] for p in c) for c in _nres]),
    )

    # ── N12：时段是左闭右开，边界整点不能被划到隔壁时段
    check(
        "黄历-时段边界是左闭右开（10 点已是午市、5 点已是早市、21 点已是夜宵）",
        period_of(datetime(2026, 10, 11, 10))[0] == "午市"
        and period_of(datetime(2026, 10, 11, 5))[0] == "早市"
        and period_of(datetime(2026, 10, 11, 14))[0] == "下午茶"
        and period_of(datetime(2026, 10, 11, 17))[0] == "晚市"
        and period_of(datetime(2026, 10, 11, 21))[0] == "夜宵"
        and period_of(datetime(2026, 10, 11, 0))[0] == "深夜",
        str([period_of(datetime(2026, 10, 11, h))[0] for h in (5, 10, 14, 17, 21, 0)]),
    )

    # ── N13：已过期必须说「已过期」，不能被「今日到期」吃掉
    check(
        "黄历-已过期的券必须说「已过期」（说成今日到期会骗人当天跑去消费）",
        expiry_note("2026-10-10", datetime(2026, 10, 11)) == "已过期",
        expiry_note("2026-10-10", datetime(2026, 10, 11)),
    )

    # ── N16：同名活动的今日基准价取最低价
    _cheap_day = CampaignDay(_T0, campaigns=(Campaign("麦麦夜市回归", price_yuan=12.9),))
    _dear_day = CampaignDay(_T0, campaigns=(Campaign("麦麦夜市回归", price_yuan=9.9),))
    check(
        "活动日历-今天已有的同名活动按最低价当基准（取 max 会把更贵的那天说成值得等）",
        len(fresh_campaigns(
            _cheap_day,
            [Campaign("麦麦夜市回归", price_yuan=9.9),
             Campaign("麦麦夜市回归", price_yuan=15.0)],
        )) == 0
        and len(fresh_campaigns(
            _dear_day, [ Campaign("麦麦夜市回归", price_yuan=12.9)]
        )) == 1,
        "基准价取错了",
    )

    # ── N15：已领取的券不能算成待领取（--claim 会重复领）
    from .assets import build_report

    class _FakeAssetsClient:
        def call_json(self, tool, arguments=None):
            return {"data": {"availablePoint": "9.9", "currency": "积分"}}

        def call(self, tool, arguments=None):
            if tool == "query-my-coupons":
                return "## 卡包券\n- **有效期**: 2026-10-11 05:00-2026-12-31 10:29\n"
            if tool == "available-coupons":
                return (
                    "### 麦麦省优惠券列表：\n"
                    "- 优惠券标题：免费脆薯饼 \\\n"
                    "  状态：已领取 \\\n"
                )
            return ""

    _rep = build_report(_FakeAssetsClient())
    check(
        "资产-已领取的券不能算成待领取（--claim 会重复领一次）",
        "· 已领取" in _rep and "待领取" not in _rep,
        _rep,
    )

    # ── N17：对调邻域必须校验「对调后的券适用于对调后的商品」
    _sx = CartItem("X", "X", 1, 2000)
    _sy = CartItem("Y", "Y", 1, 2000)
    _scps = [
        Coupon("A", "a", "A券", frozenset({"X", "Y"})),
        Coupon("B", "b", "B券", frozenset({"X"})),
    ]

    def _strict_oracle(items, a):
        # 不合规的组合（券挂到它不适用的商品上）给一个离谱的低价，
        # 把「校验写反」这件事从静默变成看得见。
        for idx, ci in a.mapping.items():
            if items[idx].product_code not in _scps[ci].product_codes:
                return PriceResult(price_fen=0, original_fen=4000)
        return PriceResult(price_fen=4000 - 100 * len(a.mapping), original_fen=4000)

    _sopt = CouponOptimizer(_strict_oracle, max_calls=40)
    _climbed = _sopt._hill_climb(
        [_sx, _sy],
        _scps,
        _sopt._eligibility([_sx, _sy], _scps),
        Assignment(mapping={0: 1, 1: 0}),
    )
    _sbad = [
        (i, ci)
        for i, ci in _climbed.mapping.items()
        if [_sx, _sy][i].product_code not in _scps[ci].product_codes
    ]
    check(
        "引擎-对调邻域必须校验「对调后的券适用于对调后的商品」（table 的 key/value 别弄反）",
        not _sbad,
        f"对调出了不合规的挂法 {_sbad}",
    )

    # ── N19：贪心必须最受约束的券先占坑
    _gopt = CouponOptimizer(lambda items, a: PriceResult(price_fen=0), max_calls=1)
    _gcart = [CartItem("X", "X"), CartItem("Y", "Y"), CartItem("Z", "Z")]
    _gcps = [
        Coupon("w", "w", "通用券", frozenset()),
        Coupon("n", "n", "只挂X", frozenset({"X"})),
    ]
    check(
        "引擎-贪心必须最受约束的券先占坑（可挂商品最少的先选，否则窄券抢不到位置）",
        _gopt.greedy(_gcart, _gcps).mapping.get(0) == 1,
        str(_gopt.greedy(_gcart, _gcps).mapping),
    )

    # ── N18：黄历的「宜」按省下的钱降序，最省的那条必须是第一条
    _amenu = [
        CartItem(product_code="B1", name="板烧三件套", unit_price_fen=3700, list_price_fen=3700),
        CartItem(product_code="B2", name="麦香鱼", unit_price_fen=1500, list_price_fen=1500),
    ]
    _acps = [
        Coupon(coupon_id="b1", coupon_code="b1", title="板烧立减8元",
               product_codes=frozenset({"B1"})),
        Coupon(coupon_id="b2", coupon_code="b2", title="麦香鱼立减3元",
               product_codes=frozenset({"B2"})),
    ]

    def _alm2(items, a):
        base = sum(i.unit_price_fen for i in items)
        saved = sum(800 if ci == 0 else 300 for ci in (a.mapping or {}).values())
        return PriceResult(price_fen=base - saved, original_fen=base)

    _aday = build_almanac(_amenu, _acps, _alm2, Budget(40), now=datetime(2026, 10, 11, 12))
    _yes = [r for r in _aday["readings"] if r.kind == "宜"]
    check(
        "黄历-宜按省下的钱降序（最省的那条排第一，否则「今天该吃什么」第一句就是错的）",
        len(_yes) >= 2
        and _yes[0].saved_fen == max(r.saved_fen for r in _yes)
        and _yes[0].product_name == "板烧三件套",
        str([(r.product_name, r.saved_fen) for r in _yes]),
    )

    # ── 18. README 说「完整演示一共六段」，这句话必须跟 demo 对得上
    # 上一版 README 写的是「共七段」，实际只有六段，而且段名还漏了两个 ——
    # 一句散文里的数字没人管，被人数出来就是「主打不编数据的项目自己编了」。
    _SECTIONS = [
        "麦门管家 · 离线演示",
        "历史订单复刻",
        "麦门黄历",
        "券包体检",
        "麦门活动日历",
        "麦门段位",
    ]
    _hit = [s for s in _SECTIONS if s in _demo_out]
    check(
        "文档-演示段落数必须是六段（README 里那句「六段」不能漂）",
        # 双向：demo 真跑出来的段数得是 6，README 里那句「一共**六段**」也得在。
        # 只查一边的话，改 README 不改代码（或反过来）都不会红。
        len(_hit) == 6 and "一共**六段**" in _readme,
        f"demo {len(_hit)} 段 / README 写的是六段吗：{'一共**六段**' in _readme}",
    )
    check(
        "文档-README 要把六段都提到（不能只说「还有好几段」）",
        all(s in _readme for s in _SECTIONS),
        f"README 没提：{[s for s in _SECTIONS if s not in _readme]}",
    )

    # ================================================================ README 版式
    # 下面这几条守的不是功能，是「路人划不划走」。《Art of README》里两条硬性结论：
    #   ① 许可证要往上放 —— 协议合不来的人会在几秒内划走，不该让他翻到底才发现
    #   ② Caveats and limitations mentioned up-front —— 限制要前置
    # 这两条没法靠眼睛检查维持：README 每次改动都可能顺手把它们挪走。
    # 只看 «全篇有没有» 不够 —— 得看它在第几行。挪到文末就等于没前置，
    # 而 "LICENSE 文件存在" 从来不是问题，问题是读者会不会在决定要不要用之前看到。
    _head20 = "\n".join(_readme.splitlines()[:20])
    _lic_ok = ("License" in _head20) or ("[MIT](LICENSE)" in _head20)
    check(
        "文档-许可证必须在首屏就看得见（翻到文末才发现等于没写）",
        _lic_ok,
        "前 20 行里没有 License 徽章也没有 [MIT](LICENSE)",
    )
    # 这是一条独立的安全承诺，不是许可证的附属品：
    # 「Token 只留在本机」是陌生人决定要不要填 Token 的唯一依据。
    _disclaimer = next(
        (ln for ln in _readme.splitlines()[:20] if ln.startswith("> **MIT 开源")),
        "",
    )
    check(
        "文档-首屏免责一行三联：非官方 / 不碰支付 / Token 不出本机",
        all(
            k in _disclaimer
            for k in ("非麦当劳官方产品", "不碰支付", "不代下单", "Token 只留在本机")
        ),
        f"顶部免责行缺项：{_disclaimer!r}",
    )
    # 只看 «有没有» 不够 —— 得看它在第几行。挪到文末就等于没前置。
    _redline_row = next(
        (i for i, ln in enumerate(_readme.splitlines(), 1) if "红线" in ln and "#redline" in ln),
        -1,
    )
    check(
        "文档-限制必须前置（红线要在首屏就出现，不能藏到第 700 行）",
        0 < _redline_row <= 40,
        f"红线首次出现在第 {_redline_row} 行（要求 ≤40）",
    )
    # 目录里的每一个锚点都得有对应的 <a id>，否则点了不跳 ——
    # 中文标题的 GitHub slug 会吃掉全角冒号和引号，赌 slug 必然出问题，
    # 所以全部改成显式 id，这里再验一次「写的链接和目标对得上」。
    _anchors = set(re.findall(r'<a id="([^"]+)"></a>', _readme))
    _links = set(re.findall(r"\]\(#([^)]+)\)", _readme))
    _dead = sorted(_links - _anchors)
    check(
        "文档-README 里的每一个页内跳转都要跳得到（不能有死链）",
        not _dead,
        f"指向不存在锚点的链接：{_dead}",
    )
    # ── 相对链接必须真的点得到
    # 上一轮把自用传播稿 docs/ 移出了仓库，README 却还留着 4 个指向它的链接：
    # 本地全部点得开，远端全部 404 —— 因为那些文件根本没被推上去。
    # 「存在但没被跟踪」是最阴的一种死链：作者永远发现不了，路人一点就空。
    # 这里不调 git（自检要能在没装 git 的机器上跑），改成读 .gitignore：
    # 凡是命中忽略规则的链接目标，一律算死链；文件不存在当然也算。
    _gitignore = (_Path(__file__).resolve().parent.parent / ".gitignore").read_text(
        encoding="utf-8"
    )
    _ignored = {
        ln.strip().rstrip("/")
        for ln in _gitignore.splitlines()
        if ln.strip() and not ln.strip().startswith("#") and ln.strip().endswith("/")
    }
    _dead_rel = []
    for _t in re.findall(r"\[[^\]]*\]\((?!https?://|#|mailto:)([^)\s]+)\)", _readme):
        _p = _Path(__file__).resolve().parent.parent / _t
        if not _p.exists():
            _dead_rel.append(f"{_t}（文件不存在）")
        elif _t.split("/")[0] in _ignored:
            _dead_rel.append(f"{_t}（在 .gitignore 里，推上去就是 404）")
    check(
        "文档-README 里的相对链接必须真的点得到（不能指向没上传的文件）",
        not _dead_rel,
        f"点不到的链接：{_dead_rel}",
    )

    # 徽章一个占一行的话，首屏 6 行就被彩色小图吃掉了。
    # banner 行 = 以 ! 或 [! 开头的行。
    _head_rows = [
        ln
        for ln in _readme.splitlines()[:30]
        if ln.strip().startswith("![") or ln.strip().startswith("[![")
    ]
    check(
        "文档-徽章必须挤在同一行（拆成多行会吃掉首屏六行）",
        len(_head_rows) <= 1,
        f"前 30 行里有 {len(_head_rows)} 行是徽章：{[r[:40] for r in _head_rows]}",
    )
    # 「目录」这个名字曾经挂着一堆文件树，挂在第 690 行，谁也看不到它。
    # 名不符实的章节比没有更糟 —— 读者会以为那就是目录。
    # 判据只看「代码地图」这四个字在不在，不看它是不是 h2：
    # 这一节现在被折进了 <details>（README 太长，文件树不是路人要看的东西），
    # 标题降级成 summary。钉死成 "## 代码地图" 的话，任何把长章节折起来的
    # 改动都会误伤 —— 断言要守的是「名字不许再骗人」，不是「必须是 h2」。
    check(
        "文档-不再有名不符实的「目录」章节（文件树应叫代码地图）",
        "## 目录" not in _readme and "代码地图" in _readme,
        f"README 里仍有「目录」字样或找不到「代码地图」：{_readme.count('目录')} 处「目录」",
    )
    check(
        "文档-长 README 必须给导航表（没有 Tableau of Contents 就是让人迷路）",
        "在这一页里找东西" in _readme and "#install" in _readme and "#commands" in _readme,
        "缺 TOC 或导航链接不完整",
    )

    # ================================================================ 24.
    # 变异猎手（第六轮）补的断言：下面每一条都对应一处「改坏后自检全绿」的变异。
    # 这一轮专门挑前五轮没点名过的模块（route / insights / audit 候选采样 /
    # persona 段位 / snapshot 截断 / assets 截止时刻 / campaign 正文取价 /
    # nutrition 配对 / cli 的数量与打烊过滤），8 处里 8 处都是零覆盖。

    # ── R3：拆单建议的收益必须严格为正
    # 为什么单列：原来的断言只查返回值的**形状**（是不是三元组），
    # 于是「把 < 放宽成 <=」让推荐列表里混进「省 ¥0」的建议，自检依旧全绿 ——
    # 用户看到"分开下单更便宜"照着做，一分钱省不到。
    from . import insights as _ins6

    _sp_cart = [CartItem("SP1", "甲A", 1, 2000), CartItem("SP2", "乙B", 1, 2000)]
    _sp_flat = lambda items, a: PriceResult(  # 完全不打折：拆不拆一个价
        price_fen=sum(i.unit_price_fen * i.quantity for i in items), original_fen=0
    )
    _sp_got = _ins6.split_suggestions(
        _sp_flat, _sp_cart, [], lambda c: [None], whole_fen=4000, per_call_budget=8
    )
    check(
        "洞察-拆单建议不许报零收益（省 ¥0 也算建议是谎报）",
        _sp_got == [],
        str([(x[0].product_code, x[2]) for x in _sp_got]),
    )

    # ── R5：体检候选的去重键必须带价格
    # 同一个 productCode 有不同规格/不同价时，只按 code 去重会少试一件，
    # 而那件恰好可能够到门槛 —— 一张好券被静默判成废券。
    from .audit import _candidates

    _dup_menu = [
        CartItem("SAME", "同码便宜", 1, 1000),
        CartItem("SAME", "同码昂贵", 1, 3000),
    ]
    _dup_cands = _candidates(_dup_menu, Coupon("K", "KK", "通用券"), probe=3)
    check(
        "体检-候选去重键必须带价格（同码不同规格要各试一件）",
        len(_dup_cands) == 2,
        str([(c.name, c.effective_price_fen()) for c in _dup_cands]),
    )

    # ── R7：快照超上限时留的是**最近**的 N 条
    # entries[:N] 会把用户攒了半年的账本换成他最早那几十次「随手一跑」——
    # brag 晒出去的数字直接变假。而没有这条断言时，改方向一条都不红。
    _tmpdir3 = _tf.mkdtemp(prefix="mcdbutler-selftest-3-")
    _old_snap3 = _snap.SNAPSHOT_FILE
    _snap.SNAPSHOT_FILE = _Path(_tmpdir3) / "snapshots.json"
    try:
        for _i in range(_snap.MAX_ENTRIES + 1):
            _snap.save(
                _snap.entry_from_cart(
                    cart=[CartItem("Z", f"第{_i}单", 1, 2000)],
                    paid_fen=1500,
                    baseline_fen=2000,
                    discount_fen=500,
                    store_code="S1",
                    store_name="测试店",
                    city="",
                    keyword="",
                    coupon_titles=[],
                    with_order_label="",
                )
            )
        _kept = _snap.load_all()
        check(
            "账本-超出上限时留最新的 N 条（不是最早的 N 条）",
            len(_kept) == _snap.MAX_ENTRIES
            and (_kept[-1].get("items") or [{}])[0].get("name") == f"第{_snap.MAX_ENTRIES}单",
            f"留了 {len(_kept)} 条，最后一条是 {(_kept[-1].get('items') or [{}])[0].get('name')}",
        )
    finally:
        _snap.SNAPSHOT_FILE = _old_snap3

    # ── R8：卡包「有效期」取的是区间**结束**时刻
    # 取成 group(1) 时，今天才开始、晚上才失效的券会被报成「今天到期/已过期」，
    # 用户被吓得当天跑去消费；而原先这条只断了 `end is not None`。
    _md_coupons = parse_coupon_markdown(COUPON_MD)
    check(
        "卡包-有效期取的是结束时刻而不是开始时刻",
        len(_md_coupons) > 1 and _md_coupons[1]["end"] == datetime(2026, 10, 11, 10, 29),
        str(_md_coupons[1]["end"] if len(_md_coupons) > 1 else None),
    )

    # ── R9：标题里没有价格时，正文兜底取的是**最低**价
    # 报告里那句「最低 ¥X」就是这个返回值；取 max 会把承诺的最低价印成最贵的那个。
    from .campaign import _first_price

    _bp = _first_price("超值套餐大放送", "限时 9.9 元起，另有 14.9 元可选")
    check("日历-正文兜底取价取最低价（报告里写的是「最低」）", _bp == 9.9, str(_bp))

    # ── R10：一条营养数据只能配一个菜
    # 营养表没有 productCode，只能按名字模糊配对；若允许同一条数据被多个菜串用，
    # 「巨无霸套餐」会拿「巨无霸」的营养数据再算一遍，热量/蛋白全是假的。
    from .nutrition import match_menu

    _mm_nut = {"巨无霸": {"kcal": 500, "protein": 25.0, "fat": 20.0,
                          "carb": 40.0, "sodium": 800, "calcium": 100}}
    _mm_got = match_menu(
        [CartItem("M1", "巨无霸", 1, 2500), CartItem("M2", "巨无霸套餐", 1, 3500)],
        _mm_nut,
    )
    check(
        "营养-一条营养数据只能配一个菜（不能串味重复计数）",
        len(_mm_got) == 1 and _mm_got[0][0].product_code == "M1",
        str([p[0].product_code for p in _mm_got]),
    )

    # ── R11：`薯条x2` 的数量必须真的进购物车
    # 数量丢了的话，optimize 按 1 件算价，用户照着屏幕上的数去点会少一份。
    _cart_q, _missed_q = _pick_cart(menu_match, ["龙焰鸡腿堡三件套x2"], cp_match)
    check(
        "匹配-数量写法必须真的进购物车（x2 不能只算 1 件）",
        len(_cart_q) == 1 and _cart_q[0].quantity == 2,
        str([(c.product_code, c.quantity) for c in _cart_q]),
    )

    # ── R16：打烊门店的判定不能放宽成 falsy
    # 原来这条是**自实现**的：自己在自检里再写一遍同样的列表推导式、
    # 再断言自己算出来的结果 —— 改 cli.run_compare 那一行完全不会红。
    # 换成真跑 compare（把假的 client 塞进 mcd_client），让它自己把过滤结果
    # 打印出来再断言。这里没法抽纯函数：过滤逻辑内联在 run_compare 里，
    # 抽一个「跟实现长得一样的副本」等于把注水换个写法。
    from . import mcd_client as _mc6
    from .cli import run_compare

    class _StoreClient6:
        """只回答 query-nearby-stores，其余返回空 —— 够走到「 Shopping 车为空」为止，
        而上一层要断言的那两句恰好在那之前就打印了。"""

        def __init__(self, stores):
            self.stores = stores

        def initialize(self):
            return None

        def data(self, tool, payload):
            return list(self.stores) if tool == "query-nearby-stores" else {}

    _real_client6 = _mc6.McdMcpClient
    _mc6.McdMcpClient = lambda *a, **k: _StoreClient6(
        [
            {"storeCode": "A", "storeName": "营业店", "distance": 100, "businessStatus": True},
            {"storeCode": "B", "storeName": "打烊店", "distance": 200, "businessStatus": False},
            {"storeCode": "C", "storeName": "缺字段店", "distance": 300},  # 字段缺失按营业
        ]
    )
    try:
        import contextlib as _ctx6
        import io as _io6

        _buf6 = _io6.StringIO()
        with _ctx6.redirect_stdout(_buf6):
            run_compare("", "", "", 40, "walk", None, None, 5)
        _out6 = _buf6.getvalue()
    finally:
        _mc6.McdMcpClient = _real_client6
    check(
        "营业-打烊门店被排除而字段缺失不误杀（跑真的 compare）",
        "1 家门店当前打烊" in _out6 and "取附近 2 家门店参与比价" in _out6,
        (_out6 or "")[:160],
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
    if passed != total:
        print("")
        for line in _failure_lines(_RESULTS):
            print(line)
    return 0 if passed == total else 1
