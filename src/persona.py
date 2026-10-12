"""麦门管家的「人」：段位、口吻、晒单。

为什么要有这一层
----------------
引擎算得再准，如果输出全是表格，用户记不住这个项目。
「麦门管家」这名字本身是有人设的（麦门是麦粉的黑话，麦门永存），
但 CLI 一直在用工程师的口吻说话 —— 名字和内容对不上，这是最大的浪费。

所以这一层补上人格：

- **段位**：让"省钱"从一次性结果变成能积累、能晒的东西
- **口吻**：结论用管家的话说，恭敬但不谄媚，敢劝、敢说实话
- **晒单**：一键生成能粘进朋友圈/群里的文本卡片

一条硬原则
----------
人格只包在**结论**外面。表格、金额、调用次数这些保持专业，
绝不用梗去糊弄数字 —— 一个管家的体面，在于账目清清楚楚。
"""

from __future__ import annotations

from collections import Counter

# ------------------------------------------------------------------ 段位
# 门槛按「累计省下多少钱」划分（单位：分）。
# 取名走麦门黑话：路人 → 学徒 → 信徒 → 执事 → 主教 → 大主教 → 麦门永存。
RANKS: list[tuple[int, str, str]] = [
    (0, "麦门路人", "刚进门，还没尝到甜头"),
    (1000, "麦门学徒", "开始懂规矩了"),
    (5000, "麦门信徒", "已经会挑券了"),
    (15000, "麦门执事", "券在手里，心里有数"),
    (40000, "麦门主教", "一单能省出一包薯条"),
    (100000, "麦门大主教", "麦当劳见了您都得让三分"),
    (300000, "麦门永存", "您就是麦门本身"),
]


def rank_of(saved_fen: int) -> dict:
    """按**真实成交的累计实付金额**给段位。返回当前段位、下一档、还差多少。

    为什么只认真实成交：`optimize` 是测算，同一单跑 15 次就是 15 条记录 ——
    段位要按测算算，就变成了「谁把命令多跑几遍谁段位高」的游戏，
    发出去的卡片也是一份自己解释不清的假成绩单。
    真实成交（来自 `replay` 导入的历史订单）按订单号去重，跑多少遍都是那几笔。

    为什么用**实付**而不是「省了多少」：历史订单只有实付金额，
    当时的裸买价无从得知 —— 编一个 baseline 去算省了多少就是虚报。
    花出去的钱是硬事实，虽然段位因此更像「麦门资历」而不是「省钱战功」，
    但至少它不掺假。
    """
    saved_fen = max(0, int(saved_fen))
    cur_idx = 0
    for i, (need, _, _) in enumerate(RANKS):
        if saved_fen >= need:
            cur_idx = i
    need, title, comment = RANKS[cur_idx]
    nxt = None
    if cur_idx + 1 < len(RANKS):
        n_need, n_title, _ = RANKS[cur_idx + 1]
        nxt = {"title": n_title, "need_fen": n_need, "gap_fen": n_need - saved_fen}
    return {
        "title": title,
        "comment": comment,
        "index": cur_idx,
        "max_index": len(RANKS) - 1,
        "next": nxt,
    }


def progress_bar(index: int, max_index: int, width: int = 12) -> str:
    """段位进度条。满级时给个实心的。"""
    if max_index <= 0:
        return "█" * width
    filled = int(round(index / max_index * width))
    return "█" * filled + "░" * (width - filled)


# ------------------------------------------------------------------ 管家的口吻
def yuan(fen: int) -> str:
    return f"¥{fen / 100:.2f}"


def butler_saved(saved_fen: int) -> str:
    """管家点评这一单省了多少。敢说实话：省得少就说省得少。"""
    if saved_fen <= 0:
        return "这一单实在没处省，券都用尽了。小的无能，您骂得对。"
    if saved_fen < 300:
        return f"这一单小的替您压到头了，省下 {yuan(saved_fen)}。不多，但蚊子腿也是肉——麦门讲究的就是积少成多。"
    if saved_fen < 1500:
        return f"省下 {yuan(saved_fen)}，够加一份薯条了。这单办得还行，您赏脸再看下一单？"
    if saved_fen < 5000:
        return f"省下 {yuan(saved_fen)}，这单办得漂亮。照这个势头，段位还往上走。"
    return f"省下 {yuan(saved_fen)}。小的斗胆说一句：这单的券，算是物尽其用了。"


