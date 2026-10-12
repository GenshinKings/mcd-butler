"""路程性价比模型：把「省下的钱」和「跑这一趟的成本」放进同一个单位里比。

为什么需要这一层
----------------
优惠券引擎回答的是「这单最省几块」，但它默认**去这家店是免费的**。
现实里不是：门店离你 144 米和 6.7 公里，是两种完全不同的决策。
省下 ¥5 但要走 20 分钟，对很多人来说并不划算。

这一节把"距离"翻译成"分钟"和"分钱"，让两家店能在同一个量纲上排序。

数据来源
--------
距离不是我们算的，是 `query-nearby-stores` 接口真实返回的 `distance` 字段（单位：米）。
得来速门店同样返回该字段（实测 6720 米）。所以这一层的输入是**真值**，不是估算。

可覆盖的假设
------------
速度、起步价、每公里价、进出店开销都是"常识默认值"，用户可用参数改。
凡是默认值，代码里都写在常量里并注明出处，不做黑箱。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .persona import butler_warning


# ------------------------------------------------------------------ 出行方式
@dataclass(frozen=True)
class TravelMode:
    key: str
    label: str
    speed_m_per_min: int  # 米/分钟
    base_fen: int  # 起步价（分）
    per_km_fen: int  # 每公里加价（分）


# 速度取常见通勤经验值；打车按国内常见 起步¥14 + ¥2.5/km 估算。
# 这些数字不是接口给的，是默认值 —— 用户可用 --mode/--yuan-per-km 覆盖。
MODES: dict[str, TravelMode] = {
    "walk": TravelMode("walk", "步行", 80, 0, 0),
    "bike": TravelMode("bike", "骑行", 200, 0, 0),
    "bus": TravelMode("bus", "公交/地铁", 250, 200, 0),
    "taxi": TravelMode("taxi", "打车", 400, 1400, 250),
}

DEFAULT_MODE = "walk"

# 往返固定开销（分钟）：进店、等餐、找座位、停车。
# 不随距离变化，但真实存在；不减掉它会系统性高估"近店"的优势。
OVERHEAD_MIN = 5


@dataclass(frozen=True)
class TravelCost:
    """一趟来回的成本。距离是接口真值，其余是按 mode 换算出来的。"""

    meters: int  # 单程距离（米），接口原值
    round_meters: int
    minutes: int  # 往返耗时（含固定开销）
    money_fen: int  # 往返路费（分）
    time_money_fen: int = 0  # 时间折算成的钱（分）；只在你给了时薪时才非零

    @property
    def total_fen(self) -> int:
        return self.money_fen + self.time_money_fen

    @property
    def km(self) -> float:
        return self.round_meters / 1000.0


def yuan_to_fen(yuan: float) -> int:
    """元 → 分。

    用 `to_fen` 而不是 `int(round(float(yuan) * 100))`：后者是 banker's rounding
    加二进制浮点两个坑叠在一起。--yuan-per-km 0.125 会被算成 12 分而不是 13 分。
    项目里 coupon_engine 和 audit 两处都已经明确禁用了这个写法。"""
    from .coupon_engine import to_fen

    return to_fen(yuan)


def travel_cost(
    meters: int,
    mode: str = DEFAULT_MODE,
    yuan_per_km: float | None = None,
    wage_per_hour: float | None = None,
) -> TravelCost:
    """把单程距离换算成往返的时间与钱。

    meters   接口返回的 distance（米），单程。
    mode     出行方式 key，见 MODES。
    yuan_per_km 覆盖该方式的每公里价（元）。
    wage_per_hour 时薪（元）。给了就把耗时折成钱，参与排序。

    为什么要给"时薪"这个口子：
    有人觉得走 15 分钟无所谓，有人觉得那 15 分钟值 20 块。
    这是主观的，工具不该替用户决定 —— 不给时薪就只按真金白银排。
    """
    tm = MODES.get(mode, MODES[DEFAULT_MODE])
    meters = max(0, int(meters))
    round_m = meters * 2

    minutes = int(round(round_m / tm.speed_m_per_min)) + OVERHEAD_MIN

    per_km = tm.per_km_fen
    if yuan_per_km is not None:
        per_km = yuan_to_fen(yuan_per_km)
    money = tm.base_fen + int(round(per_km * round_m / 1000.0))

    time_money = 0
    if wage_per_hour:
        time_money = int(round(yuan_to_fen(wage_per_hour) * minutes / 60.0))

    return TravelCost(
        meters=meters,
        round_meters=round_m,
        minutes=minutes,
        money_fen=money,
        time_money_fen=time_money,
    )


# ------------------------------------------------------------------ 门店报价
@dataclass
class StoreQuote:
    """一家店的综合报价：到手价 + 这一趟的路费。"""

    store_code: str
    store_name: str
    address: str = ""
    distance_m: int = 0
    paid_fen: int = 0  # 到手价（含券）
    baseline_fen: int = 0  # 裸买价
    travel: TravelCost = field(default_factory=lambda: travel_cost(0))
    missing: tuple[str, ...] = ()  # 这家店没有的商品；有缺失就不该拿来比价
    #: 接口没返回 distance。曾经这里直接填 0，于是「距离未知」被当成
    #: 「就在你脚下、路费 ¥0」，而综合成本里路费又是最大的一项 ——
    #: 一家不知道有多远的店会被排到「综合最划算」第一名。
    #: 屏幕上同一段还写着「距离是真实值，不是估算」，等于自相矛盾。
    distance_unknown: bool = False

    @property
    def saved_fen(self) -> int:
        return self.baseline_fen - self.paid_fen

    @property
    def total_fen(self) -> int:
        """综合成本 = 到手价 + 路费（+ 时间折钱，若给了时薪）。"""
        return self.paid_fen + self.travel.total_fen

    @property
    def comparable(self) -> bool:
        """能不能参与排序：商品缺货的店不能跟齐全的比，那是不公平比较。

        距离未知的店同样不能比：综合成本里含路费，路费算不出来，
        那个总数就是假的，拿它去排名等于拿一个编出来的数赢。
        """
        return not self.missing and not self.distance_unknown

    @property
    def reason_uncomparable(self) -> str:
        """为什么这家店不参与排序。缺货和距离未知要说不同的话。"""
        if self.missing:
            return "缺货"
        if self.distance_unknown:
            return "距离未知"
        return ""


def rank_stores(quotes: list[StoreQuote]) -> list[StoreQuote]:
    """按综合成本升序排。缺货的店、距离未知的店沉到最后。"""
    return sorted(
        quotes,
        key=lambda q: (not q.comparable, q.total_fen, q.distance_m),
    )


def verdict(quotes: list[StoreQuote]) -> str:
    """给一句人话结论：到底该不该跑这一趟、该去哪家店。"""
    good = [q for q in quotes if q.comparable]
    if not good:
        if all(q.distance_unknown for q in quotes):
            return (
                "这几家店接口都没返回距离，算不出路费，也就没法比「哪家更划算」。"
                "它们的到手价在表里，路费得你自己算。"
            )
        return "没有一家店能凑齐你要的东西，换个关键词试试。"

    best = good[0]
    lines: list[str] = []

    if len(good) == 1:
        lines.append(f"附近只有「{best.store_name}」有你要的东西。")
    else:
        second = good[1]
        gap = second.total_fen - best.total_fen
        # 时间折钱只在给了时薪时才非零，必须写进算式，否则加起来对不上
        time_part = (
            f" + 时间 ¥{best.travel.time_money_fen / 100:.2f}"
            if best.travel.time_money_fen
            else ""
        )
        lines.append(
            f"综合算下来去「{best.store_name}」最划算："
            f"到手 ¥{best.paid_fen / 100:.2f} + 路费 ¥{best.travel.money_fen / 100:.2f}"
            f"{time_part} = ¥{best.total_fen / 100:.2f}。"
        )
        if gap > 0:
            lines.append(
                f"比次优的「{second.store_name}」省 ¥{gap / 100:.2f}"
                f"（它远 {second.distance_m - best.distance_m} 米）。"
            )
        else:
            # 同城同券时各家到手价常常一模一样，这时"距离"就是唯一的决策变量。
            # 不点破这一点，用户会以为工具没算明白。
            dd = second.distance_m - best.distance_m
            dt = second.travel.minutes - best.travel.minutes
            lines.append(
                f"各家到手价其实一样（¥{best.paid_fen / 100:.2f}），"
                f"那就挑最近的 —— 比次选近 {dd} 米、来回少花 {dt} 分钟。"
            )

    # 这趟路费值不值 —— 交给管家说，干巴巴的"等于白跑"没人记得住。
    # 但比的必须是「比次优那家省多少」，不是「比券省的钱多不多」：
    # 后者是范畴错误，换个出行方式结论就反转（见 persona.butler_warning）。
    alt_gap = None
    if len(good) > 1:
        alt_gap = good[1].total_fen - best.total_fen
    lines.append(
        butler_warning(
            best.travel.money_fen,
            best.saved_fen,
            alt_gap_fen=alt_gap,
            minutes=best.travel.minutes,
        )
    )

    return "\n".join(lines)
