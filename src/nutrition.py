"""热量预算搭配器。

这一节的难点全在**解析**上：`list-nutrition-foods` 的 `success` 是 true、
`data` 却是**一个字符串**，既不是对象也不是数组：

    [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
      猪柳麦满分,null,1288,308,16,16,24,781,213
      猪柳蛋麦满分,null,1618,387,23,21,25,846,243
      ...

自定义的类 CSV：首行用 `[条数]{字段清单}:` 声明，后面每行一条，逗号分隔。
`data["xxx"]` 和 `for x in data` 都会以意想不到的方式失败。

还有一个更麻烦的限制：**营养表里没有 `productCode`**，
所以只能按品名去和菜单匹配。匹配不上就丢弃，宁可少选也不猜。
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any

_HDR_RE = re.compile(r"^\s*\[(\d+)\]\s*\{([^}]*)\}\s*:?\s*$")


def parse_nutrition(raw: Any) -> dict[str, dict]:
    """把类 CSV 字符串解析成 {品名: {kcal, protein, ...}}。"""
    if not isinstance(raw, str):
        return {}
    lines = [l for l in raw.split("\n") if l.strip()]
    if not lines:
        return {}

    header: list[str] = []
    body_start = 0
    m = _HDR_RE.match(lines[0])
    if m:
        header = [f.strip() for f in m.group(2).split(",")]
        body_start = 1
    else:
        # 声明行格式变了就退化成默认字段，别直接崩
        header = [
            "productName",
            "nutritionDescription",
            "energyKj",
            "energyKcal",
            "protein",
            "fat",
            "carbohydrate",
            "sodium",
            "calcium",
        ]

    out: dict[str, dict] = {}
    for line in lines[body_start:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(header):
            continue
        row = dict(zip(header, parts))
        name = row.get("productName", "").strip()
        if not name:
            continue
        out[name] = {
            "kcal": _int(row.get("energyKcal")),
            "kj": _int(row.get("energyKj")),
            "protein": _float(row.get("protein")),
            "fat": _float(row.get("fat")),
            "carb": _float(row.get("carbohydrate")),
            "sodium": _int(row.get("sodium")),
            "calcium": _int(row.get("calcium")),
        }
    return out


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _canon(s: str) -> str:
    """品名归一化：去商标符号与所有空白。

    只做这两件事 —— 不做「套餐/件套」后缀剥离。
    见 match_menu 里那条血的教训：一旦允许跨单品/套餐匹配，
    单品会拿到套餐的热量、套餐会拿到单品的热量，两个方向都错。
    """
    for ch in "™®©Ⓡ":
        s = s.replace(ch, "")
    return re.sub(r"\s+", "", s or "")


def match_menu(menu: list, nutrition: dict[str, dict]) -> list[tuple]:
    """把菜单和营养表按品名对上。

    只认**归一化后完全相等**的名字，不再做「一方包含另一方」的回退。

    为什么砍掉子串回退（实测踩过的坑）：
    营养表里有「板烧鸡腿堡套餐」，菜单里是「板烧鸡腿堡」——
    子串匹配让单品拿到了套餐（含薯条可乐）的热量 640kcal；
    反过来「麦香鸡套餐」又匹配到「麦香鸡」，套餐拿到单品的 380kcal。
    两个方向都是错的，而且命中哪个取决于 dict 的插入顺序。

    热量建议是拿给减脂人群看的，错 260kcal 等于把人往反方向带。
    匹配不上就丢掉 —— 宁可少几件，也不能拿错误的营养数据给建议。
    """
    exact: list[tuple] = []
    by_canon: dict[str, tuple[str, dict]] = {}
    for n, v in nutrition.items():
        by_canon.setdefault(_canon(n), (n, v))
    used: set[str] = set()

    for item in menu:
        nm = (item.name or "").strip()
        if not nm:
            continue
        hit = by_canon.get(_canon(nm))
        if hit is None:
            continue
        canon_key = _canon(nm)
        if canon_key in used:
            continue
        exact.append((item, hit[1]))
        used.add(canon_key)
    return exact


# 组合枚举的规模上限。超过就把候选池收窄，宁可少看几个也不能让用户干等。
# 参考量级：C(150,4) ≈ 2000 万，实测要跑 30 秒；收到 3 万以内是零点几秒。
MAX_COMBOS = 30_000

# size 的硬上限。没有它，`--size 50` 会让组合枚举跑到 9e15 量级；
# _trim_pool 只是裁剪候选池的兜底，真正的闸门必须在入口就夹住。
MAX_MEAL_SIZE = 5


def clamp_size(size: int) -> int:
    """把 --size 夹回可枚举的范围。

    单独成函数而不是写在 CLI 里：`_trim_pool` 是按「传进来的 size」算枚举量的，
    只要 size 够大，它自己也会把池子裁到近乎不裁（size=50 时直接留 50 个候选）。
    所以闸门必须在入口，而且要能被离线断言到。
    """
    if size < 1:
        return 1
    return min(size, MAX_MEAL_SIZE)


def _trim_pool(cands: list[tuple], size: int) -> list[tuple]:
    """把候选池收窄到「组合数不超过上限」的规模。

    收窄的依据是**每千卡蛋白质**（蛋白密度）：
    在热量有上限的前提下，这个指标最能反映「这一口值不值得吃」。
    只按蛋白质绝对值排序的话，会把高蛋白高热量的品全排在前面，
    结果凑出来的搭配根本塞不进热量预算。

    另外无条件保留蛋白质最高的 `size` 个 —— 用户很可能就是冲着它们来的，
    不能因为密度略低就被过滤掉。
    """
    if size <= 0:
        return cands
    n = len(cands)
    budget = MAX_COMBOS
    pool = n
    # 逐步缩小候选数，直到组合数落回上限内。
    #
    # 注意：这里算的是 **全部枚举量** sum(C(pool, i), i=1..size)，不是 C(pool, size)。
    # 早先只算 C(pool, size)，于是 --size 50 时被裁到 53 个候选就认为"安全"了 ——
    # 但 plan() 枚举的是 i=1..size 的每一档，真实规模由最大的那一项决定：
    # C(53, 26) ≈ 9.0e15。进程无输出、无进度、无警告地跑到天荒地老，
    # 用户只能 Ctrl-C。二项式系数在中间最大，只看端点等于没看。
    while pool > size:
        total = 0
        combos = 1
        for i in range(1, min(size, pool) + 1):
            combos = combos * (pool - i + 1) // i
            total += combos
            if total > budget:
                break
        if total <= budget:
            break
        pool -= 1
    pool = max(pool, size)
    if pool >= n:
        return cands

    def density(p: tuple) -> float:
        kcal = p[1]["kcal"]
        return (p[1]["protein"] / kcal) if kcal > 0 else 0.0

    must = sorted(cands, key=lambda p: -p[1]["protein"])[:size]
    must_keys = {id(p[0]) for p in must}
    rest = [p for p in cands if id(p[0]) not in must_keys]
    rest.sort(key=lambda p: (-density(p), p[1]["kcal"]))
    kept = must + rest[: max(pool - len(must), 0)]
    # 保持原顺序，输出才不会看起来是乱的
    order = {id(p[0]): i for i, p in enumerate(cands)}
    kept.sort(key=lambda p: order[id(p[0])])
    return kept


def plan(
    pairs: list[tuple],
    kcal_budget: int,
    size: int = 3,
    top: int = 5,
) -> list[list[tuple]]:
    """在热量预算内挑搭配，按蛋白质降序。

    为什么以蛋白质为目标：减脂场景的诉求是「热量有上限，但不能饿」，
    蛋白质是最能反映「这顿扛不扛饿」的指标。纯按热量排序会选出一份沙拉+零卡可乐，
    那不叫搭配。

    候选规模可控时为精确最优；候选过多（组合数超过 MAX_COMBOS）时按
    「每千卡蛋白密度」启发式裁剪候选池，结果为近似最优 —— 文案上称「推荐搭配」而非「最优」。
    """
    cands = [p for p in pairs if p[1]["kcal"] > 0]
    cands = _trim_pool(cands, size)
    results: list[list[tuple]] = []

    for n in range(1, min(size, len(cands)) + 1):
        for combo in combinations(cands, n):
            kcal = sum(p[1]["kcal"] for p in combo)
            if kcal > kcal_budget:
                continue
            results.append(list(combo))

    results.sort(
        key=lambda c: (
            -sum(p[1]["protein"] for p in c),
            sum(p[1]["kcal"] for p in c),
        )
    )
    return results[:top]


def summarize(combo: list[tuple]) -> dict:
    return {
        "kcal": sum(p[1]["kcal"] for p in combo),
        "protein": round(sum(p[1]["protein"] for p in combo), 1),
        "fat": round(sum(p[1]["fat"] for p in combo), 1),
        "carb": round(sum(p[1]["carb"] for p in combo), 1),
        "sodium": sum(p[1]["sodium"] for p in combo),
        "price_fen": sum(p[0].effective_price_fen() * p[0].quantity for p in combo),
    }
