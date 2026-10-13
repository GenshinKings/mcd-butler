"""券价最优组合引擎 —— 麦门管家的核心。

问题建模
--------
麦当劳的 `calculate-price` 允许 items[] 中**每个商品各自携带一张券**，
所以「怎么用券最划算」不是「挑一张最猛的券」，而是一个
**券 → 购物车商品 的二分匹配问题**：

    约束 1：一张券最多用一次
    约束 2：一个商品最多挂一张券
    约束 3：券只能挂到它 products[] 里声明过的商品上

难点在于：接口**不返回券的折扣金额**（只有标题和适用商品），
因此无法离线估算收益，只能把 `calculate-price` 当作黑盒求值器。

求解策略
--------
因为目标函数是黑盒、且每次求值要走网络（限流 600 次/分钟），
这里用「贪心构造 + 禁忌局部搜索 + 扰动重启」而不是穷举：

    1. 贪心：按「可挂商品数」升序安排券（最受约束的先占坑）
    2. 局部搜索：六种邻域动作（删 / 加 / 换 / 挪 / 对调 / 抢占），
       每一步都用真实算价接口验证。先一路下降；
       卡在局部最优时允许横移一步（价格不变）换片盆地，
       禁忌表负责不让它绕回原地
    3. 扰动重启：从当前最优解随机踢掉几张券，再让邻域把坑填回去
       （ruin & recreate）
    4. 预算耗尽、或再也走不动时收工

为什么不是纯爬山：纯爬山能证明的只是「一步走不到更好的」，
而用户要的是「没有更好的」。实测把预算放大 12 倍（40 → 500 次调用），
命中穷举最优只从 76.7% 走到 84.0% —— 多给的钱只买回 7 个点，
说明剩下的失败全是「走不到」，该改的是走法，不是加钱。

这样既能在有限调用次数内逼近最优，又不会依赖未在文档中承诺的字段。

合规：`calculate-price` 只算价，不产生任何订单。
"""

from __future__ import annotations

import itertools
import json
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Iterator, Sequence


def to_fen(value: Any) -> int:
    """任意金额写法 → 分（整数）。

    为什么不能用 `int(float(x) * 100)`：
    二进制浮点表示不了十进制小数，`33.30 * 100` 实际是 `3329.9999...`，
    直接截断就变成 3329 分 —— 每件商品少一分，一整单就差出好几毛，
    而且这种错误极难发现，因为它只在特定数字上出现。

    `Decimal` 按十进制算，再四舍五入，才是金额该有的处理方式。
    """
    if value is None or value == "":
        return 0
    try:
        return int((Decimal(str(value).strip()) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        ))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return 0


