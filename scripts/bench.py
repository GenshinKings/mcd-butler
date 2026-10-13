"""引擎体检台：拿穷举最优当标尺，量求解器到底差多少。

为什么单独一个文件：selftest 里那条「对照穷举最优」只有一个总数，
看不出**差在哪种场景**。这里按规模分层、按 oracle 形态分组，
让「引擎哪里不行」变成一个能读出来的数，而不是一个感觉。

用法：
    python scripts/bench.py                 # 默认两种 oracle、各 300 组
    python scripts/bench.py --cases 800 --budget 40
    python scripts/bench.py --mode pair     # 只跑配对相关的 oracle

零第三方依赖，纯离线，几秒跑完。
"""

from __future__ import annotations

import argparse
import itertools
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.coupon_engine import (  # noqa: E402
    Assignment,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
    WithOrderOption,
)


# ------------------------------------------------------------------ 场景生成


def make_case(rnd: random.Random, mode: str, n_items=None, n_cps=None, n_tiers=None):
    """造一个随机场景。

    mode="linear"  券的收益只跟「用没用这张券」有关，与挂哪件商品无关
    mode="pair"    券的收益跟「券 × 商品」这一对有关 —— 更接近真实：
                   同一张「9.9 元中杯冰美式」挂在不同杯型上，省的就不一样；
                   打折券挂贵的东西上才值钱。
    mode="threshold"
                   在 pair 之上再加**满减门槛**：折后价够到门槛才减，且只享
                   最高一档。这是麦当劳最常见的形态，也是最难搜的一种 ——
                   多挂一张券可能让折后价跌破门槛、反而更贵，目标函数因此
                   变成多峰的，局部最优遍地都是。爬山最容易在这种地形上翻车。

    两种都带真实世界里的两种坑：
      - 负收益券（挂上反而加价），对应「券被随单购作废」
      - 随单购与某张券互斥，对应「随单购后券不再叠加」
    """
    n_items = n_items or rnd.randint(2, 6)
    n_cps = n_cps or rnd.randint(2, 7)
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

    if mode == "linear":
        power = {
            c.coupon_id: rnd.choice([-300, -100, 0, 200, 400, 800, 1200]) for c in coupons
        }
        pair_gain: dict = {}
    else:
        power = {}
        pair_gain = {}
        for c in coupons:
            for p in c.product_codes:
                pair_gain[(c.coupon_id, p)] = rnd.choice(
                    [-400, -200, -100, 0, 150, 300, 500, 900, 1500]
                )

    # 满减门槛（只 threshold 模式用）：按**折后价**判，所以多挂一张券可能
    # 跌破门槛、反而更贵 —— 目标函数由此变成多峰的。这正是真实满减的脾气。
    thresholds: list[tuple[int, int]] = []
    if mode == "threshold":
        base = sum(i.unit_price_fen for i in cart)
        t1 = int(base * rnd.uniform(0.55, 0.85))
        d1 = rnd.choice([300, 500, 800])
        t2 = int(base * rnd.uniform(0.9, 1.2))
        d2 = d1 + rnd.choice([300, 500, 900])
        thresholds = sorted([(t1, d1), (t2, d2)], key=lambda x: -x[1])

    card_discount: dict[str, int] = {"": 0}
    # n_tiers 指定「随单购档位」个数：档位越多，「先爬哪个档位」越值钱，
    # 也就越能试出「给每个档位留保底预算」到底有没有用。
    for w in range(n_tiers if n_tiers is not None else rnd.randint(0, 2)):
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
        discount = 0
        for item_idx, ci in assignment.mapping.items():
            cid = coupons[ci].coupon_id
            code = items[item_idx].product_code
            discount += pair_gain.get((cid, code), 0) if pair_gain else power.get(cid, 0)
        label = assignment.with_order.label if assignment.with_order else ""
        hit_ids = [coupons[ci].coupon_id for _, ci in assignment.mapping.items()]
        if label and conflict.get(label) in hit_ids:
            cid = conflict[label]
            idx = [i for i, c in assignment.mapping.items() if coupons[c].coupon_id == cid]
            if idx:
                code = items[idx[0]].product_code
                discount -= pair_gain.get((cid, code), 0) if pair_gain else power.get(cid, 0)
        discount += card_discount.get(label, 0)
        if thresholds:
            # 只享最高一档，且按折后价判 —— 于是「多挂一张券」可能把折后价
            # 压到门槛之下，净损失那一整档满减。多峰就是这么来的。
            after = max(subtotal - discount, 0)
            for t, d in thresholds:  # 已按优惠额降序排好
                if after >= t:
                    discount += d
                    break
        return PriceResult(
            price_fen=max(subtotal - discount, 0),
            original_fen=subtotal,
            discount_fen=discount,
        )

    return cart, coupons, oracle, options