def butler_total(saved_fen: int, orders: int, real_orders: int = 0) -> str:
    """管家对「累计账单」的点评（区别于单笔的 butler_saved）。

    措辞上必须守住一条：这是**测算**出来的省钱空间，不等于已经省下的钱。
    管家可以讨喜，但不能替主人吹牛 —— 吹出去的牛，主人得自己圆。
    """
    if orders <= 0 or saved_fen <= 0:
        return "账上还是空的。先让小的办一单，才有得记。"
    avg = saved_fen // max(orders, 1)
    tail = "积少成多，这就是麦门的算法。"
    if avg >= 2000:
        tail = "您这手气，小的服气。"
    base = f"共测算 {orders} 次，替您找出 {yuan(saved_fen)} 的省钱空间，平均每单 {yuan(avg)}。{tail}"
    if real_orders == 0:
        base += "（这些都是测算，不是已成交的省钱 —— 小的不敢替您记虚账。）"
    else:
        base += f"（其中 {real_orders} 笔是您的真实订单。）"
    return base


def butler_almanac(best_saved_fen: int, today_expiry: int) -> str:
    """黄历收尾。管家不重复表格里已有的数字，只说那一句该说的。"""
    if today_expiry > 0:
        return (
            f"小的多嘴一句：今天有 {today_expiry} 张券到期，"
            f"作废了比花错了更可惜。先紧着它们点。"
        )
    if best_saved_fen >= 1000:
        return "今天手气不错，照着这张单子点，不吃亏。"
    if best_saved_fen > 0:
        return "今天能省的都在上面了，不多，但都是真的 —— 小的不编。"
    return "今天这家店实在没什么可省的。换个时段或换家门店，小的再给您算一张。"


def butler_warning(
    travel_fen: int,
    saved_fen: int,
    alt_gap_fen: int | None = None,
    minutes: int = 0,
) -> str:
    """路费这笔账该怎么说 —— 管家最该说的一句实话。

    旧版拿「路费」跟「券省下的钱」比，那是范畴错误：
    路费是**出行方式**的成本（打车 ¥14.72、走路 ¥0），跟用不用券毫无关系。
    同一单同一家店，换个 `--mode` 就从"白跑"变成"稳赚"，这话就没意义了；
    而且项目并不支持外卖，却劝人"不如躺着等外卖"，是自己打自己脸。

    真正该比的决策量是：**去这家**比**去次优那家**省下多少（alt_gap），
    以及这趟来回要花多少时间。省得少还要跑到远处，才叫不值。
    """
    go = f"这趟来回 {minutes} 分钟" if minutes else "这一趟"
    if travel_fen <= 0:
        return "走着去，路费为零 —— 这一趟稳赚不赔。"

    if alt_gap_fen is not None:
        if alt_gap_fen <= 0:
            return (
                f"{go}，路费 {yuan(travel_fen)}。"
                f"不过次选那家和这家综合花费一样，那就挑近的，别跟路费较劲。"
            )
        if alt_gap_fen < travel_fen:
            return (
                f"小的斗胆劝一句：跑远这家只比次选省 {yuan(alt_gap_fen)}，"
                f"可{go}、路费 {yuan(travel_fen)} —— 省的钱还不够跑腿的。"
                f"图省事就挑近的那家。"
            )
        return (
            f"{go}，路费 {yuan(travel_fen)}；"
            f"但这家比次选省 {yuan(alt_gap_fen)}，划得来，值得跑。"
        )

    if saved_fen <= 0:
        return (
            f"{go}，路费 {yuan(travel_fen)}。这一单券没省出钱 —— "
            f"路费是您为吃这口饭花的，小的照实报，不替您粉饰。"
        )
    return (
        f"{go}，路费 {yuan(travel_fen)}；券省下 {yuan(saved_fen)}。"
        f"这两笔各是各的账，小的不拿它们互相比。"
    )