def as_fen(value: Any) -> int:
    """接口**已经是「分」**的字段 → 整数分。

    和 `to_fen` 的区别必须说清楚，混用会差 100 倍：
        `to_fen`  元 → 分（乘 100），用于菜单价（实测 `currentPrice` 是元字符串）
        `as_fen`  分 → 分（不乘），用于算价结果（实测 `price` 是分整数）

    两者都要容忍脏数据：同一个字段在不同工具里可能返回
    `5700` / `"5700"` / `None` / `""`，任何一种都不该让整条链路崩掉。
    """
    if value is None or value == "":
        return 0
    try:
        return int(
            Decimal(str(value).strip()).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return 0


@dataclass(frozen=True)
class CartItem:
    """购物车条目。

    注意 `unit_price_fen`（菜单标价）**可能是条件价**：
    实测发现四件套一类商品的 `currentPrice` 标 29 元，
    而 `discountType` 是「随单购麦金卡优惠」，`originalPrice` 是 57 元 ——
    不随单购麦金卡就只能按 57 元结算。
    因此必须同时保留 `list_price_fen`（无门槛价），
    否则展示出来的价格会骗人。
    """

    product_code: str
    name: str = ""
    quantity: int = 1
    unit_price_fen: int = 0  # 菜单标价（可能是条件价）
    list_price_fen: int = 0  # 无门槛价（originalPrice）
    discount_type: str = ""
    needs_with_order: bool = False

    # 随单购所需参数（来自 query-meals 的 withOrder 字段）
    membership_code: str = ""
    membership_spec_id: str = ""

    def base_total_fen(self) -> int:
        """不随单购时应付的金额。"""
        return self.effective_price_fen() * self.quantity

    def effective_price_fen(self) -> int:
        """当前条件下真正拿得到的单价。"""
        if self.needs_with_order:
            return self.list_price_fen or self.unit_price_fen
        return self.unit_price_fen

    def condition_note(self) -> str:
        if not self.needs_with_order:
            return ""
        return (
            f"标价 ¥{self.unit_price_fen / 100:.2f} 需{discount_label(self.discount_type)}"
            f"，否则按 ¥{self.list_price_fen / 100:.2f}"
        )


def discount_label(discount_type: str) -> str:
    return discount_type or "无条件优惠"


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def valid_until_end(text: Any) -> str:
    """从有效期区间里取**结束日**。

    `query-store-coupons` 的 `tradeDateTime` 给的是一整串区间：

        "2026-10-11 00:00:00-2026-10-17 23:59:59"

    早先版本把这个字符串整个塞进 `Coupon.valid_until`，下游再截前 10 位，
    拿到的是**开始日** —— 于是「还有 6 天才过期」的券被报成「今天到期」，
    用户被吓得当天就跑去消费，还可能白跑一趟。

    取不到任何日期就返回空串，让下游按「不知道到期日」处理；
    绝不能拿开始日冒充到期日。
    """
    if not text:
        return ""
    hits = _DATE_RE.findall(str(text))
    return hits[-1] if hits else ""


@dataclass(frozen=True)
class Coupon:
    """一张可用券。"""

    coupon_id: str
    coupon_code: str
    title: str = ""
    product_codes: frozenset[str] = frozenset()
    product_names: frozenset[str] = frozenset()
    valid_until: str = ""

    def applies_to(self, product_code: str) -> bool:
        # products 为空表示文档未给出限制，此时视作通用券
        return not self.product_codes or product_code in self.product_codes


@dataclass(frozen=True)
class PriceResult:
    """一次算价结果，金额单位统一为「分」。"""

    price_fen: int
    original_fen: int = 0
    discount_fen: int = 0
    delivery_fen: int = 0
    packing_fen: int = 0

    @property
    def saved_fen(self) -> int:
        """按接口 `originalPrice` 口径算的节省额。

        注意：随单购会把卡费计入 originalPrice，所以这个值可能是虚高的。
        想知道「真实省了多少」，请用 `saved_vs(裸买价)`。
        """
        return self.original_fen - self.price_fen

    def saved_vs(self, baseline_fen: int) -> int:
        """相对裸买价的真实节省。这才是用户关心的数字。"""
        return baseline_fen - self.price_fen

    def yuan(self, fen: int) -> str:
        return f"{fen / 100:.2f}"


def neighborhood_size(table: dict[int, list[int]], mapping: dict[int, int]) -> int:
    """邻域候选总数 —— 与 `_neighborhood` 实际 yield 的个数**逐个对齐**。

    这个数之所以要算得准，是因为它同时管两件事：

    一、**还值不值得探索**。横移和扰动都要先扫一遍邻域，而邻域规模随
    商品数、券数平方级上涨。6 件商品 7 张券时一遍要 70 多次求值，
    40 次预算连一遍都扫不完 —— 这时还去横移，等于把本该用来下降的钱
    扔进一场注定半途而废的探索。实测就是这么翻的车：命中率上去了，
    平均差却从 ¥1.20 恶化到 ¥1.80。所以规矩是：剩下的钱不够再扫一遍就别开探索。

    二、**该给用户推荐多大预算**（`suggest_budget`）。这一条直接决定
    用户看到的那句「加 --budget N 再跑一次」里的 N 是不是可信。
    早先这个数把「加券」邻域按「券数 × 商品数」粗算，而商品占满时
    它实际一个候选都不产生 —— 高估近一倍，推荐值也就跟着离谱。

    所以这里逐个邻域精确计数，与生成器一一对应。改动邻域时必须同步改这里，
    `selftest` 里有一条断言专门钉住两者不许走散。
    """
    occupied = set(mapping)
    used = set(mapping.values())
    m = len(mapping)
    return (
        m  # 邻域 1：删 —— 每个已挂位置各一个
        + sum(  # 邻域 2：加 —— 未用的券 × 还空着的商品
            1
            for ci, hits in table.items()
            if ci not in used
            for item_idx in hits
            if item_idx not in occupied
        )
        + sum(  # 邻域 3：换 —— 已挂商品 × 适用于它且还没用的券
            1
            for item_idx, ci in mapping.items()
            for cj, hits in table.items()
            if cj != ci and cj not in used and item_idx in hits
        )
        + sum(  # 邻域 4：挪 —— 已挂的券 × 它还能挂的空闲商品
            1
            for item_idx, ci in mapping.items()
            for target in table.get(ci, ())
            if target != item_idx and target not in occupied
        )
        + sum(  # 邻域 5：对调 —— 还要两边都适用才成立
            1
            for (i1, c1), (i2, c2) in itertools.combinations(list(mapping.items()), 2)
            if i1 in table.get(c2, ()) and i2 in table.get(c1, ())
        )
        + sum(  # 邻域 6：抢占 —— cj 得适用于 i
            1
            for (i, _ci), (j, cj) in itertools.permutations(list(mapping.items()), 2)
            if i in table.get(cj, ())
        )
    )


#: 一次局部搜索典型要迭代几轮。这个数是**扫出来的**，不是拍的：
#: 拿 suggest_budget 自己推荐的预算去真跑，看命中率随它怎么变 ——
#:   T=4  →  6 件 7 券推荐 150，只命中 57%
#:   T=8  →  推荐 300，命中 86%      ← 拐点
#:   T=12 →  推荐 450，仍是 86%      ← 再加钱没用，白等
#:   T=20 →  推荐 750，仍是 86%
#: 所以取 8：再往上只会让用户多等，换不来更准。它只用于估量级，
#: 不参与任何正确性判断。
TYPICAL_ITERATIONS = 8


def suggest_budget(
    table: dict[int, list[int]], n_items: int, n_tiers: int, current: int
) -> int:
    """按这一单的规模，估一个「够搜得像样」的 --budget 值。

    为什么要估这个数：用户真正想知道的不是「调用了 40 次」，而是
    「我这一单到底搜透了没、要搜透得给多少」。原来的提示只会机械地说
    「加 --budget 80」（当前值的两倍），对 2 件商品是浪费、对 6 件商品
    又远远不够 —— 那是个拍脑袋的数，不是算出来的。

    模型：一次局部搜索 ≈ 迭代 `TYPICAL_ITERATIONS` 轮 × 每轮扫半遍邻域
    （first-improvement 平均扫一半就找到改进），每个随单购档位各来一遍，
    再加裸买那一次。向上取整到 50，免得报出一串看着像精确值的零头 ——
    它估的是量级，报成 470 比报成 468 更诚实。
    """
    # 贪心解通常把 min(商品数, 券数) 个位置填满，用它当探针估算邻域规模
    filled = min(n_items, len(table))
    probe = {i: c for i, c in enumerate(sorted(table)[:filled])}
    per_search = TYPICAL_ITERATIONS * max(neighborhood_size(table, probe) // 2, 1)
    raw = 1 + n_tiers + n_tiers * per_search
    rounded = -(-raw // 50) * 50
    # 只往上建议，永不往下：预算给多了只是慢一点，给少了是给出个没搜完的价
    return max(rounded, current)


@dataclass(frozen=True)
class WithOrderOption:
    """一个「随单购」档位。

    `payload` 和 `label` 必须分开 —— 这不是洁癖，是踩过坑的：

    早先版本把展示用的 `_label` 直接塞进同一个 dict 发给官方接口。
    官方当前没做严格 schema 校验所以没出事，但只要哪天它开始校验未知字段，
    整个算价请求会被拒，而这个 bug 在离线测试里根本测不出来
    （离线 oracle 不校验 payload）。

    所以这里定死：**payload 里只有官方字段，一个多余的键都不能有。**
    """

    payload: dict
    label: str = ""

    def signature(self) -> str:
        # 用 json 而不是 repr：dict 的 repr 依赖插入顺序，
        # 两个字段相同、顺序不同的 dict 会被当成两个方案，白白多调接口。
        return json.dumps(
            {"p": self.payload, "l": self.label}, sort_keys=True, ensure_ascii=False
        )


@dataclass
class Assignment:
    """一个候选方案：item_index -> coupon_index。"""

    mapping: dict[int, int] = field(default_factory=dict)
    with_order: WithOrderOption | None = None  # None 表示不随单购
    price: PriceResult | None = None
    #: False 表示「还没找完就停了」（预算用尽），不是「已经找不到更便宜的了」。
    #
    # 为什么要区分：默认 --budget 在 6 件商品 8 张券以上就必然花光。
    # 混成同一件事，用户看到的是一个**没有被否定的最优解**——
    # 他会拿着这个数字去下单，然后发现柜台那边还能更便宜。
    # 「我不确定有没有更好的」和「这就是最好的」是两种完全不同的结论。
    converged: bool = True

    def signature(self) -> tuple:
        wo = self.with_order
        if wo is None:
            wo_sig = ""
        elif isinstance(wo, WithOrderOption):
            wo_sig = wo.signature()
        else:
            # 兼容离线演示里直接传字符串档位的写法，别让它悄悄崩在这里
            wo_sig = str(wo)
        return (tuple(sorted(self.mapping.items())), wo_sig)

    def describe(self, cart: Sequence[CartItem], coupons: Sequence[Coupon]) -> list[str]:
        lines = []
        for item_idx, coupon_idx in sorted(self.mapping.items()):
            item = cart[item_idx]
            coupon = coupons[coupon_idx]
            lines.append(f"{item.name or item.product_code}  ←  {coupon.title}")
        if self.with_order:
            lines.append(f"随单购：{self.with_order.label}")
        return lines


PriceOracle = Callable[[list[CartItem], "Assignment"], PriceResult]


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    """跨求解器共享的算价调用预算。

    为什么需要它：`optimize` 跑完还会跑洞察（边际成本要逐件剔除重算、
    拆单建议要各拆一遍），每一路都会新建一个 CouponOptimizer。
    各记各的账的话，用户设 `--budget 40`，实际能打出 79 次 ——
    麦当劳限流 600 次/分钟不会当场报错，但用户看到「预算 40 用了 79 次」
    只会得出一个结论：这工具不守规矩。

    所以全局只认这一个计数器，谁调接口谁先问它。
    """

    def __init__(self, total: int):
        self.total = max(int(total), 0)
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(self.total - self.used, 0)

    def spend(self, n: int = 1) -> None:
        self.used += n

    def refund(self, n: int = 1) -> None:
        """退还预扣的次数（缓存命中时用：那一趟没真的发请求）。"""
        self.used = max(self.used - n, 0)

    def __repr__(self) -> str:
        return f"Budget({self.used}/{self.total})"


class CouponOptimizer:
    """券价最优组合求解器。

    oracle 是唯一的真实来源：给定购物车与方案，返回真实价格。
    把它抽象成可注入函数，是为了让 demo 模式能离线复现完整演示。
    """

    def __init__(
        self,
        oracle: PriceOracle,
        max_calls: int = 40,
        verbose: bool = False,
        max_restarts: int = 3,
        restart_seed: int = 20261011,
        budget: Budget | None = None,
        tabu_tenure: int = 6,
        max_iterations: int = 64,
        sideways_reserve: int = 6,
    ):
        """参数说明：

        `max_calls`  本求解器自己的上限
        `budget`     全局共享预算；不传就退化为「只受 max_calls 约束」
        `max_restarts`
            局部搜索收敛后，从当前最优解**随机踢掉几张券**再重来的轮数
            （ruin & recreate）。0 = 不做扰动。只在预算有富余时发生。
        `tabu_tenure`
            横移时「刚走过的解多久之内不许回去」。太小会原地打转，
            太大会堵死通路。
        `max_iterations`
            单轮局部搜索的迭代上限。横移不降价，得有个闸，
            否则预算全烧在价格不变的原地踏步上。
        `sideways_reserve`
            剩余预算低于这个数就不再尝试横移 —— 把钱留给别的随单购档位和扰动重启。
        没有 `min_tier_quota` 了。原先给每个随单购档位设了 6 次的保底，
        理由是「不让第一个档位把预算吃光」—— 听着对，但消融实测在
        三种 oracle 形态 × 三档预算下**全是零贡献**，于是删掉，
        只保留「按剩余档位数均分」这一层。留着一个没测出收益的旋钮，
        比没有这个旋钮更糟：它会让人以为那里已经保护过了。
        """
        self.oracle = oracle
        self.max_calls = max(int(max_calls), 0)
        self.verbose = verbose
        self._budget = budget or Budget(self.max_calls)
        # 收敛后做几轮「踢掉几张券再填回去」的扰动。0 = 不做。
        self.max_restarts = max(0, max_restarts)
        self.restart_seed = restart_seed
        self.tabu_tenure = max(int(tabu_tenure), 0)
        self.max_iterations = max(int(max_iterations), 1)
        self.sideways_reserve = max(int(sideways_reserve), 0)
        self.calls = 0
        # 同一个方案会被不同的邻域动作重复生成（例如先删后加绕回原点），
        # 缓存命中就不消耗调用次数 —— 预算是按次计的，省一次是一次。
        self._cache: dict[tuple, PriceResult] = {}
        self._failures = 0
        # 是否中途收过工（预算见底），决定最终结果的 `converged` 标记
        self._stopped = False

    # ---------- 求值 ----------

    @property
    def remaining(self) -> int:
        """还能调几次：全局预算和本求解器上限，取更紧的那个。"""
        return min(self._budget.remaining, max(self.max_calls - self.calls, 0))

    @property
    def budget_used(self) -> int:
        """全局预算已用次数（含别的求解器花掉的）。"""
        return self._budget.used

    def evaluate(self, cart: Sequence[CartItem], assignment: Assignment) -> PriceResult:
        """求值一次；相同方案直接命中缓存，不计入调用次数。

        为什么要两次记账：本实例的 `_cache` 之外，`self.oracle` 可能还被
        `cached_oracle` 套了一层**跨求解器共享**的缓存（一次 advise 会建十几个
        求解器）。命中后者时同样没发生网络往返，预扣的那一次必须退还 ——
        否则「没花钱的请求」照样吃掉预算，用户设的 `--budget` 又变成了摆设。
        """
        sig = assignment.signature()
        if sig in self._cache:
            assignment.price = self._cache[sig]
            return assignment.price
        if self._budget.remaining <= 0:
            raise BudgetExhausted(f"算价调用已达上限 {self._budget.total}")
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"本轮求解已达上限 {self.max_calls}")
        self._budget.spend()
        self.calls += 1

        _stats = getattr(self.oracle, "calls", None)
        _before = _stats["calls"] if isinstance(_stats, dict) else None
        try:
            result = self.oracle(list(cart), assignment)
        except Exception:
            # 这一趟没拿到价格，预扣的次数必须还回去。
            # 预算保护的是「真实发生的网络请求」，不是「evaluate 被调用了几次」。
            # 不还的后果是双重的：额度白白少一次，且该方案因为没写进 _cache，
            # 后面扰动重来时还会为同一个注定失败的方案再付一遍钱。
            self._budget.refund()
            self.calls -= 1
            raise
        if isinstance(_stats, dict) and _before == _stats["calls"]:
            # 上游缓存命中：把刚才预扣的那一趟还回去。
            # 预算保护的是真实网络请求，不是 evaluate 的调用次数。
            self._budget.refund()
            self.calls -= 1

        assignment.price = result
        self._cache[sig] = result
        self._failures = 0
        return result

    def try_evaluate(
        self, cart: Sequence[CartItem], candidate: Assignment
    ) -> PriceResult | None:
        """算不出来就返回 None，让搜索继续试别的邻域，而不是整轮崩掉。

        但**不能无脑吞掉所有异常**：如果 Token 失效或触发限流，
        每一次求值都会失败，全返回 None 的话搜索会静默跑完，
        最后报出一个「没找到更好的组合」——把真正的故障伪装成正常结果。
        所以连续失败到一定次数就原样抛出，让上层看见真实原因。
        """
        price, _ = self._try_evaluate(cart, candidate)
        return price

    def _try_evaluate(
        self, cart: Sequence[CartItem], candidate: Assignment
    ) -> tuple[PriceResult | None, str]:
        """返回 `(价格, 原因)`，原因三种：

            "ok"      算出来了
            "budget"  预算耗尽 —— 必须停止搜索，这时收工是诚实的
            "error"   单次失败（网络抖动 / 接口报错）—— 该**跳过这个候选继续试**

        为什么非得分开：爬山里如果只看 `price is None`，
        一次网络抖动就会被当成「邻域内没有更便宜的」，于是只爬两步就收工，
        最后报一个「已是最优」，实际上用户多付了钱，而且全程没有任何报错。
        """
        from .mcd_client import McpError  # 局部导入，避免模块级循环依赖

        try:
            return self.evaluate(cart, candidate), "ok"
        except BudgetExhausted:
            return None, "budget"
        except (McpError, TimeoutError, OSError) as exc:
            self._failures += 1
            if self._failures >= 3:
                raise
            if self.verbose:
                print(f"  （一次算价失败，跳过该候选：{exc}）")
            return None, "error"

    # ---------- 构造 ----------

    def _eligibility(
        self, cart: Sequence[CartItem], coupons: Sequence[Coupon]
    ) -> dict[int, list[int]]:
        """coupon_index -> 可挂的商品下标列表。"""
        table: dict[int, list[int]] = {}
        for ci, coupon in enumerate(coupons):
            hits = [
                ii for ii, item in enumerate(cart) if coupon.applies_to(item.product_code)
            ]
            if hits:
                table[ci] = hits
        return table

    def greedy(
        self, cart: Sequence[CartItem], coupons: Sequence[Coupon], shuffle: int = 0
    ) -> Assignment:
        """最受约束优先：可挂商品越少的券越先占坑。

        `shuffle` > 0 时打乱同长度券的先后顺序（`shuffle` 用作随机种子）。
        贪心的起点不同，落到的局部最优也不同 —— 这是扰动重启的多样性来源。
        种子固定，所以同一 seed 结果可复现，演示不会每次跑出不同数字。
        """
        table = self._eligibility(cart, coupons)
        used_items: set[int] = set()
        mapping: dict[int, int] = {}
        order = sorted(table, key=lambda c: len(table[c]))
        if shuffle:
            rng = random.Random(shuffle)
            # 只在「可挂商品数相同」的券之间换顺序，尽量不破坏最受约束优先的初衷
            buckets: dict[int, list[int]] = {}
            for ci in order:
                buckets.setdefault(len(table[ci]), []).append(ci)
            order = []
            for key in sorted(buckets):
                group = buckets[key][:]
                rng.shuffle(group)
                order.extend(group)
        for ci in order:
            for ii in table[ci]:
                if ii not in used_items:
                    mapping[ii] = ci
                    used_items.add(ii)
                    break
        return Assignment(mapping=mapping)

    # ---------- 搜索 ----------

    def optimize(
        self,
        cart: Sequence[CartItem],
        coupons: Sequence[Coupon],
        with_order_options: Iterable[WithOrderOption | None] = (None,),
    ) -> Assignment:
        table = self._eligibility(cart, coupons)
        best: Assignment | None = None

        # ---- 第零步：先把「一张券都不挂、也不随单购」算出来钉成下界（1 次调用）
        #
        # 没有这个下界会出什么事：预算不够让爬山把陷阱券删完时，best 会停在
        # 一个比裸买更贵的解上，屏幕上于是出现「真实省下 ¥-1.00」——
        # 挂券挂出了负收益。而裸买是唯一一个「一定拿得到」的价：
        # 它是地板，不是候选之一。先把它踩在脚下，后面只许更好，不许更差。
        #
        # 只算一次、也不拿它去爬山（空 mapping 没有券可换可删，爬了也是空转）。
        bare = Assignment(mapping={}, with_order=None)
        if self.try_evaluate(cart, bare) is not None:
            best = bare

        # ---- 第一步：每个随单购档位先用贪心各试一次（每档只花 1 次调用）
        #
        # 为什么不能「一个档位爬完山再试下一个」：
        # 第一个档位的爬山会把预算吃干，轮到后面的档位时 try_evaluate 返回 None，
        # 直接 break —— 于是那些档位连一次都没被算过。
        # 实测 400 组随机用例里，这种写法让 22 组拿到了更差的结果，
        # 而且错得很隐蔽：mapping 一模一样，只是随单购档位选错了。
        # 「要不要随单购」往往值好几块钱，比在单一档位里多爬两步重要得多。
        screened: list[Assignment] = []
        for with_order in with_order_options:
            start = self.greedy(cart, coupons)
            start.with_order = with_order
            if self.try_evaluate(cart, start) is None:
                # 区分「预算见底」和「瞬时网络抖动」：
                # 只有前者才真正停；后者跳到下一个随单购档位再试，
                # 否则一次超时就会静默跳过「要不要随单购」这个最值钱的决策。
                if self.remaining <= 0:
                    break
                continue
            screened.append(start)
            if best is None or start.price.price_fen < best.price.price_fen:
                best = start

        # ---- 第二步：按贪心价从低到高排，再决定「深搜几个档位」
        #
        # 这一步经历过一次矫枉过正，值得记下来。最早是「第一个档位一路爬到
        # 收敛」，结果它把预算吃干，后面的档位连一次邻域都进不去，只能拿贪心
        # 那个价交差 —— 于是改成「先筛一遍、再逐个深搜」。
        # 但逐个深搜在预算紧张时是灾难：6 件商品 7 张券、40 次预算下，一遍
        # 邻域就要 48 次调用，均成三份每份 12 次，**三份全是半途而废** ——
        # 命中率从 40% 直接掉到 0%，平均差 ¥3.93 恶化到 ¥16.53。
        #
        # 所以规矩是：钱够给每个档位各扫一遍，才逐个深搜；不够就把全部预算
        # 压在**筛出来最好的那一个**档位上。预算紧张时，「把一个档位搜透」
        # 永远胜过「三个档位各摸一把」。
        screened.sort(key=lambda a: a.price.price_fen)
        if not screened:
            targets = []
        else:
            probe = neighborhood_size(table, screened[0].mapping)
            deep_all = self.remaining >= max(probe, 1) * len(screened)
            targets = screened if deep_all else screened[:1]
        n_tiers = len(targets)
        for rank, start in enumerate(targets):
            if self.remaining <= 0:
                break
            left = n_tiers - rank
            quota = self.remaining if left == 1 else max(1, self.remaining // left)
            climbed = self._local_search(cart, table, start, quota)
            if climbed.price is not None and (
                best is None or climbed.price.price_fen < best.price.price_fen
            ):
                best = climbed

        # ---- 第三步：扰动重启（ruin & recreate）
        #
        # 原来的重启是「换个贪心起点再爬一遍」，这里有个洞：贪心按「可挂商品数」
        # 分桶后只在桶内打乱，而各券可挂商品数常常互不相同 —— 桶里只有一张券时
        # 顺序原封不动，重启出来的起点和首轮逐位相同，等于同一座山爬两遍，
        # 白花预算还假装做了多样性。
        #
        # 改成从**当前最优解**出发，随机踢掉几张券（ruin），再让邻域把坑填回去
        # （recreate）。起点既不同、又是从已知最好的地方出发 —— 这才是重启该有的样子。
        rng = random.Random(self.restart_seed)
        for _ in range(self.max_restarts):
            if best is None or best.price is None:
                break
            # 一轮扰动 = 踢掉几张券 + 把坑重新填回去，至少要「再扫一遍邻域」的钱。
            # 不够就别开：开到一半断了，等于把本可以下降的钱打了水漂。
            if self.remaining <= self.sideways_reserve + self._sweep_cost(
                table, best.mapping
            ):
                break
            alt = self._ruin(best.mapping, rng)
            alt.with_order = best.with_order
            # 踢完跟原来一模一样（多半是没什么券可踢），这次扰动没意义
            if alt.signature() == best.signature():
                continue
            if self.try_evaluate(cart, alt) is None:
                if self.remaining <= 0:
                    break
                continue
            alt = self._local_search(cart, table, alt, self.remaining)
            if alt.price is not None and alt.price.price_fen < best.price.price_fen:
                best = alt

        if best is None or best.price is None:
            # 预算连一次贪心都没跑成。
            # 这里**不能**绕过 evaluate 直接调 oracle —— 那会同时绕过预算和缓存，
            # 表面上看是"兜底成功"，实际是静默超支。这里照常记账：
            # 预算真的没了就抛 BudgetExhausted，让上层明确告诉用户调大 --budget。
            best = Assignment(mapping={}, with_order=None)
            best.price = self.evaluate(cart, best)

        # 预算花光意味着计划里的搜索有一部分根本没跑 —— 这时给出的解可以是
        # 好的，但绝不是「穷举过一遍后最好的」。标注出来，让上层如实相告。
        # 方向偏保守（宁愿说不确定，也不谎报最优）：max_calls 恰好用尽且其实
        # 已收敛的情况会被一并标注，代价只是多一句温和的提示。
        if self._stopped or self.remaining <= 0:
            best.converged = False
        return best

    def _stopped_early(self, current: Assignment) -> Assignment:
        """预算见底时收工，并把「没算完」这件事记在结果上。

        诚实比给出一个漂亮的数字更重要：调用方拿到 `converged=False`
        就必须告诉用户「这里可能还有更便宜的组合」，而不是默认它是最优解。
        """
        current.converged = False
        self._stopped = True
        return current

    def _local_search(
        self,
        cart: Sequence[CartItem],
        table: dict[int, list[int]],
        start: Assignment,
        quota: int = 0,
    ) -> Assignment:
        """局部搜索：先一路下降，卡住之后横移一步换片盆地，再卡住才收工。

        和纯爬山的差别就在「卡住之后不立刻认输」这半句。
        纯爬山宣布最优时说的那句话，真身是「从这儿出发，一步走不到更好的」；
        而组合优化里大量的最优解需要先横移一步（价格暂时不变）才换得到盆地。

        这不是纸上谈兵，是量出来的：把预算放大 12 倍（40 → 500 次调用），
        命中穷举最优只从 76.7% 走到 84.0%。多给的钱只买回来 7 个点，
        说明剩下那 16% 是「走不到」，不是「没走完」—— 该改的是走法，不是加钱。

        `quota` 是本轮最多愿意花掉的求值次数（0 = 不限，只看全局预算）。
        """
        current = start
        if current.price is None:
            if self.try_evaluate(cart, current) is None:
                return current
        current_price = current.price.price_fen

        tabu: dict[tuple, int] = {}
        iteration = 0
        # 开工前定一次：这一轮开不开「抢占」邻域。
        # 判据是「剩下的钱够不够扫一遍完整邻域」—— 够，才付得起那份 O(m²)。
        wide = self.remaining >= neighborhood_size(table, start.mapping)
        # 本轮的花费用「还剩多少」当闸，而不是另起一个计数器：
        # 缓存命中不扣预算，这个闸也就跟着不关，不用额外去对齐两套账。
        floor = max(self.remaining - quota, 0) if quota else 0

        while self.remaining > floor and iteration < self.max_iterations:
            iteration += 1

            # ---- 阶段一：严格下降。见好就收，一步都不浪费在平局上。
            current, current_price, status = self._scan(
                cart,
                table,
                current,
                current_price,
                accept_equal=False,
                tabu=None,
                iteration=iteration,
                wide=wide,
            )
            if status == "exhausted":
                return self._stopped_early(current)
            if status == "moved":
                continue

            # ---- 阶段二：邻域扫完了才谈得上「卡住」。预算还够就横移换盆地。
            #
            # 为什么只在卡住之后才横移：横移不降价，每一趟都是纯支出。
            # 放到下降阶段去做，预算会全烧在价格不变的原地踏步上 ——
            # 这正是「加了平局反而更差」最典型的翻车方式。
            # 预算紧的时候不做横移：横移不降价，是纯投资，
            # 投到一半断了就血本无归。至少得留出「再扫半遍邻域」的钱。
            if self.remaining <= self.sideways_reserve + self._sweep_cost(
                table, current.mapping
            ) // 2:
                break
            # 把脚下这块地方钉进禁忌表：下一步不许原路走回来
            tabu[current.signature()] = iteration
            current, current_price, status = self._scan(
                cart,
                table,
                current,
                current_price,
                accept_equal=True,
                tabu=tabu,
                iteration=iteration,
                wide=wide,
            )
            if status == "exhausted":
                return self._stopped_early(current)
            if status != "moved":
                break  # 连平局都走不动，这片盆地是真探完了

        return current

    def _ruin(self, mapping: dict[int, int], rng: random.Random) -> Assignment:
        """从一份方案里随机踢掉几张券，造一个「不同但接近」的新起点。

        踢几张是随机的（至少一张、至多全踢）：只踢一张的话，邻域 2 多半会
        原路把它加回去，等于没扰动；全踢光又退化成从零贪心，白丢已有成果。
        """
        if not mapping:
            return Assignment(mapping={})
        k = rng.randint(1, len(mapping))
        victims = rng.sample(sorted(mapping), k)
        return Assignment(mapping={i: c for i, c in mapping.items() if i not in victims})

    def _sweep_cost(self, table: dict[int, list[int]], mapping: dict[int, int]) -> int:
        """扫一遍完整邻域要花多少次求值（精确值，不是估算）。"""
        return neighborhood_size(table, mapping)

    def _scan(
        self,
        cart: Sequence[CartItem],
        table: dict[int, list[int]],
        current: Assignment,
        current_price: int,
        accept_equal: bool,
        tabu: dict[tuple, int] | None,
        iteration: int,
        wide: bool = True,
    ) -> tuple[Assignment, int, str]:
        """扫邻域，遇到第一个能接受的候选就走过去。

        返回 `(当前解, 当前价, 状态)`，状态三种：
            "moved"      走了一步
            "exhausted"  预算见底，必须收工
            "stuck"      邻域**扫完了**，没有能接受的

        这里曾经有个「本轮最多求几个新候选」的上限，想法是把一次大扫描切成
        几批、靠缓存接力看完。听起来漂亮，实测却更差（命中掉 1.6 个点、
        平均差恶化 ¥0.70）：把一次本可以一口气走通的下降切成几段，
        每段都还没走到改进点就被掐断。删掉了。
        """
        for candidate in self._neighborhood(table, current, wide):
            if tabu is not None:
                left = tabu.get(candidate.signature())
                if left is not None and iteration - left < self.tabu_tenure:
                    continue  # 刚从那儿出来，不许马上回去
            price, reason = self._try_evaluate(cart, candidate)
            if reason == "budget":
                return current, current_price, "exhausted"
            if price is None:
                continue  # 单次失败（抖动/接口报错）：跳过，继续试下一个候选
            if price.price_fen < current_price:
                return candidate, price.price_fen, "moved"
            if accept_equal and price.price_fen == current_price:
                # 平局也走：价格没变，但换了个站位，看到的邻域就不同了 ——
                # 门就是这样一道道打开的。禁忌表负责不让我们绕回原地。
                return candidate, price.price_fen, "moved"
        return current, current_price, "stuck"

    def _neighborhood(
        self, table: dict[int, list[int]], current: Assignment, wide: bool = True
    ) -> Iterator[Assignment]:
        """枚举当前解「一步之遥」的所有候选（惰性生成，扫到就求值，不预先展开）。

        六个邻域的顺序不是随手排的：
            1 删   2 加               改变「挂了几张券」
            3 换   4 挪   5 对调       重排「谁挂谁」
            6 抢占                    挤掉别人，自己上位

        `wide=False` 时关掉「抢占」。这不是可有可无的开关，是实测逼出来的：
        抢占的候选数是 O(m²)，6 件商品时能让单次扫描贵一倍。预算宽裕时它
        值那笔钱（三种 oracle 形态下都能换来 3~8 个点的命中率），
        但预算紧张时，多出来的这一半候选是**拿「少走两步下降」换的** ——
        分层数据说得清清楚楚，40 次预算下 5 件 6 券以上的平均差因此从
        ¥2.46 恶化到 ¥6.65，命中率也从 54.5% 掉到 36.4%。

        所以规矩是：**剩下的钱够扫一遍完整邻域，才开抢占。**
        """
        mapping = current.mapping
        wo = current.with_order

        # 邻域 1：删掉一张已挂的券
        # 这不是多余的 —— 实测券会被随单购作废，挂着反而占坑。
        for item_idx in list(mapping):
            yield Assignment(
                mapping={k: v for k, v in mapping.items() if k != item_idx},
                with_order=wo,
            )

        # 邻域 2：给空闲商品补一张未用的券
        used_coupons = set(mapping.values())
        for ci, hits in table.items():
            if ci in used_coupons:
                continue
            for item_idx in hits:
                if item_idx in mapping:
                    continue
                yield Assignment(mapping={**mapping, item_idx: ci}, with_order=wo)

        # 邻域 3：把某个**已挂商品**上的券，换成另一张还没用、且也适用它的券
        #
        # 这是最容易被漏掉的一个邻域，也是实测里最值钱的一个。
        # 贪心是「最受约束的券先占坑」，于是那张只能用在 1 个商品上的券
        # 会第一个抢到位置 —— 但它未必是那件商品上最猛的一张。
        # 此时「删」会让总价变贵（不通过）、「加」无处可加（商品已占），
        # 两个邻域都动不了，就卡死在次优解上。
        used_coupons = set(mapping.values())
        for item_idx, ci in list(mapping.items()):
            for cj, hits in table.items():
                if cj == ci or cj in used_coupons:
                    continue
                if item_idx not in hits:
                    continue
                swapped = dict(mapping)
                swapped[item_idx] = cj
                yield Assignment(mapping=swapped, with_order=wo)

        # 邻域 4：把已挂的券挪到另一个它还适用的**空闲**商品上
        for item_idx, ci in list(mapping.items()):
            for target in table.get(ci, []):
                if target == item_idx or target in mapping:
                    continue
                moved = dict(mapping)
                del moved[item_idx]
                moved[target] = ci
                yield Assignment(mapping=moved, with_order=wo)

        # 邻域 5：两张已挂的券对调
        # 对调之后是「商品 i1 挂券 c2、商品 i2 挂券 c1」，
        # 所以要校验的是 c2 适用于 i1、c1 适用于 i2。
        # table 的 key 是券下标、value 是商品下标列表 —— 千万别把两者弄反。
        for (i1, c1), (i2, c2) in itertools.combinations(list(mapping.items()), 2):
            if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):
                continue
            swapped = dict(mapping)
            swapped[i1], swapped[i2] = c2, c1
            yield Assignment(mapping=swapped, with_order=wo)

        # 邻域 6：抢占 —— 把别人身上那张券抢过来，自己原来那张就此作废
        #
        # 这次改动补的第三个洞，也是最要紧的一个。
        # 「换」只允许换成**没人用**的券，「挪」只允许挪到**空着**的商品，
        # 于是下面这种解在只有前五个邻域时**根本不可达**：
        #
        #     当前 {A挂X, B挂Y}，最优 {A挂Y}
        #       删掉 B  → 总价变贵，被拒绝
        #       给 A 换券 → Y 正被 B 占用，被 `cj in used_coupons` 排除
        #
        # 每一步单看都不便宜，可它们的组合才是真最优。实测 2 件商品 2 张券
        # 这种小到能穷举的场景（解空间才 21 个方案，40 次预算绰绰有余）
        # 也只能命中 71.4% —— 缺的就是这一个动作。
        #
        # 用 permutations 而不是 combinations：抢占是有方向的
        # （i 抢 j 的 与 j 抢 i 的 是两回事），两个方向都得给。
        if not wide:
            return
        for (i, _ci), (j, cj) in itertools.permutations(list(mapping.items()), 2):
            if i not in table.get(cj, []):
                continue  # cj 压根不适用于 i，抢过来也挂不上
            seized = dict(mapping)
            del seized[j]  # cj 离开 j，j 就此空着
            seized[i] = cj  # cj 占住 i，i 上原来那张被挤掉
            yield Assignment(mapping=seized, with_order=wo)
