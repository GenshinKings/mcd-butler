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
    """按累计省下的钱给段位。返回当前段位、下一档、还差多少。"""
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


def butler_warning(travel_fen: int, saved_fen: int) -> str:
    """路费吃掉优惠时的劝退——这是管家最该说的一句实话。"""
    if travel_fen <= 0:
        return "走着去，路费为零，这一趟稳赚不赔。"
    if saved_fen <= 0:
        return f"这趟路费 {yuan(travel_fen)}，可这一单压根没省下钱。出门纯属花钱买罪受。"
    if travel_fen >= saved_fen:
        return (
            f"小的斗胆劝一句：这趟路费 {yuan(travel_fen)}，"
            f"比您省下的 {yuan(saved_fen)} 还贵。\n"
            f"  您这是花钱出门买罪受——除非本来就要出去，否则不如躺着等外卖。"
        )
    return (
        f"路费 {yuan(travel_fen)}，比省下的 {yuan(saved_fen)} 少，"
        f"这趟划算，出门不亏。"
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
    for e in entries:
        base = e.get("baseline_fen") or 0
        paid = e.get("paid_fen") or 0
        if isinstance(base, int) and isinstance(paid, int) and base > paid:
            saved += base - paid
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
        "saved_fen": saved,
        "top_store": stores.most_common(1)[0][0] if stores else "",
        "top_coupon": coupons.most_common(1)[0][0] if coupons else "",
        "first_at": times[0] if times else "",
        "last_at": times[-1] if times else "",
    }
