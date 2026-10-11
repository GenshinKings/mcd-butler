"""餐品详情 —— 回答一个菜单接口永远答不上来的问题：**这个套餐里到底是什么，换一样要加多少钱**。

为什么要有这一层
----------------
`query-meals` 只给「名字 + 价格」。但麦当劳一半以上的销量是**套餐**，
而套餐有两件事在菜单里完全看不到：

1. **它到底由什么组成。** 「龙焰鸡腿堡三件套」三件是哪三件，菜单不说。
2. **换一样要加多少钱。** 中薯条换大薯条 +¥1.5、换扭扭薯 +¥1.5、
   猪柳蛋堡换成猪柳堡**反而便宜 ¥4** —— 这些差价只有 `query-meal-detail`
   会告诉你，而且它藏在 `diffPrice` 这种 `"+ ¥1.5"` 的字符串里。

第二点尤其值钱：**「换项」是唯一一个用户自己能决定、而且方向可能是省钱
而不是加钱的操作**。换项差价是负数的那些，是白捡的钱。

关于入参（实测，别照着文档写）
------------------------------
工具名 `query-meal-detail` 的必填入参是 `storeCode + orderType + beType + code`。
那个 code 的字段名是 **`code` 而不是 `productCode`** ——
菜单接口叫 `productCode`，详情接口叫 `code`，照着菜单的写法传会静默失败。
到店自取（beType=1）时 `orderType=1` 且 **不要传 `beCode`**（传了会报错）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .coupon_engine import to_fen

# diffPrice 实测长这样：「+ ¥0」「+ ¥1」「+ ¥1.5」「- ¥4」
# 符号和金额之间可能有空格，¥ 也可能写成 ￥。
_DIFF_RE = re.compile(r"([+-])\s*[¥￥]\s*(\d+(?:\.\d+)?)")


def diff_price_fen(text: Any) -> int:
    """把 `"+ ¥1.5"` 解析成 150 分；解析不出来算 0（不加价）。

    注意符号：`- ¥4` 是**便宜了** 4 块，这正是最该被看见的那种换项。
    """
    if not text:
        return 0
    m = _DIFF_RE.search(str(text))
    if not m:
        return 0
    fen = to_fen(m.group(2))
    return -fen if m.group(1) == "-" else fen


@dataclass(frozen=True)
class Choice:
    """套餐某一环里的一个可选项。"""

    code: str
    name: str
    is_default: bool
    diff_fen: int          # 相对默认项的差价（正数=加价，负数=反而便宜）
    support_modify: bool   # 可特调（去生菜、加酱之类）

    def diff_text(self) -> str:
        if self.diff_fen == 0:
            return "不加价"
        sign = "+" if self.diff_fen > 0 else "-"
        return f"{sign}¥{abs(self.diff_fen) / 100:.2f}"


@dataclass(frozen=True)
class Round:
    """套餐的一环（选主食 / 选小食 / 选蘸酱……）。"""

    name: str
    choices: tuple[Choice, ...]

    def default_choice(self) -> Choice | None:
        for c in self.choices:
            if c.is_default:
                return c
        return self.choices[0] if self.choices else None


@dataclass(frozen=True)
class MealDetail:
    """一个餐品的详情。"""

    code: str
    name: str
    support_modify: bool
    rounds: tuple[Round, ...]

    @property
    def is_combo(self) -> bool:
        return len(self.rounds) > 0

    def cheaper_swaps(self) -> list[tuple[Choice, Choice]]:
        """所有「换过去反而更便宜」的选项。返回 (默认项, 更便宜的可选项)。

        这是这份数据里唯一能直接变成钱的东西 —— 别的都是信息，
        只有这个是「你点的时候选一下就能少付几块」。
        """
        out = []
        for r in self.rounds:
            base = r.default_choice()
            if base is None:
                continue
            for c in r.choices:
                # 按**身份**跳过默认项，不能按 code 相等：
                # 实测 choices 里 code 这个字段是可能缺失的，那时所有项的
                # code 都是空串、彼此相等，于是整环被全部跳过 ——
                # 「换成这些反而更便宜」这一段会整个消失，用户拿不到唯一
                # 那个能直接少付钱的信息。
                if c is base:
                    continue
                if c.diff_fen < 0:
                    out.append((base, c))
        return sorted(out, key=lambda p: p[1].diff_fen)

    def priciest_swap(self) -> tuple[Choice, Choice] | None:
        """最贵的一个换项，用来回答「这一环最多会让我多付多少」。"""
        best = None
        for r in self.rounds:
            base = r.default_choice()
            if base is None:
                continue
            for c in r.choices:
                if c.code == base.code:
                    continue
                if best is None or c.diff_fen > best[1].diff_fen:
                    best = (base, c)
        return best


def parse_meal_detail(data: Any) -> MealDetail | None:
    """解析 `query-meal-detail` 的返回。解析不出来返回 None（不抛）。

    返回体实测是个**裸对象**（不在数组里），字段只有
    `code / name / rounds[] / image / supportModify`；
    `rounds[]` 里每个又有 `choices[]`。非套餐（比如棒球帽）`rounds` 是空数组。
    """
    if not isinstance(data, dict):
        return None
    code = str(data.get("code") or "").strip()
    if not code:
        return None

    rounds: list[Round] = []
    for r in data.get("rounds") or []:
        if not isinstance(r, dict):
            continue
        choices: list[Choice] = []
        for c in r.get("choices") or []:
            if not isinstance(c, dict):
                continue
            choices.append(
                Choice(
                    code=str(c.get("code") or "").strip(),
                    name=str(c.get("name") or "").strip(),
                    # 实测见过 isDefault 缺失、以及同一环里两个 isDefault=1 的情况，
                    # 所以「默认」只看标志位，取第一个命中的。
                    is_default=bool(c.get("isDefault") == 1),
                    diff_fen=diff_price_fen(c.get("diffPrice")),
                    support_modify=bool(c.get("supportModify")),
                )
            )
        rounds.append(
            Round(name=str(r.get("name") or "").strip(), choices=tuple(choices))
        )

    return MealDetail(
        code=code,
        name=str(data.get("name") or "").strip() or code,
        support_modify=bool(data.get("supportModify")),
        rounds=tuple(rounds),
    )


def render(detail: MealDetail, store_name: str = "") -> str:
    """渲染成能直接读的样子。"""
    lines: list[str] = []
    lines.append("═" * 58)
    head = "餐品详情"
    if store_name:
        head += f" · {store_name}"
    lines.append(f"  {head}")
    lines.append("═" * 58)
    lines.append(f"  {detail.name}"
                 + ("　【可特调】" if detail.support_modify else ""))
    lines.append("")

    if not detail.is_combo:
        lines.append("  这是单品，没有可换的组成部分。")
        return "\n".join(lines)

    for i, r in enumerate(detail.rounds, start=1):
        lines.append(f"  {i}. {r.name}")
        base = r.default_choice()
        for c in r.choices:
            mark = "（默认）" if base is not None and c.code == base.code else ""
            tail = "" if c.diff_fen == 0 else f"　{c.diff_text()}"
            mod = "　【可特调】" if c.support_modify else ""
            lines.append(f"      · {c.name}{tail}{mark}{mod}")
        lines.append("")

    swaps = detail.cheaper_swaps()
    if swaps:
        lines.append("── 换成这些反而更便宜 " + "─" * 36)
        for base, c in swaps[:3]:
            lines.append(f"  ↓ {base.name} → {c.name}　{c.diff_text()}")
        lines.append("")

    top = detail.priciest_swap()
    if top is not None and top[1].diff_fen > 0:
        lines.append(f"  这一单换项最贵会加 {top[1].diff_text()}（{top[1].name}）。")
    lines.append("  差价来自接口自己的 diffPrice 字段，不猜。")
    return "\n".join(lines)
