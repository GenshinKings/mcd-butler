"""麦门管家 CLI。

    python -m src.cli demo        # 无需 Token，离线跑完整演示
    python -m src.cli optimize    # 真实账号：定位门店 → 算价 → 券价寻优
    python -m src.cli replay      # 历史订单复刻 + 价格变动汇报
    python -m src.cli tools       # 列出 MCP 暴露的全部工具
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

from .coupon_engine import (
    Assignment,
    Budget,
    BudgetExhausted,
    CartItem,
    Coupon,
    CouponOptimizer,
    PriceResult,
    WithOrderOption,
    to_fen,
    valid_until_end,
)
from .mcd_client import McpError, McpFatalError, load_dotenv
from .nutrition import MAX_MEAL_SIZE
from .oracle import make_price_oracle
from . import demo_data as demo


FEN = lambda f: f"{f / 100:.2f}"


def _dw(s: str) -> int:
    """显示宽度：中文等全角字符占 2 列。

    不做这个的话，f-string 的 :<N> 按字符数补空格 ——
    「巨无霸」补到 10 列实际只占 6，「中份薯条」占 8，
    于是同一个循环里打出来的两行永远错位，表格越看越累。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """按**显示宽度**左对齐补空格（f-string 的 :<N> 做不到）。"""
    return s + " " * max(0, width - _dw(s))


def _clip(s: str, width: int) -> str:
    """按显示宽度截断，超出部分收成 `…`。

    为什么必须截：菜单里偶尔会有「香辣鸡腿堡中份薯条中杯可乐」这种超长品名，
    一个 26 列的名字能把整张表的金额列整体顶到右面去，
    前九行辛辛苦苦对齐的列全废了。宁可截掉尾巴，也不让表格散架。
    """
    if _dw(s) <= width:
        return s
    import unicodedata

    out = ""
    used = 0
    for ch in s:
        w = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if used + w > width - 1:  # 留一列给 …
            break
        out += ch
        used += w
    return out + "…"


def _dev_note(text: str) -> None:
    """只给开发者看的注脚 —— 正常输出里不该出现。

    「含卡费口径」「真实值不是估算」「接口返回的是字符串，需自行解析」
    这类话都是作者写给自己的保证，对一个只想省几块钱的顾客纯属噪音：
    他不知道什么是卡费口径，也不关心接口返回的是字符串还是数字。

    真要排查时设 `MCD_BUTLER_DEBUG=1` 就能看到，而且走 stderr，
    不会污染 `>` 重定向出来的结果文件。
    """
    if os.environ.get("MCD_BUTLER_DEBUG", "").strip() not in ("", "0"):
        print(text, file=sys.stderr)


def _print_search_note(converged: bool, budget: int) -> list[str]:
    """把「这次到底有没有试遍所有可能」如实告诉用户，顺便给下一步。

    为什么要说这件事：`--budget` 默认 40，购物车 6 件商品 8 张券以上基本
    必然花光。以前屏幕上只有一句「算价接口共调用 40 次（上限 40）」——
    那是给开发者的 telemetry：用户既看不懂，也看不出「所以这次没算完」，
    只会拿着那个数字下单，然后发现柜台那边还能更便宜。

    拆成纯函数是为了能离线断言：埋在某条命令的收尾里时，
    就算哪天有人把它删了，一条自检都不会红。
    """
    if converged:
        return []
    lines = [
        "",
        "这次没把所有组合都试完 —— 上面是到目前为止找到的最低价，",
        f"但不保证没有更便宜的了。想试得更全，加 --budget {budget * 2} 再跑一次。",
    ]
    for line in lines:
        print(line)
    return lines


def _snapshot_saved_note(path) -> str:
    """快照存好后对用户说的那一句。

    只说「存进本机账本」，**不打印路径**：家目录里含 Windows 用户名
    （C:\\Users\\张三\\.mcd-butler\\），那不是使用者该暴露给别人的信息。
    抽成纯函数是为了能断言 —— 埋在 try/except 里时没人管得到。
    """
    # 不用 `del path` 把它抹掉：那样一旦有人把路径写回文案，函数是直接
    # NameError 崩掉，而不是安静地打一行含用户名的话 —— 崩溃反而更难查。
    # 留着不用，让变异测试能「正常地」把它抓出来。
    _ = path
    return "\n已存进本机账本（~/.mcd-butler/），下次可用 replay 对比这单的价格变动。"


def _replay_source(order: dict) -> str:
    """「复刻的是哪一单」的人话描述。

    只给日期，**不给订单号**：订单号能在客服系统反查到手机号 / 收货地址 /
    支付流水，屏幕上多印这一串等于把整单隐私摊开。
    """
    return f"历史订单（{order.get('createTime', '')[:10]}）"


def _format_quote_row(q, comparable: bool) -> str:
    """compare 表格的一行数据。

    金额三列一律带 ¥ —— 裸 "%9.2f" 的话用户看不出 36.00 是元还是分。
    """
    if not comparable:
        # 缺货和「接口没给距离」是两回事，屏幕上一律写「不参与」的话，
        # 用户会以为这家店东西不全，其实是距离不知道 —— 那是两件不同的事实。
        return (
            _pad(q.store_name[:22], 24)
            + f"{'—' if q.distance_unknown else q.distance_m:>6}"
            + f"{q.reason_uncomparable:>12}"
            + f"{'—':>9}"
            + f"{'不参与':>10}"
        )
    return (
        _pad(q.store_name[:22], 24)
        + f"{q.distance_m:>6}m"
        + f"{q.travel.minutes:>7}分"
        + f"¥{q.paid_fen / 100:>8.2f}"
        + f"¥{q.travel.money_fen / 100:>8.2f}"
        + f"¥{q.total_fen / 100:>9.2f}"
    )


def _format_cart_line(
    name: str, price_fen: int, *, qty: str = "", suffix: str = "", indent: int = 2
) -> str:
    """购物车/菜单里「一件商品」那一行。

    商品名一律走 `_pad` 按**显示宽度**补空格：f-string 的 `:<18` 按字符数补，
    中文一个字占两列，于是「巨无霸」和「麦辣鸡腿堡」的价格列永远差两格 ——
    同一个列表里每一行都错位，看着就累。抽成函数是为了能断言。
    """
    return f"{' ' * indent}- {_pad(_clip(name, 18), 18)}{qty}¥{FEN(price_fen)}{suffix}"


# compare 一次要对每家店各打 2 次非算价接口，不设上限会静默吃满配额。
MAX_COMPARE_STORES = 20
# 一家店至少要有这么多次算价，爬山才爬得动；少于这个数搜出来的「最优」
# 基本等于贪心第一刀，拿它去跟别家比没有意义。
MIN_CALLS_PER_STORE = 6