# ------------------------------------------------------------------ 晒单
def brag_card(
    saved_fen: int,
    orders: int,
    rank_title: str,
    top_store: str = "",
    top_coupon: str = "",
    first_at: str = "",
    real_orders: int = 0,
    with_link: bool = False,
) -> str:
    """生成一段能直接粘进朋友圈/群里的文本卡片。

    为什么要能晒：省钱这件事，跟朋友说的那一刻才有快感。
    一个只能自己看的省钱工具，传播不出去。

    两条克制：
    1. **不说谎** —— 绝大多数记录是"测算"，不是真实成交。
       把测算说成"我累计省了 ¥XX"，用户会被朋友一句"你哪单省的"问住。
       所以金额写「累计测算可省」，并单独标出真实成交笔数。
    2. **默认不带链接** —— 朋友圈/小红书里裸链接会被判广告而限流。
       想带链接发 CSDN/知乎时用 `brag --with-link`。
    """
    lines = [
        "┌──────────────────────────────────┐",
        "│   麦 门 管 家 · 我 的 麦 门 账 单   │",
        "└──────────────────────────────────┘",
        "",
        f"  段位：{rank_title}",
        f"  累计测算可省：{yuan(saved_fen)}",
        f"  经手：{orders} 次测算 / {real_orders} 笔真实成交",
    ]
    if top_store:
        lines.append(f"  最常光顾：{top_store}")
    if top_coupon:
        lines.append(f"  最常用券：{top_coupon}")
    if first_at:
        lines.append(f"  记账起始：{first_at[:10]}")
    lines.append("")
    lines.append("  账单由 mcd-butler 在本地生成，不上传任何数据")
    if with_link:
        lines.append("  https://github.com/GenshinKings/mcd-butler")
    return "\n".join(lines)


def summarize(entries: list[dict]) -> dict:
    """从本地快照汇总出账单数据。字段缺失一律当 0，绝不因脏数据崩。

    `real_orders` 单独计数：绝大多数快照是"测算"（引擎算出来能省多少），
    不是真实成交。把两者混成一个"累计省下 ¥XX"拿出去晒，
    用户会被朋友问住 —— 那不是他省的钱。所以数量与金额都必须分开报。
    """
    saved = 0
    real_saved = 0
    real_paid = 0
    for e in entries:
        base = e.get("baseline_fen") or 0
        paid = e.get("paid_fen") or 0
        if isinstance(base, int) and isinstance(paid, int) and base > paid:
            saved += base - paid
            # 只有真实成交（order）才进「真省下的钱」。
            # 测算（estimate）是「这么点能省多少」的空间，用户未必真买了 ——
            # 把它算进省下的钱，段位就成了「把 optimize 多跑几次」的游戏。
            if e.get("kind") == "order":
                real_saved += base - paid
        # 真实成交的**实付金额**：这是段位的依据。
        # 用实付而不是「省了多少」，是因为历史订单的裸买价无从得知 ——
        # 编一个 baseline 去算省了多少，等于替主人虚报。花出去的钱才是硬事实。
        if e.get("kind") == "order" and isinstance(paid, int) and paid > 0:
            real_paid += paid
    stores = Counter(
        (e.get("store_name") or e.get("store_code") or "?") for e in entries
    )
    coupons = Counter()
    for e in entries:
        for t in e.get("coupon_titles") or []:
            if t:
                coupons[t] += 1
    times = sorted(e.get("saved_at") or "" for e in entries)
    real_orders = sum(1 for e in entries if e.get("kind") == "order")
    return {
        "orders": len(entries),
        "real_orders": real_orders,
        "est_orders": len(entries) - real_orders,
        "saved_fen": saved,
        "real_saved_fen": real_saved,
        "real_paid_fen": real_paid,
        "est_saved_fen": saved - real_saved,
        "top_store": stores.most_common(1)[0][0] if stores else "",
        "top_coupon": coupons.most_common(1)[0][0] if coupons else "",
        "first_at": times[0] if times else "",
        "last_at": times[-1] if times else "",
    }
