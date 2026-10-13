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

import hashlib
import itertools
import json
import random
import re

from .assets import parse_claimable_markdown, parse_coupon_markdown, fmt_num, _num
from .snapshot import entry_from_cart
from .coupon_engine import (
    Assignment,
    Budget,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
    WithOrderOption,
    as_fen,
    neighborhood_size,
    suggest_budget,
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

    # 8b. 契约锁定（「裸买基准在 optimize 后拿不到」这个坑，从根上填了）：
    # 早先 optimize 从不算裸买价，预算被爬山吃光后 try_evaluate(裸买) 返回 None，
    # 调用方只能把 baseline 挪到 optimize 之前算 —— 顺序一写反就 AttributeError，
    # 而这个坑没有任何东西拦得住。现在引擎第零步就把裸买价算出来并进了缓存，
    # 于是**什么时候问都有答案**，顺序问题从此不存在。
    # 预算给 3（小到 optimize 一定会吃光），锁的是「吃光了也问得到」。
    opt_budget = Budget(3)
    eng = CouponOptimizer(demo.demo_oracle, max_calls=3, budget=opt_budget)
    eng.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
    late = eng.try_evaluate(list(cart), Assignment(mapping={}, with_order=None))
    check(
        "契约-optimize吃光预算后仍问得到裸买基准（引擎第零步算过并缓存了）",
        late is not None and late.price_fen > 0,
        f"应命中缓存返回一个真价，实际 {late!r}",
    )
    # 下界契约：给的价**绝不能比不挂券更贵**。预算小到爬山删不完陷阱券时，
    # 早先会停在比裸买贵的解上，屏幕上出现「真实省下 ¥-1.00」。
    # 先判 None 再取价：删掉第零步后这里 late 就是 None，
    # 直接 `.price_fen` 会让**整屏自检崩掉**（变异测试标成"崩"而不是"红"）——
    # 崩也算被抓住，但给用户看的是一屏 traceback，不是一句人话。
    _bare_now = demo.demo_oracle(list(cart), Assignment(mapping={}, with_order=None))
    check(
        "引擎-预算再小也不许给出比裸买更贵的解（挂券不能挂出负收益）",
        late is not None and late.price_fen <= _bare_now.price_fen,
        f"best={getattr(late, 'price_fen', None)} 裸买={_bare_now.price_fen}",
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
    # 这条断言原来是「拆单建议带得出金额」，并且指望 demo 场景里一定有拆单收益。
    # 但那个收益是假的：逐件算下来，demo 里 4 条拆单建议**每一条**都是
    # 「两单都随单购麦金卡」换来的 —— 拆成两单还各买一张卡，多付一份卡费，
    # 账面上便宜 ¥6，付款时更贵。旧断言等于在给这个假收益背书。
    #
    # 现在反过来钉住：这种收益必须一分不报。
    # 单独看这条是弱断言（返回 [] 就通过），所以下面 R7 有一条配对的强断言：
    # 真能靠拆单多核销一张券时，必须报出来。两条一起才守得住。
    check(
        "洞察-拆单收益若只能靠买两张随单购卡换来就不许报（账面便宜、付款更贵）",
        splits == [],
        str([(x[0].product_code, x[1], x[2]) for x in splits]),
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

    # 15b. 算价缓存：跨求解器共享 + 键必须带上购物车
    # 起因是一次 advise 会建十几个 CouponOptimizer（求边际成本每件一个、
    # 拆单每件两个），各自持一份互不相通的缓存，同样的方案被反复付费重算。
    from .oracle import cached_oracle, cart_fingerprint, real_calls as _real_calls

    _net = {"n": 0}

    def _counting(items, assignment):
        _net["n"] += 1
        _base = sum(i.unit_price_fen * i.quantity for i in items)
        return PriceResult(price_fen=_base - len(assignment.mapping) * 300,
                           original_fen=_base)

    _co = cached_oracle(_counting)
    _cA = [CartItem(product_code="P1", quantity=1, unit_price_fen=1000)]
    _same = Assignment(mapping={0: 0})
    _r1 = _co(_cA, _same)
    _r2 = _co(_cA, _same)
    check(
        "缓存-同一个购物车+同一个方案只真正算一次",
        _net["n"] == 1 and _real_calls(_co) == 1,
        f"发了 {_net['n']} 次",
    )
    check(
        "缓存-命中返回的价格与首次完全一致（纯记忆化，不改变结果）",
        _r2.price_fen == _r1.price_fen,
        f"{_r1.price_fen} vs {_r2.price_fen}",
    )
    # 键漏掉 cart 是会咬人的：有人把求解器提到循环外复用，下一个购物车
    # 就会静默地用上一个购物车的价格，且查不出来。
    _cB = [CartItem(product_code="P9", quantity=1, unit_price_fen=9900)]
    _co(_cB, _same)
    check(
        "缓存-换一份购物车必须重新算价（缓存键含购物车指纹）",
        _net["n"] == 2 and cart_fingerprint(_cA) != cart_fingerprint(_cB),
        f"发了 {_net['n']} 次",
    )

    # 15b-2. 缓存命中不该扣预算：预算保护的是真实网络请求，
    # 一次 advise 会新建十几个求解器，候选高度重叠；如果查表也算钱，
    # 用户设的 --budget 等于被自己的缓存吃掉一半。
    _net2 = {"n": 0}

    def _counting2(items, assignment):
        _net2["n"] += 1
        _base = sum(i.unit_price_fen * i.quantity for i in items)
        return PriceResult(price_fen=_base - len(assignment.mapping) * 300,
                           original_fen=_base)

    _shared_budget = Budget(50)
    _co2 = cached_oracle(_counting2)
    _optA = CouponOptimizer(_co2, max_calls=50, budget=_shared_budget)
    _optB = CouponOptimizer(_co2, max_calls=50, budget=_shared_budget)
    _a15 = Assignment(mapping={0: 0})
    _optA.evaluate(_cA, _a15)
    _after_first = _shared_budget.used
    _optB.evaluate(_cA, _a15)  # 同一个购物车 + 同一个方案 → 应命中共享缓存
    check(
        "缓存-第二个求解器命中共享缓存时不扣预算也没发请求",
        _shared_budget.used == _after_first and _net2["n"] == 1,
        f"预算 {_after_first}→{_shared_budget.used}，实际请求 {_net2['n']} 次",
    )

    # T5 的源头：未收敛标记本身必须生效（前面的断言测的是 optimize 的收尾，
    # 那句被抹掉时 `_stopped_early` 还能救回来，所以这里再钉一处）
    _ce15 = CouponOptimizer(_counting2, max_calls=99)
    _marked = _ce15._stopped_early(Assignment(mapping={}))
    check(
        "收敛-提前收工必须标记未收敛（不能悄悄说是最优）",
        _marked.converged is False and _ce15._stopped is True,
        f"converged={_marked.converged}",
    )

    # T11/T12：报错文案不许把 Python 类型名 / 原始响应体吐给用户
    from . import oracle as _oracle15
    from .mcd_client import McpError as _McpError15

    class _BadClient:
        def data(self, tool, arguments):
            return ["not", "a", "dict"]  # 触发「非对象结构」那一条

    try:
        _oracle15.make_price_oracle(_BadClient(), "S1", [])(
            [CartItem(product_code="P1", unit_price_fen=1000)], Assignment(mapping={})
        )
        _t11_msg = ""
    except _McpError15 as _e15:
        _t11_msg = str(_e15)
    check(
        "错误提示-Python 类型名（NoneType/list…）不许出现在报错里",
        _t11_msg != "" and "list" not in _t11_msg and "NoneType" not in _t11_msg,
        repr(_t11_msg),
    )

    import io as _io15b
    import urllib.error as _uerr15
    import urllib.request as _ureq15
    from . import mcd_client as _mc15

    _SECRET_BODY = b'{"phone":"13800000000","address":"secret"}'

    def _http502(req, timeout=None):
        raise _uerr15.HTTPError(req.full_url, 502, "err", {}, _io15b.BytesIO(_SECRET_BODY))

    _real_urlopen15 = _ureq15.urlopen
    try:
        _ureq15.urlopen = _http502
        _c502 = _mc15.McdMcpClient("tok", max_retries=0)
        try:
            _c502._post({"x": 1})
            _t12_msg = ""
        except Exception as _e502:  # noqa: BLE001
            _t12_msg = str(_e502)
    finally:
        _ureq15.urlopen = _real_urlopen15
    check(
        "错误提示-HTTP 响应体不许原样透传给用户（可能含个人信息）",
        '"error":"x"' not in _t12_msg and "502" not in _t12_msg,
        repr(_t12_msg),
    )

    # 15c. 「这是最优」和「没算完」必须分开报
    # 预算花光时爬山只是"邻域里暂时扫不动了"，把它说成最优解，
    # 用户会拿着这个数字去下单，然后发现柜台那边还能更便宜。
    import random as _rnd15

    _conv = {"tight": 0, "loose": 0, "n": 0}
    for _s15 in range(12):
        _r15 = _rnd15.Random(_s15 * 31)
        _c15, _k15, _o15, _w15 = _random_case(_r15)
        try:
            _tight = CouponOptimizer(_o15, max_calls=8).optimize(_c15, _k15, _w15)
            _loose = CouponOptimizer(_o15, max_calls=400).optimize(_c15, _k15, _w15)
        except Exception:  # noqa: BLE001
            continue
        if _tight.price is None or _loose.price is None:
            continue
        _conv["n"] += 1
        _conv["tight"] += int(_tight.converged)
        _conv["loose"] += int(_loose.converged)
    check(
        "收敛-预算给足时必须敢说「这就是最优」",
        _conv["n"] > 0 and _conv["loose"] == _conv["n"],
        f"{_conv['loose']}/{_conv['n']} 收敛",
    )
    check(
        "收敛-预算卡死时不许假装是最优（那等于骗用户下单）",
        _conv["tight"] < _conv["n"],
        f"{_conv['tight']}/{_conv['n']} 标了收敛 —— 预算 8 次不可能全算完",
    )
    import io as _io15
    import contextlib as _ctx15
    from .cli import _print_search_note

    _b15 = _io15.StringIO()
    with _ctx15.redirect_stdout(_b15):
        _quiet15 = _print_search_note(True, 40)
    check(
        "收敛-收敛时不啰嗦（不打扰是默认状态）",
        _quiet15 == [] and _b15.getvalue() == "",
        repr(_b15.getvalue()[:80]),
    )
    _b16 = _io15.StringIO()
    with _ctx15.redirect_stdout(_b16):
        _loud15 = _print_search_note(False, 40)
    check(
        "收敛-没算完要明确说「不保证最便宜」并给下一步",
        _loud15 != []
        and any("不保证" in ln for ln in _loud15)
        and any("--budget 80" in ln for ln in _loud15),
        repr(_loud15),
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
    v_taxi = verdict([q_taxi])
    # 只有一家可比时没有 alt_gap，管家照实报路费与券省额，并且**不许**拿
    # 「路费比券省的多」当劝退理由 —— 那是范畴错误（换个 --mode 结论就反），
    # 更不许劝人"等外卖"（本项目不支持外卖）。
    check(
        "比价-只有一家时如实报路费且不拿路费比券钱",
        "路费" in v_taxi and "各是各的账" in v_taxi and "外卖" not in v_taxi,
        v_taxi[-60:],
    )

    # 两家可比、跑远只省一点点时，管家必须出言劝退——而且劝的是
    # 「比次选省的钱 vs 路费」这一对，不是「券省的钱 vs 路费」。
    # 远店货便宜 ¥2.80、但打车多花 ¥2.00 路费，综合只便宜 ¥0.80 ——
    # 比路费少，所以该劝退（数字用 taxi 实算：200m→¥15.00，600m→¥17.00）。
    q_near = StoreQuote("N", "近店", distance_m=200, paid_fen=3680, baseline_fen=3880,
                        travel=travel_cost(200, "taxi"))
    q_far = StoreQuote("F", "远店", distance_m=600, paid_fen=3400, baseline_fen=3880,
                       travel=travel_cost(600, "taxi"))
    v_far = verdict([q_far, q_near])
    check(
        "比价-跑远省的不够路费时管家出言劝退",
        "劝" in v_far and "挑近" in v_far,
        v_far[-80:],
    )

    # 接口没返回 distance 的门店，不许被当成「0 米 / 路费 ¥0」。
    # 综合成本里路费是最大的一项，填 0 等于把一家不知道有多远的店
    # 送上「综合最划算」第一名 —— 而屏幕上同一段还写着「距离是真实值」。
    q_unknown = StoreQuote("U", "距离未知的店", distance_m=0, paid_fen=3000,
                           baseline_fen=3880, travel=travel_cost(0, "walk"),
                           distance_unknown=True)
    check(
        "比价-距离未知的店不许参与综合排序（路费算不出来，总数就是假的）",
        not q_unknown.comparable and q_unknown.reason_uncomparable == "距离未知",
    )
    _ranked_unknown = rank_stores([q_unknown, q_near])
    check(
        "比价-距离未知的店必须沉到最后，不能靠假路费排第一",
        _ranked_unknown[-1].store_code == "U",
        str([q.store_code for q in _ranked_unknown]),
    )
    check(
        "比价-全是距离未知时要承认算不出哪家划算（而不是说没店有货）",
        "算不出路费" in verdict([q_unknown]),
        verdict([q_unknown])[:60],
    )

    # compare 的 --budget 是**总数**上限，不是每家上限。
    # 旧写法 max(6, budget//n) 让 --budget 60 --top 20 实际打 120 次，
    # 超了用户设的上限一倍，屏幕上却还印着「均分」。
    from .cli import MIN_CALLS_PER_STORE, _split_budget

    _worst = 0
    for _b in (6, 20, 40, 60, 100, 200):
        for _n in (1, 2, 5, 8, 20):
            _per, _keep = _split_budget(_b, _n)
            _worst = max(_worst, _per * _keep - _b)
    check(
        "比价-预算均分后总数不许超过 --budget（说好是总数上限）",
        _worst <= 0,
        f"最多超 {_worst} 次",
    )
    check(
        "比价-预算不够就少比几家，不许每家都浅尝辄止",
        _split_budget(60, 20)[1] == 10 and _split_budget(60, 20)[0] == 6,
        str(_split_budget(60, 20)),
    )
    check(
        "比价-预算充足时家家都要比到（不必砍店）",
        _split_budget(600, 20) == (30, 20),
        str(_split_budget(600, 20)),
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
        # 0 张和 1 张必须解释「为什么没用上」并且给下一步；
        # 2 张以上时券已经在用了，多说一句是噪音，故意不说。
        #
        # 判据是**语义**不是措辞 —— 早先这里钉的是「券码」这个词，
        # 结果把面向用户的文案锁死在技术黑话上：想改成「限时券这儿用不上」
        # 就得先改测试，本末倒置。真正要保证的是「解释+去处+该闭嘴时闭嘴」。
        check(
            "券范围-券没用上时要解释清楚，够用时不要啰嗦",
            outs[0] != outs[1]
            and all("assets" in o and len(o.strip()) > 20 for o in outs[:2])
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

    # 「减不动」不等于「没有券」。
    # 一件商品明明有一张适用券、只是挂上去一分没省，如果只把「省到钱的」
    # 记进 covered，它就会掉进 orphans 被印成「这张券池里没有它能用的券」——
    # 把「减不动」说成「没有」。变异测试证明这条曾经完全没有防护。
    _zero_menu = [
        CartItem("Z1", "能省的商品", 1, 3000, 3000),
        CartItem("Z2", "减不动的商品", 1, 2000, 2000),
    ]
    _zero_cps = [
        Coupon("z1", "y1", "真能省的券", product_codes=frozenset({"Z1"})),
        Coupon("z2", "y2", "挂了不减钱的券", product_codes=frozenset({"Z2"})),
    ]

    def _zero_oracle(items, assignment):
        base = sum(i.unit_price_fen * i.quantity for i in items)
        off = 500 if items[0].product_code == "Z1" and assignment.mapping else 0
        return PriceResult(price_fen=base - off, original_fen=base)

    _zero_day = build_almanac(
        _zero_menu, _zero_cps, _zero_oracle, Budget(40), now=datetime(2026, 10, 11, 12)
    )
    check(
        "黄历-有券但减不动的商品不许说成「没有它能用的券」（减不动≠没有）",
        not any("没有它能用的券" in (r.note or "") for r in _zero_day["readings"]),
        str([(r.product_name, r.note) for r in _zero_day["readings"]]),
    )
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

    # 段位只能有一把尺子。曾经 optimize 收尾传 `real_saved_fen`、brag 传
    # `real_paid_fen`：历史订单没有裸买价，real_saved 恒为 0，
    # 于是 optimize 收尾永远印最低档「麦门路人」，而 brag 印的是另一档 ——
    # 同一个用户被自己的工具告知两个段位。
    # 光测 `rank_of` 本身抓不到这种错，必须钉住**调用点传了什么**。
    import inspect as _inspect

    from . import cli as _cli_mod

    _cli_src = _inspect.getsource(_cli_mod)
    check(
        "段位-不许拿省额定段位（历史订单无裸买价，那个数恒为 0）",
        "rank_of(real_fen)" not in _cli_src
        and 'rank_of(s["real_saved_fen"]' not in _cli_src
        and 'rank_of(s.get("real_saved_fen"' not in _cli_src,
        "还有调用点在拿 real_saved_fen 定段位",
    )
    check(
        "段位-所有账本段位调用点都用同一把尺子（真实成交累计实付）",
        _cli_src.count('rank_of(s.get("real_paid_fen"') >= 2,
        "至少 optimize 收尾与 brag 两处都要用 real_paid_fen",
    )
    # replay 曾在屏幕上写「加 --budget 80 再试一次」，而 replay 根本没有
    # --budget 这个参数 —— 照着敲是 unrecognized arguments。
    # 给一条走不通的自救指令，比不给更糟：用户会以为是自己敲错了。
    check(
        "命令-replay 真的有 --budget（它自己在错误提示里让人加这个）",
        'p_rep.add_argument("--budget"' in _cli_src,
    )
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
    # 劝退必须发生在「比次选省的钱 < 路费」时，而不是「券省的钱 < 路费」时。
    # 后者是范畴错误：路费是出行方式的成本，跟用不用券无关，
    # 同一单换个 --mode 就从"白跑"变"稳赚"，那句话没有意义。
    check(
        "口吻-跑远省的不够路费时敢劝退",
        "劝" in butler_warning(1500, 500, alt_gap_fen=200, minutes=30)
        and "挑近" in butler_warning(1500, 500, alt_gap_fen=200, minutes=30),
    )
    check(
        "口吻-路费比券省的多但值得跑时不劝退",
        "劝" not in butler_warning(1500, 500, alt_gap_fen=3000, minutes=30),
    )
    check(
        "口吻-只报账不比价时_two笔分开说",
        "各是各的账" in butler_warning(1500, 500) and "外卖" not in butler_warning(1500, 500),
    )
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
    # 「脏数据不崩」只喂了 {} 和 None，测不到真正会骗人的那一支。
    # 旧版本账本 / 手改过的 JSON 里金额可能是 `5000.0` 或 `"5000"`：
    # 早先写的是 `isinstance(base, int)`，类型不对就整条丢弃 ——
    # 实测一笔 ¥6.00 的真实成交被算成 ¥0.00，用户以为自己从来没省过钱。
    # 金额该按**数值**读，不该按**类型**读。
    _dirty = summarize(
        [
            {"baseline_fen": 5000.0, "paid_fen": "4400", "kind": "estimate"},
            {"baseline_fen": "8000", "paid_fen": 7500.0, "kind": "order"},
        ]
    )
    check(
        "账单-float/字符串金额不能被静默丢掉（否则真成交记成 ¥0）",
        _dirty["saved_fen"] == 1100 and _dirty["real_paid_fen"] == 7500,
        f"saved={_dirty['saved_fen']} real_paid={_dirty['real_paid_fen']}",
    )
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
        """和 demo_oracle 完全同一个世界，按固定种子约三成概率抖一下。

        刻意**不**写成「第 2、6 次调用失败」：那把断言钉死在调用序号上，
        引擎内部只要多算一次（比如第零步的裸买下界），所有序号集体错位，
        这条断言立刻失真 —— 本轮就真踩到了：加了下界之后，它从
        「抖动下仍能找到好解」悄悄退化成「抖动下没崩」，却照样绿。
        换成固定种子的伪随机序列，调用次数怎么变都不影响它测的东西。

        也刻意**不**写成「按概率随机失败」：`_failures` 是**累计**计数，
        累计 3 次就走抛错兜底（那是另一条行为，有别的断言管）。
        30% 的概率下 200 次调用必超 3 次，这条断言就变成在测「兜底抛错」了。
        所以只抖 2 次、隔几步抖一次：既避开抛错兜底，也不留连续失败。
        """

        def __init__(self) -> None:
            self._left = 2  # 总共只抖 2 次
            self.calls = 0

        def __call__(self, items, assignment):
            self.calls += 1
            if self._left > 0 and self.calls % 4 == 0:
                self._left -= 1
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

    # 这家店压根没有随单购档位时，不许拿「配上随单购后不再额外省钱」去否定一张好券。
    # 踩过：min_no 取的是**跨商品**的最小值，于是 A 商品的 base 拿去跟 B 商品的
    # 挂券价比，一家没有随单购的店里，一张真省 ¥3 的券被贴上了这句判词 ——
    # 用一个压根没当班的功能去否定一张好券。
    _nocard = audit_coupons(
        demo.audit_oracle,
        demo.AUDIT_MENU,
        demo.AUDIT_COUPONS,
        lambda cart: [None],
        budget=None,
        limit=10,
    )
    check(
        "体检-没有随单购档位时不许说「不再额外省钱」（没这功能就别拿它否定券）",
        all("不再额外省钱" not in (v.note or "") for v in _nocard)
        and any(v.value_fen > 0 for v in _nocard),
        str([(v.coupon.coupon_id, v.value_fen, v.note) for v in _nocard if v.note]),
    )

    # 上面那条只钉住了「有没有随单购档位」这一个开关，而且因为没有随单购档位，
    # 「全局取最小」和「取最值钱那件自己」恰好算出同一个数 —— 抓不住。
    # 必须造一个**有随单购档位**的世界，两种算法才会分岔：
    #   · 商品 A（¥20）：不随单购时这张券真能省 ¥3；随单购档位券被互斥，省 0
    #   · 商品 B（¥5） ：这张券一分减不动
    # 「跨商品取最小」会把 B 的 ¥5 当成「不挂券的最低价」，
    # 于是 A 上那张真省 ¥3 的券被贴上「配上随单购后不再额外省钱」——
    # 用户看到的是「你别吃 A 改吃 B，所以这张券没用」，而这不是他问的事。
    class _CardOpt:
        """随单购档位替身：`_probe` 只用到 `signature()` 做缓存键。"""

        def __init__(self, label: str):
            self.label = label

        def signature(self) -> str:
            return self.label

    _CARD_OPT = _CardOpt("麦金卡")

    def _cross_oracle(items, a):
        base = sum(i.unit_price_fen * i.quantity for i in items)
        item = items[0]
        if item.product_code != "EXP":
            return PriceResult(price_fen=base, original_fen=base)  # 便宜货：减不动
        # 贵的东西：随单购会跟这张券互斥，所以只有不随单购时才真省 ¥3
        off = 300 if (a.mapping and a.with_order is None) else 0
        return PriceResult(price_fen=base - off, original_fen=base)

    _cross_menu = [
        CartItem("EXP", "贵的东西", 1, 2000),
        CartItem("CHEAP", "便宜货", 1, 500),
    ]
    _cross_cps = [Coupon("X1", "y1", "通用券", product_codes=frozenset())]
    _cross_v = audit_coupons(
        _cross_oracle,
        _cross_menu,
        _cross_cps,
        lambda c: [None, _CARD_OPT],
        budget=None,
        limit=5,
    )
    check(
        "体检-不许拿别的商品的最低价去否定这张券（跨商品比价是错的）",
        _cross_v[0].value_fen == 300
        and "不再额外省钱" not in (_cross_v[0].note or ""),
        f"value={_cross_v[0].value_fen} note={_cross_v[0].note!r}",
    )
    # 反向钉住：同一件商品上，随单购那一档确实更便宜时，这句话**该说**。
    # 只钉一边的话，「永远不许说这句话」也能绿 —— 那是另一种谎。
    def _card_wins_oracle(items, a):
        base = sum(i.unit_price_fen * i.quantity for i in items)
        if a.with_order is not None:
            return PriceResult(price_fen=1500, original_fen=base)  # 随单购档只要 ¥15
        off = 300 if a.mapping else 0
        return PriceResult(price_fen=base - off, original_fen=base)

    _cw_v = audit_coupons(
        _card_wins_oracle,
        [CartItem("EXP", "贵的东西", 1, 2000)],
        _cross_cps,
        lambda c: [None, _CARD_OPT],
        budget=None,
        limit=5,
    )
    check(
        "体检-随单购那一档确实更便宜时必须说清楚（不许只钉一边装绿）",
        "不再额外省钱" in (_cw_v[0].note or ""),
        f"value={_cw_v[0].value_fen} note={_cw_v[0].note!r}",
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

    _gap, _trunc, _min_base_any = _probe(
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

    # 构造方式必须是 rate_limited 标记，而不是像以前那样拼一句「触发限流」的
    # 错误消息去喂给关键词匹配 —— 那样断言跟着文案走，文案一改断言就失去意义。
    _rl = McpFatalError(_mc._RATE_LIMIT_MSG, rate_limited=True)
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

    # ── 1d-2. 面向用户的文案不许夹带内部实现
    # 这一版的起因：报错里出现过 `type(data).__name__`、`src/oracle.py`、
    # `{price} 分`、`mapping[0]=3`，还有经 McpError 原样透传的 HTTP 响应体。
    # 对写程序的人是线索，对使用者是天书；更糟的是响应体可能带个人信息。
    from . import mcd_client as _mc

    _internal_words = [
        "Traceback",
        "NoneType",
        "src/oracle.py",
        "as_fen",
        "to_fen",
        "tools/call",
        "HTTPError",
        "membershipCode=",
    ]
    _all_hints: list[str] = []
    for _e in (
        McpFatalError(_mc._RATE_LIMIT_MSG, rate_limited=True),
        McpFatalError(_mc._HTTP_UNAVAILABLE_MSG),
        McpError("麦当劳接口这次没有返回数据，稍后再试。"),
    ):
        _all_hints += _error_hint(_e)[1]
    _leaked = [w for w in _internal_words if any(w in ln for ln in _all_hints)]
    check(
        "错误提示-面向用户的文案不许出现 Traceback / 源码路径 / 内部字段名",
        not _leaked,
        f"泄漏：{_leaked}",
    )
    check(
        "错误提示-限流单独一支，而不会被当成 Token 问题误导用户",
        "隔一两分钟" in "\n".join(_error_hint(
            McpFatalError(_mc._RATE_LIMIT_MSG, rate_limited=True))[1]),
        str(_error_hint(McpFatalError(_mc._RATE_LIMIT_MSG, rate_limited=True))[1]),
    )

    # ── 1d-3. 未预料异常必须有人话出口，不能裸抛栈给使用者
    import os as _os
    import io as _io9
    import contextlib as _ctx9
    from .cli import _unexpected_error

    _initial_debug = _os.environ.get("MCD_BUTLER_DEBUG")
    try:
        _os.environ.pop("MCD_BUTLER_DEBUG", None)
        _buf9 = _io9.StringIO()
        _err9 = _io9.StringIO()
        # stdout **和 stderr 都要抓**：诊断信息本就该走 stderr，
        # 只抓 stdout 的话「忘了关掉调试输出」这种改动一条都抓不到。
        with _ctx9.redirect_stdout(_buf9), _ctx9.redirect_stderr(_err9):
            _rc9 = _unexpected_error(RuntimeError("boom"))
        _quiet = _buf9.getvalue() + _err9.getvalue()
        _os.environ["MCD_BUTLER_DEBUG"] = "1"
        _err10 = _io9.StringIO()
        # stdout 也要重定向，否则这几行提示会真的印在自检的输出里，
        # 看起来就像"自检运行中出了个 boom 异常"。
        with _ctx9.redirect_stdout(_io9.StringIO()), _ctx9.redirect_stderr(_err10):
            _unexpected_error(RuntimeError("boom"))
        _loud = _err10.getvalue()
    finally:
        if _initial_debug is None:
            _os.environ.pop("MCD_BUTLER_DEBUG", None)
        else:
            _os.environ["MCD_BUTLER_DEBUG"] = _initial_debug

    check(
        "兜底-未预料异常默认不把 traceback 甩给使用者（stdout 和 stderr 都不许）",
        "Traceback" not in _quiet and "runtimeerror" not in _quiet.lower(),
        repr(_quiet[:160]),
    )
    check(
        "兜底-未预料异常要给「没碰你的钱」和去哪儿反馈",
        "支付" in _quiet and "issues" in _quiet,
        repr(_quiet[:160]),
    )
    check(
        "兜底-设了 MCD_BUTLER_DEBUG 才给栈（排障通道必须留着）",
        # 查「有没有栈帧」而不是 "Traceback" 字面：没经过 raise 的异常没有
        # traceback 对象，这时走的是 print_stack()，输出里只有 File "…
        'File "' in _loud and "boom" in _loud,
        repr(_loud[-160:]),
    )
    check("兜底-异常退出码统一为 4", _rc9 == 4, str(_rc9))

    # ── 1f. 面向用户的输出不许夹带内部实现 / 隐私
    # 这一组的起因很具体：曾经屏幕上出现过 membershipCode、订单号、
    # C:\Users\张三\.mcd-butler\、NoneType、tools/call、以及整个 HTTP 响应体。
    # 其中订单号和响应体是**隐私泄露**（能反查到手机号 / 地址 / 支付流水），
    # 其余是让用户以为自己把软件装坏了。
    import pathlib as _pl15
    import subprocess as _sp15

    _root15 = str(_pl15.Path(__file__).resolve().parent.parent)
    _screen = ""
    for _cmd15 in ("demo", "demo --all"):
        _r15b = _sp15.run(
            [_sp15.sys.executable, "-m", "src.cli", *_cmd15.split()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=_root15,
        )
        _screen += (_r15b.stdout or "") + (_r15b.stderr or "")

    _forbidden = [
        "membershipCode=",          # 会员卡内部编码
        "storeCode=",               # 接口字段名
        "Traceback (most recent",   # 给使用者看的裸栈
        "NoneType",                 # Python 类型名
        "tools/call",               # RPC 内部方法名
        "src/oracle.py",            # 源码路径
        ":\\Users\\",               # 家目录，含本机用户名
        "算价接口返回了非对象结构",
        "as_fen",
    ]
    _sep15 = [w for w in _forbidden if w in _screen]
    check(
        "隐私-面向用户的输出不含内部标识 / 家目录 / 裸 traceback",
        not _sep15,
        f"泄漏：{_sep15}",
    )
    check(
        "隐私-演示输出确实抓到了内容（上面的检查不是空跑）",
        len(_screen.strip()) > 200 and "省下" in _screen,
        f"只抓到 {len(_screen)} 字符",
    )

    # 上面那条端到端扫描覆盖不到所有分支（很多路径 demo 走不到），
    # 所以这几处关键文案再各钉一根钉子 —— 都是抽出来的纯函数。
    from .cli import (
        _format_quote_row,
        _replay_source,
        _snapshot_saved_note,
        _with_order_options,
    )

    _wo15 = _with_order_options([
        CartItem(product_code="P1", membership_code="SECRET-123",
                 needs_with_order=True, discount_type="随单购麦金卡优惠")
    ])
    check(
        "隐私-会员卡编码不许出现在任何 label 里（只许留在 payload）",
        len(_wo15) >= 2
        and all(o is None or "membershipCode" not in (o.label or "") for o in _wo15)
        and any("SECRET-123" in str(getattr(o, "payload", {})) for o in _wo15 if o),
        repr([getattr(o, "label", None) for o in _wo15]),
    )
    _src = _replay_source({"orderId": "ORDER-SECRET-9", "createTime": "2026-07-14 12:31:05"})
    check(
        "隐私-订单号不许上屏（能在客服系统反查手机号/地址/支付流水）",
        "ORDER-SECRET-9" not in _src and "2026-07-14" in _src,
        repr(_src),
    )
    check(
        "隐私-账本路径不许打印绝对路径（家目录含本机用户名）",
        "Users" not in _snapshot_saved_note("C:\\Users\\张三\\.mcd-butler\\s.json")
        and ".mcd-butler" in _snapshot_saved_note("/x")
        and "C:" not in _snapshot_saved_note("C:\\Users\\张三\\x.json"),
        repr(_snapshot_saved_note("C:\\Users\\张三\\.mcd-butler\\s.json")),
    )

    # ── 「照着做」：算出答案不等于能下单，最后一公里得补上
    from .cli import (
        _condition_warning,
        _distance_of,
        _due_today_report,
        _how_to_order,
        _parse_server_datetime,
    )

    # 距离未知 ≠ 0 米。这一条的防护曾经是零（变异删掉传参，自检一条没红）。
    check(
        "比价-门店字典里没给 distance 就是「不知道」，不能填 0 冒充就在楼下",
        _distance_of({}) == (0, False)
        and _distance_of({"distance": 1200}) == (1200, True)
        and _distance_of({"distance": "1200"}) == (0, False)
        and _distance_of({"distance": True}) == (0, False),
        f"{_distance_of({})} {_distance_of({'distance': '1200'})}",
    )

    # 到期提醒是唯一不可逆的事（券过期作废，错过就没了）。
    # 「没查到」必须跟「没有今天到期的」说不同的话。
    _due_ok = _due_today_report([{"title": "A", "valid_text": "今天", "due_today": True}], None)
    _due_no = _due_today_report([{"title": "A", "valid_text": "x"}], None)
    _due_err = _due_today_report([], RuntimeError("限流"))
    check(
        "管家-卡包没查到时必须明说（不能跟「没有今天到期的」混成一句）",
        "没查到" in "".join(_due_err)
        and "没法保证" in "".join(_due_err)
        and "今天内到期" not in "".join(_due_err),
        str(_due_err),
    )
    check(
        "管家-有今天到期的券要点名，没有就说没有（三种情形三种说法）",
        "今天内到期，不用就作废" in "".join(_due_ok)
        and "A" in "".join(_due_ok)
        and "没有今天到期的" in "".join(_due_no)
        and _due_today_report([], None) == [],
    )

    # 条件价（标价 ¥29、不满足条件按 ¥57 结账）必须单独喊一声。
    # 这是最刺痛的一条真发现，藏在行尾「← …」里等于没有。
    _cond_cart = [
        CartItem("P1", "四件套", 1, 2900, 5700),
        CartItem("P2", "巨无霸", 1, 2500, 2500),
    ]
    _cond_txt = "".join(_condition_warning(_cond_cart))
    check(
        "管家-购物车里有条件价时必须单独告警（标价不是你能拿到的价）",
        "条件价" in _cond_txt and "四件套" in _cond_txt and "¥29.00" in _cond_txt
        and "¥57.00" in _cond_txt and "差 ¥28.00" in _cond_txt
        and "巨无霸" not in _cond_txt,
        _cond_txt,
    )
    check(
        "管家-没有条件价时不要乱喊（没坑不能硬造一个）",
        _condition_warning([CartItem("P2", "巨无霸", 1, 2500, 2500)]) == [],
    )

    # 上面这些断言验的是「函数本身对不对」。
    # 但变异测试证明还有一类更隐蔽的失效：**函数是对的，压根没接进命令里** ——
    # 把 print 那几行删掉，上面每条断言照样全绿，而用户永远看不到这段输出。
    # 所以再钉一道总闸：这些新东西必须真的出现在对应命令的源码里。
    import inspect

    from .cli import run_advise, run_compare, run_optimize

    _src_of = {
        "optimize": inspect.getsource(run_optimize),
        "advise": inspect.getsource(run_advise),
        "compare": inspect.getsource(run_compare),
    }
    check(
        "管家-照着做的步骤必须真的打印出来（optimize 与 advise 都要）",
        "_how_to_order" in _src_of["optimize"] and "_how_to_order" in _src_of["advise"],
    )
    check(
        "管家-条件价告警必须真的接进 optimize（不能只写个函数放那儿）",
        "_condition_warning" in _src_of["optimize"],
    )
    check(
        "管家-到期提醒必须走 _due_today_report（没查到和没有要分开说）",
        "_due_today_report" in _src_of["advise"],
    )
    check(
        "比价-距离未知必须真的传进 StoreQuote（否则又变成 0 米）",
        "distance_unknown" in _src_of["compare"] and "_distance_of" in _src_of["compare"],
    )

    # 服务端时间必须一律转成不带时区的墙上时间：
    # 留着 tzinfo 的话，naive−aware 相减直接 TypeError，而那个异常落在
    # 外层 except 里被吞掉 —— 「今天到期」这条最要紧的提醒会静默消失。
    _pdt = _parse_server_datetime("2026-10-13T04:24:00.123456+08:00")
    check(
        "时间-服务端时间必须剥掉时区（naive−aware 相减会让到期提醒静默消失）",
        _pdt is not None and _pdt.tzinfo is None and _pdt.hour == 4,
        repr(_pdt),
    )
    check(
        "时间-带 GMT 后缀、带空格分隔的报文都要认（取墙上时间，不做时区换算）",
        _parse_server_datetime("2026-10-13 04:24:00 GMT+08:00") == _pdt.replace(microsecond=0)
        and _parse_server_datetime("2026-10-13T04:24:00Z") is not None
        and _parse_server_datetime("不是时间") is None,
        str(_parse_server_datetime("2026-10-13 04:24:00 GMT+08:00")),
    )

    _how_cart = [CartItem("A", "巨无霸", 1, 2500), CartItem("B", "中份薯条", 2, 1200)]
    _how_cps = [Coupon("c0", "x0", "巨无霸立减 5 元"), Coupon("c1", "x1", "小食买一送一（薯条）")]
    _how_best = Assignment(mapping={0: 0, 1: 1})
    _how_txt = "\n".join(_how_to_order(_how_cart, _how_cps, _how_best))
    check(
        "管家-最优解必须给出「照着做」的步骤（加什么 / 每张券挂哪件）",
        "加购" in _how_txt
        and "巨无霸×1" in _how_txt
        and "中份薯条×2" in _how_txt
        and "巨无霸 ← 「巨无霸立减 5 元」" in _how_txt
        and "中份薯条 ← 「小食买一送一（薯条）」" in _how_txt,
        _how_txt,
    )
    check(
        "管家-没搜到底时要提醒先别照着做（结论还会变）",
        "还没搜到底" in "\n".join(
            _how_to_order(_how_cart, _how_cps, _how_best, converged=False)
        ),
    )
    check(
        "管家-一件券都挂不上时要直说，不许编一步操作出来",
        "没有券能挂" in "\n".join(
            _how_to_order(_how_cart, _how_cps, Assignment(mapping={}))
        ),
    )
    # 随单购的卡费是**花出去退不回来**的钱，而且下一单还得再买一次。
    # 只说「卡费另算」，用户会当成一笔可以忽略的小钱 —— 其实它是这单的真实成本。
    class _WO2:
        label = "麦金卡（随单购）"

        def signature(self) -> str:
            return "wo2"

    _card_txt = "\n".join(
        _how_to_order(_how_cart, _how_cps, Assignment(mapping={0: 0}, with_order=_WO2()))
    )
    check(
        "管家-随单购必须说清卡费不退、下一单还得再买（不是「另算」两个字）",
        "不退" in _card_txt and "再买一次" in _card_txt,
        _card_txt,
    )
    # 算价时刻 ≠ 下单时刻。跨过时段边界（早市 10:30 收）价格就变了，
    # 拿着 10:29 算好的价 10:31 去结账一定多付钱 —— 这句不说是害人。
    _timed = "\n".join(
        _how_to_order(_how_cart, _how_cps, _how_best, now=datetime(2026, 10, 13, 10, 29))
    )
    check(
        "管家-报出算价时刻并警告跨时段就不是这个价",
        "10:29 算的" in _timed and "10:30" in _timed,
        _timed,
    )
    check(
        "管家-没给时间就不编一个时刻（不知道就是不知道）",
        "算的" not in _how_txt,
        _how_txt,
    )
    # 打烊 = 白跑一趟，不可逆。字段缺失时必须闭嘴（没查到 ≠ 打烊）。
    from .cli import _closed_warning

    _cw = _closed_warning({"storeName": "人民广场店", "businessStatus": False})
    check(
        "管家-门店打烊时必须先喊一声（白跑一趟没人赔）",
        len(_cw) == 2 and "没在营业" in _cw[0] and "人民广场店" in _cw[0],
        str(_cw),
    )
    check(
        "管家-营业状态未知时不许说打烊（没查到不等于打烊）",
        _closed_warning({"storeName": "X"}) == []
        and _closed_warning({"storeName": "X", "businessStatus": True}) == [],
    )

    # compare 表格：金额必须带 ¥，否则用户分不清 36.00 是元还是分
    class _Q15:
        store_name = "人民广场麦当劳餐厅（一号店）超长名字测试"
        distance_m = 1440
        paid_fen = 3600
        total_fen = 3800
        comparable = True
        distance_unknown = False
        reason_uncomparable = "缺货"
        missing: list[str] = []

        class travel:  # noqa: N801
            minutes = 40
            money_fen = 200

    # 中文对齐：这一条是用户抱怨过的（"格式不够美观，看得很累"）。
    # 判据是**金额列的显示列位置**必须一致 —— 只查字符串长度没用，
    # `:<18` 补出来的字符串长度反而相同，只是显示出来差两格。
    from .cli import _dw as _dw15, _format_cart_line, _pad as _pad15

    def _col_of_yuan(line: str) -> int:
        return _dw15(line[: line.index("¥")])

    _l1 = _format_cart_line("巨无霸", 2500)
    _l2 = _format_cart_line("麦辣鸡腿堡", 1800)
    _l3 = _format_cart_line("香辣鸡腿堡中份薯条中杯可乐", 3900)
    _l4 = _format_cart_line("Beef Burger", 2200)
    check(
        "版式-中文商品名按显示宽度对齐（金额列必须落在同一列）",
        len({_col_of_yuan(x) for x in (_l1, _l2, _l3, _l4)}) == 1,
        f"¥ 的列位置：{[ _col_of_yuan(x) for x in (_l1, _l2, _l3, _l4)]}｜{_l1!r} {_l2!r}",
    )
    check(
        "版式-_pad 按显示宽度补（中文一个字算两列）",
        _dw15(_pad15("巨无霸", 18)) == 18 and _dw15(_pad15("麦辣鸡腿堡", 18)) == 18,
        f"{_dw15(_pad15('巨无霸', 18))} / {_dw15(_pad15('麦辣鸡腿堡', 18))}",
    )
    check(
        "版式-购物车行的数量/备注不影响金额列位置",
        _col_of_yuan(_format_cart_line("巨无霸", 2500, qty="×2  "))
        == _col_of_yuan(_format_cart_line("巨无霸", 2500, qty="×2  ")),
    )

    _row15 = _format_quote_row(_Q15(), comparable=True)
    check(
        "版式-compare 每行金额都必须带 ¥（裸数字看不出单位）",
        _row15.count("¥") == 3,
        repr(_row15),
    )
    check(
        "版式-compare 缺货行也要跟表头对齐，不能裸有没有 ¥ 的混排",
        "缺货" in _format_quote_row(_Q15(), comparable=False),
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
    # 这条曾经是凑数的：菜单合计当时恒为 0（守卫读了个不存在的 `price_fen`），
    # 于是 `if menu_total > 0` 永远不成立，这里无论守卫在不在都绿。
    # 现在改成直接断言守卫「真的会用菜单价去比」——价格离谱时必须报错。
    _scale_high = False
    try:
        _assert_price_scale({"price": 5700 * 10}, [CartItem("Z", "测试", 1, 5700)])
    except McpError:
        _scale_high = True
    check(
        "算价-到手价高过菜单价太多时必须报错（守卫不能是永远不进的死代码）",
        _scale_high,
    )
    # 反向钉住「菜单合计算得对」：把守卫实现改成读错字段，上面那条就会翻红。
    check(
        "算价-菜单合计要用真实字段算（读错字段会得到 0，守卫形同虚设）",
        CartItem("Z", "测试", 2, 5700).effective_price_fen() * 2 == 11400
        and not hasattr(CartItem("Z", "测试", 1, 5700), "price_fen"),
    )

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

    # 跑两遍：默认版（路人看到的第一印象）和 --all 版（README 贴的数字契约）。
    # 只跑一遍的话，"默认演示被塞进了六段"这种退化根本没人拦得住。
    _buf = _io4.StringIO()
    with _ctx2.redirect_stdout(_buf):
        _core_rc = run_demo()
    _core_out = _buf.getvalue()
    _buf2 = _io4.StringIO()
    with _ctx2.redirect_stdout(_buf2):
        _demo_rc = run_demo(show_all=True)
    _demo_out = _buf2.getvalue()
    check(
        "演示-离线演示能跑完且正常退出（默认 / --all 都要）",
        _core_rc == 0 and _demo_rc == 0,
        f"默认 rc={_core_rc}，--all rc={_demo_rc}",
    )

    _readme = (_Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    _must_appear = [
        "挂券后小计 ¥42.00",
        "随单购「麦金卡（随单购）」再减 ¥6.00",
        "到手 ¥6.00（省 ¥6.00，力度 50%）",
        "标的 ¥62.90 是「随单购麦金卡优惠」价",
        # 「实测」改成「实际」：这段渲染代码 demo 也在用，而离线演示
        # 一个请求都没发过 —— 说「实测」是假的。
        "标着减 ¥8.00，实际只省 ¥5.00",
        # demo 的「真实试算过」同样是假承诺：演示数据是仓库里写死的。
        "怎么挂最省（演示数据上逐张试算，不是真实门店的价格）",
        # 省下的钱里有随单购那一段，卡费还没扣 —— 必须单独挑出来说，
        # 否则坏了本项目自己立的「不拿卡费给自己脸上贴金」。
        "其中 ¥6.00 是随单购带来的，卡费还没扣进去（不随单购是 ¥42.00）",
        # 「用了 N 张券」数的是挂上去的券，不是减了钱的券。
        "（用了 0 张券（另有 1 张挂了但被随单购抵消，没减到钱） + 麦金卡（随单购））",
        # 「试出来的门槛」已改为「门槛」：券面写了就直接念券面（满 20 减 3 元
        # → 门槛 ¥20.00），不再花两次算价反推一个更粗的区间。契约随之更新。
        "门槛：满 ¥20.00（券面写的）",
        # 券名也算契约：README 里贴的券名和 demo_data.py 里的必须一致，
        # 否则读者照着 README 跑 demo 会看到另一张券。
        "中杯可乐立减 3 元",
        "巨无霸立减 5 元",
        # ── 下面这些是 demo --all 那四段的契约。
        # 之前 README 里贴的黄历/日历/段位输出是手抄的旧版：中杯可乐写成
        # 「省 ¥6.00」（真值 ¥3.00）、日历还带着已经删掉的 ═ 框线和昨天的日期。
        # 读者照着跑会发现对不上 —— 那比不贴还糟。所以逐条钉死。
        "正餐时段，套餐券最全",
        "挂「巨无霸立减 5 元」→ 到手 ¥20.00（省 ¥5.00，力度 20%）",
        "挂「汉堡类任选 -4 元」→ 到手 ¥18.00（省 ¥4.00，力度 18%）",
        "挂「中杯可乐立减 3 元」→ 到手 ¥6.00（省 ¥3.00，力度 33%）",
        "「麦旋风满 20 减 1 元」挂券反而贵 ¥1.00",
        "不满足条件要按 ¥122.50 结账，差 ¥59.60",
        # 离线演示里说「真的算过价」是假承诺；同时必须交代
        # 「时段被钉在 12:00」这个为可复现做的妥协。
        "演示把时间钉在当天 12:00",
        "券包体检 · 演示餐厅·二号店（另一套券池）",
        "在售 4 件 · 券池 5 张 · 本次体检 5 张",
        "健康度 ████████████░░░░░░░░ 60%",
        # 光给一个百分比，用户只能猜分母是什么。定义必须上屏。
        "健康度 = 真能省钱的 3 张 ÷ 体检到的 5 张",
        "挂「板烧鸡腿堡」 ¥18.00 → ¥14.00",
        "门槛：满 ¥15.00（券面写的），便宜的东西够不到",
        "今天有 2 个活动",
        "超值𝟡.𝟡元早餐两件套陪你开工啦　¥9.90",
        "学生专属！49元150天麦金学期卡　¥49.00",
        "接下来 7 天，1 天有今天吃不到的新活动：",
        "麦麦夜市回归",
        "价格取自活动文案自己写的数字；没写的显示「未标价」，不猜。",
        # 「不急可以等这天」是替用户判断紧迫性 —— 等不等由他自己定。
        "等不等你自己定",
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

    # ── 17b. README 贴的 --all 输出天生带日期（今天几号、几天后的活动），
    # 写进文档那一刻就在过期。所以必须写明「录于哪天」，读者才知道日期行不算数。
    check(
        "文档-README 贴的 --all 输出必须标注录制日期（不标注就天天过期）",
        "录于 20" in _readme,
        "README 里没写「录于 YYYY-MM-DD」；demo --all 的输出带日期行，"
        "不标注的话读者会当成永远成立",
    )
    # 日历那段的 ═══ 框线删过一次，别被旧稿粘贴带回来。
    check(
        "文档-README 里的 ═══ 框线不许复活（日历段删过一次）",
        "══" not in _readme,
        "README 里又出现 ═ 框线了",
    )

    # ── 17c. README 里「命中 94%」这个数字必须是跑出来的，不是拍的整数。
    # 种子固定，所以每次都是同一个值 —— 引擎一旦变好或变坏，README 就得跟着改，
    # 否则这里红。（往上吹和往下压都会红：吹了显得虚，压了显得怂。）
    _rate_txt = f"{rate:.0%}"
    check(
        "文档-README 写的命中率必须是实测值（引擎改了文档就得跟着改）",
        _rate_txt in _readme,
        f"实测 {_rate_txt}（{hit}/{cases}），README 里找不到这个数",
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

    # ── N12：时段是左闭右开，边界不能被划到隔壁时段。
    # 早餐 10:30 才收摊，所以 10:15 必须还算早市 —— 早先按整点切，
    # 10:15 就被划进午市，一张还能用的早餐券被说成过了时段。
    check(
        "黄历-时段边界左闭右开且早餐按 10:30 收摊",
        period_of(datetime(2026, 10, 11, 5))[0] == "早市"
        and period_of(datetime(2026, 10, 11, 10, 15))[0] == "早市"
        and period_of(datetime(2026, 10, 11, 10, 30))[0] == "午市"
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
    from .assets import build_report, points_advice
    from .delivery import describe, mask_address

    class _FakeAssetsClient:
        def call_json(self, tool, arguments=None):
            return {"data": {"availablePoint": "9.9", "currency": "积分"}}

        def data(self, tool, arguments=None):
            # 周报要取服务端时间判断「今天到期」。这里故意**不**提供时间：
            # 真正的服务端不可达就是这个样子，于是周报会退回本机时间，
            # 并在报告里明说「按本机时间判断到期」—— 这条下面有断言盯着。
            return None

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
    # 问不到服务端时间时必须自己说出来：本机时钟不准 → 到期判断偏 → 漏报，
    # 而券作废不可逆，用户有权知道这个「今天」是哪个钟给的。
    check(
        "资产-服务端时间问不到时明说按本机时间判断到期（漏报不可逆）",
        "按本机时间判断到期" in _rep,
        _rep[:200],
    )
    # 反过来：给得出服务端时间就不该再喊这一句（不是每次都喊 = 不是装饰）
    check(
        "资产-拿得到服务端时间时不再喊本机时间警告",
        "按本机时间判断到期" not in build_report(_FakeAssetsClient(), now=datetime(2026, 10, 13, 12, 0)),
        "",
    )

    # ── 积分去向：报「您有 N 分」却不说这 N 分能干嘛，是在制造焦虑；
    #    但为了显得有用而列出一堆其实要另付现金的商品，是骗人。
    _pts = {"currency": "麦享会积分", "available": 9.9,
            "expire_this_month": 0, "expire_next_month": 0}
    _lot = {"name": "麦麦积分抽奖", "status": "进行中", "draw_point": 24,
            "available": 9.9, "eligible": False, "reason": "积分不足"}
    check(
        "积分-抽不起时如实说还差多少",
        "还差 14.1" in "".join(points_advice(_pts, _lot, {})),
    )
    check(
        "积分-商城没有纯兑换项时不许编兑换清单",
        "没有一件是纯积分兑换"
        in "".join(points_advice(_pts, _lot, {"total": 50, "exchangeable": []})),
    )
    _mall = {
        "total": 50,
        "exchangeable": [
            {"name": "1积分应援", "point": 1, "price": 0, "cat": ""},
            {"name": "香芋派", "point": 50, "price": 0, "cat": ""},
        ],
    }
    _ad = "".join(points_advice(_pts, _lot, _mall))
    check(
        "积分-要区分「换得起」和「还差多少」",
        "换得起" in _ad and "✅" in _ad and "再攒 40.1" in _ad,
        _ad[:120],
    )
    check(
        "积分-够抽时报得出能抽几次",
        "够抽" in "".join(
            points_advice({"currency": "分", "available": 100,
                           "expire_this_month": 0, "expire_next_month": 0},
                          {"name": "抽", "status": "进行中", "draw_point": 24,
                           "available": 100, "eligible": True, "reason": ""}, {})
        ),
    )

    # ── 账本：取「最近一单」时必须是真实成交，不能是自己刚写进去的测算
    from . import snapshot as _snapmod

    _orig_load = _snapmod.load_all
    try:
        # 顺序是刻意的：真实成交**在前**、测算**在后**。
        # 反过来的话 entries[-1] 正好就是那条真实成交，
        # 于是「优先真实成交」和「直接取最后一条」返回同一个东西 ——
        # 变异测试真的抓到了这一条：V5 改回去，自检全绿。
        # 现实里就是这个顺序：replay / optimize 每次把测算追加到末尾。
        _snapmod.load_all = lambda: [
            {"kind": "order", "paid_fen": 5000, "store_code": "S1"},
            {"kind": "estimate", "paid_fen": 3000, "store_code": "S1"},
        ]
        _got = _snapmod.latest("S1")
    finally:
        _snapmod.load_all = _orig_load
    check(
        "账本-取最近一单优先真实成交（拿自己的测算当基准，diff 恒为 0）",
        _got is not None and _got.get("kind") == "order" and _got.get("paid_fen") == 5000,
        str(_got),
    )

    # 「读不出来」和「本来就是空的」必须分开：合并成 0 会让屏幕打印
    # 「账本是空的，没有可撤回的记录」，用户以为数据没了、甚至以为清空成功了。
    _orig_read = _snapmod._read
    try:
        _snapmod._read = lambda: ([{"paid_fen": 1}], True)
        _broken_n = _snapmod.delete_last(1)
    finally:
        _snapmod._read = _orig_read
    check(
        "账本-读不出来时撤回返回 -1（不许翻译成「账本是空的」）",
        _broken_n == -1,
        str(_broken_n),
    )

    # ── 段位不能刷：测算重复记一百次也不许涨，真实订单还得按指纹去重。
    # 早先段位按「累计省下」算，而 optimize 默认每次都记账 ——
    # 于是同一单跑 15 次就是「省了 ¥94.50、麦门信徒」，发出去是张假成绩单。
    _many = [{"baseline_fen": 5000, "paid_fen": 4400, "kind": "estimate"}] * 100
    _s_many = summarize(_many)
    check(
        "段位-测算刷不出来（重复一百次仍是路人）",
        rank_of(_s_many["real_paid_fen"])["title"] == "麦门路人"
        and _s_many["real_orders"] == 0,
        f"{_s_many['real_paid_fen']} / {rank_of(_s_many['real_paid_fen'])['title']}",
    )
    check(
        "段位-真实成交按实付累计（不编裸买价）",
        rank_of(summarize([{"paid_fen": 6000, "baseline_fen": 0,
                            "kind": "order"}])["real_paid_fen"])["title"] == "麦门信徒",
        str(summarize([{"paid_fen": 6000, "baseline_fen": 0, "kind": "order"}])["real_paid_fen"]),
    )
    # 订单指纹必须是**哈希**而不是订单号原文：订单号能在客服系统反查手机号
    # 与支付流水，落到磁盘上等于把整单隐私写进文件。
    _fake_order = {
        "orderNo": "20261011abcd1234",
        "storeCode": "S1",
        "realTotalAmount": "38.00",
    }
    _key = hashlib.sha256(
        str(_fake_order["orderNo"]).encode("utf-8")
    ).hexdigest()[:16]
    _entry = entry_from_cart(
        cart=[], paid_fen=3800, baseline_fen=0, discount_fen=0,
        store_code="S1", store_name="", city="", keyword="",
        coupon_titles=[], with_order_label="", kind="order", order_key=_key,
    )
    check(
        "账本-真实订单只存订单号哈希（不落原文，反查不到手机号）",
        _entry["order_key"] == _key
        and "20261011abcd1234" not in json.dumps(_entry, ensure_ascii=False),
        _entry["order_key"],
    )

    # ── 外送：收货地址是能定位到一个人的数据，输出必须脱敏；
    #    没有地址时只能说「算不了」，不许拿估算冒充。
    check(
        "外送-地址脱敏要打掉门牌尾号",
        mask_address("上海市人民广场小区3号楼1502室").count("*") >= 4,
        mask_address("上海市人民广场小区3号楼1502室"),
    )

    class _FakeDeliveryClient:
        def call_json(self, tool, arguments=None):
            if tool == "delivery-query-addresses":
                return {"data": {"addresses": []}}
            return {"data": {}}

        def call(self, tool, arguments=None):
            return ""

    _dl = "\n".join(describe(_FakeDeliveryClient()))
    check(
        "外送-没有收货地址时如实说算不了且不编价",
        "收货地址：0 个" in _dl and "算不出" in _dl and "不拿" in _dl,
        _dl[:80],
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
    _climbed = _sopt._local_search(
        [_sx, _sy],
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

    # ── N21：抢占邻域 —— 有一类最优解只有它走得到
    #
    # 场景：A 挂 X、B 挂 Y，最优其实是「A 挂 Y」。
    # 逐个邻域看过去，前五个**一个都动不了**：
    #   删 A 上的 X → X 是正收益，删了变贵        拒绝
    #   删 B 上的 Y → 同样变贵                    拒绝
    #   给 A 换券   → Y 正被 B 占着，换不到        无候选
    #   把 Y 挪到 A → A 被 X 占着，挪不过去        无候选
    #   对调        → X 不适用于 B                 无候选
    # 只有「抢占」能一步到位：把 Y 从 B 抢过来，X 就此作废。
    #
    # 这条断言钉的是那个洞本身 —— 补上它之前，2 件商品 2 张券这种
    # 小到能穷举的场景（解空间才 21 个方案）也只能命中 71.4%。
    _pa = CartItem("A", "A", 1, 2000)
    _pb = CartItem("B", "B", 1, 2000)
    _pgain = {("X", "A"): 200, ("Y", "A"): 800, ("Y", "B"): 100}
    _pcps = [
        Coupon("X", "x", "X券", frozenset({"A"})),
        Coupon("Y", "y", "Y券", frozenset({"A", "B"})),
    ]

    def _pair_oracle(items, a):
        _sub = sum(i.unit_price_fen * i.quantity for i in items)
        _d = sum(
            _pgain.get((_pcps[ci].coupon_id, items[idx].product_code), 0)
            for idx, ci in a.mapping.items()
        )
        return PriceResult(price_fen=_sub - _d, original_fen=_sub, discount_fen=_d)

    _popt = CouponOptimizer(_pair_oracle, max_calls=40)
    _ptable = _popt._eligibility([_pa, _pb], _pcps)
    _got = _popt._local_search(
        [_pa, _pb], _ptable, Assignment(mapping={0: 0, 1: 1})
    )  # A 挂 X、B 挂 Y，价 ¥37.00
    check(
        "引擎-抢占邻域必须存在（最优解只能靠「抢走别人身上的券」才走得到）",
        _got.price is not None and _got.price.price_fen == 3200,
        f"停在 ¥{(_got.price.price_fen if _got.price else -1) / 100:.2f}（最优 ¥32.00）",
    )

    # ── N22：横移 —— 卡在局部最优时要肯走「价格不变」的那一步
    # 纯下降在「邻域里没有更便宜的」时就宣布到顶了，可它证明的只是
    # 「一步走不到更好」。平局步不降价，但换个站位就能换一片盆地。
    _ha = CartItem("A", "A", 1, 1000)
    _hb = CartItem("B", "B", 1, 1000)
    _hcps = [Coupon("X", "x", "X券", frozenset({"A", "B"}))]

    def _flat_oracle(items, a):
        _sub = sum(i.unit_price_fen * i.quantity for i in items)
        # 挂哪儿都省 5 元 —— 于是「把 X 从 A 挪到 B」是一步平局
        return PriceResult(
            price_fen=_sub - 500 * len(a.mapping), original_fen=_sub
        )

    _hopt = CouponOptimizer(_flat_oracle, max_calls=40)
    _htable = _hopt._eligibility([_ha, _hb], _hcps)
    _hcur = Assignment(mapping={0: 0})  # A 挂 X
    _hopt.evaluate([_ha, _hb], _hcur)
    _hprice = _hcur.price.price_fen
    _loose, _, _lt = _hopt._scan(
        [_ha, _hb], _htable, _hcur, _hprice, accept_equal=True, tabu=None, iteration=1
    )
    _strict, _, _st = _hopt._scan(
        [_ha, _hb], _htable, _hcur, _hprice, accept_equal=False, tabu=None, iteration=1
    )
    check(
        "引擎-卡住时要肯走平局步（只肯下降会原地认输，横移才换得到盆地）",
        _st == "stuck" and _lt == "moved" and _loose.mapping == {1: 0},
        f"严格={_st}（应 stuck） 平局={_lt}（应 moved） 落点={_loose.mapping}",
    )

    # ── N23：邻域计数必须与生成器逐个对齐
    # suggest_budget 报给用户的「--budget N」就靠这个数。
    # 它要是比实际多算一倍，推荐值就跟着翻倍，用户会白等一场。
    _nopt = CouponOptimizer(lambda items, a: PriceResult(price_fen=0), max_calls=1)
    _ncart = [CartItem(f"P{i}", f"P{i}") for i in range(4)]
    _ncps = [
        Coupon("c0", "0", "0", frozenset({"P0", "P1", "P2"})),
        Coupon("c1", "1", "1", frozenset({"P1", "P2", "P3"})),
        Coupon("c2", "2", "2", frozenset({"P0", "P3"})),
        Coupon("c3", "3", "3", frozenset({"P1"})),
    ]
    _ntab = _nopt._eligibility(_ncart, _ncps)
    _mism: list[str] = []
    for _raw in (
        {}, {0: 0}, {1: 1}, {0: 0, 1: 1}, {0: 0, 2: 2},
        {0: 0, 1: 1, 2: 2}, {1: 3, 3: 1}, {0: 0, 3: 1},
    ):
        _m = {i: c for i, c in _raw.items() if i in _ntab.get(c, ())}
        _actual = sum(1 for _ in _nopt._neighborhood(_ntab, Assignment(mapping=_m)))
        _est = neighborhood_size(_ntab, _m)
        if _actual != _est:
            _mism.append(f"{_m}: 生成器 {_actual} ≠ 计数 {_est}")
    check(
        "引擎-邻域计数必须与生成器逐个对齐（预算建议靠它，错了就给用户报假数）",
        not _mism,
        "; ".join(_mism[:3]),
    )

    # ── N24：预算建议只往上抬，且档位越多要得越多
    _s1 = suggest_budget(_ntab, 4, 1, 40)
    _s3 = suggest_budget(_ntab, 4, 3, 40)
    check(
        "引擎-预算建议只往上不往下，且随单购档位越多要得越多",
        _s1 >= 40 and _s3 > _s1 and _s1 % 50 == 0,
        f"1 档建议 {_s1}、3 档建议 {_s3}",
    )

    # ── N25：预算不够扫一遍邻域时，只深搜筛出来最好的那一个档位
    #
    # 6 件商品 7 张券、40 次预算下，一遍邻域就要 57 次调用。均成三份每份 12 次，
    # 三份全是半途而废 —— 实测命中率从 40% 掉到 0%，平均差 ¥3.93 恶化到 ¥16.53。
    # 所以这时必须把钱全压在最好的那一个档位上。
    #
    # 断言钉的是**行为**（深搜了几个档位），不是那句算术 ——
    # 只验证「40 < 57×3」这种常量关系，守的是运气，不是代码。
    _fcart = [CartItem(f"F{i}", f"F{i}", 1, 2000) for i in range(6)]
    _fcps = [
        Coupon(f"K{c}", f"k{c}", f"K{c}", frozenset(f"F{i}" for i in range(6)))
        for c in range(7)
    ]
    _foptions: list = [
        None,
        WithOrderOption(payload={"membershipCode": "1"}, label="卡1"),
        WithOrderOption(payload={"membershipCode": "2"}, label="卡2"),
    ]

    def _f_oracle(items, a):
        _sub = sum(i.unit_price_fen * i.quantity for i in items)
        return PriceResult(
            price_fen=_sub - 100 * len(a.mapping), original_fen=_sub
        )

    _deep: list[int] = []
    _orig_ls = CouponOptimizer._local_search

    def _spy_ls(self, cart, table, start, quota=0):  # noqa: ANN001
        _deep.append(1)
        return _orig_ls(self, cart, table, start, quota)

    CouponOptimizer._local_search = _spy_ls  # type: ignore[method-assign]
    try:
        CouponOptimizer(_f_oracle, max_calls=40, budget=Budget(40)).optimize(
            _fcart, _fcps, _foptions
        )
    finally:
        CouponOptimizer._local_search = _orig_ls  # type: ignore[method-assign]
    check(
        "引擎-预算不够扫一遍邻域时只深搜一个档位（逐个档位均分会全成半途而废）",
        len(_deep) == 1,
        f"深搜了 {len(_deep)} 个档位（预算 40 只够搜透一个）",
    )

    # ── N26：卡住之后必须真的去试平局步（不是只在注释里写了）
    _eqs: list[bool] = []
    _orig_scan = CouponOptimizer._scan

    def _spy_scan(self, *a, **kw):  # noqa: ANN001
        _eqs.append(bool(kw.get("accept_equal")))
        return _orig_scan(self, *a, **kw)

    CouponOptimizer._scan = _spy_scan  # type: ignore[method-assign]
    try:
        CouponOptimizer(_flat_oracle, max_calls=40)._local_search(
            [_ha, _hb], _htable, Assignment(mapping={0: 0})
        )
    finally:
        CouponOptimizer._scan = _orig_scan  # type: ignore[method-assign]
    check(
        "引擎-局部搜索卡住后必须真的去试平局步（只肯下降会原地认输）",
        any(_eqs),
        f"扫描时的 accept_equal 序列 {_eqs}",
    )

    # ── N27：算价失败必须把预扣的次数还回去
    # 预算保护的是**真实网络请求**，不是「evaluate 被调用了几次」。
    # 不还的后果是双重的：额度白白少一次，且这个方案因为没写进 _cache，
    # 后面扰动重来时还会为同一个注定失败的方案再付一遍钱。
    _flaky_budget = Budget(10)

    def _flaky_oracle(items, assignment):
        raise TimeoutError("模拟一次网络抖动")

    _fk = [CartItem("F", "F", 1, 1000)]
    _fkopt = CouponOptimizer(_flaky_oracle, max_calls=10, budget=_flaky_budget)
    for _ in range(5):
        try:
            _fkopt.evaluate(_fk, Assignment(mapping={0: 0}))
        except Exception:  # noqa: BLE001, S110
            pass  # 失败本身就是这次要验的场景
    check(
        "引擎-算价失败必须退还预扣（一次抖动不该吃掉额度）",
        _flaky_budget.used == 0,
        f"5 次全失败后预算却用掉了 {_flaky_budget.used} 次",
    )

    # ── N28：没搜完时给的预算建议必须是**按规模算出来的**，不是当前值的两倍
    # 「加 --budget 80」这种机械翻倍，对 2 件商品是浪费（本来就搜透了）、
    # 对 6 件商品又远远不够（要几百次才搜得动）。得给一个算出来的数。
    _b28 = _io15.StringIO()
    with _ctx15.redirect_stdout(_b28):
        _note28 = _print_search_note(False, 40, suggested=300)
    check(
        "收敛-没算完时给的是按规模算出来的预算数（不是机械翻倍）",
        any("300" in ln for ln in _note28) and not any("80" in ln for ln in _note28),
        repr(_b28.getvalue()[:120]),
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
        "复刻上一单",
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
        "演示-默认演示只跑人人用得到的链路（玩法向四段不许挤占第一印象）",
        # demo 是路人的第一印象：黄历/体检/日历/段位堆上去，主线就没了。
        # 但它们不能凭空消失 —— 所以同时要求给出 `--all` 这个出口。
        #
        # 不能只查段名字符串：默认演示的收尾提示里也会写「黄历 / 券包体检…」，
        # 那是**告诉用户去哪儿找**，不是把段落跑出来了。所以查的是段落标题
        # 全称（带命令名括号的那一行）——只有真跑段落时才会打出来。
        all(
            s not in _core_out
            for s in (
                "麦门黄历（almanac）",
                "券包体检（audit）",
                "麦门活动日历（campaign）",
                "麦门段位（brag）",
            )
        )
        and "--all" in _core_out,
        f"默认演示里混进了玩法向段落，或没给出 --all 出口："
        f"{[s for s in ('麦门黄历（almanac）', '券包体检（audit）', '麦门活动日历（campaign）', '麦门段位（brag）') if s in _core_out]}",
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

    # ── R7：拆开的两单不许把同一张券用两次
    # 上面那条只盯「< 放宽成 <=」，对更严重的虚报完全失明：
    # 旧实现把完整券池分别交给两单各跑一遍，两边都挑中同一张券，
    # 报出「拆开省 ¥5.00」——那 5 块需要同一张券核销两次，客人根本拿不到。
    # 下面构造的语义是「每单最多用 1 张券」，正好是真实场景里最常见的限制。
    _sp_disc = {0: 500, 1: 500}  # 券下标 -> 能减多少

    def _sp_one_per_order(items, a):
        total = sum(i.unit_price_fen * i.quantity for i in items)
        best = 0
        for ci in a.mapping.values():
            best = max(best, _sp_disc.get(ci, 0))
        return PriceResult(price_fen=total - best, original_fen=0)

    _sp_pool = [Coupon("C0", "X0", "满减券甲"), Coupon("C1", "X1", "满减券乙")]
    _sp_whole = 3500  # 整单：两件 ¥40，每单只能用 1 张券 → ¥35
    # 只有一张券时：拆开也不会更便宜（第二单没券可用）
    _sp_one = _ins6.split_suggestions(
        _sp_one_per_order,
        _sp_cart,
        [_sp_pool[0]],
        lambda c: [None],
        whole_fen=_sp_whole,
        per_call_budget=8,
    )
    check(
        "洞察-拆单不许把同一张券在两单里各用一次（报出来的价必须是真能买到的）",
        _sp_one == [],
        str([(x[0].product_code, x[1], x[2]) for x in _sp_one]),
    )
    # 两张券时：拆开确实能各用一张，这个收益是真的，必须报出来
    _sp_two = _ins6.split_suggestions(
        _sp_one_per_order,
        _sp_cart,
        _sp_pool,
        lambda c: [None],
        whole_fen=_sp_whole,
        per_call_budget=8,
    )
    check(
        "洞察-真能靠拆单多核销一张券时必须报出来（不许为了保守把真收益也砍掉）",
        _sp_two and all(g > 0 for _, _, g in _sp_two) and _sp_two[0][1] == 3000,
        str([(x[0].product_code, x[1], x[2]) for x in _sp_two]),
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

    # ── R11：摊销价在「标题里没有数字」时也不许漏进来
    # 上面那条「49元150天麦金学期卡 → 49」靠的是标题里有数字；
    # 标题一旦没数字，正文兜底就会把「每天低至 0.33 元」原样报成活动价。
    check(
        "日历-标题没数字时，正文里的摊销价也不许当活动价",
        _first_price("麦金学期卡限时开售", "每天低至 0.33 元，一卡在手") is None,
        str(_first_price("麦金学期卡限时开售", "每天低至 0.33 元，一卡在手")),
    )
    # ── R12：标题取价取「第一个」不是「最小的」
    # 「双人餐 99元（含两份各 49 元）」取 min 会得到 49 —— 那是拆开的单价，
    # 不是这一份的卖价。屏幕上会印「今天最低 ¥49.00」，而实际要付 ¥99。
    check(
        "日历-标题里有多个价时取第一个而不是最小的（卖价先出现）",
        _first_price("双人餐 99 元（含两份各 49 元）") == 99.0,
        str(_first_price("双人餐 99 元（含两份各 49 元）")),
    )
    # 反向钉住：别为了挡摊销价把真卖价也一起砍了。
    # 「每天 10 点起 9.9 元」的 9.9 是真价 —— 句首那个「每天」说的是时间不是钱。
    check(
        "日历-挡摊销价不能误伤真卖价（「每天 10 点起 9.9 元」仍是 9.9）",
        _first_price("早餐活动", "每天 10 点起 9.9 元抢麦旋风") == 9.9,
        str(_first_price("早餐活动", "每天 10 点起 9.9 元抢麦旋风")),
    )
    # 上面那条只覆盖了「正文兜底」。标题里的摊销价同样要抹 ——
    # 变异测试证明：只抹正文、不抹标题，自检一条都不会红。
    check(
        "日历-标题里的摊销价也要抹（「每天低至 0.33 元」不是价签）",
        _first_price("每天低至 0.33 元限时开售") is None,
        str(_first_price("每天低至 0.33 元限时开售")),
    )

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
    # 上面这条在「只认精确匹配」之后已经**拦不住**去重被删掉了：
    # 「巨无霸套餐」压根匹配不上，结果天然只有 1 条。
    # 必须造两个归一化后**同名的不同菜品**（菜单里真有这种：品名差个空格/商标号），
    # 去重失效时它们会共用同一条营养数据，热量被算两遍。
    _mm_dup = match_menu(
        [CartItem("M1", "巨无霸", 1, 2500), CartItem("M2", "巨 无 霸", 1, 2500)],
        _mm_nut,
    )
    check(
        "营养-归一化后同名的两件菜只能配一次（去重键不能只留原来的菜名）",
        len(_mm_dup) == 1 and _mm_dup[0][0].product_code == "M1",
        str([p[0].product_code for p in _mm_dup]),
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

    # ---------------------------------------------------------------- 最后一条
    # README 到处写着「N 项自检」，这个 N 每次补断言都会变。
    # 手写的话一定会漂：读者照着跑，看到 269/269 而文档写着 266，
    # 第一反应是「是不是我的版本不对」。所以让文档反过来被自检钉住。
    _declared = {int(x) for x in re.findall(r"(\d+)\s*项(?:自检|检查|离线自检|，覆盖)", _readme)}
    check(
        "文档-README 里写的自检项数必须等于真实条数（加断言就得改文档）",
        bool(_declared) and _declared == {len(_RESULTS) + 1},
        f"README 写的是 {sorted(_declared)}，实际是 {len(_RESULTS) + 1} 条",
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