def _split_budget(budget: int, n_stores: int) -> tuple[int, int]:
    """把总预算均分给 n 家店，返回 (每家多少次, 实际参与比价的店数)。

    两条规矩：

    1. **各家必须一样多。** 共用一份预算时最近的店先跑，爬山 + 重启能吃掉
       大半额度，后面的店只剩残余额度草草爬几步 —— 到手价系统性偏向近店，
       而屏幕上没有任何一行说明各家搜索深度不同。比价的前提是同一把尺子。

    2. **总数不许超过 --budget。** 早先写的是 `max(6, budget // n)`，
       于是 `--budget 60 --top 20` 实际能打 120 次，是用户设的上限的两倍，
       而屏幕上还印着「均分」—— 自己打自己的脸。
       预算不够就少比几家（只比最近的），并把这件事说清楚。
    """
    n = max(1, n_stores)
    per = budget // n
    keep = n_stores
    if per < MIN_CALLS_PER_STORE and n_stores > 1:
        keep = min(n_stores, max(1, budget // MIN_CALLS_PER_STORE))
        per = budget // max(1, keep)
    return per, keep


def clamp_top(top: int) -> int:
    """把 compare 的 --top 夹回 1..MAX_COMPARE_STORES。

    给 0 或负数时早先走的 else 分支完全不截断，等于拿全部门店参与比价 ——
    每家店 2 次非算价接口不计入 Budget，屏幕上「共调用 N 次」跟真实请求数对不上，
    600 次/分钟的配额却照吃不误。
    """
    if top < 1:
        return 1
    return min(top, MAX_COMPARE_STORES)


def _advise_within(days: int) -> int:
    """`advise --days N` / `campaign --days N` 往活动日历里传的窗口。

    早先这个数在 campaign 内部写死 7，跟命令行脱钩：用户改了窗口，报告里还是 7 天。
    """
    return max(1, days)


def _meal_payload(store_code: str, code: str) -> dict:
    """`query-meal-detail` 的入参。

    两个反直觉的点，都是从报错里试出来的：
    - 键是 `code` 不是 `productCode`（传 productCode 接口不认）
    - 到店自取**不能**传 `beCode`（传了会报错）
    单独成函数，是为了让这两条能被离线断言到。
    """
    return {
        "storeCode": store_code,
        "orderType": 1,
        "beType": 1,
        "code": code,
    }


# ==================================================================== demo

def run_demo(show_all: bool = False) -> int:
    """离线演示：默认只跑**人人都用得到**的那条链路。

    为什么默认是两段而不是六段：
        demo 是路人的第一印象。早先它一口气喷六段（黄历、券包体检、
        活动日历、麦门段位……），读者还没看懂「券挂到哪件商品」这条主线，
        就被一堆玩法冲散了 —— 演示堆料等于没演示。
        所以默认只跑「购物车 + 券 → 逐张试出真实价值 → 挂上去 → 省了多少」
        和「复刻上一单」，玩法向的四段留给 `demo --all`。
    """
    print("麦门管家 · 离线演示（无需 Token · 演示数据）")
    print("-" * 46)

    cart = [
        demo.DEMO_MENU[0],  # 巨无霸
        demo.DEMO_MENU[2],  # 中份薯条
        demo.DEMO_MENU[4],  # 中杯可乐
        demo.DEMO_MENU[5],  # 麦旋风
    ]
    coupons = demo.DEMO_COUPONS

    print("\n购物车")
    name_w = max(_dw(i.name) for i in cart)
    for item in cart:
        print(f"  {_pad(item.name, name_w + 2)}¥{FEN(item.unit_price_fen)}")
    subtotal = sum(i.unit_price_fen for i in cart)
    print(f"  {_pad('合计', name_w + 2)}¥{FEN(subtotal)}")

    by_code = {m.product_code: m for m in demo.DEMO_MENU}
    print(f"\n可用券（{len(coupons)} 张）")
    title_w = max(_dw(c.title) for c in coupons)
    for c in coupons:
        names = "、".join(by_code[code].name for code in sorted(c.product_codes))
        print(f"  {_pad(c.title, title_w + 2)}适用 {names}")

    # 40 与 CLI 的 --budget 默认值一致。早先这里写 60，于是演示打出
    # 「共调用 42 次」，而 README 写的是「默认 40 次」—— 读者会以为程序
    # 超了自己的上限。演示不联网，但数字口径必须跟真实命令对得上。
    optimizer = CouponOptimizer(demo.demo_oracle, max_calls=40)
    best = optimizer.optimize(cart, coupons, demo.DEMO_WITH_ORDER_OPTIONS)

    # 早先这里写「每张券都真实试算过」。但这是一次**离线演示**：
    # 菜单、券、价格全是仓库里写死的演示数据，一个请求都没发过。
    # 拿「真实」二字形容它，等于教用户把演示当实测 ——
    # 而他一旦照着演示里那个价去柜台，落差会直接算在这个项目头上。
    print("\n怎么挂最省（演示数据上逐张试算，不是真实门店的价格）")
    if best.price is None:
        # demo 是离线演示，用户没有 --budget 可调。顶层那句「加 --budget」的
        # 通用提示在这里是错的（照着敲会报「无法识别的参数」），必须本地化。
        print("\n演示数据这次没算出价格，请把这条反馈到 issue。")
        return 1

    # 逐件列出到手价，并显式写出随单购那一档减了多少。
    # 没有这段时，屏幕上是「原价 ¥56.00 → 到手 ¥36.00，省下 ¥20.00」，
    # 而分项加出来只有 ¥42.00 —— 差的 ¥6.00 没有任何一行解释。
    # 读者按计算器加不出结论，第一反应是"这工具在编数字"。
    per_item = 0
    for idx, item in enumerate(cart):
        ci = best.mapping.get(idx)
        sub = optimizer.oracle([item], Assignment(mapping={0: ci} if ci is not None else {}))
        per_item += sub.price_fen
        title = coupons[ci].title if ci is not None else "没挂券"
        print(
            f"  {_pad(item.name, name_w + 2)}¥{FEN(item.unit_price_fen):>5}"
            f"  ← {_pad(title, title_w + 2)}→ ¥{FEN(sub.price_fen):>5}"
        )
    print("  " + "-" * 46)
    print(f"  挂券后小计 ¥{FEN(per_item)}")

    wo_gain = 0
    if best.with_order is not None:
        no_wo = optimizer.oracle(cart, Assignment(mapping=best.mapping, with_order=None))
        wo_gain = no_wo.price_fen - best.price.price_fen
        print(f"  随单购「{best.with_order.label}」再减 ¥{FEN(wo_gain)}（卡费另算）")
        if per_item != no_wo.price_fen:
            print(
                f"  （注意：各件单独算合计 ¥{FEN(per_item)}，整单一起算是 "
                f"¥{FEN(no_wo.price_fen)} —— 券有跨件门槛时不能简单相加）"
            )
    # 一行收束：原价/到手/省下/试算次数。不再单打「不用券直接买」一行 ——
    # 那行的信息（¥56.00、差 ¥20.00）跟原价和省下完全重复，纯占屏幕。
    print("  " + "-" * 46)
    # 「省下」必须把随单购那一段单独挑出来说。
    # 写成一个数（¥20.00）时，里面有 ¥6.00 是随单购带来的，而卡费还没扣 ——
    # 本项目自己立的规矩是「一律以裸买价为基准，不拿卡费给自己脸上贴金」，
    # 一行「省下 ¥20.00」正好坏了这条规矩。
    print(
        f"  原价 ¥{FEN(best.price.original_fen)} → 到手 ¥{FEN(best.price.price_fen)}"
        f"，省下 ¥{FEN(best.price.saved_fen)}"
    )
    if wo_gain:
        print(
            f"  其中 ¥{FEN(wo_gain)} 是随单购带来的，卡费还没扣进去"
            f"（不随单购是 ¥{FEN(best.price.price_fen + wo_gain)}）"
        )

    print("\n复刻上一单（同样的单，今天再买多少钱）")
    _demo_replay()

    if not show_all:
        print("\n  就这两段：把购物车和券丢进来，算出怎么挂最省。")
        print("  还有黄历 / 券包体检 / 活动日历 / 段位，想看就跑：")
        print("    python -m src.cli demo --all")
        return 0

    # 不打外层段落标题：黄历/体检/日历各自 render 的第一行就是自己的标题，
    # 再套一层就变成「麦门黄历 / 麦门黄历 · 2026-10-12」这种重复。
    # 早年这里还用 ===== 框着，和模块内部的 ═══ 框叠成两套横线，更乱。
    _demo_almanac()

    print("\n")
    _demo_audit()

    print("\n")
    _demo_campaign()

    print("\n")
    _demo_ranks()
    return 0


def _demo_campaign() -> None:
    """活动日历的离线演示。

    这个功能是唯一一个**必须联网才看得到**的 —— 没这条演示，
    路人 clone 下来跑 `demo` 根本不知道还有「择日」这回事。
    """
    from datetime import date

    from .campaign import advise_line, parse_campaign_markdown, render

    # 这里的 days 是解析出来的**活动列表**，不是天数 —— 演示窗口固定 7 天。
    # （曾因为变量名撞车把列表传进 _advise_within，直接 TypeError 崩掉整段演示。）
    days = parse_campaign_markdown(demo.campaign_demo_markdown())
    print(render(days, today=date.today(), within=7, max_per_day=3))
    for line in advise_line(days, date.today()):
        print(f"  {line}")
    print("\n  （演示报文，结构照搬真实返回；真实门店跑 `python -m src.cli campaign`）")


def _demo_almanac() -> None:
    """黄历的离线演示：每一条宜忌都来自 demo_oracle 的算价（演示数据）。

    `now` 必须跟着真时钟走（只把时段钉在午市，让输出可复现）。
    早先这里整个写死 `datetime(2026, 10, 11, 12)`，后果是**从第二天起
    demo 的黄历永远印 2026-10-11**，而同一屏里的活动日历用的是真今天
    —— 两块直接对不上。
    """

    from .almanac import build as build_almanac
    from .almanac import render as render_almanac
    from .persona import butler_almanac

    day = build_almanac(
        list(demo.DEMO_MENU),
        demo.DEMO_COUPONS,
        demo.demo_oracle,
        Budget(40),
        now=datetime.now().replace(hour=12, minute=0, second=0, microsecond=0),
    )
    best = max((r.saved_fen for r in day["readings"]), default=0)
    print(render_almanac(day, butler_almanac(best, len(day["expiry"]))))
    # 两句话都必须说，缺一句就是在骗：
    # ① 时段被钉在 12:00（为了每次跑出来一致），所以它印的「午市」
    #   未必是你现在所处的时段 —— 黄历自己的原则就是「先把现在几点说清楚」，
    #   演示这边做了妥协就必须把妥协写出来。
    # ② 数据是演示数据，不是真实门店。
    print(
        "  （每条宜忌都在演示数据上算过价；为让输出可复现，"
        "演示把时间钉在当天 12:00，所以这里写的「午市」不一定是你现在的时段。"
        "真实门店跑 `python -m src.cli almanac`）"
    )


def _demo_audit() -> None:
    """券包体检的离线演示。

    用一套带「门槛规则」的专用演示数据：真实接口就是这样 ——
    不够门槛它不报错也不提示，就是不给你减，只能一件一件试出来。
    """
    from .audit import audit_coupons, render

    verdicts = audit_coupons(
        demo.audit_oracle,
        demo.AUDIT_MENU,
        demo.AUDIT_COUPONS,
        lambda cart: demo.AUDIT_WITH_ORDER_OPTIONS,
        limit=10,
    )
    print(
        render(
            verdicts,
            # 必须换一个店名：这一段的菜单和券池是**另一套演示数据**
            # （带门槛规则，用来演「够不到门槛一分不减」）。
            # 沿用上面那家店的名字，读者会以为同一家店的巨无霸券
            # 一会儿立减 5 元一会儿立减 8 元 —— 那是首页在编数据的观感。
            store_name="演示餐厅·二号店（另一套券池）",
            menu_count=len(demo.AUDIT_MENU),
            calls=sum(v.calls for v in verdicts),
            total_coupons=len(demo.AUDIT_COUPONS),
        )
    )
    print("  （演示数据；真实门店跑 `python -m src.cli audit`）")


def _demo_ranks() -> None:
    """段位体系的离线展示。真人跑 `brag` 读的是本机快照。"""
    from .persona import RANKS, progress_bar, rank_of

    print("  麦门段位")
    for need, title, comment in RANKS:
        r = rank_of(need)
        print(
            f"  ¥{need / 100:>7.0f} 起　{_pad(title, 8)}{progress_bar(r['index'], r['max_index'])}　{comment}"
        )
    print("\n  段位来自本机快照累计（~/.mcd-butler/），不联网、不上传。")
    print("  看自己的账本：`python -m src.cli brag`")


def _distance_of(store: dict) -> tuple[int, bool]:
    """门店字典里的距离（米），以及**接口到底给没给**。

    返回两个值是刻意的：只返回一个 int 时，「没这个字段」只能填 0，
    于是「不知道有多远」被当成「就在你脚下」——而路费是综合成本里最大的
    一项，一家距离未知的店会靠一个编出来的 ¥0 路费排到第一名。

    字符串不算数：接口偶尔把距离写成 "1200"，那是另一个口径，
    当成数字用等于把没验证过的格式喂进路程模型。
    """
    raw = store.get("distance")
    if isinstance(raw, bool):  # bool 是 int 的子类，别把 True 当成 1 米
        return 0, False
    if isinstance(raw, (int, float)):
        return int(raw), True
    return 0, False


def _due_today_report(mine: list[dict], lookup_error: BaseException | None) -> list[str]:
    """「今天有没有券要作废」这一段要打印的行。

    抽成纯函数是为了它能被断言 —— 这段逻辑的保护曾经是零：
    变异测试把它整段删掉，118 条自检一条都没红。而它管的恰恰是
    **唯一不可逆**的那件事：券过期作废，错过了就真没了。
    """
    if lookup_error is not None:
        return [
            f"⚠️ 卡包这次没查到（{lookup_error}），今天有没有券要作废我没法保证。",
            "",
        ]
    due = [c for c in mine if c.get("due_today")]
    if due:
        out = ["🚨 今天内到期，不用就作废："]
        for c in due:
            out.append(f"   - {c['title']}　{c['valid_text']}")
        out.append("")
        return out
    if mine:
        return [f"卡包 {len(mine)} 张，没有今天到期的。", ""]
    return []


def _condition_warning(cart) -> list[str]:
    """购物车里「标价不是你能拿到的价」的那些件，单独喊一声。

    这是本项目最刺痛的一条真发现（标价 ¥29、不满足条件要按 ¥57 结账），
    埋在行尾的「← …」注释里没人会看见 —— 用户是在柜台前才发现价不对的。
    """
    cond = [it for it in cart if 0 < it.unit_price_fen < it.list_price_fen]
    if not cond:
        return []
    worst = max(cond, key=lambda i: i.list_price_fen - i.unit_price_fen)
    return [
        f"\n⚠️ 这一单有 {len(cond)} 件是「条件价」，标价不是你能拿到的价：",
        f"   最狠的是「{worst.name or worst.product_code}」——"
        f"标价 ¥{FEN(worst.unit_price_fen)}，不满足条件要按 ¥{FEN(worst.list_price_fen)} 结账"
        f"（差 ¥{FEN(worst.list_price_fen - worst.unit_price_fen)}）。",
    ]


def _count_working_coupons(oracle, cart, best) -> int:
    """最优解里有几张券**真的**减了钱。

    `len(best.mapping)` 数的是「挂上去的券」，不是「减了钱的券」。
    券与随单购互斥时，挂上去的那张会在整单里被扣回去，
    屏幕上却还印着「用了 N 张券」——用户照着去 App 里挂，一分钱不会少。

    判据是把每一张单独摘掉再算一次价：总价**变高**才说明它真的有用。
    """
    if not best.mapping:
        return 0
    working = 0
    for item_idx in best.mapping:
        without = {k: v for k, v in best.mapping.items() if k != item_idx}
        try:
            p = oracle(cart, Assignment(mapping=without, with_order=best.with_order)).price_fen
        except Exception:  # noqa: BLE001 - 数不清就说不清，别让演示崩在这里
            continue
        if p > best.price.price_fen:
            working += 1
    return working


def _how_to_order(cart, coupons, best, converged: bool = True, now=None) -> list[str]:
    """把最优解翻译成「在 App 里手怎么点」。

    为什么必须有这一段：算出「巨无霸挂 A、薯条挂 B」之后，用户还是得
    切回小程序、凭记忆把这一单重放一遍。少了这一段，工具只给了答案，
    没给做法 —— 真实点单链路里，这就是最后一公里，断了等于没算。

    只说**确定的**事：加什么、每张券挂在哪件。不猜 App 的界面长什么样。

    `now` 给了就在末尾补一句时效 —— 算价时刻不等于下单时刻，
    这条不说，用户拿着 10:29 算好的早市价在 10:31 去结账，一定多付钱。
    """
    from datetime import datetime as _dt

    lines: list[str] = []
    goods = "、".join(
        f"{it.name or it.product_code}×{it.quantity}" for it in cart
    )
    lines.append(f"  1) 加购：{goods}")
    if best.mapping:
        steps = []
        for item_idx, ci in sorted(best.mapping.items()):
            it = cart[item_idx]
            steps.append(f"{it.name or it.product_code} ← 「{coupons[ci].title}」")
        lines.append("  2) 结算页把券逐张挂到对应商品：" + "；".join(steps))
    else:
        lines.append("  2) 这一单没有券能挂上，直接下就是最便宜的")
    if best.with_order is not None:
        # 「卡费另算」四个字不够：卡费是**花出去退不回来**的钱，
        # 而且下一单想再享这个价还得再买一次。只说"另算"，
        # 用户会以为这是一次性小钱，实际上它是这单的真实成本之一。
        lines.append(
            f"  3) 这个价要随单购「{best.with_order.label}」才拿得到 —— "
            "卡费另付、**买了不退**，下一单还得再买一次才享得到"
        )
    if isinstance(now, _dt):
        # 算价时刻 ≠ 下单时刻。早市 10:30 收、夜市 21:00 起，
        # 跨过那个点，屏幕上这个价就不是柜台那个价了。
        lines.append(
            f"  ⚠️ 这个价是 {now:%H:%M} 算的。拖过时段边界（早市 10:30 收、"
            "夜市 21:00 起）就不是这个价了 —— 到柜台前再看一眼几点"
        )
    if not converged:
        lines.append("  （这一单还没搜到底，照着做之前可以先 --budget 调大再看一眼）")
    return lines


def _demo_replay() -> None:
    """复刻历史订单，并给出「今天再买一次」的到手价对比。

    对比口径必须是同为到手价：
        上次实付（含券）  vs  今天用引擎算出的最优到手价（含券）
    拿「上次实付」比「今天菜单原价」是苹果比橘子，会被一眼看穿。
    """
    optimizer = CouponOptimizer(demo.demo_oracle, max_calls=60)

    for order in demo.DEMO_ORDER_HISTORY:
        date = order["createTime"][:10]
        print(f"\n  {date[5:]} · {order['storeName']}")

        cart: list[CartItem] = []
        for p in order["products"]:
            cur = demo.DEMO_MENU_BY_CODE[p["productCode"]]
            cart.append(
                CartItem(
                    product_code=cur.product_code,
                    name=cur.name,
                    quantity=p["quantity"],
                    unit_price_fen=cur.unit_price_fen,
                )
            )
            snap = demo.DEMO_PRICE_SNAPSHOT.get(p["productCode"], {})
            then = snap.get(date)
            flag = ""
            if then is not None and cur.unit_price_fen != then:
                # 只印一个 "+1.00" 读者不知道这是什么：涨价？还是比上次贵？
                # 把「当时多少钱 → 现在多少钱」两端都写出来，才加得起来。
                flag = f"   当时 ¥{FEN(then)} → 现在 ¥{FEN(cur.unit_price_fen)}"
            qty = p["quantity"]
            item_line = _pad(f"{cur.name} ×{qty}", 16) if flag else f"{cur.name} ×{qty}"
            print(f"    {item_line}{flag}")

        # 今天的最优到手价
        best = optimizer.optimize(cart, demo.DEMO_COUPONS, demo.DEMO_WITH_ORDER_OPTIONS)
        if best.price is None:
            continue

        paid_before = order["realTotalAmount"]
        paid_now = best.price.price_fen
        diff = paid_now - paid_before
        verdict = "贵了" if diff > 0 else ("便宜了" if diff < 0 else "持平")
        mark = "  ⚠️" if diff > 0 else ""

        # 「已用尽 N 张券 + 麦金卡（随单购），其中…再减 ¥6.00（卡费另算）」
        # 这种括号套括号的话读者根本不读。收成一行「用了 N 张券 + X」，
        # 随单购的账在第一段寻优结果里已经摊开，这里不重复。
        print(
            f"    上次实付 ¥{FEN(paid_before)} → 今天 ¥{FEN(paid_now)}"
            f"，{verdict} ¥{FEN(abs(diff))}{mark}"
        )
        # 数的是**真的减了钱**的券，不是「挂上去的券」。
        # 券与随单购互斥时（实测：麦金卡不能与部分券同享），挂上去的那张
        # 会在整单里被扣回去，屏幕上却还写着「用了 1 张券」——
        # 用户照着去 App 里挂这张券，一分钱不会少，还以为自己记错了。
        work = _count_working_coupons(optimizer.oracle, cart, best)
        used = f"用了 {work} 张券"
        idle = len(best.mapping) - work
        if idle > 0:
            used += f"（另有 {idle} 张挂了但被随单购抵消，没减到钱）"
        if best.with_order is not None:
            used += f" + {best.with_order.label}"
        print(f"    （{used}）")


# ==================================================================== real

def run_tools() -> int:
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    tools = client.list_tools()
    print(f"MCP 暴露 {len(tools)} 个工具：\n")
    for t in sorted(tools, key=lambda x: x["name"]):
        print(f"  {t['name']}")
    return 0


def run_assets(claim: bool) -> int:
    """资产周报。默认只读；--claim 才领券。"""
    from .assets import build_report
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    print(build_report(client, claim=claim))
    return 0


def run_delivery() -> int:
    """麦乐送（外送）链路体检：这条通不通，通到哪一步。

    为什么要有这条命令：只点外送的人打开本工具，全程只能算到店价 ——
    他不知道为什么，也不知道该去哪儿补。与其让他以为「这工具不支持外卖」
    而走掉，不如把卡在哪一步说清楚。
    """
    from .delivery import describe
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    print("\n麦乐送（外送）\n")
    for line in describe(client):
        print(line)
    return 0


def run_campaign(days: int) -> int:
    """麦门活动日历：今天有什么活动，以及哪天有今天吃不到的新品。"""
    from .campaign import fetch, render
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    today = server_now(client).date()
    parsed = fetch(client)
    if not parsed:
        print("活动日历这次没返回可解析的内容（不影响其它功能）。")
        return 0
    print(render(parsed, today=today, within=days, max_per_day=4))
    return 0


def run_audit(
    city: str, keyword: str, budget: int, limit: int, store_code: str | None = None
) -> int:
    """券包体检：一张一张试出来，你手里的券到底值多少钱。

    跟 `advise` 的区别要说清楚：
        advise 回答「**这一单**怎么买最便宜」
        audit  回答「**这些券**本身值不值钱」
    后者是前者的前提 —— 拿着一堆废券去优化，优化得再好也是白搭。
    """
    from .audit import audit_coupons, render
    from .mcd_client import McdMcpClient
    from .oracle import make_price_oracle

    client = McdMcpClient()
    client.initialize()

    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        return 1
    store, store_code, menu, coupons = ctx

    if not coupons:
        print("这家门店当前没有可体检的券。")
        return 0

    shared = Budget(budget)
    oracle = make_price_oracle(client, store_code, coupons)
    verdicts = audit_coupons(
        oracle, menu, coupons, _with_order_options, budget=shared, limit=limit
    )
    calls = sum(v.calls for v in verdicts)

    print(
        render(
            verdicts,
            store_name=store.get("storeName", ""),
            menu_count=len(menu),
            calls=calls,
            total_coupons=len(coupons),
            now=server_now(client),
        )
    )

    audited = len(verdicts)
    if audited < len(coupons):
        print(f"  （券池共 {len(coupons)} 张，本次只体检了 {audited} 张；"
              f"想多查几张：`--limit {len(coupons)} --budget {max(budget, audited * 12 + 40)}`）")

    unknown = sum(1 for v in verdicts if v.status == "unknown")
    if unknown:
        print(f"  ⚠️ 有 {unknown} 张没查出来 —— 通常是算价接口限流或 Token 失效，不是这些券没用。")

    # 券池小不是算漏：门店券接口本来就只给这么多，卡包里的券拿不到券码。
    # 不点破，用户会以为「我明明有 6 张券，怎么只看到 1 张」。
    if len(coupons) <= 2:
        print()
        print("提示：这家店当前可挂载的券只有这几张，不代表你卡包里只有这几张。")
        print("      早餐券这类限时的券这里用不上。想看全部券和到期日：")
        print("        python -m src.cli assets")

    print("  门槛和「废券」都不是接口告诉我们的，是拿不同价位的商品一件件试出来的。")
    return 0


# 「现在几点」全项目只有一个出处（src/clock.py）。
# 曾经 `advise` 用服务端时间、`assets` 用本机时间，同一个账号同一时刻
# 一个报「今天有 N 张要作废」、另一个报「0 张」—— 而错的方向是漏报，
# 券过期不可逆。现在两边都调同一个函数，想再分叉也分叉不了。
# 这里保留 `_parse_server_datetime` 这个名字，是因为自检直接引用它。
from .clock import (  # noqa: E402  （re-export，见上）
    parse_server_datetime as _parse_server_datetime,
)
from .clock import server_now, server_now_with_source  # noqa: E402


def _load_menu_and_coupons(client, store_code: str):
    """给定 storeCode，载入菜单与券池。"""
    meals = client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
    menu = _parse_menu(meals)
    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": store_code, "orderType": 1, "beType": 1})
    )
    return menu, coupons


def _locate(client, city: str, keyword: str, store_code: str | None = None):
    """定位门店 + 载入菜单 + 拉券池。

    两条必须守住的规矩：

    1. **不许静默换店。** 接口按关键词搜不到时仍会返回一串"最接近"的门店，
       而门店决定了菜单、券池和最终价格。静默取第一条，用户拿着 A 店的价格
       跑到 B 店结账，只会得出一个结论：这软件骗人。所以匹配不上时必须明说。
    2. 给了 `store_code` 就直接用它，不再猜 —— 用户已经知道自己去哪家店。
    """
    if store_code:
        menu, coupons = _load_menu_and_coupons(client, store_code)
        if not menu:
            print("这家店今天没有可点的菜单。换一家店试试，或者换个关键词再搜。")
            return None
        return {"storeCode": store_code, "storeName": f"门店 {store_code}"}, store_code, menu, coupons

    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not isinstance(stores, list) or not stores:
        print("未找到门店，请换一个城市/关键词。")
        return None
    # 先找名字/地址里真带关键词的那家；找不到才退回第一条。
    # 无脑取第一条会出现「搜 A 店却按 B 店的价格算」这种最难发现的错。
    store = None
    if keyword:
        for s in stores:
            if isinstance(s, dict) and keyword in f"{s.get('storeName', '')}{s.get('address', '')}":
                store = s
                break
    if store is None:
        store = stores[0]
    if not isinstance(store, dict):
        print("门店数据格式异常。")
        return None
    store_code = store.get("storeCode")
    if not store_code:
        print("门店缺少 storeCode，无法继续。")
        return None

    # 只有「关键词没匹配上」且「最近的店也远得离谱」时才警告。
    # 搜地标（如"人民广场"）返回的店名里本就不含地标名，那是正常使用，不该报警。
    dist = store.get("distance")
    far = isinstance(dist, (int, float)) and dist > 1000
    if keyword and far and keyword not in f"{store.get('storeName', '')}{store.get('address', '')}":
        print(f"⚠️ 没有名字里带「{keyword}」的门店，最接近的一家也在 {int(dist)} 米外。")
        print(f"   实际用的是：{store.get('storeName', '未知门店')}")
        print(f"   下面算出来的价格、券和菜单**都是这一家的**。")
        print(f"   想换一家：把 --store 换成别的编码（本店就是 {store_code}）")
        print(f"   想看附近还有哪些店：python -m src.cli compare")

    for line in _closed_warning(store):
        print(line)

    menu, coupons = _load_menu_and_coupons(client, store_code)
    return store, store_code, menu, coupons


def _closed_warning(store: dict) -> list[str]:
    """门店没在营业时的提醒。单独抽出来是因为它必须能被离线断言。

    为什么非说不可：白跑一趟是**不可逆**的损失 —— 路费和时间都回不来，
    而接口明明给了 `businessStatus`。早先只有 `compare` 会剔打烊店，
    `optimize` / `advise` 走 `_locate` 这条路：用户拿着算好的价跑到一家
    关着门的店，只会得出一个结论 —— 这软件骗人。

    价照算（他要的就是这个价），但得先让他知道门可能是关着的。
    字段缺失时不喊：不知道就是不知道，不能把"没查到"说成"打烊"。
    """
    if store.get("businessStatus") is not False:
        return []
    return [
        f"⚠️ 接口说「{store.get('storeName') or '这家店'}」现在没在营业。",
        "   下面的价照算，但你得先确认它还开着门 —— 白跑一趟没人赔你。",
    ]


def _explain_coupon_scope(n: int) -> None:
    """解释「我卡包里明明有券，为什么这里没用上」。

    这是实测出来的接口事实，不是猜测：
    - `query-my-coupons`（卡包）返回的是**纯 Markdown**，里面**没有 couponId/couponCode**
    - 而 `calculate-price` 挂券必须靠券码，只有 `query-store-coupons` 会给
    所以卡包里的券不是"算漏了"，是程序上拿不到券码、挂不上去；
    还有一部分是时段限制（比如早餐券只在 05:00-10:29 可用）。

    不点破这一句，用户会以为引擎算错了 —— 这跟项目「把账算明白」的初衷相悖。
    """
    if n > 1:
        return
    if n == 0:
        print("提示：这家门店当前没有可挂载的券。")
    else:
        print("提示：本次只用到 1 张券，不代表你卡包里只有 1 张。")
    print(
        "      早餐券这类限时的券这里用不上。想看全部券和到期日：\n"
        "        python -m src.cli assets"
    )


def run_advise(
    city: str,
    keyword: str,
    budget: int,
    store_code: str | None = None,
    days: int = 7,
    items: list[str] | None = None,
) -> int:
    """一键顾问：今天该怎么点。

    一条硬原则：**管家不替主人点菜。**

    早先版本在没给 `--items` 时，会自己从菜单里拼一单出来 ——
    取「菜单里前几个被券覆盖的商品」，然后把它打印成「推荐这一单」。
    那不是推荐，是替主人做决定：拼出来的往往是他根本不想吃的东西，
    还得按这一单去记账、涨段位。一个真管家不会这么办事。

    现在没给 `--items` 时，只做管家该做的事：把主人手里的券在这家店盘一遍，
    说清每张券真实值多少钱、挂在哪个商品上最值，然后请主人自己开口说想吃什么。
    """
    from .assets import fetch_my_coupons
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()

    # 0. 先看活动日历：不急的话，改天可能更划算 —— 这比省几块钱更值钱
    # 时间基准必须用服务端时间：黄历已经这么做了，活动日历若用本机时间，
    # 时区一错就会把明天的活动说成今天（server_now 的注释里写过这个坑）。
    from .campaign import advise_line, fetch as fetch_campaign

    for line in advise_line(
        fetch_campaign(client), today=server_now(client).date(), within=_advise_within(days)
    ):
        print(line)
    print()

    # 1. 先看有没有今天就要作废的券 —— 这个比省几块钱更急
    #    时间基准同样必须是服务端时间：本机时区一错，
    #    「今天到期」这条最要紧的提醒就会漏报（原来这里用的是 datetime.now()）。
    lookup_error = None
    try:
        mine = fetch_my_coupons(client, now=server_now(client))
    except McpError as exc:
        # 卡包没查到就是没查到。原来这里 `except Exception: mine = []`，
        # 于是屏幕上「没有今天到期的」—— 用户以为自己没有快过期的券，
        # 实际是这一趟压根没问到服务器。到期作废是不可逆的，这条不能瞒。
        mine = []
        lookup_error = exc
    for line in _due_today_report(mine, lookup_error):
        print(line)

    # 2. 再算最优一单
    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx
    print(f"门店：{store.get('storeName', '未知门店')}　可用券 {len(coupons)} 张")
    _explain_coupon_scope(len(coupons))

    if not coupons:
        print("\n当前时段这张门店没有可用券。")
        print("提示：很多券是时段券（早餐/夜宵），换个时间再试；")
        print("      或先跑 `python -m src.cli assets --claim` 把能领的领了。")
        return 0

    oracle = make_price_oracle(client, store_code, coupons)

    # 2. 主人开口说了想吃什么 —— 就只算这一单
    if items:
        cart, missed = _pick_cart(menu, items, coupons)
        if missed:
            print("\n⚠️ 下列餐品在菜单里没找到，已跳过：")
            for m in missed:
                print(f"  - {m}")
            print("  用 `python -m src.cli menu --filter 关键词` 看看准确的品名。")
        if not cart:
            print("\n购物车为空，无法寻优。")
            return 1
        print("\n您这一单：")
        for item in cart:
            note = item.condition_note()
            suffix = f"   ← {note}" if note else ""
            print(
                _format_cart_line(
                    item.name or item.product_code,
                    item.effective_price_fen(),
                    suffix=suffix,
                    indent=3,
                )
            )
        shared = Budget(budget)
        optimizer = CouponOptimizer(oracle, max_calls=budget, budget=shared)
        baseline = optimizer.evaluate(cart, Assignment(mapping={}, with_order=None))
        best = optimizer.optimize(cart, coupons, _with_order_options(cart))
        if best.price is None:
            print("\n这单一件商品都没算成价，加 --budget 80 再试一次。")
            return 1
        print("\n最优挂法：")
        for line in best.describe(cart, coupons):
            print(f"   {line}")
        print("\n照着做：")
        for line in _how_to_order(cart, coupons, best, best.converged, now=server_now(client)):
            print(line)
        print(f"\n裸买 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
        print(f"真实省下 ¥{FEN(best.price.saved_vs(baseline.price_fen))}")
        _print_insights(oracle, cart, coupons, best, shared)
        _print_search_note(best.converged, budget)
        print(f"\n{_butler_close(best.price.saved_vs(baseline.price_fen), wrote=False)}")
        return 0

    # 3. 主人没说想吃什么 —— 小的不替您点菜，只把券盘清楚
    from .audit import audit_coupons

    print("\n您没说想吃什么 —— 小的不敢替您点菜。先把您手里的券在这家店盘一遍：\n")
    shared = Budget(budget)
    verdicts = audit_coupons(
        oracle,
        menu,
        coupons,
        _with_order_options,
        budget=shared,
        limit=max(1, len(coupons)),
    )
    worth = sorted((v for v in verdicts if v.value_fen > 0), key=lambda v: -v.value_fen)
    if not worth:
        print("  这家店的券，在这家店当前时段一件商品上都省不出钱。")
        print("  多半是时段不对（早餐券/夜宵券）或门槛没到 —— 不是券没用。")
    else:
        for v in worth:
            iname = (v.best_item.name or v.best_item.product_code) if v.best_item else "？"
            print(f"  · {v.coupon.title}")
            print(f"      挂「{iname}」最值，省 ¥{FEN(v.value_fen)}")
            if v.gap_fen > 0:
                print(f"      （券面 ¥{FEN(v.face_fen)}，实际只值这么多，落差 ¥{FEN(v.gap_fen)}）")
        dead = len(verdicts) - len(worth)
        if dead:
            print(f"\n  另有 {dead} 张在这家店省不出钱（时段/门槛/商品不匹配）。")
    print("\n想让小的算您这一单怎么点最省：")
    print('  python -m src.cli advise --items "板烧鸡腿堡三件套,薯条x2"')
    print("（不知道准确品名就先 `python -m src.cli menu --filter 关键词`）")
    return 0


def _butler_close(saved_fen: int, wrote: bool = False) -> str:
    """主流程收尾：先点评这一单，再报一下账本。

    累计数据来自本机快照，读不出来（权限/损坏）就只说这一单，
    绝不因为账单读不到而把整单结果搞崩。

    口径必须分开说：测算出来的叫「省钱空间」，真实成交省下的才叫「省下」。
    混在一起报一个「累计省了 ¥XX」，用户拿去跟朋友说，会被一句
    「你哪单省的」问住 —— 那不是他省的钱。
    """
    from .persona import butler_saved, rank_of, summarize
    from .snapshot import load_all

    lines = [butler_saved(saved_fen)]
    try:
        s = summarize(load_all())
    except Exception:
        s = None
    if s and s.get("orders"):
        est_n = s.get("est_orders", 0)
        est_fen = s.get("est_saved_fen", 0)
        real_n = s.get("real_orders", 0)
        real_fen = s.get("real_saved_fen", 0)
        # 段位必须和 `brag` 用同一把尺子 —— **真实成交的累计实付**。
        # 早先这里传的是 real_saved_fen：历史订单只记得住实付，裸买价无从得知
        # 所以记的是 0，real_saved 恒为 0 —— optimize 收尾于是永远印最低档
        # 「麦门路人」，而同一个账本 `brag` 印的是另一档。一个用户，两个段位。
        r = rank_of(s.get("real_paid_fen", 0))
        parts = []
        if est_n:
            parts.append(f"{est_n} 次测算，找出 ¥{est_fen / 100:.2f} 的省钱空间（尚未成交）")
        if real_n:
            if real_fen > 0:
                parts.append(f"{real_n} 笔真实成交，真省下 ¥{real_fen / 100:.2f}")
            else:
                # 不能印「真省下 ¥0.00」——那是在说他一分没省。
                # 事实是这一段**无从得知**：历史订单里没有当时的裸买价，不编。
                parts.append(
                    f"{real_n} 笔真实成交，累计实付 ¥{s.get('real_paid_fen', 0) / 100:.2f}"
                    "（省了多少无从得知，不编）"
                )
        if parts:
            lines.append("账上：" + "；".join(parts) + "。")
        lines.append(f"段位「{r['title']}」只按真实成交算 —— 测算不算数，刷不动。")
        if not wrote:
            lines.append("（这一单是测算，没记进账本；要记就加 --save）")
        lines.append("想看完整账单和可晒的卡片：`python -m src.cli brag`")
    return "\n".join(lines)


def run_almanac(city: str, keyword: str, budget: int, store_code: str | None = None) -> int:
    """麦门黄历：今天宜吃什么、忌吃什么。

    和别的"趣味黄历"唯一的区别是 —— 这里的每一条宜忌都真的打过算价接口。
    宜：挂这张券，接口真的给便宜了这么多钱。
    忌：要么没券可挂（白付原价），要么挂上去反而更贵（随单购把卡费算进来了）。
    """

    from .almanac import build as build_almanac
    from .almanac import render as render_almanac
    from .mcd_client import McdMcpClient
    from .persona import butler_almanac

    client = McdMcpClient()
    client.initialize()

    print(f"定位门店：{city} / {keyword}")
    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx
    print(f"命中：{store.get('storeName', '未知门店')}　菜单 {len(menu)} 项，券 {len(coupons)} 张")
    if not coupons:
        print("\n这家门店当前没有可挂载的券，黄历算不出来。")
        print("小的不编 —— 换个时间或换家门店再来。")
        return 1

    oracle = make_price_oracle(client, store_code, coupons)

    shared = Budget(budget)
    day = build_almanac(menu, coupons, oracle, shared, now=server_now(client))
    best_saved = max((r.saved_fen for r in day["readings"]), default=0)
    print(render_almanac(day, butler_almanac(best_saved, len(day["expiry"]))))
    print("\n想直接照着点：python -m src.cli advise")
    return 0


def run_meal(
    city: str,
    keyword: str,
    name_kw: str,
    code: str | None,
    store_code: str | None = None,
) -> int:
    """餐品详情：这个套餐由什么组成、换一样要加（或减）多少钱。

    换项差价是**唯一一个用户自己能决定、而且方向可能是省钱**的操作：
    `diffPrice` 里带负号的那些，是白捡的钱。
    """
    from .mcd_client import McdMcpClient
    from .meal_detail import parse_meal_detail, render as render_meal

    client = McdMcpClient()
    client.initialize()
    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, _coupons = ctx

    if code:
        target = next((i for i in menu if i.product_code == code), None)
        if target is None:
            print("这家店没有这道餐品。不加 --code 可以直接按名字搜。")
            return 1
    else:
        hits = [i for i in menu if name_kw in (i.name or "")]
        if not hits:
            print(f"没有名字里带「{name_kw}」的餐品。用 `menu` 先看有哪些。")
            return 1
        # 有套餐优先看套餐 —— 单品的详情永远是空的，看了也白看。
        combo = [
            i for i in hits if "件套" in (i.name or "") or "套餐" in (i.name or "")
        ]
        target = (combo or hits)[0]
        if len(hits) > 1:
            print(
                f"（匹配到 {len(hits)} 件，取「{target.name}」；"
                f"要看别的请加上它的编号）"
            )
            print()

    # 入参是 `code` 不是 `productCode`；到店自取不传 beCode（传了会报错）。
    data = client.data("query-meal-detail", _meal_payload(store_code, target.product_code))
    detail = parse_meal_detail(data)
    if detail is None:
        print(f"「{target.name}」没返回详情（可能是不支持拆分的单品）。")
        return 1
    print(render_meal(detail, store_name=store.get("storeName", "")))
    return 0


def run_menu(city: str, keyword: str, filter_kw: str, limit: int, store_code: str | None = None) -> int:
    """列出门店菜单。带券的商品会被标出来——那是真正值得点的。"""
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()
    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx

    covered: set[str] = set()
    for c in coupons:
        covered |= set(c.product_codes)

    print(f"门店：{store.get('storeName', '未知门店')}")
    print(f"共 {len(menu)} 个品类，{len(coupons)} 张可用券\n")

    shown = 0
    for item in menu:
        if filter_kw and filter_kw not in (item.name or ""):
            continue
        if shown >= limit:
            print(f"\n（只显示前 {limit} 条，用 --limit 调整或加 --filter 缩小范围）")
            break
        price = item.effective_price_fen()
        note = f"  ← {item.condition_note()}" if item.needs_with_order else ""
        mark = " [有券]" if item.product_code in covered else ""
        print(f"  ¥{FEN(price):>6}  {item.name or item.product_code}{mark}{note}")
        shown += 1
    if shown == 0:
        print(f"没有匹配「{filter_kw}」的餐品。")
    return 0


def run_optimize(
    city: str,
    keyword: str,
    budget: int,
    # 默认**不**记账。原先默认每次都写快照，于是「输错店名随手一跑」
    # 「--budget 1 探测一下」也全被记成省钱记录，用户得再用 forget 手工擦。
    # 记账是用户主动认可的动作，得他点头（--save）才写。
    save: bool = False,
    items: list[str] | None = None,
    insights: bool = True,
    store_code: str | None = None,
) -> int:
    from .mcd_client import McdMcpClient

    client = McdMcpClient()
    client.initialize()

    print(f"定位门店：{city} / {keyword}")
    ctx = _locate(client, city, keyword, store_code)
    if ctx is None:
        print("未找到门店，请换一个关键词。")
        return 1
    store, store_code, menu, coupons = ctx
    print(f"命中：{store.get('storeName', '未知门店')}（{store.get('address', '未知地址')}）")
    print(f"菜单载入 {len(menu)} 个品类")

    print(f"门店可用券 {len(coupons)} 张")
    _explain_coupon_scope(len(coupons))

    oracle = make_price_oracle(client, store_code, coupons)

    if items:
        cart, missed = _pick_cart(menu, items, coupons)
        if missed:
            print("\n⚠️ 下列餐品在菜单里没找到，已跳过：")
            for m in missed:
                print(f"  - {m}")
            print("  用 `python -m src.cli menu --filter 关键词` 看看准确的品名。")
    else:
        cart = _build_cart(menu, coupons)
        # 必须说清楚这单是小的替您挑的：不标注的话，屏幕上那句「最优组合」
        # 会被当成「您想吃的这单的最优解」，而它其实是工具按「有券覆盖」
        # 随便凑的示例单 —— 拿它去下单，等于替主人做了决定。
        print(
            "\n（您没说想吃什么，下面这单是小的按「这家的券能挂哪些商品」凑的示例，"
            "\n  不是您想吃的。要算自己那一单：`--items \"板烧鸡腿堡三件套,薯条x2\"`）"
        )

    if not cart:
        print("购物车为空，无法寻优。")
        return 1

    print("\n购物车：")
    for item in cart:
        note = item.condition_note()
        price = item.effective_price_fen()
        suffix = f"   ← {note}" if note else ""
        qty = f"×{item.quantity} " if item.quantity > 1 else ""
        print(
            _format_cart_line(
                item.name or item.product_code, price, qty=qty, suffix=suffix
            )
        )

    for line in _condition_warning(cart):
        print(line)

    shared = Budget(budget)
    optimizer = CouponOptimizer(oracle, max_calls=budget, budget=shared)
    # 裸买基准用 evaluate 直接算：预算耗尽抛 BudgetExhausted、网络错抛 McpError，
    # 不被 try_evaluate 把瞬时抖动误判成「预算不足」而误导用户调大 --budget。
    baseline = optimizer.evaluate(cart, Assignment(mapping={}, with_order=None))
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    if best.price is None:
        print("\n这单一件商品都没算成价，加 --budget 80 再试一次。")
        return 1
    print("\n最优组合：")
    for line in best.describe(cart, coupons):
        print(f"  {line}")
    # 光给答案不算完事：用户还得切回 App 手工重放这一单。
    # 把「手怎么点」写出来，才是把结论送到能下单的地方。
    print("\n照着做：")
    for line in _how_to_order(cart, coupons, best, best.converged, now=server_now(client)):
        print(line)
    # 真实节省必须以「裸买价」为基准。
    # 不能用接口返回的 discount：随单购会把卡费计入 originalPrice
    # （实测 97.50 → 107.40，多出的 9.90 就是卡费），导致 discount 虚高。
    saved = baseline.price_fen - best.price.price_fen
    print(f"\n裸买价 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
    print(f"真实省下 ¥{FEN(saved)}")
    _dev_note(f"接口 discount 报 ¥{FEN(best.price.discount_fen)}（含卡费口径，仅作对照）")

    # 拆开「券省了多少 / 卡省了多少」。挤在一行总数里，用户按菜单价自己算
    # 加不出这个数，只会以为工具在编 —— 而且办卡是要花钱的，这笔必须单独看见。
    if best.with_order is not None:
        no_wo = optimizer.try_evaluate(
            cart, Assignment(mapping=best.mapping, with_order=None)
        )
        if no_wo is None:
            # `try_evaluate` 返回 None 有**两种**原因，混成一句"预算不足"是误导：
            # 用户照着去调大 --budget，而真实原因是限流 / 接口拒绝，
            # 调多大都一样。这里按剩余预算分开说。
            if optimizer.remaining <= 0:
                print("  （预算用完了，没能拆出券与随单购各自的贡献，--budget 调大可看到）")
            else:
                print(
                    "  （这一档算价没成功，没能拆出券与随单购各自的贡献 —— "
                    "不是预算问题，等一会儿重试即可）"
                )
        else:
            coupon_gain = baseline.price_fen - no_wo.price_fen
            wo_gain = no_wo.price_fen - best.price.price_fen
            print(f"  ├ 券本身省 ¥{FEN(coupon_gain)}")
            print(f"  └ 「{best.with_order.label}」再省 ¥{FEN(wo_gain)}（卡费另算，买卡前先算这笔）")

    if insights:
        _print_insights(oracle, cart, coupons, best, shared)
    _print_search_note(best.converged, budget)

    if save:
        from .snapshot import entry_from_cart, save as save_snapshot

        label = best.with_order.label if best.with_order is not None else ""
        try:
            path = save_snapshot(
                entry_from_cart(
                    cart=cart,
                    paid_fen=best.price.price_fen,
                    baseline_fen=baseline.price_fen,
                    # 存"真实节省"而不是接口 discount：后者含卡费口径，
                    # 会被当成"我这次省了这么多"读出去。
                    discount_fen=saved,
                    store_code=store_code,
                    store_name=store.get("storeName", ""),
                    city=city,
                    keyword=keyword,
                    coupon_titles=[coupons[ci].title for ci in best.mapping.values()],
                    with_order_label=label,
                )
            )
            print(_snapshot_saved_note(path))
        except OSError:
            # 快照只是锦上添花：写盘失败（权限/磁盘满）绝不该让已算完的结果崩掉。
            # 也不打印裸异常 —— "[Errno 28] No space left" 对路人毫无意义。
            print("\n（这次的结果没能存进账本，不影响上面的价格。）")
    else:
        print("\n（本次未存快照，账本里不会有这一单）")

    print(f"\n{_butler_close(saved, wrote=save)}")
    return 0


def run_compare(
    city: str,
    keyword: str,
    items: str,
    budget: int,
    mode: str,
    yuan_per_km: float | None,
    wage: float | None,
    top: int,
) -> int:
    """多门店比价 + 路程性价比：这单到底该去哪家店、值不值得跑这一趟。

    为什么需要这个命令
    ------------------
    券引擎只回答「这单最省几块」，它默认**去这家店是免费的**。
    现实里 144 米和 6.7 公里是两种完全不同的决策：
    省下 ¥5 但要走 20 分钟，对很多人来说并不划算。

    所以这里把接口真实返回的 `distance` 换算成时间和钱，
    让「优惠」和「路程」在同一个量纲上比 —— 这才是完整的一笔账。

    范围
    ----
    只做到店（含得来速）。外卖（麦乐送）价格与配送费取不到真值，暂不纳入，
    不做估算也不给对照入口 —— 宁可少一块，也不拿假数字冒充。
    """
    from dataclasses import replace

    from .mcd_client import McdMcpClient
    from .route import MODES, OVERHEAD_MIN, StoreQuote, rank_stores, travel_cost, verdict

    client = McdMcpClient()
    client.initialize()

    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not isinstance(stores, list) or not stores:
        print("未找到门店，请换一个城市/关键词。")
        return 1
    stores = [s for s in stores if isinstance(s, dict) and s.get("storeCode")]

    # 打烊的店先剔掉：推荐一家已经关门的店是硬伤，不是"排序靠后"能弥补的。
    # businessStatus 实测是布尔值（True=营业中）；字段缺失时不敢乱猜，按营业处理。
    closed = [s for s in stores if s.get("businessStatus") is False]
    if closed:
        names = "、".join(
            f"{s.get('storeName', '?')}（{s.get('businessStartTime', '')}-{s.get('businessEndTime', '')}）"
            for s in closed[:3]
        )
        print(f"（{len(closed)} 家门店当前打烊，已排除：{names}）")
    stores = [s for s in stores if s.get("businessStatus") is not False]

    # distance 是接口真实返回的（米）；个别门店可能没有该字段，排到最后
    stores.sort(
        key=lambda s: s.get("distance")
        if isinstance(s.get("distance"), (int, float))
        else 10**9
    )
    clamped = clamp_top(top)
    if clamped != top:
        print(f"--top 需在 1..{MAX_COMPARE_STORES} 之间，已按 {clamped} 处理。")
    stores = stores[:clamped]
    if not stores:
        print("附近没有正在营业的门店，换个关键词试试。")
        return 1

    print(f"定位 {city} / {keyword}：取附近 {len(stores)} 家门店参与比价")
    _dev_note("距离取接口返回的 distance 字段，单位米（真实值，不是估算）")

    # 用最近的店把「想吃什么」解析成商品编码，后面各家按同一组编码对齐，
    # 否则各家菜单品名不一致会导致比价失真。
    first_code = str(stores[0]["storeCode"])
    menu0 = _parse_menu(
        client.data("query-meals", {"storeCode": first_code, "orderType": 1, "beType": 1})
    )
    coupons0 = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": first_code, "orderType": 1, "beType": 1})
    )
    tokens = [t.strip() for t in items.split(",") if t.strip()] if items else []
    if tokens:
        cart0, missed = _pick_cart(menu0, tokens, coupons0)
        if missed:
            print("\n⚠️ 这些在最近的店里没找到，已跳过：")
            for m in missed:
                print(f"  - {m}")
    else:
        cart0 = _build_cart(menu0, coupons0)

    wants: list[tuple[str, int, str]] = [
        (it.product_code, it.quantity, it.name or it.product_code) for it in cart0
    ]
    if not wants:
        print("购物车为空，无法比价。")
        return 1
    print(f"\n比价标的（在最近的店解析，各家按同一组编码对齐）：")
    print("  " + "、".join(n for _, _, n in wants))

    # 每家店各分一份预算，**不能共用一份**。
    # 共用时最近的店先跑，爬山 + 2 轮重启能吃掉大半预算，后面的店只剩残余额度
    # 草草爬几步 —— 到手价因此系统性偏向最近的店，而屏幕上没有任何一行说明
    # 各家搜索深度不同。比价的前提是同一把尺子，所以必须均分并如实标出来。
    per_store, keep = _split_budget(budget, len(stores))
    if keep < len(stores):
        dropped = len(stores) - keep
        stores = stores[:keep]
        print(
            f"\n（--budget {budget} 只够给 {keep} 家店搜到同样深度，"
            f"远的 {dropped} 家不参与比价 —— "
            f"与其每家都浅尝辄止，不如少比几家比得准。想都比就调大 --budget）"
        )
    n_stores = len(stores)
    print(
        f"\n算价预算：每家 {per_store} 次（--budget {budget} 均分给 {n_stores} 家，"
        f"避免近店吃得深、远店吃得浅）"
    )
    quotes: list[StoreQuote] = []

    for st in stores:
        code = str(st.get("storeCode", ""))
        dist, dist_known = _distance_of(st)
        is_first = code == first_code
        menu = menu0 if is_first else _parse_menu(
            client.data("query-meals", {"storeCode": code, "orderType": 1, "beType": 1})
        )
        coupons = coupons0 if is_first else _parse_coupons(
            client.data("query-store-coupons", {"storeCode": code, "orderType": 1, "beType": 1})
        )
        by_code = {i.product_code: i for i in menu}
        cart: list[CartItem] = []
        missing: list[str] = []
        for pc, qty, name in wants:
            base = by_code.get(pc)
            if base is None:
                missing.append(name)
                continue
            cart.append(replace(base, quantity=qty))

        if missing:
            # 缺货的店不能跟齐全的比，那是不公平比较 —— 记下来但不参与排序
            quotes.append(
                StoreQuote(
                    store_code=code,
                    store_name=st.get("storeName", ""),
                    address=st.get("address", ""),
                    distance_m=dist,
                    travel=travel_cost(dist, mode, yuan_per_km, wage),
                    missing=tuple(missing),
                    distance_unknown=not dist_known,
                )
            )
            continue

        oracle = make_price_oracle(client, code, coupons)

        shared = Budget(per_store)
        optimizer = CouponOptimizer(oracle, max_calls=per_store, budget=shared)
        try:
            baseline = optimizer.evaluate(cart, Assignment(mapping={}, with_order=None))
            best = optimizer.optimize(cart, coupons, _with_order_options(cart))
        except BudgetExhausted:
            print(f"\n（预算用完，「{st.get('storeName', '')}」及之后未测算）")
            break
        if best.price is None:
            continue
        quotes.append(
            StoreQuote(
                store_code=code,
                store_name=st.get("storeName", ""),
                address=st.get("address", ""),
                distance_m=dist,
                paid_fen=best.price.price_fen,
                baseline_fen=baseline.price_fen,
                travel=travel_cost(dist, mode, yuan_per_km, wage),
                distance_unknown=not dist_known,
            )
        )

    if not quotes:
        print("一家店都没算出来，调大 --budget 再试。")
        return 1

    ranked = rank_stores(quotes)
    tm = MODES.get(mode, MODES["walk"])
    print(
        f"\n出行方式：{tm.label}（{tm.speed_m_per_min} 米/分，"
        f"起步 ¥{tm.base_fen / 100:.2f}，每公里 ¥{tm.per_km_fen / 100:.2f}，"
        f"另计 {OVERHEAD_MIN} 分钟进出店开销）"
    )
    if wage:
        print(f"时间成本：按时薪 ¥{wage:.0f}/小时把等待也折算成钱")

    # 表头和数据行必须都走 _pad：f-string 的 :<N 按**字符数**补空格，
    # 「人民广场店」(5字/10列) 和「麦当劳XX餐厅」会被补成不同的实际宽度，
    # 于是中文店名一长表头就和数据错开（实测差 7 列）。
    # 金额三列统一带 ¥ —— 裸 "%9.2f" 用户看不出 36.00 是元还是分。
    print("\n" + "-" * 58)
    print(
        _pad("门店", 24) + f"{'距离':>8}" + f"{'到手价':>10}"
        + f"{'路费':>9}" + f"{'综合':>10}"
    )
    print("-" * 58)
    for q in ranked:
        if not q.comparable:
            print(_format_quote_row(q, comparable=False))
            print(f"    缺少：{'、'.join(q.missing)}")
            continue
        print(_format_quote_row(q, comparable=True))
    print("-" * 58)
    print("（综合 = 到手价 + 路费）")

    print()
    print(verdict(ranked))
    return 0


def run_brag(with_link: bool = False) -> int:
    """麦门账单：段位 + 能直接晒出去的卡片。

    不需要 Token，也不联网 —— 数据全部来自本机快照（~/.mcd-butler/）。

    为什么要有这个命令：省钱这件事，**能跟朋友说的那一刻才有快感**。
    一个只能自己看的省钱工具，是传播不出去的。

    但卡片里绝不把"测算"说成"已省"：快照里绝大多数条目只是引擎算出的
    省钱空间，用户根本没下单。混着报出去，等于给用户一张他自己圆不上的成绩单。
    """
    from .persona import brag_card, butler_total, progress_bar, rank_of, summarize
    from .snapshot import load_all

    entries = load_all()
    if not entries:
        print("账上还空着。先让小的办一单：")
        print('  python -m src.cli optimize --items "板烧鸡腿堡三件套,薯条x2"')
        print("（跑完会自动记进本地快照，之后这里就有得看了）")
        return 1

    s = summarize(entries)
    # 段位只按真实成交的实付累计：测算可以靠重复跑命令刷上去，
    # 真实订单按订单号去重，刷不动。
    r = rank_of(s.get("real_paid_fen", 0))

    print("\n麦门账单（全部来自本机快照，不联网、不上传）\n")
    print(f"  累计测算可省   ¥{s['est_saved_fen'] / 100:.2f}　　共 {s['est_orders']} 次测算")
    if s["real_orders"] == 0:
        # 这行必须说：否则用户把「测算」当「已省」，晒出去会被朋友问住。
        print("  ⚠️ 其中 0 笔是真实成交 —— 这是「这么点能省多少」，不是已经省下的钱。")
    elif s["real_saved_fen"] > 0:
        print(
            f"  真实成交       {s['real_orders']} 笔，真省下 ¥{s['real_saved_fen'] / 100:.2f}"
        )
    else:
        # 真实成交的裸买价无从得知（账本里记的是 0，刻意不编），
        # 所以这里不能印「真省下 ¥0.00」——那是说他一分没省，而真实情况是不知道。
        print(
            f"  真实成交       {s['real_orders']} 笔，累计实付 ¥{s['real_paid_fen'] / 100:.2f}"
        )
        print("  （省了多少无从得知：历史订单里没有当时的裸买价，小的不替您编一个）")
    if s["top_store"]:
        print(f"  最常光顾   {s['top_store']}")
    if s["top_coupon"]:
        print(f"  最常用券   {s['top_coupon']}")
    if s["first_at"]:
        print(f"  记账起始   {s['first_at'][:10]}")

    print(f"\n  当前段位   {r['title']}  {progress_bar(r['index'], r['max_index'])}")
    print(f"             {r['comment']}　（按真实成交累计实付算，测算不涨段位）")
    if r["next"]:
        n = r["next"]
        if s["real_orders"]:
            print(f"  真实消费再累计 ¥{n['gap_fen'] / 100:.2f} 即可升到「{n['title']}」")
        else:
            print("  想升段位，得先有真实成交：`python -m src.cli replay` 导入您的历史订单。")
    else:
        print("  已满级 —— 您就是麦门本身。")

    print(f"\n  {butler_total(s['saved_fen'], s['orders'], s['real_orders'])}")
    print()
    print(
        brag_card(
            s["saved_fen"],
            s["orders"],
            r["title"],
            s["top_store"],
            s["top_coupon"],
            s["first_at"],
            real_orders=s["real_orders"],
            with_link=with_link,
        )
    )
    print("\n（上面这段可以直接复制粘贴去晒）")
    if not with_link:
        print("（默认不带仓库链接——朋友圈里裸链接容易被判广告；"
              "发 CSDN/知乎时用 `brag --with-link`）")
    print("\n不想要的记录可以撤回：`python -m src.cli forget --last`")
    return 0


def run_forget(last: int, all_: bool) -> int:
    """撤回账本里的记录。

    为什么必须有：optimize 默认把每一次测算都写进账本，于是
    「输错店名随手一跑」「--budget 1 探测一下」也被记成了省钱记录。
    用户要晒卡片之前，得能把自己不认的条目删掉 —— 否则那张卡片
    是一份他自己都解释不清的假成绩单。
    """
    from .snapshot import clear_all, delete_last

    if all_:
        n = clear_all()
        print(f"已清空本地账本，共移除 {n} 条记录。")
        return 0
    # max(last, 1) 会把「我一条都不想删」变成「删 1 条」。
    # --last 0 的语义是明确的：别动我的账本。
    if last <= 0:
        print("--last 需要是正整数；一条都不撤回，账本未改动。")
        return 0
    n = delete_last(last)
    if n < 0:
        # 读不出来 ≠ 空的。说成"空的"，用户会以为自己没数据、甚至以为清空
        # 已经成功，而数据其实还在盘上 —— 只是这一趟没解析出来。
        from .snapshot import SNAPSHOT_FILE

        print("账本读不出来，小的没敢动它（怕把还能救的数据弄没了）。")
        print(f"  它在 {SNAPSHOT_FILE}")
        print("  先复制一份备份，再打开看看是不是被别的程序写坏了。")
        return 1
    if n == 0:
        print("账本是空的，没有可撤回的记录。")
        return 1
    print(f"已撤回最近 {n} 条记录。用 `python -m src.cli brag` 看看现在的账本。")
    return 0


def _with_order_label(with_order) -> str:
    """随单购档位的名字；没选档位时返回空串。

    `WithOrderOption` 是 **frozen dataclass，不是 dict** ——
    判 `isinstance(x, dict)` 恒为 False。这个坑在 `oracle.py` 里踩过一次
    （随单购一次都没真的发出去过）；`run_replay` 是第二处残留：
    标签永远是空串，快照的 `with_order_label` 一栏永远空白，
    于是 `brag` 生成的卡片说不清这单到底靠什么省的钱。
    """
    return with_order.label if with_order is not None else ""


def _replay_baseline_fen(optimizer, cart: list[CartItem]) -> int | None:
    """这一单「一件券都不挂、也不买随单购」的裸买价（分）。

    算不出来返回 None —— 宁可不说，也不拿接口的 `original_fen` 顶上（理由见下）。

    不能拿接口返回的 `original_fen` 当裸买价：最优解一旦选了随单购档位，
    接口会把 **卡费** 算进 `originalPrice`，于是「省下的钱」凭空多出卡费那一截。
    实测同一单差 ¥9.90，而 `brag` 是对全量快照求和的 ——
    每跑一次 `replay`，累计节省就被虚高一次，而且用户看不出来。

    单独拆出来是因为：这段逻辑埋在 `run_replay` 里时没法离线断言，
    改回「拿接口的 original 当基准」也不会有任何一条自检变红。
    """
    try:
        return optimizer.evaluate(cart, Assignment(mapping={}, with_order=None)).price_fen
    except BudgetExhausted:
        # 预算被爬山吃光了。这行只是一条补充信息，不该把整份对比结果掀翻 ——
        # 早先它抛出去，屏幕上是「完整对比表 + 一句算价次数太少」，rc=3，
        # 而且后面的记账全不执行。宁可这一行不印。
        return None


def run_replay(store_code: str | None, list_only: bool, budget: int = 40) -> int:
    """历史订单复刻：把这单按今天的菜单和券重算一遍，报价格变动。

    数据源有两条，按优先级：
    1. 真实历史订单（order-list）—— 账号下过单才有，很多账号是空的；
    2. 本地快照 —— optimize 跑完自动存的「上次算过的那单」。

    比较口径统一为「上次实付」vs「今天同款购物车的最优实付」。
    不能直接比今天菜单的标价：那等于拿「用券后的价」比「不用的价」，
    是在骗自己。
    """
    from .mcd_client import McdMcpClient
    from . import snapshot as snap

    if list_only:
        entries = snap.load_all()
        if not entries:
            print("还没有快照。先跑一次 `python -m src.cli optimize` 生成。")
            return 1
        print(f"本地快照 {len(entries)} 条：")
        for i, e in enumerate(entries, 1):
            print(f"  {i}. {snap.describe(e)}")
        return 0

    client = McdMcpClient()
    client.initialize()

    source = ""
    paid_fen: int | None = None
    want: list[tuple[str, int]] = []  # (productCode, quantity)
    target_store = store_code

    # ---- 1. 真实历史订单
    try:
        data = client.data("order-list", {}) or {}
        orders = (data or {}).get("list") or []
    except Exception:
        orders = []

    real_order = None
    if orders:
        order = orders[0]
        real_order = order
        target_store = target_store or str(order.get("storeCode", ""))
        try:
            paid_fen = to_fen(order.get("realTotalAmount"))
        except (TypeError, ValueError):
            paid_fen = None
        for p in order.get("orderProductList") or []:
            code = str(p.get("productCode", ""))
            if code:
                want.append((code, int(p.get("quantity") or 1)))
        source = _replay_source(order)

    # ---- 2. 退回本地快照
    if not want:
        entry = snap.latest(target_store)
        if entry is None:
            print("既没有历史订单，也没有本地快照。")
            print("先跑 `python -m src.cli optimize --city 上海 --keyword 人民广场` 生成一单。")
            print("（离线演示：`python -m src.cli demo`）")
            return 1
        target_store = entry.get("store_code", "")
        paid_fen = entry.get("paid_fen")
        want = [(i["code"], int(i.get("qty") or 1)) for i in entry.get("items") or []]
        # 「上次实付」是笔什么钱，必须写在脸上：快照里绝大多数条目是测算
        # （只是"这么点能省多少"），不是真成交。拿测算值当"上次实付"去比，
        # 两个数都是算出来的，diff 也就没什么意义了 —— 用户会拿它做决策。
        kind = "真实成交" if entry.get("kind") == "order" else "测算（不是真花过的钱）"
        source = f"本地快照 {entry.get('saved_at', '')}　[{kind}]"

    print(f"复刻来源：{source}")
    print(f"目标门店：{target_store}")

    meals = client.data("query-meals", {"storeCode": target_store, "orderType": 1, "beType": 1})
    menu = _parse_menu(meals)
    by_code = {i.product_code: i for i in menu}
    print(f"今日菜单 {len(menu)} 个品类")

    cart: list[CartItem] = []
    missing: list[str] = []
    for code, qty in want:
        item = by_code.get(code)
        if item is None:
            missing.append(code)
            continue
        cart.append(CartItem(**{**item.__dict__, "quantity": qty}))
    if not cart:
        print("这单里的商品今天一个都点不到了，无法对比。")
        return 1
    if missing:
        print(f"⚠️ {len(missing)} 个商品已从今日菜单下架：{', '.join(missing)}")

    print("\n复刻购物车：")
    for item in cart:
        print(
            _format_cart_line(
                item.name or item.product_code,
                item.effective_price_fen(),
                qty=f"×{item.quantity}  ",
            )
        )

    coupons = _parse_coupons(
        client.data("query-store-coupons", {"storeCode": target_store, "orderType": 1, "beType": 1})
    )
    print(f"\n今日可用券 {len(coupons)} 张")
    _explain_coupon_scope(len(coupons))

    oracle = make_price_oracle(client, target_store, coupons)

    optimizer = CouponOptimizer(oracle, max_calls=budget)
    best = optimizer.optimize(cart, coupons, _with_order_options(cart))
    if best.price is None:
        # 早先这里写「加 --budget 80 再试一次」，而 replay 根本没有 --budget
        # 这个参数 —— 照着敲是 unrecognized arguments。给了走不通的自救指令
        # 比不给更糟：用户会以为是自己的问题。现在参数真有了，话也就对了。
        print(f"\n这单一件商品都没算成价（算价预算 {budget} 次）。加 --budget 80 再试一次。")
        return 1
    print("\n今日最优组合：")
    for line in best.describe(cart, coupons):
        print(f"  {line}")

    print("\n" + "-" * 62)
    if paid_fen is None:
        print(f"上次实付金额未知，今日最优价 ¥{FEN(best.price.price_fen)}")
    else:
        diff = best.price.price_fen - paid_fen
        verdict = "涨了" if diff > 0 else ("降了" if diff < 0 else "持平")
        mark = " 🔺" if diff > 0 else (" 🔻" if diff < 0 else "")
        print(f"上次实付 ¥{FEN(paid_fen)}  →  今日最优 ¥{FEN(best.price.price_fen)}")
        print(f"同样的单，{verdict} ¥{FEN(abs(diff))}{mark}")
    print("-" * 62)

    # 这一趟算出来的**不记进账本**。
    #
    # 早先这里无条件 save 一条测算，于是跑十次 replay 账本里就多十条，
    # 而 `latest()` 取的是最后一条 —— 也就是上一次 replay 自己写进去的测算价。
    # 第二次起就成了「上次算出来的价」比「这次算出来的价」，两个测算值相减恒为
    # ¥0.00：真正的涨价只能被检出一次，之后永远"持平"。
    # 规矩得和 optimize 一致 —— 测算不算数，也就不记账。
    baseline_fen = _replay_baseline_fen(optimizer, cart)
    if baseline_fen is not None:
        print(f"（今日裸买价 ¥{FEN(baseline_fen)}，挂券后 ¥{FEN(best.price.price_fen)}）")

    # 只有**真实成交**才落账：段位靠它涨，也只认它。记账前先查重 ——
    # 同一笔订单跑十遍 replay 也只算一次。
    from .snapshot import (
        entry_from_cart,
        has_order_key,
        save as save_snapshot,
    )

    if real_order is not None and isinstance(paid_fen, int) and paid_fen > 0:
        import hashlib

        raw_no = str(real_order.get("orderNo") or real_order.get("orderId") or "")
        key = hashlib.sha256(raw_no.encode("utf-8")).hexdigest()[:16] if raw_no else ""
        if key and not has_order_key(key):
            save_snapshot(
                entry_from_cart(
                    cart=cart,
                    paid_fen=paid_fen,
                    # 当时的裸买价无从得知，就**不编一个** —— 填 0 表示
                    # 「这一单只记真实成交金额，不记省了多少」。
                    baseline_fen=0,
                    discount_fen=0,
                    store_code=target_store,
                    store_name="",
                    city="",
                    keyword="",
                    coupon_titles=[],
                    with_order_label="",
                    kind="order",
                    order_key=key,
                )
            )
            print("\n（这笔真实成交已记进账本 —— 段位只认这个，测算不算数）")
        elif key:
            print("\n（这笔真实成交以前记过了，不重复计）")

    if real_order is None:
        # 这一趟是拿本地快照复刻的 —— 而本地快照本身多半也是测算。
        # 必须说清楚：屏幕上这两个数都是算出来的，不是他真花过的钱。
        print("\n（这一趟是测算，没记进账本。要记一单就用 `optimize --save`）")
    return 0


def run_nutrition(city: str, keyword: str, kcal: int, size: int) -> int:
    """热量预算搭配：给定热量上限，挑出蛋白质最高的合法组合。"""
    # 组合枚举的规模随 size 指数增长，必须夹住。
    # 早先 --size 是裸 int：nutrition --size 50 会让进程无输出地跑到天荒地老，
    # 用户只能 Ctrl-C，而且不知道是自己参数给大了。
    clamped = clamp_size(size)
    if clamped != size:
        print(f"--size 需在 1..{MAX_MEAL_SIZE} 之间，已按 {clamped} 处理。")
        _dev_note("再大组合数会指数爆炸，所以入口就夹住")
        size = clamped
    from .mcd_client import McdMcpClient, extract_payload
    from . import nutrition as nut

    client = McdMcpClient()
    client.initialize()

    stores = client.data(
        "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword}
    )
    if not isinstance(stores, list) or not stores:
        print("未找到门店，请换一个关键词。")
        return 1
    store = stores[0]
    if not isinstance(store, dict):
        print("门店数据格式异常。")
        return 1
    store_code = store.get("storeCode")
    if not store_code:
        print("门店缺少 storeCode，无法继续。")
        return 1
    print(f"门店：{store.get('storeName', '未知门店')}")

    menu = _parse_menu(
        client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
    )
    print(f"菜单 {len(menu)} 个品类")

    raw = extract_payload(client.call("list-nutrition-foods", {}))
    table = nut.parse_nutrition((raw or {}).get("data"))
    print(f"营养表 {len(table)} 条")
    _dev_note("接口返回的是字符串，需自行解析")

    pairs = nut.match_menu(menu, table)
    # 匹配率必须如实报：只认名字完全对得上的，宁可少几件也不拿错数据凑数
    # （单品拿到套餐热量会差出 200+ kcal，那是把减脂的人往反方向带）。
    print(
        f"能对上营养数据的餐品 {len(pairs)} 个 / {len(menu)} 个"
        f"（名字对不上的一律不用，不用错数据凑数）"
    )

    if not pairs:
        print("菜单与营养表一个都对不上，无法给出建议。")
        return 1

    combos = nut.plan(pairs, kcal_budget=kcal, size=size)
    if not combos:
        if pairs and all(p[1]["kcal"] == 0 for p in pairs):
            # 对上了名字但热量全读成 0：基本是接口字段变动导致解析失败，别假装「没有搭配」。
            print("营养表解析异常：所有餐品热量都读成了 0，可能是接口字段变动，无法搭配。")
        else:
            print(f"在 {kcal} kcal 预算内凑不出任何组合，试试提高预算。")
        return 1

    print(f"\n热量预算 {kcal} kcal 内推荐搭配（按蛋白质降序）：")
    for i, combo in enumerate(combos, 1):
        s = nut.summarize(combo)
        names = " + ".join(p[0].name or p[0].product_code for p in combo)
        print(f"\n  {i}. {names}")
        print(
            f"     {s['kcal']} kcal　蛋白 {s['protein']}g　脂肪 {s['fat']}g　"
            f"碳水 {s['carb']}g　钠 {s['sodium']}mg"
        )
        print(f"     合计 ¥{FEN(s['price_fen'])}")
    return 0


def _print_insights(oracle, cart, coupons, best, budget) -> int:
    """打印省钱洞察，返回本次洞察消耗的接口调用次数。

    `budget` 是**全局共享**的预算对象，不是数字 ——
    洞察里每一路都会新建求解器，只有共用同一个计数器，
    才能保证「用户设了 --budget 40，就绝不会打出第 41 次」。
    """
    from . import insights as ins

    if best.price is None:
        return 0
    whole = best.price.price_fen
    spent_before = budget.used if hasattr(budget, "used") else 0
    counter = [0]

    print("\n" + "-" * 62)
    print("省钱洞察")
    print("-" * 62)

    # --- 1. 每件的真实成本
    marginals = ins.marginal_costs(
        oracle,
        cart,
        coupons,
        _with_order_options,
        baseline_fen=whole,
        per_call_budget=max(budget.remaining // max(len(cart), 1), 4),
        call_counter=counter,
        budget=budget,
    )
    if marginals:
        print("\n  这一单里，每件东西实际花了你多少：")
        for m in marginals:
            name = m.item.name or m.item.product_code
            qty = f"×{m.item.quantity}" if m.item.quantity > 1 else ""
            if m.delta_fen > 0:
                tag = f"  省 ¥{FEN(m.delta_fen)}"
            elif m.delta_fen < 0:
                tag = "  ⚠️ 反而更贵"
            else:
                tag = "  没享受到优惠"
            print(
                f"    {_pad(name, 18)}{_pad(qty, 6)}标价 ¥{FEN(m.list_fen):>6}"
                f"  →  实际 ¥{FEN(m.marginal_fen):>6}{tag}"
            )

    # --- 2. 挂了等于没挂的券
    redundant = ins.redundant_coupons(oracle, cart, coupons, best, budget=budget)
    counter[0] += len(best.mapping)
    if redundant:
        print("\n  ⚠️ 以下券挂了等于没挂（摘掉它总价一分不变）：")
        for item, coupon in redundant:
            print(f"    - {coupon.title}  ← 挂在「{item.name or item.product_code}」")
        print("    常见成因：随单购已经把这件降到券面价了。")

    # --- 3. 拆单建议
    splits = ins.split_suggestions(
        oracle,
        cart,
        coupons,
        _with_order_options,
        whole_fen=whole,
        per_call_budget=max(budget.remaining // max(len(cart), 1), 4),
        call_counter=counter,
        budget=budget,
    )
    if splits:
        print("\n  💡 分开下单更便宜：")
        for item, total, gain in splits:
            print(
                f"    - 把「{item.name or item.product_code}」单独下一单"
                f"  →  两单合计 ¥{FEN(total)}，省 ¥{FEN(gain)}"
            )

    if not marginals and not redundant and not splits:
        print("\n  （这张单的券结构简单，没有额外的省钱空间）")
    if budget.remaining <= 0:
        print("\n  （算价调用已到 --budget 上限，洞察到此为止；调大 --budget 可看完整分析）")
    return (budget.used - spent_before) if hasattr(budget, "used") else counter[0]


def _parse_menu(raw: dict):
    """把 query-meals 的 meals 映射摊平成 CartItem 列表。

    关键：`currentPrice` 可能是「随单购麦金卡优惠」这类条件价，
    必须把 originalPrice 一起留下来，否则展示会骗人。
    """
    if not isinstance(raw, dict):
        # 接口偶发返回 list/str/None（而非预期 dict），直接当空菜单，别抛 AttributeError。
        return []
    items: list[CartItem] = []
    detail = raw.get("meals", {}) or {}

    for code, info in detail.items():
        if not isinstance(info, dict):
            continue
        discount_type = info.get("discountType") or ""
        with_order = info.get("withOrder") or {}
        price = to_fen(info.get("currentPrice"))
        list_price = to_fen(info.get("originalPrice"))
        # 条件价的判据统一成「标价 < 原价」，而不是只认「随单购」这四个字。
        # 真实菜单里还有「麦金卡优惠」「会员价」「早餐卡优惠」等写法，
        # 它们同样是普通顾客付不出来的低价；只认随单购的话，屏幕上就会
        # 印着一个你根本拿不到的价格，而用户以为那就是这单的价。
        # 反过来，canWithOrder 只用来决定要不要发 withOrder 参数 ——
        # 它是「能不能随单购」，不是「这个价是不是条件价」。
        conditional = 0 < price < list_price
        needs = conditional
        items.append(
            CartItem(
                product_code=code,
                name=info.get("name", ""),
                quantity=1,
                unit_price_fen=price,
                list_price_fen=list_price,
                discount_type=discount_type,
                needs_with_order=needs,
                membership_code=str(with_order.get("membershipCode", "")),
                membership_spec_id=str(with_order.get("specId", "")),
            )
        )
    return items


def _build_cart(menu: list[CartItem], coupons: list[Coupon], size: int = 4) -> list[CartItem]:
    """构造演示购物车：优先纳入被券覆盖的商品。

    随机取前 N 个餐品的话，购物车大概率一张券都用不上，
    引擎就退化成「啥也没优化」，演示没有说服力。
    """
    covered = {code for c in coupons for code in c.product_codes}
    picked: list[CartItem] = []
    seen: set[str] = set()

    for item in menu:
        if len(picked) >= size:
            break
        if item.product_code in covered and item.product_code not in seen:
            picked.append(item)
            seen.add(item.product_code)

    for item in menu:
        if len(picked) >= size:
            break
        if item.product_code not in seen:
            picked.append(item)
            seen.add(item.product_code)
    return picked


_SYMBOL_STRIP = "™®©Ⓡ"


def _norm_name(s: str) -> str:
    """归一化餐品名用于模糊匹配：去掉 ™®© 等商标符号与首尾空白。

    真实菜单里「麦咖啡™冰美式中杯」带 ™，用户打字通常不带，
    朴素子串匹配会被商标符号整段切断而失配，导致券挂不上。
    """
    if not s:
        return ""
    for ch in _SYMBOL_STRIP:
        s = s.replace(ch, "")
    return s.strip()


def _pick_cart(
    menu: list[CartItem], wants: list[str], coupons: list[Coupon] | None = None
) -> tuple[list[CartItem], list[str]]:
    """按用户给的餐品名挑购物车。

    支持两种写法：
        --items 巨无霸,中份薯条
        --items "巨无霸x2,中份薯条"      （数量）

    匹配规则：先按商品编码精确匹配，再按名称精确匹配，最后退化成子串包含。
    匹配不上就原样报回来，绝不拿一个不相干的商品凑数。
    """
    by_code = {i.product_code: i for i in menu}
    by_name = {_norm_name(i.name or ""): i for i in menu}
    # 券名 → 菜单商品编码：接口两侧命名常不一致
    # （券上写「麦咖啡™冰美式中杯」，菜单里却叫「麦咖啡™美式」），
    # 用户照着券名点单时，菜单名匹配不上，需回退到券名解析。
    menu_codes = set(by_code)
    by_coupon_name: dict[str, str] = {}
    for c in coupons or []:
        code = next((cc for cc in c.product_codes if cc in menu_codes), None)
        if not code:
            continue
        for pn in c.product_names:
            n = _norm_name(pn)
            if n:
                by_coupon_name.setdefault(n, code)

    cart: list[CartItem] = []
    missed: list[str] = []
    # 先收集「要什么、各要几份」，最后再一次性构造 ——
    # CartItem 是 frozen dataclass，创建之后改不动 quantity。
    qty_by_code: dict[str, int] = {}
    proto: dict[str, CartItem] = {}

    for raw in wants:
        token = (raw or "").strip()
        if not token:
            continue
        qty = 1
        m = re.search(r"[xX*×]\s*(\d+)\s*$", token)
        if m:
            qty = max(int(m.group(1)), 1)
            token = token[: m.start()].strip()
        if not token:
            continue

        item = by_code.get(token) or by_name.get(_norm_name(token))
        if item is None:
            ntok = _norm_name(token)
            hits = [i for i in menu if ntok and ntok in _norm_name(i.name or "")]
            item = hits[0] if hits else None
        if item is None:
            # 菜单名仍失配 → 回退到券名（接口两侧命名不一致的兜底）
            ntok = _norm_name(token)
            for cn, code in by_coupon_name.items():
                if ntok and (ntok in cn or cn in ntok):
                    item = by_code.get(code)
                    break
        if item is None:
            missed.append(raw)
            continue

        # 同一个商品写了两次就合并数量，而不是加两行
        qty_by_code[item.product_code] = qty_by_code.get(item.product_code, 0) + qty
        proto[item.product_code] = item

    for code, qty in qty_by_code.items():
        base = proto[code]
        cart.append(CartItem(**{**base.__dict__, "quantity": qty}))
    return cart, missed


def _with_order_options(cart: list[CartItem]) -> list:
    """随单购候选档位。

    菜单里带 canWithOrder 的商品，其标价往往挂在「随单购麦金卡/早餐卡」下，
    所以「要不要随单购」本身就是一个需要试算的决策维度。
    """
    options: list[WithOrderOption | None] = [None]
    seen: set[str] = set()
    for item in cart:
        if not item.needs_with_order or not item.membership_code:
            continue
        if item.membership_code in seen:
            continue
        seen.add(item.membership_code)
        # payload 里只放官方字段；展示文案单独放 label。
        # 混在一起的话，官方一旦做严格 schema 校验，整个算价请求都会被拒。
        options.append(
            WithOrderOption(
                payload={
                    "membershipCode": item.membership_code,
                    "membershipSpecId": item.membership_spec_id,
                },
                # membershipCode 是内部会员卡编码，只留在 payload 里发出去。
                # 一旦进 label，用户会在「最优组合」里看到一串陌生数字。
                label=item.discount_type or "随单购",
            )
        )
    return options


def _parse_coupons(raw) -> list[Coupon]:
    if not isinstance(raw, (list, tuple)):
        # 接口偶发返回 dict/str/None，迭代会炸；直接当无券。
        return []
    out: list[Coupon] = []
    for entry in raw:
        # 券条目本身可能是 str/None（接口换形态时遇到过），products[] 里的元素
        # 同理。主链路上一个 AttributeError 就能让整单算不出来，所以逐个守卫。
        if not isinstance(entry, dict):
            continue
        products = entry.get("products") or []
        if not isinstance(products, (list, tuple)):
            products = []
        codes = {
            str(p.get("productCode"))
            for p in products
            if isinstance(p, dict) and p.get("productCode")
        }
        names = {
            str(p.get("productName"))
            for p in products
            if isinstance(p, dict) and p.get("productName")
        }
        out.append(
            Coupon(
                coupon_id=str(entry.get("couponId", "")),
                coupon_code=str(entry.get("couponCode", "")),
                title=entry.get("title", ""),
                product_codes=frozenset(codes),
                product_names=frozenset(names),
                # tradeDateTime 是「起-止」区间字符串，必须取结束日；
                # 直接塞整串会让下游把开始日当成到期日（券明明还有 6 天却被报成今天到期）。
                valid_until=valid_until_end(entry.get("tradeDateTime")),
            )
        )
    return out


# ==================================================================== main

def _force_utf8_stdout() -> None:
    """把 stdout/stderr 强制成 UTF-8。

    这不是洁癖，是**Windows 上必然会崩**的问题：本项目的输出满是
    `═ │ ¥ ░ ⏳ 🆕` 这类字符。在 en-US 代码页（cp1252）下 `selftest`
    第一行中文就 `UnicodeEncodeError`；在中国区默认代码页（cp936）下
    连人民币符号 `¥`（U+00A5）都打不出来 —— 实测 `demo` 第 4 行直接退出 1。

    本函数只做一件事：让「输出什么样的字」不再取决于用户机器的区域设置。
    `errors="replace"` 是刻意的 —— 遇到真打不出的字符退化成 `?`，
    也比整条命令崩掉强。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass  # 不是 TextIOWrapper（被重定向/被测试捕获）就不管


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    # 先把 .env 读进来，默认门店才能用得上（也避免"照着 README 做却报缺 Token"）
    load_dotenv()
    city_default = os.environ.get("MCD_DEFAULT_CITY", "上海")
    kw_default = os.environ.get("MCD_DEFAULT_KEYWORD", "人民广场")

    parser = argparse.ArgumentParser(prog="mcd-butler", description="麦门管家")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="离线演示，无需 Token")
    p_demo.add_argument(
        "--all",
        action="store_true",
        help="连玩法向的四段一起演示（黄历 / 券包体检 / 活动日历 / 段位）",
    )
    sub.add_parser("tools", help="列出 MCP 工具")
    p_self = sub.add_parser("selftest", help="离线自检解析器与引擎，无需 Token")
    p_self.add_argument(
        "--twice",
        action="store_true",
        help="连跑两轮并比对断言总数（验证自检本身可重入，供 scripts/mutate.py 使用）",
    )

    p_adv = sub.add_parser("advise", help="一键顾问：今天该怎么点最划算")
    p_adv.add_argument("--city", default=city_default)
    p_adv.add_argument("--keyword", default=kw_default)
    p_adv.add_argument("--budget", type=int, default=40, help="算价接口调用上限")
    p_adv.add_argument("--store", default=None, help="直接指定 storeCode，跳过关键词搜索")
    p_adv.add_argument("--days", type=int, default=7, help="活动日历往后看几天")
    p_adv.add_argument(
        "--items",
        default="",
        help='指定想吃什么，逗号分隔："板烧鸡腿堡三件套,薯条x2"；'
        "不给就不替你点菜，只把券盘一遍",
    )

    sub.add_parser(
        "delivery",
        help="麦乐送（外送）链路体检：这条通不通、卡在哪一步（只读）",
    )

    p_camp = sub.add_parser("campaign", help="麦门活动日历：今天有什么活动、哪天有新品")
    p_camp.add_argument("--days", type=int, default=7, help="往后看几天")

    p_aud = sub.add_parser("audit", help="券包体检：每张券真实值多少钱、门槛在哪")
    p_aud.add_argument("--city", default=city_default)
    p_aud.add_argument("--keyword", default=kw_default)
    p_aud.add_argument("--budget", type=int, default=120, help="算价接口调用上限")
    p_aud.add_argument("--limit", type=int, default=10, help="最多体检几张券")
    p_aud.add_argument("--store", default=None, help="直接指定 storeCode，跳过关键词搜索")

    p_assets = sub.add_parser("assets", help="麦门资产周报（默认只读）")
    p_assets.add_argument(
        "--claim", action="store_true", help="真的去领券（会写入你的真实会员账号）"
    )

    p_meal = sub.add_parser(
        "meal", help="餐品详情：这个套餐由什么组成、换一样要加/减多少钱"
    )
    p_meal.add_argument("--city", default=city_default)
    p_meal.add_argument("--keyword", default=kw_default)
    p_meal.add_argument("--name", default="", help="餐品名字关键词，如「三件套」")
    p_meal.add_argument("--code", default=None, help="餐品 code（在 menu 里查得到）")
    p_meal.add_argument("--store", default=None, help="直接指定 storeCode")

    p_menu = sub.add_parser("menu", help="列出门店菜单")
    p_menu.add_argument("--city", default=city_default)
    p_menu.add_argument("--keyword", default=kw_default)
    p_menu.add_argument("--filter", default="", help="只显示名称包含该关键词的餐品")
    p_menu.add_argument("--limit", type=int, default=40, help="最多显示多少条")
    p_menu.add_argument("--store", default=None, help="直接指定 storeCode，跳过关键词搜索")

    p_opt = sub.add_parser("optimize", help="真实账号券价寻优")
    p_opt.add_argument("--city", default=city_default)
    p_opt.add_argument("--keyword", default=kw_default)
    p_opt.add_argument("--budget", type=int, default=40, help="算价接口调用上限")
    p_opt.add_argument(
        "--items",
        default="",
        help="指定想吃什么，逗号分隔，可带数量：\"巨无霸x2,中份薯条\"；留空则自动挑选",
    )
    p_opt.add_argument(
        "--save",
        action="store_true",
        help="把这一单记进本地账本（默认不记 —— 测算不等于已省下的钱）",
    )
    p_opt.add_argument(
        "--no-insights",
        action="store_true",
        help="跳过省钱洞察（会少算若干次价，快一些）",
    )
    p_opt.add_argument("--store", default=None, help="直接指定 storeCode，跳过关键词搜索")

    p_rep = sub.add_parser("replay", help="历史订单复刻与价格变动")
    p_rep.add_argument("--store", default=None, help="指定门店编码，默认用最近快照")
    p_rep.add_argument("--list", action="store_true", help="只列出本地快照")
    p_rep.add_argument("--budget", type=int, default=40, help="算价接口调用上限")

    p_cmp = sub.add_parser("compare", help="多门店比价 + 路程性价比（该不该跑这一趟）")
    p_cmp.add_argument("--city", default=city_default)
    p_cmp.add_argument("--keyword", default=kw_default)
    p_cmp.add_argument("--budget", type=int, default=60, help="算价接口调用上限")
    p_cmp.add_argument(
        "--items",
        default="",
        help="指定想吃什么，逗号分隔，可带数量；留空则自动挑选",
    )
    p_cmp.add_argument(
        "--mode",
        default="walk",
        choices=sorted(["walk", "bike", "bus", "taxi"]),
        help="出行方式：walk 步行 / bike 骑行 / bus 公交地铁 / taxi 打车",
    )
    p_cmp.add_argument("--yuan-per-km", type=float, default=None, help="覆盖每公里路费（元）")
    p_cmp.add_argument("--wage", type=float, default=None, help="时薪（元/小时），把等待时间也折算成钱")
    p_cmp.add_argument("--top", type=int, default=3, help="最多比几家店")

    p_brag = sub.add_parser("brag", help="麦门账单：段位 + 可晒的卡片（读本地快照，无需 Token）")
    p_brag.add_argument(
        "--with-link", action="store_true", help="卡片里带上仓库链接（发 CSDN/知乎时用）"
    )

    p_forget = sub.add_parser("forget", help="撤回本地账本里的记录（不联网）")
    p_forget.add_argument("--last", type=int, default=1, help="撤回最近几条，默认 1 条")
    p_forget.add_argument("--all", action="store_true", help="清空整本账")

    p_alm = sub.add_parser("almanac", help="麦门黄历：今天宜吃什么忌吃什么（每条都真算过价）")
    p_alm.add_argument("--city", default=city_default)
    p_alm.add_argument("--keyword", default=kw_default)
    p_alm.add_argument("--budget", type=int, default=40, help="算价接口调用上限")
    p_alm.add_argument("--store", default=None, help="直接指定 storeCode，跳过关键词搜索")

    p_nut = sub.add_parser("nutrition", help="热量预算搭配")
    p_nut.add_argument("--city", default=city_default)
    p_nut.add_argument("--keyword", default=kw_default)
    p_nut.add_argument("--kcal", type=int, default=600, help="热量上限（千卡）")
    p_nut.add_argument("--size", type=int, default=3, help="最多几件")

    args = parser.parse_args(argv)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except BudgetExhausted as exc:
        # 预算太小，连一次贪心都没跑完。
        # 这时候不该编一个价格出来，明确告诉用户怎么改。
        print(f"\n{exc}")
        print("算价次数设置得太少了，没算出结果。加 --budget 80 再试一次。")
        return 3
    except McpError as exc:
        code, lines = _error_hint(exc)
        print("")
        for line in lines:
            print(line)
        return code
    except Exception as exc:  # noqa: BLE001
        # 最后一道兜底。写软件的人习惯看 traceback，普通人不习惯 ——
        # 屏幕上蹦出几十行 "File ... line ... in ..." 只会让人以为是自己弄坏了。
        # 给一句人话 + 一个去处；真要排查，设 MCD_BUTLER_DEBUG=1 就能看到原始栈。
        return _unexpected_error(exc)


def _unexpected_error(exc: BaseException) -> int:
    """未预料异常的统一出口：人话提示 + 可选 traceback。

    拆成纯函数是为了能离线断言。以前这段埋在 except 里，
    「是否对用户隐藏了 traceback」这件事一条自检都管不到。
    """
    shown = os.environ.get("MCD_BUTLER_DEBUG", "").strip() not in ("", "0")
    kind = exc.__class__.__name__
    lines = [
        "",
        f"出错了：{exc}" if str(exc).strip() else "出错了：程序内部异常。",
        "这不影响你的账号和余额 —— 没有发出任何下单或支付请求。",
        "麻烦把上面这行提示和你敲的命令一起提到 "
        "https://github.com/GenshinKings/mcd-butler/issues",
    ]
    for line in lines:
        print(line)
    if shown:
        import traceback

        # 诊断信息走 stderr：正常结果才走 stdout，否则 `>` 重定向出来的
        # 文件会被调试信息污染，脚本里没法直接用。
        print("\n---- 调试信息（MCD_BUTLER_DEBUG=1 才会显示）----", file=sys.stderr)
        if exc.__traceback__ is not None:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        else:
            # 有些异常不是从 try 里 raise 出来的（比如直接构造传入），
            # 没有 traceback 对象。这时 print_exception 只吐一行、
            # 看不出在哪发生的 —— 退而求其次打当前调用栈，总比没有强。
            print("（这个异常没有携带调用栈，下面是发生位置）", file=sys.stderr)
            traceback.print_stack(file=sys.stderr)
        print(f"（异常类型：{kind}）", file=sys.stderr)
    return 4


def _error_hint(exc: Exception) -> tuple[int, list[str]]:
    """把接口异常翻译成「用户能照着做」的提示，返回 (退出码, 要印的行)。

    为什么必须分开：Token 失效和业务性拒绝 **长得很像** —— 都只是一句错误消息。
    混着印有两种后果，而且都不轻：
      · 502 / 断网被印成「换一家门店试试」→ 用户在一台根本连不上网的机器上
        反复换店换商品，折腾一整天也想不到是代理的问题；
      · 「该券不适用于此门店」被印成「Token 失效，请重新激活」→ 用户去重新
        激活一个好端端的 Token，激活完还是一样的错。

    单独拆出来是因为：埋在 `main()` 的 except 里时没法离线断言，
    哪天有人把两个分支合并回一句，一条自检都不会红。
    """
    if getattr(exc, "rate_limited", False):
        # 限流单独一支：McpFatalError 现在自带 rate_limited 标记，不用猜文案。
        # 限流的正确动作只有一个：**等**。让用户去检查 Token 或代理是误导，
        # 而且重试循环已经退避过了，这里只交代结果。
        return 2, [
            f"{exc}",
            "同一时间请求太多，麦当劳那边暂时不给应答了。",
            "这不是命令的问题，隔一两分钟重跑一次就好。",
        ]
    if isinstance(exc, McpFatalError):
        # 环境故障：Token 失效 / 断网 / 服务端 5xx。
        # 直接抛 traceback 对使用者毫无帮助，给一句能照着做的提示。
        return 2, [
            f"接口调用失败：{exc}",
            "常见原因：Token 失效或过期（到 https://open.mcd.cn/mcp 重新领取）、",
            "          网络不通（本机需要代理时请设置 https_proxy）、",
            "          或同一时间请求太多被限流（隔一两分钟再试）。",
        ]
    # 业务性拒绝：工具调通了、对面也回答了，只是答案是否定的
    #（券不适用这家店 / storeCode 与 beCode 不配对 / 门店不支持该券）。
    return 2, [
        f"接口拒绝了这次请求：{exc}",
        "这不是网络或 Token 问题 —— 换一家门店、换个商品，或换张券试试。",
    ]


def _dispatch(args) -> int:
    if args.cmd == "demo":
        return run_demo(show_all=getattr(args, "all", False))
    if args.cmd == "tools":
        return run_tools()
    if args.cmd == "selftest":
        from .selftest import run as run_selftest, run_repeat

        print("离线自检（无需 Token）\n")
        if getattr(args, "twice", False):
            return run_repeat(2)
        return run_selftest()
    if args.cmd == "advise":
        picked = [s for s in (getattr(args, "items", "") or "").split(",") if s.strip()]
        return run_advise(
            args.city, args.keyword, args.budget, args.store, args.days, picked or None
        )
    if args.cmd == "delivery":
        return run_delivery()
    if args.cmd == "campaign":
        return run_campaign(_advise_within(args.days))
    if args.cmd == "audit":
        return run_audit(args.city, args.keyword, args.budget, args.limit, args.store)
    if args.cmd == "meal":
        return run_meal(args.city, args.keyword, args.name, args.code, args.store)
    if args.cmd == "menu":
        return run_menu(args.city, args.keyword, args.filter, args.limit, args.store)
    if args.cmd == "assets":
        return run_assets(args.claim)
    if args.cmd == "replay":
        return run_replay(args.store, args.list, args.budget)
    if args.cmd == "compare":
        return run_compare(
            args.city,
            args.keyword,
            args.items,
            args.budget,
            args.mode,
            args.yuan_per_km,
            args.wage,
            args.top,
        )
    if args.cmd == "brag":
        return run_brag(getattr(args, "with_link", False))
    if args.cmd == "forget":
        return run_forget(getattr(args, "last", 1), getattr(args, "all", False))
    if args.cmd == "almanac":
        return run_almanac(args.city, args.keyword, args.budget, args.store)
    if args.cmd == "nutrition":
        return run_nutrition(args.city, args.keyword, args.kcal, args.size)
    if args.cmd == "optimize":
        picked = [s for s in (args.items or "").split(",") if s.strip()]
        return run_optimize(
            args.city,
            args.keyword,
            args.budget,
            save=getattr(args, "save", False),
            items=picked,
            insights=not args.no_insights,
            store_code=args.store,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