def bruteforce_best(cart, coupons, oracle, options) -> int:
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


# ------------------------------------------------------------------ 体检


def _neighborhood_without_seize(self, table, current):
    """消融用：只含前五个邻域（拿掉「抢占」），用来量那一个动作值多少。"""
    mapping = current.mapping
    wo = current.with_order
    for item_idx in list(mapping):
        yield Assignment(
            mapping={k: v for k, v in mapping.items() if k != item_idx}, with_order=wo
        )
    used = set(mapping.values())
    for ci, hits in table.items():
        if ci in used:
            continue
        for item_idx in hits:
            if item_idx in mapping:
                continue
            yield Assignment(mapping={**mapping, item_idx: ci}, with_order=wo)
    for item_idx, ci in list(mapping.items()):
        for cj, hits in table.items():
            if cj == ci or cj in used:
                continue
            if item_idx not in hits:
                continue
            swapped = dict(mapping)
            swapped[item_idx] = cj
            yield Assignment(mapping=swapped, with_order=wo)
    for item_idx, ci in list(mapping.items()):
        for target in table.get(ci, []):
            if target == item_idx or target in mapping:
                continue
            moved = dict(mapping)
            del moved[item_idx]
            moved[target] = ci
            yield Assignment(mapping=moved, with_order=wo)
    for (i1, c1), (i2, c2) in itertools.combinations(list(mapping.items()), 2):
        if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):
            continue
        swapped = dict(mapping)
        swapped[i1], swapped[i2] = c2, c1
        yield Assignment(mapping=swapped, with_order=wo)


def run(
    mode: str, cases: int, budget: int, seed0: int = 0, cfg=None, n_tiers: int | None = None
):
    """跑一批用例。

    cfg 是消融配置（dict），用来逐个关掉某个改动，量它到底值多少 ——
    「觉得这个改进应该有用」不算数，得看关掉它之后数字掉不掉。
    """
    cfg = cfg or {}
    hit = 0
    gaps: list[int] = []
    misses: list[tuple[int, int, int]] = []  # (n_items, n_cps, gap)
    by_size: dict[tuple[int, int], list[int]] = {}
    small_hit = small_n = 0  # ≤4 件的「正常购物车」单独统计

    for s in range(cases):
        rnd = random.Random(seed0 + s * 7919)
        cart, coupons, oracle, options = make_case(rnd, mode, n_tiers=n_tiers)
        optimum = bruteforce_best(cart, coupons, oracle, options)
        try:
            kwargs = {k: v for k, v in cfg.items() if not k.startswith("_")}
            opt = CouponOptimizer(oracle, max_calls=budget, **kwargs)
            if cfg.get("_no_seize"):
                opt._neighborhood = lambda t, c, _s=opt: _neighborhood_without_seize(
                    _s, t, c
                )
            got = opt.optimize(cart, coupons, options)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{mode}] case {s} 求解器抛异常: {exc!r}")
            continue
        if got.price is None:
            continue
        gap = got.price.price_fen - optimum
        gaps.append(gap)
        key = (len(cart), len(coupons))
        by_size.setdefault(key, []).append(gap)
        if len(cart) <= 4:
            small_n += 1
            if gap == 0:
                small_hit += 1
        if gap == 0:
            hit += 1
        else:
            misses.append((len(cart), len(coupons), gap))

    done = max(len(gaps), 1)
    return {
        "mode": mode,
        "cases": len(gaps),
        "hit": hit,
        "rate": hit / done,
        "avg_gap": sum(gaps) / done,
        "max_gap": max(gaps) if gaps else 0,
        "misses": misses,
        "by_size": by_size,
        "small_rate": small_hit / max(small_n, 1),
        "small_n": small_n,
    }


