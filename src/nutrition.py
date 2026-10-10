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


def match_menu(menu: list, nutrition: dict[str, dict]) -> list[tuple]:
    """把菜单和营养表按品名对上。

    没有 productCode 可用，只能按名字匹配。先精确，再退到「一方包含另一方」。
    匹配不上就丢掉 —— 宁可少选几个，也不能拿错误的营养数据去给建议。
    """
    exact: list[tuple] = []
    by_name = {n: v for n, v in nutrition.items()}
    used: set[str] = set()

    for item in menu:
        nm = (item.name or "").strip()
        if nm in by_name:
            exact.append((item, by_name[nm]))
            used.add(nm)

    for item in menu:
        nm = (item.name or "").strip()
        if nm in used or not nm:
            continue
        hit = None
        for nname, val in by_name.items():
            if nname in used:
                continue
            if nm in nname or nname in nm:
                hit = (nname, val)
                break
        if hit:
            exact.append((item, hit[1]))
            used.add(hit[0])
    return exact


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
    """
    cands = [p for p in pairs if p[1]["kcal"] > 0]
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