# 消融矩阵：逐个关掉一个改动，看数字掉不掉。
# 「这个改进应该有用」不算数，关掉它数字不动，就说明它只是自我安慰。
ABLATIONS: list[tuple[str, dict]] = [
    ("新算法全开", {}),
    ("去掉「抢占」邻域", {"_no_seize": True}),
    ("去掉横移（卡住就认输）", {"sideways_reserve": 1 << 30}),
    ("去掉扰动重启", {"max_restarts": 0}),
    ("去掉扫描上限", {"scan_cap_divisor": 0}),
    ("去掉档位保底", {"min_tier_quota": 1}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--budget", type=int, default=40)
    ap.add_argument(
        "--mode",
        choices=["linear", "pair", "threshold", "both"],
        default="both",
        help="oracle 形态：linear 券收益与商品无关 / pair 券×商品相关 / "
        "threshold 再加满减门槛（多峰，最贴近真实也最难搜）",
    )
    ap.add_argument(
        "--ablate",
        action="store_true",
        help="跑消融矩阵：逐个关掉一个改动，量它到底值多少",
    )
    ap.add_argument(
        "--tiers",
        type=int,
        default=None,
        help="固定随单购档位个数（档位越多，越能试出档位保底有没有用）",
    )
    ap.add_argument(
        "--autobudget",
        action="store_true",
        help="用引擎自己推荐的预算跑，检验 suggest_budget 推的数够不够用",
    )
    args = ap.parse_args()

    if args.autobudget:
        # 让引擎自己报「这一单要多少预算」，再拿那个预算真跑一遍 ——
        # 推荐的数要是撑不起像样的命中率，那句提示就是在误导用户。
        from src.coupon_engine import suggest_budget

        print(f"自动预算检验（oracle=pair，样本 {args.cases} 组，档位 {args.tiers or '随机'}）")
        print(f"{'规模':<14}{'推荐预算':>10}{'命中':>8}{'平均差':>10}")
        buckets: dict[tuple[int, int], list[int]] = {}
        recs: dict[tuple[int, int], int] = {}
        for s in range(args.cases):
            rnd = random.Random(s * 7919)
            cart, coupons, oracle, options = make_case(rnd, "pair", n_tiers=args.tiers)
            optimum = bruteforce_best(cart, coupons, oracle, options)
            probe_opt = CouponOptimizer(oracle, max_calls=1 << 30)
            table = probe_opt._eligibility(cart, coupons)
            rec = suggest_budget(table, len(cart), len(options), 40)
            recs[(len(cart), len(coupons))] = rec
            try:
                got = CouponOptimizer(oracle, max_calls=rec).optimize(
                    cart, coupons, options
                )
            except Exception:  # noqa: BLE001
                continue
            if got.price is None:
                continue
            buckets.setdefault((len(cart), len(coupons)), []).append(
                got.price.price_fen - optimum
            )
        for key in sorted(buckets):
            gs = buckets[key]
            h = sum(1 for g in gs if g == 0)
            print(
                f"  {key[0]} 件 {key[1]} 券{'':<6}{recs[key]:>8}"
                f"{h / len(gs):>9.1%}{sum(gs) / len(gs) / 100:>9.2f}元"
            )
        return 0

    if args.ablate:
        amode = "pair" if args.mode == "both" else args.mode
        print(
            f"消融矩阵（oracle={amode}，预算 {args.budget} 次，样本 {args.cases} 组，"
            f"档位 {args.tiers or '随机'}）"
        )
        print(f"{'配置':<26}{'命中':>8}{'≤4件命中':>10}{'平均差':>10}{'最差':>10}")
        for name, cfg in ABLATIONS:
            r = run(amode, args.cases, args.budget, cfg=cfg, n_tiers=args.tiers)
            print(
                f"{name:<26}{r['rate']:>7.1%}{r['small_rate']:>10.1%}"
                f"{r['avg_gap'] / 100:>9.2f}元{r['max_gap'] / 100:>9.2f}元"
            )
        return 0

    modes = ["linear", "pair"] if args.mode == "both" else [args.mode]
    for mode in modes:
        r = run(mode, args.cases, args.budget)
        print(f"\n=== oracle 形态：{mode}  预算 {args.budget} 次  样本 {r['cases']} 组 ===")
        print(f"命中穷举最优：{r['hit']}/{r['cases']} = {r['rate']:.1%}")
        print(f"平均差 ¥{r['avg_gap'] / 100:.2f}   最差 ¥{r['max_gap'] / 100:.2f}")
        if r["misses"]:
            big = sorted(r["misses"], key=lambda x: -x[2])[:8]
            print("  差得最多的几组（商品数, 券数, 差额）：")
            for it, cp, g in big:
                print(f"    {it} 件 / {cp} 券 → 多付 ¥{g / 100:.2f}")
        # 按规模分层：看是不是「越大越不行」
        rows = []
        for (it, cp), gs in sorted(r["by_size"].items()):
            h = sum(1 for g in gs if g == 0)
            rows.append((it, cp, len(gs), h / len(gs), sum(gs) / len(gs)))
        print("  按规模分层（商品 券 样本 命中率 平均差）：")
        for it, cp, n, rate, avg in rows:
            print(f"    {it:>2} 件 {cp:>2} 券  n={n:<4} {rate:>6.1%}  ¥{avg / 100:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
