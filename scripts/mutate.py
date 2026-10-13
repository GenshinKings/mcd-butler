"""变异测试：故意把核心逻辑改坏，看自检会不会翻红。

为什么要有这个脚本：
    自检全绿**不能证明**自检有效。上一轮把 60 处核心逻辑逐个改坏，
    其中 33 处改坏之后自检依然全绿 —— 那些断言是装饰品，
    它们守的是运气（恰好没人改那行），不是行为。

    这个脚本把「改坏 → 跑自检 → 数有几条变红」固化成一条命令，
    每次补完断言都能立刻验证补的是不是真的。

用法：
    python scripts/mutate.py            # 跑全部变异（约 12 分钟）
    python scripts/mutate.py --quick    # 只跑标了快速档的那几条
    python scripts/mutate.py --only=V   # 只跑名字里带「V」的（改完引擎后用它复验）

判定：
    改坏后 0 条变红 = 该处没有回归防护（P0）。
"""

from __future__ import annotations

import atexit
import io
import json
import pathlib
import signal
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# ---------------------------------------------------------------- 安全网
# 这个脚本的工作方式是「把源码改坏 → 跑自检 → 还原」。
# 一旦它在改坏之后、还原之前被打断（Ctrl-C、超时被 SIGTERM、IDE 杀进程），
# **源码就停在改坏的状态**，而且 git diff 看起来只是普通的一行改动 ——
# 用户会带着一个坏掉的仓库继续干活，还以为是自己的改动。
#
# 所以除了 try/finally，还要挂 atexit + 信号处理器：
# Ctrl-C 走 KeyboardInterrupt、SIGTERM 默认直接终止进程，两者都不跑 finally。
_BACKUPS: dict[pathlib.Path, str] = {}


def _restore_all() -> None:
    for path, text in list(_BACKUPS.items()):
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
        except OSError:
            pass
    _BACKUPS.clear()


def _on_signal(signum, _frame):  # noqa: ANN001
    _restore_all()
    sys.exit(130 if signum == signal.SIGINT else 143)


atexit.register(_restore_all)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _on_signal)
    except (ValueError, OSError):
        pass  # 非主线程里装不了信号处理器，atexit 仍然兜底

# (名字, 文件, 原文, 改坏成, 是否快速档)
MUTATIONS: list[tuple[str, str, str, str, bool]] = [
    (
        "401 降级为业务性失败",
        "mcd_client.py",
        'raise McpFatalError("Token 不对或已过期，请重新设置 MCP_TOKEN。", detail) from exc',
        'raise McpError("Token 不对或已过期，请重新设置 MCP_TOKEN。", detail) from exc',
        True,
    ),
    (
        "黄历吞掉环境故障",
        "almanac.py",
        "    except McpFatalError:\n        raise\n    except Exception:\n        return None",
        "    except Exception:\n        return None",
        True,
    ),
    (
        # 原文从 `except McpFatalError` 收紧成 `except McpError`：
        # 业务性失败（不是环境故障，但同样表示「这一趟没问到」）也必须穿透，
        # 否则返回空列表 = 把「没查到」说成「没有」。
        "卡包把限流降级为空",
        "assets.py",
        "    except McpError:\n        # 业务性失败也必须穿透：返回空等于把「没查到」说成「没有」。\n        raise\n    except Exception:\n        return []",
        "    except Exception:\n        return []",
        True,
    ),
    (
        "随单购守卫退回判 dict",
        "oracle.py",
        "if assignment.with_order is not None:",
        "if isinstance(assignment.with_order, dict):",
        True,
    ),
    (
        "券下标越界守卫删掉",
        "oracle.py",
        "if ci is not None and not (0 <= ci < len(coupons)):",
        "if False:",
        True,
    ),
    (
        "量纲守卫删掉小数点判据",
        "oracle.py",
        'if isinstance(raw, str) and "." in raw:',
        "if False:",
        True,
    ),
    (
        "probed 虚报成候选数",
        "audit.py",
        "        v.probed = probed_ok",
        "        v.probed = len(cands)",
        True,
    ),
    (
        "健康度分母漏掉未查出的券",
        "audit.py",
        "counted = worth + dud + dead + unknown",
        "counted = worth + dud + dead",
        True,
    ),
    (
        "样本太小告警关掉",
        "audit.py",
        "if counted and counted < 3:",
        "if False:",
        True,
    ),
    (
        "日历 label 采信「今日」标记",
        "campaign.py",
        "        if delta == 0:\n            return \"今天\"",
        "        if self.is_today or delta == 0:\n            return \"今天\"",
        True,
    ),
    (
        "upcoming 的 within 写死 7",
        "campaign.py",
        "def upcoming(\n    days: list[CampaignDay], today: date | None = None, within: int = 7\n) -> list[CampaignDay]:\n    \"\"\"今天之后 `within` 天内还有活动的日子。\"\"\"",
        "def upcoming(\n    days: list[CampaignDay], today: date | None = None, within: int = 7\n) -> list[CampaignDay]:\n    \"\"\"今天之后 `within` 天内还有活动的日子。\"\"\"\n    within = 7",
        True,
    ),
    (
        "advise_line 的 within 写死 7",
        "campaign.py",
        "    base = today or date.today()\n    out: list[str] = []",
        "    within = 7\n    base = today or date.today()\n    out: list[str] = []",
        True,
    ),
    (
        "取价前不挖门槛",
        "campaign.py",
        "vals = [\n        float(m.group(1))\n        for m in _PRICE_RE.finditer(_strip_amortized(_strip_discounts(title)))\n    ]",
        "vals = [\n        float(m.group(1))\n        for m in _PRICE_RE.finditer(title)\n    ]",
        False,
    ),
    (
        "取价前不抹摊销价（「每天低至 0.33 元」被当成活动价）",
        "campaign.py",
        "for m in _PRICE_RE.finditer(_strip_amortized(_strip_discounts(title)))",
        "for m in _PRICE_RE.finditer(_strip_discounts(title))",
        True,
    ),
    (
        "标题取价退回 min（「双人餐 99 元（含两份各 49 元）」报成 ¥49）",
        "campaign.py",
        "    vals = [v for v in vals if 0.1 <= v <= 10000]\n    if vals:\n        return vals[0]",
        "    vals = [v for v in vals if 0.1 <= v <= 10000]\n    if vals:\n        return min(vals)",
        True,
    ),
    (
        "同名更便宜的活动不算新",
        "campaign.py",
        "if base is not None and c.price_yuan is not None and c.price_yuan < base:",
        "if c.title not in known:",
        False,
    ),
    (
        "cheaper_swaps 退回按 code 跳过默认项",
        "meal_detail.py",
        "                if c is base:",
        "                if c.code == base.code:",
        True,
    ),
    (
        "isDefault 放宽为真值判断",
        "meal_detail.py",
        'is_default=bool(c.get("isDefault") == 1),',
        'is_default=bool(c.get("isDefault")),',
        True,
    ),
    (
        "营养 --size 不夹取",
        "nutrition.py",
        "    if size < 1:\n        return 1\n    return min(size, MAX_MEAL_SIZE)",
        "    return size",
        True,
    ),
    (
        "营养枚举量只看端点",
        "nutrition.py",
        "            total += combos\n            if total > budget:\n                break\n        if total <= budget:",
        "        if combos <= budget:",
        True,
    ),
    (
        "元转分退回 banker's rounding",
        "route.py",
        "    return to_fen(yuan)",
        "    return int(round(float(yuan) * 100))",
        True,
    ),
    (
        "forget --last 0 静默删 1 条",
        "cli.py",
        "    if last <= 0:\n        print(\"--last 需要是正整数；一条都不撤回，账本未改动。\")\n        return 0\n    n = delete_last(last)",
        "    n = delete_last(max(last, 1))",
        True,
    ),
    (
        "compare --top 不夹取",
        "cli.py",
        "    if top < 1:\n        return 1\n    return min(top, MAX_COMPARE_STORES)",
        "    return top",
        True,
    ),
    (
        "meal 入参键退回 productCode",
        "cli.py",
        '        "code": code,',
        '        "productCode": code,',
        True,
    ),
    (
        "advise 窗口不跟 --days 走",
        "cli.py",
        "    return max(1, days)",
        "    return 7",
        True,
    ),
    (
        "数量守卫改回双向",
        "oracle.py",
        "    if got < want:",
        "    if got != want:",
        False,
    ),
    (
        "演示券名改掉（README 贴的数字会失效）",
        "demo_data.py",
        '"安格斯厚牛堡立减 8 元"',
        '"安格斯厚牛堡立减 6 元"',
        False,
    ),
    (
        "README 删掉一段演示输出",
        "README.md",
        "挂券后小计 ¥42.00",
        "（这一行被删掉了）",
        True,
    ),
    (
        "演示券写死过期日期",
        "demo_data.py",
        "_DEMO_WITH_ORDER_DISCOUNT = {\"\": 0, \"麦金卡（随单购）\": 600}",
        "_DEMO_WITH_ORDER_DISCOUNT = {\"\": 0, \"麦金卡（随单购）\": 600}\n"
        "DEMO_COUPONS[0] = Coupon(coupon_id='C0', coupon_code='x', "
        "title='过期的券', product_codes=frozenset(), valid_until='2020-01-01')",
        False,
    ),
    # ---- 以下 10 处是第四轮补的：专打本轮 A–L 里 mutate.py 还没覆盖的点 ----
    (
        "C-读到一半的错缩回只认 URLError",
        "mcd_client.py",
        "            except _TRANSIENT as exc:",
        "            except urllib.error.URLError as exc:",
        True,
    ),
    (
        "C-429 退避退回 0.5s（限流窗口是 60s）",
        "mcd_client.py",
        "        backoff = min(max(backoff, 5.0), 30.0)",
        "        backoff = backoff",
        True,
    ),
    (
        "C-429 退避不封顶（把命令行拖成卡死）",
        "mcd_client.py",
        "        backoff = min(max(backoff, 5.0), 30.0)",
        "        backoff = max(backoff, 5.0)",
        True,
    ),
    (
        "C-resp.read() 去掉 errors=replace",
        "mcd_client.py",
        'raw = resp.read().decode("utf-8", errors="replace")',
        'raw = resp.read().decode("utf-8")',
        True,
    ),
    (
        "E-run_replay 随单购标签退回判 dict",
        "cli.py",
        '    return with_order.label if with_order is not None else ""',
        '    return with_order.label if isinstance(with_order, dict) else ""',
        True,
    ),
    (
        "E-裸买价退回接口 original（把卡费算成省下的钱）",
        "cli.py",
        "    return optimizer.evaluate(cart, Assignment(mapping={}, with_order=None)).price_fen",
        "    return optimizer.evaluate(cart, Assignment(mapping={}, with_order=None)).original_fen",
        True,
    ),
    (
        "E-main 不再分开提示 McpFatalError",
        "cli.py",
        "    if isinstance(exc, McpFatalError):",
        "    if False:",
        True,
    ),
    (
        "E-run_demo 删掉随单购单独一行",
        "cli.py",
        '        print(f"  随单购「{best.with_order.label}」再减 ¥{FEN(wo_gain)}（卡费另算）")',
        "        pass",
        True,
    ),
    (
        "I-snapshot 退回模块级 Path.home()",
        "snapshot.py",
        'SNAPSHOT_DIR = _home_dir() / ".mcd-butler"',
        'SNAPSHOT_DIR = Path.home() / ".mcd-butler"',
        True,
    ),
    (
        "K-演示券「中杯可乐立减 3 元」改名",
        "demo_data.py",
        '        title="中杯可乐立减 3 元",',
        '        title="中杯可乐立减 2 元",',
        False,
    ),
    (
        "L-selftest run() 不再清空 _RESULTS",
        "selftest.py",
        "    _RESULTS.clear()",
        "    pass",
        True,
    ),
    (
        "M-演示求价器不看券有效期（时钟一歪就印假账）",
        "demo_data.py",
        "        if _expired(DEMO_COUPONS[coupon_idx].valid_until):\n            continue\n",
        "",
        False,
    ),
    # ---- 以下 19 处是第五轮（变异猎手）自造的 ----
    # A. 先打本轮新引入的代码路径
    (
        "N2-指数退避退化成线性（attempt=0 时一秒都不等）",
        "mcd_client.py",
        "    backoff = base * (2**attempt)",
        "    backoff = base * attempt",
        True,
    ),
    (
        "N3-业务性拒绝的提示抄成「Token 失效，请重新激活」",
        "cli.py",
        '        "这不是网络或 Token 问题 —— 换一家门店、换个商品，或换张券试试。",',
        '        "Token 失效，请到 https://open.mcd.cn/mcp 重新激活。",',
        True,
    ),
        (
        "N4-run_repeat 的可重入判据写死 True（--twice 失效）",
        "selftest.py",
        "    stable = _reentrant(totals)",
        "    stable = True",
        True,
    ),
        (
        "N5-mutate.py 崩溃判定退回只看变红条数（崩=无防护）",
        "scripts/mutate.py",
        '        if red == 0 and rc == 0:\n            verdict = "P0 无防护"',
        '        if red == 0:\n            verdict = "P0 无防护"',
        True,
    ),
    (
        "N6-演示券「今天到期」被判成已过期（当天还能用的券被废）",
        "demo_data.py",
        "        return date.fromisoformat(valid_until[:10]) < date.today()",
        "        return date.fromisoformat(valid_until[:10]) <= date.today()",
        False,
    ),
    # B. 再打至今零覆盖的老代码
    (
        "N7-snapshot.latest 取最早那条（replay 复刻的是最老的一单）",
        "snapshot.py",
        "    return entries[-1] if entries else None",
        "    return entries[0] if entries else None",
        True,
    ),
    (
        "N8-delete_last 去掉 n=0 守卫（--last 0 会清空整本账）",
        "snapshot.py",
        "    keep = entries[:-n] if 0 < n < len(entries) else ([] if n >= len(entries) else entries)",
        "    keep = entries[:-n]",
        True,
    ),
    (
        "N9-describe 把「测算」标成「成交」（replay --list 说谎）",
        "snapshot.py",
        '    tag = "成交" if entry.get("kind") == "order" else "测算"',
        '    tag = "成交" if entry.get("kind") != "order" else "测算"',
        True,
    ),
    (
        "N10-营养的热量预算放宽一倍（超预算的搭配照样推荐）",
        "nutrition.py",
        "            if kcal > kcal_budget:",
        "            if kcal > kcal_budget * 2:",
        True,
    ),
    (
        "N11-营养搭配排序反向（蛋白最少的排最前）",
        "nutrition.py",
        '            -sum(p[1]["protein"] for p in c),',
        '            sum(p[1]["protein"] for p in c),',
        True,
    ),
    (
        # 时段改成「分钟边界」之后，边界判据从 `start <= h < end` 变成
        # `start <= mins < end`。写反之后 10:30 整会被判成早市、
        # 05:00 整会被判成深夜 —— 早餐券还能用的时候说它过期了。
        "N12-黄历时段边界写反（10:30 归早市、5:00 归深夜）",
        "almanac.py",
        "        if start <= mins < end:",
        "        if start < mins <= end:",
        True,
    ),
    (
        "N12b-时段退回整点切分（10:15 被误划午市，早餐券提前 15 分钟作废）",
        "almanac.py",
        "    mins = now.hour * 60 + now.minute",
        "    mins = now.hour * 60",
        True,
    ),
    (
        "N13-已过期的券报成「今日到期」（d<0 被 d<1 吃掉）",
        "almanac.py",
        '    if d < 0:\n        return "已过期"',
        '    if d < 1:\n        return "今日到期，不用就作废"',
        True,
    ),
    (
        "N14-summarize 把测算数当成成交数（晒单卡片说谎）",
        "persona.py",
        '    real_orders = sum(1 for e in entries if e.get("kind") == "order")',
        '    real_orders = sum(1 for e in entries if e.get("kind") != "order")',
        True,
    ),
    (
        "N15-已领取的券算成待领取（--claim 会重复领）",
        "assets.py",
        '    pending = [c for c in claimable if "已领取" not in (c.get("status") or "")]',
        '    pending = [c for c in claimable if "已领取" in (c.get("status") or "")]',
        True,
    ),
    (
        "N16-同名的今日基准价取 max（更便宜的活动被判成「今天已有」）",
        "campaign.py",
        "            known[c.title] = c.price_yuan if old is None else min(old, c.price_yuan)",
        "            known[c.title] = c.price_yuan if old is None else max(old, c.price_yuan)",
        True,
    ),
    (
        "N17-对调邻域的适用校验弄反（table 的 key/value 反了）",
        "coupon_engine.py",
        "            if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):",
        "            if i1 not in table.get(c1, []) or i2 not in table.get(c2, []):",
        False,
    ),
    (
        "N18-宜的排序漏了负号（最省的那条被排到最后）",
        "almanac.py",
        "    positives.sort(key=lambda r: -r.saved_fen)",
        "    positives.sort(key=lambda r: r.saved_fen)",
        False,
    ),
    (
        "N19-贪心改成「最不受约束的券先占坑」",
        "coupon_engine.py",
        "        order = sorted(table, key=lambda c: len(table[c]))",
        "        order = sorted(table, key=lambda c: len(table[c]), reverse=True)",
        False,
    ),
    (
        "N20-README 的「六段」漂回「七段」（散文里的数字没人管）",
        "README.md",
        "一共**六段**",
        "一共**七段**",
        False,
    ),
    (
        "N21-自检失败时不汇总失败项（几百行 PASS 里悄悄红了）",
        "selftest.py",
        "    out.append(\"   自检不过 = 项目当前状态与预期不符。请不要跳过这几条。\")\n"
        "    return out",
        "    return []",
        False,
    ),
    # ---- 以下 16 处是第六轮（变异猎手）自造的 ----
    # 专打前五轮一次都没点名过的代码：route 的路程换算、insights 的判据、
    # audit 的候选采样、persona 的段位边界、snapshot 的截断方向、
    # assets 的截止时刻、campaign 正文取价、nutrition 的菜单配对、
    # cli 的数量装配、oracle 的拆行挂券、coupon_engine 的到期日与主循环。
    (
        "R1-路程忘了往返（round_m 只算单程，耗时与路费统统一半）",
        "route.py",
        "    round_m = meters * 2",
        "    round_m = meters",
        True,
    ),
    (
        "R2-rank_stores 让缺货的店参与排序（缺货店 paid=0 会稳居第一）",
        "route.py",
        "        key=lambda q: (not q.comparable, q.total_fen, q.distance_m),",
        "        key=lambda q: (q.total_fen, q.distance_m),",
        True,
    ),
    (
        # 原文重写过：现在两单**依次**挑券（先跑的那单挑走之后，后跑的只能用剩下
        # 的券），并且两单都随单购的不算数。三条规矩各配一条变异。
        "R3-拆单建议放宽成「不更贵就推荐」（省 ¥0 也当省钱建议吐出来）",
        "insights.py",
        "        if best_total is None or best_total >= whole_fen:\n            continue",
        "        if best_total is None or best_total > whole_fen:\n            continue",
        True,
    ),
    (
        "R3b-拆单又把同一张券在两单里各用一次（报出拿不到的价）",
        "insights.py",
        "                left = [c for k, c in enumerate(coupons) if k not in taken]",
        "                left = list(coupons)",
        True,
    ),
    (
        "R3c-拆单不再排除「两单都随单购」（账面上便宜、付款时多付一份卡费）",
        "insights.py",
        "                if b1.with_order and b2.with_order:\n                    continue",
        "                pass",
        True,
    ),
    (
        "R4-候选采样不再补最贵那件（满减券被系统性地判成废券）",
        "audit.py",
        "    if uniq[-1] not in picks:\n        picks[-1] = uniq[-1]",
        "    picks = picks[:probe]",
        True,
    ),
    (
        "R5-候选去重键丢了价格（同码不同规格只试一件，报告少试品）",
        "audit.py",
        "        k = (it.product_code, it.effective_price_fen())",
        "        k = it.product_code",
        True,
    ),
    (
        "R6-段位门槛改成严格大于（正好攒到 ¥10.00 反倒降一级）",
        "persona.py",
        "        if saved_fen >= need:",
        "        if saved_fen > need:",
        True,
    ),
    (
        "R7-快照截断方向写反（留最早的 50 条，最新的记录被丢掉）",
        "snapshot.py",
        "    entries = entries[-MAX_ENTRIES:]",
        "    entries = entries[:MAX_ENTRIES]",
        True,
    ),
    (
        "R8-卡包截止时刻取成开始时刻（今天才开始的券被报成今天到期）",
        "assets.py",
        '        return datetime.strptime(m.group(2), "%Y-%m-%d %H:%M")',
        '        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")',
        True,
    ),
    (
        "R9-正文兜底取价取成 max（「最低 ¥X」印的是最贵那个）",
        "campaign.py",
        "    return min(vals) if vals else None",
        "    return max(vals) if vals else None",
        True,
    ),
    (
        # 配对改成「归一化后完全相等」之后，去重键从菜名换成了 canon_key。
        "R10-营养配对允许一条数据被多个菜串用（营养值重复计数）",
        "nutrition.py",
        "        if canon_key in used:\n            continue",
        "        pass",
        True,
    ),
    (
        "R10b-营养配对退回双向子串（单品拿套餐热量、套餐拿单品热量）",
        "nutrition.py",
        "        hit = by_canon.get(_canon(nm))\n        if hit is None:\n            continue",
        "        hit = None\n        for k, v in by_canon.items():\n            if k in _canon(nm) or _canon(nm) in k:\n                hit = v\n                break\n        if hit is None:\n            continue",
        True,
    ),
    (
        "R11-装配购物车时把数量丢了（薯条x2 只按 1 件算价）",
        "cli.py",
        '        cart.append(CartItem(**{**base.__dict__, "quantity": qty}))',
        '        cart.append(CartItem(**{**base.__dict__}))',
        True,
    ),
    (
        "R12-拆行后每行都挂券（一张券只用一次，接口会按两行收）",
        "oracle.py",
        "            if ci is not None and k == 0:",
        "            if ci is not None:",
        True,
    ),
    (
        "R13-贪心占坑后不再 break（一张券被挂到多个商品上）",
        "coupon_engine.py",
        "                    mapping[ii] = ci\n                    used_items.add(ii)\n                    break",
        "                    mapping[ii] = ci\n                    used_items.add(ii)",
        True,
    ),
    (
        "R14-券有效期取成开始日（还有 6 天的券被报成今天到期）",
        "coupon_engine.py",
        '    return hits[-1] if hits else ""',
        '    return hits[0] if hits else ""',
        True,
    ),
    (
        "R15-最贵换项判据写反（问「最多加多少」答了个最便宜的）",
        "meal_detail.py",
        "                if best is None or c.diff_fen > best[1].diff_fen:",
        "                if best is None or c.diff_fen < best[1].diff_fen:",
        True,
    ),
    (
        "R16-打烊判定放宽成 falsy（字段缺失的门店被误杀）",
        "cli.py",
        '    closed = [s for s in stores if s.get("businessStatus") is False]',
        '    closed = [s for s in stores if not s.get("businessStatus")]',
        True,
    ),
    # ---------------------------------------------------------------- README 版式
    # 版式约束跟功能约束一样，不写断言就是装饰品 —— 而且它们比功能更容易漂：
    # 谁改 README 都可能顺手把许可证那行删了、把徽章拆成几行、把限制挪到文末。
    # 这些变异全是「真实会发生的手滑」，不是编出来的。
    (
        "S1-首屏免责漏写「Token 不出本机」（陌生人最关心的那一条没了）",
        "README.md",
        "> **MIT 开源 · 非麦当劳官方产品 · 不碰支付、不代下单，只读公开算价接口 · Token 只留在本机**",
        "> **MIT 开源 · 非麦当劳官方产品 · 不碰支付、不代下单，只读公开算价接口**",
        True,
    ),
    (
        "S2-README 限制取消前置（红线不再在首屏出现）",
        "README.md",
        "> 觉得这几条重要的话，完整的写在 [红线](#redline) 一节 —— 不用你翻到最底下。",
        "> 更多说明见文末的红线一节。",
        True,
    ),
    (
        # 只改链接、不碰行尾数字：导航表那列「大概几行」每次压缩 README 都会变，
        # 把数字写进 old 会让这条变异在下一轮排版调整后静默失效（S3 已经跳过一次了）。
        "S3-导航表出现死链（改了锚点忘了改跳过去的链接）",
        "README.md",
        "[17 个命令都是干嘛的](#commands)",
        "[17 个命令都是干嘛的](#command-list)",
        True,
    ),
    (
        "S4-徽章被拆行（首屏多吃掉两行垂直空间）",
        "README.md",
        '![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)',
        "\n![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)\n",
        True,
    ),
    (
        "S5-代码地图改回名不符实的「目录」",
        "README.md",
        "## 代码地图",
        "## 目录",
        True,
    ),
    (
        "S6-删掉导航入口（近千行 README 没有地图）",
        "README.md",
        "**在这一页里找东西：** [装到我电脑上](#install)",
        "**在这一页里找东西：** 参见下文。",
        True,
    ),
    (
        "S7-删掉 License 徽章（首屏再也看不到协议）",
        "README.md",
        ' [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)',
        "",
        True,
    ),

    # ---- 第六轮：全局体检后新增的用户可见保护 ----
    (
        "T1-429 退避判据放宽成「任何错误都退避 5~30s」",
        "mcd_client.py",
        '    if getattr(last_exc, "rate_limited", False):',
        "    if last_exc is not None:",
        True,
    ),
    (
        "T2-限流不再打标记（退避悄悄退化成 0.5 秒）",
        "mcd_client.py",
        "                    limited = True",
        "                    limited = False",
        True,
    ),
    (
        "T3-缓存键漏掉购物车指纹（换车复用旧价，还会静默算错）",
        "oracle.py",
        "        key = (cart_fingerprint(items), assignment.signature())",
        "        key = (assignment.signature(),)",
        True,
    ),
    (
        # 原文必须能**唯一**钉住缓存命中那一处。
        # 这里曾经只写 `self._budget.refund()`,而引擎里后来又多了一处
        # 「算价失败也退还预扣」的 refund，缩进一模一样 —— 替换打到了新添的
        # 那一处，缓存退款反而没被改坏，断言于是不红，这条变异悄悄变成 0 条。
        # 教训：两处代码长得像时，原文必须带上能区分它们的上下文。
        "T4-缓存命中照样扣预算（白拿的请求也吃掉额度）",
        "coupon_engine.py",
        "            # 上游缓存命中：把刚才预扣的那一趟还回去。\n"
        "            # 预算保护的是真实网络请求，不是 evaluate 的调用次数。\n"
        "            self._budget.refund()\n"
        "            self.calls -= 1",
        "            # 上游缓存命中：把刚才预扣的那一趟还回去。\n"
        "            # 预算保护的是真实网络请求，不是 evaluate 的调用次数。\n"
        "            pass",
        True,
    ),
    (
        # 打得必须是源头：源码里标记未收敛的有两处，只抹掉 optimize 收尾那句
        # 的话 `_stopped_early` 还在标，断言照样通过 —— 这条变异就形同虚设。
        "T5-预算花光也谎报「已是最优」（抹掉未收敛标记的源头）",
        "coupon_engine.py",
        "        current.converged = False\n        self._stopped = True",
        "        self._stopped = True",
        True,
    ),
    (
        "T6-未算完却什么都不说（用户以为自己拿到了最优解）",
        "cli.py",
        "    if converged:\n        return []",
        "    if True:\n        return []",
        True,
    ),
    (
        "T7-未预料异常直接把 traceback 甩给使用者",
        "cli.py",
        '    shown = os.environ.get("MCD_BUTLER_DEBUG", "").strip() not in ("", "0")',
        "    shown = True",
        True,
    ),
    (
        "T8-会员卡编码又写回面向用户的标签里",
        "cli.py",
        '                label=item.discount_type or "随单购",',
        '                label=f"{item.discount_type}（membershipCode={item.membership_code}）",',
        True,
    ),
    (
        "T9-订单号又打回屏幕（能在客服系统反查到手机号/地址）",
        "cli.py",
        "    return f\"历史订单（{order.get('createTime', '')[:10]}）\"",
        "    return f\"历史订单 {order.get('orderId', '')} @ {order.get('createTime', '')}\"",
        True,
    ),
    (
        "T10-中文对齐退回 :<N（中文列全部错位）",
        "cli.py",
        '    return f"{\' \' * indent}- {_pad(_clip(name, 18), 18)}{qty}¥{FEN(price_fen)}{suffix}"',
        '    return f"{\' \' * indent}- {name:<18}{qty}¥{FEN(price_fen)}{suffix}"',
        True,
    ),
    (
        "T11-算价接口报错又把 Python 类型名抛给用户",
        "oracle.py",
        '            raise McpError("麦当劳算价接口这次返回的数据格式不对，请稍后再试。", data)',
        '            raise McpError(f"算价接口返回了非对象结构：{type(data).__name__}")',
        True,
    ),
    (
        # 原文里那句 `.format(code=...)` 是空转的（文案里没有占位符），已删掉。
        "T12-HttpError 的原始响应体又透传上屏（可能含个人信息）",
        "mcd_client.py",
        "                    msg = _HTTP_UNAVAILABLE_MSG\n                    limited = False",
        '                    msg = f"HTTP {exc.code}: {detail}"\n                    limited = False',
        True,
    ),
    (
        "T13-敏感信息：快照又打出绝对路径（家目录含本机用户名）",
        "cli.py",
        '    return "\\n已存进本机账本（~/.mcd-butler/），下次可用 replay 对比这单的价格变动。"',
        '    return f"\\n已存快照 → {path}"',
        True,
    ),
    (
        "T14-compare 表的金额又丢掉 ¥ 符号（看不出是元还是分）",
        "cli.py",
        '        + f"¥{q.paid_fen / 100:>8.2f}"',
        '        + f"{q.paid_fen / 100:>9.2f}"',
        True,
    ),

    # ---- 第七轮：本轮「管家不虚报」专项 ----
    # 每一条都对应一处「改坏之后用户会被骗，而屏幕上毫无异常」。
    (
        "U1-量纲守卫的菜单合计读错字段（守卫永远不进，整个自杀式保护失效）",
        "oracle.py",
        "    menu_total = sum(it.effective_price_fen() * it.quantity for it in items)",
        '    menu_total = sum(getattr(it, "price_fen", 0) or 0 for it in items)',
        True,
    ),
    (
        # 原来那种「跨商品取全局最小值」的写法被结构性地消灭了：
        # best_min_base 只在「这件商品更值」时才更新，天然绑在最值钱那件商品上，
        # 没法再用一处单行变异复现。所以改打同一条判据的另一种失效：
        # 干脆不记这个价 → 判据永远不成立 → 随单购更划算时一句都不提醒。
        "U2-体检不再记录跨档位最低价（随单购更划算时一句都不提醒）",
        "audit.py",
        "                best_min_base = min_base_any",
        "                best_min_base = None",
        True,
    ),
    (
        "U4-黄历把「减不动」说成「没有券」（probed 退回 covered）",
        "almanac.py",
        "            if i.product_code not in probed_codes",
        "            if i.product_code not in covered",
        True,
    ),
    (
        "U5-黄历不再把试过价的商品记进 probed_codes",
        "almanac.py",
        "            probed_codes.add(code)\n",
        "",
        True,
    ),
    (
        "U6-compare 预算退回 max(6, …)（总数能打到 --budget 的两三倍）",
        "cli.py",
        "    per = budget // n\n    keep = n_stores\n    if per < MIN_CALLS_PER_STORE and n_stores > 1:\n        keep = min(n_stores, max(1, budget // MIN_CALLS_PER_STORE))\n        per = budget // max(1, keep)\n    return per, keep",
        "    per = max(MIN_CALLS_PER_STORE, budget // n)\n    return per, n_stores",
        True,
    ),
    (
        "U7-距离未知的店又当成 0 米（假路费把它送上「综合最划算」第一名）",
        "cli.py",
        "        dist, dist_known = _distance_of(st)",
        "        dist, dist_known = 0, True",
        True,
    ),
    (
        "U8-距离未知的店照样参与综合排序",
        "route.py",
        "        return not self.missing and not self.distance_unknown",
        "        return not self.missing",
        True,
    ),
    (
        "U9-服务端时间又留着时区（naive−aware 相减炸掉，到期提醒静默消失）",
        "clock.py",
        "        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt",
        "        return dt",
        True,
    ),
    (
        "U10-卡包没查到又说成「没有今天到期的」（到期作废不可逆）",
        "cli.py",
        "    for line in _due_today_report(mine, lookup_error):\n        print(line)",
        "    print(f'卡包 {len(mine)} 张，没有今天到期的。')",
        True,
    ),
    (
        "U11-「用了 N 张券」退回数挂上去的（含被随单购抵消、一分没减的那张）",
        "cli.py",
        "        work = _count_working_coupons(optimizer.oracle, cart, best)",
        "        work = len(best.mapping)",
        True,
    ),
    (
        "U12-optimize 不再打印「照着做」（算了答案却不告诉人家手怎么点）",
        "cli.py",
        "    for line in _how_to_order(cart, coupons, best, best.converged, now=server_now(client)):",
        "    for line in ():",
        True,
    ),
    (
        "U13-条件价不再单独告警（标价 ¥29 实付 ¥57 又藏回行尾）",
        "cli.py",
        "    for line in _condition_warning(cart):\n        print(line)",
        "    pass",
        True,
    ),
    (
        "U14-活动日历又替用户判断「不急」（紧迫性是他的事，不是管家的）",
        "campaign.py",
        "—— 今天没有这天才有，等不等你自己定",
        "—— 今天没有，不急可以等这天",
        True,
    ),
    (
        "U15-健康度又不给分母定义（用户只能猜 60% 是在说谁）",
        "audit.py",
        '    lines.append(\n        f"  （健康度 = 真能省钱的 {worth} 张 ÷ 体检到的 {counted} 张；"',
        "    if False:\n        lines.append(",
        True,
    ),
    (
        "U16-券面额落差说成「实测」（离线演示一个请求都没发过）",
        "audit.py",
        '                f"实际只省 ¥{yuan(v.value_fen)}"',
        '                f"实测只省 ¥{yuan(v.value_fen)}"',
        True,
    ),
    # ---------- 第七轮：跨命令口径 / 不可逆损失 ----------
    (
        "V1-引擎不再先算裸买下界（预算小时能给出比不挂券更贵的解）",
        "coupon_engine.py",
        "        bare = Assignment(mapping={}, with_order=None)\n"
        "        if self.try_evaluate(cart, bare) is not None:\n"
        "            best = bare\n",
        "",
        True,
    ),
    (
        "V2-段位又改用省额（历史订单无裸买价 → 恒为 0 → optimize 收尾永远最低档）",
        "cli.py",
        '        r = rank_of(s.get("real_paid_fen", 0))\n'
        "        parts = []",
        '        r = rank_of(s.get("real_saved_fen", 0))\n'
        "        parts = []",
        True,
    ),
    (
        "V3-账单金额又按类型读（float/字符串被静默丢成 ¥0）",
        "persona.py",
        '        base = as_fen(e.get("baseline_fen"))\n'
        '        paid = as_fen(e.get("paid_fen"))\n'
        "        if base > paid:",
        '        base = e.get("baseline_fen") or 0\n'
        '        paid = e.get("paid_fen") or 0\n'
        "        if isinstance(base, int) and isinstance(paid, int) and base > paid:",
        True,
    ),
    (
        "V4-账本读不出来又翻译成「账本是空的」（撤回返回 0）",
        "snapshot.py",
        "    entries, broken = _read()\n    if broken:\n        return -1\n    if not entries:\n        return 0",
        "    entries, broken = _read()\n    if broken or not entries:\n        return 0",
        True,
    ),
    (
        "V5-取最近一单又退回 entries[-1]（拿自己的测算当基准，diff 恒为 0）",
        "snapshot.py",
        '    for e in reversed(entries):\n        if e.get("kind") == "order":\n            return e\n'
        "    return entries[-1] if entries else None",
        "    return entries[-1] if entries else None",
        True,
    ),
    (
        "V6-打烊店又不提醒（白跑一趟不可逆）",
        "cli.py",
        '    if store.get("businessStatus") is not False:\n        return []\n',
        "    return []\n",
        True,
    ),
    (
        "V7-随单购又把卡费说成「另算」（不说不退、不说下次还得再买）",
        "cli.py",
        '            "卡费另付、**买了不退**，下一单还得再买一次才享得到"',
        '            "（卡费另算）"',
        True,
    ),
    (
        "V8-又不报算价时刻（跨过时段边界价格就变了）",
        "cli.py",
        '    if isinstance(now, _dt):\n',
        "    if False:\n",
        True,
    ),
    # ---- 下面这几条守的是搜索算法本身。
    # 它们对应的数字都是 `scripts/bench.py --ablate` 量出来的，不是「觉得应该有用」。
    (
        "V9-抢占邻域又没了（有一类最优解只有它走得到，小场景命中率掉到 71%）",
        "coupon_engine.py",
        "        if not wide:\n            return\n",
        "        return\n",
        True,
    ),
    (
        "V10-卡住后又不走平局步（只肯下降，等于在局部最优原地认输）",
        "coupon_engine.py",
        "                accept_equal=True,\n",
        "                accept_equal=False,\n",
        True,
    ),
    (
        "V11-邻域计数又退回上界（预算建议会翻倍，用户白等一场）",
        "coupon_engine.py",
        "        + sum(  # 邻域 5：对调 —— 还要两边都适用才成立\n"
        "            1\n"
        "            for (i1, c1), (i2, c2) in itertools.combinations(list(mapping.items()), 2)\n"
        "            if i1 in table.get(c2, ()) and i2 in table.get(c1, ())\n"
        "        )\n",
        "        + m * (m - 1) // 2\n",
        True,
    ),
    (
        "V12-预算建议又改成往下压（报个比现在还小的数等于没说）",
        "coupon_engine.py",
        "    return max(rounded, current)",
        "    return min(rounded, current)",
        True,
    ),
    (
        "V13-预算不够时又去逐个档位均分（每档都半途而废，6 件 7 券命中掉到 0%）",
        "coupon_engine.py",
        "            targets = screened if deep_all else screened[:1]",
        "            targets = screened",
        True,
    ),
    (
        # 同理，原文必须带上能区分的上下文，且**真的删掉那两行** ——
        # 早先只改了注释、没动 refund/raise，等于什么都没改坏，自然不红。
        "V14-算价失败又不退还预扣（一次抖动白白吃掉额度）",
        "coupon_engine.py",
        "            self._budget.refund()\n"
        "            self.calls -= 1\n"
        "            raise",
        "            raise",
        True,
    ),
]


#: 「此刻源码正被改坏」的标记。存在即表示上一次没走到还原那一步。
#: 放在 .workbuddy/ 下（已被 .gitignore 忽略），不会污染仓库。
_MARKER = ROOT / ".workbuddy" / "mutate-in-progress.json"


def _marker_recover() -> str | None:
    """启动时若发现上次「改坏后没还原」的标记，就地还原并说明。

    为什么需要它：这个脚本的工作方式是「改坏 → 跑自检 → 还原」。
    一旦在中间被打断（Ctrl-C、超时被 SIGTERM、IDE 杀进程），源码就停在
    改坏的状态，而 `git diff` 看起来只是普通的一行改动 ——
    用户会带着一个坏仓库继续干活，还以为是自己的改动。
    真的发生过：`clamp_top` 被留在 `return top`，自检两轮莫名少 2 条。

    为什么不直接拿「改坏版文本」去源码里搜：试过，误报一大片 ——
    很多改坏版本身就是常见代码片段（`except Exception:\n return None`
    在正常代码里到处都是），数出 11 处「残留」，其实全是误伤。
    所以改成标记文件：改坏前写、还原后删，精确且能自动恢复。

    **还原前必须先对账**：只有当文件当前内容**正是**那条变异的改坏版时，才动它。
    否则说明被打断之后源码又被改过（可能是用户自己动手修的），
    这时拿备份覆盖等于把人的改动抹掉 —— 只警告，不覆盖。
    """
    if not _MARKER.exists():
        return None
    try:
        data = json.loads(_MARKER.read_text(encoding="utf-8"))
        rel, backup = data["file"], data["backup"]
        path = (SRC / rel) if (SRC / rel).is_file() else (ROOT / rel)
        mut = next((m for m in MUTATIONS if m[0] == data["name"]), None)
        if mut is None:
            _MARKER.unlink()
            return f"清掉了一条对不上号的旧标记（{data['name']}）"
        expected_bad = backup.replace(mut[2], mut[3], 1)
        if path.read_text(encoding="utf-8") != expected_bad:
            _MARKER.unlink()
            return (
                f".. 上次跑 {data['name']} 时被打断，但 {rel} 现在已经不是当时的改坏版"
                f"（多半已经还原过、或被人动过）—— 不敢覆盖，已清掉标记，请自行确认"
            )
        path.write_text(backup, encoding="utf-8", newline="\n")
        _MARKER.unlink()
        return f"自动还原了上次被打断的改动：{data['name']}（{rel}）"
    except Exception as exc:  # noqa: BLE001
        return f"发现上次的标记但自动还原失败（{exc!r}），请手动检查后删除 {_MARKER}"


def _preflight() -> list[str]:
    """开工前查一件事：每条变异的原文必须**唯一**命中。

    替换用的是 `replace(原文, 改坏版, 1)`，只换第一处。两处代码长得一样时，
    改坏的很可能是另一处 —— 于是这条变异悄悄变成 0 条变红（P0），
    而**从外面完全看不出来**：它照样打印、照样计数、照样「全部抓住」。
    真的发生过：T4 打偏到后来新添的那处 refund 上，缓存退款反而没被改坏，
    这条守卫就这么静默失效了一整轮。
    """
    ambiguous: list[str] = []
    for name, fn, orig, _bad, _q in MUTATIONS:
        path = ROOT / fn if fn == "README.md" else SRC / fn
        if not path.exists():
            continue  # 自变异那条（scripts/mutate.py）由它自己管
        hits = path.read_text(encoding="utf-8").count(orig)
        if hits != 1:
            ambiguous.append(f"{name}（{fn}）：命中 {hits} 处")
    return ambiguous


def run_selftest() -> tuple[int, int, int]:
    """跑一次自检，返回 (通过数, 总数, 退出码)。

    为什么连退出码一起返回：早先只看数字，自检**崩掉**时返回 (-1, -1)，
    于是 `red = -1 - (-1) = 0`，会被误报成「P0 无防护」。
    崩是崩，无防护是无防护 —— 崩说明改动被**发现**了（只是方式很难看），
    两者必须分开报，否则修的人会朝错误的方向使劲。

    `--twice`：连跑两轮并比对断言总数，用来验证自检本身可重入。
    `selftest` 单跑一轮是发现不了「结果列表没清空」的 ——
    污染会同时放大分子和分母，打印出来的还是「N/N 通过」且全绿。
    """
    env = {"PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli", "selftest", "--twice"],
        cwd=str(ROOT),
        capture_output=True,
        env={**__import__("os").environ, **env},
    )
    text = proc.stdout.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.endswith("通过") and "/" in line:
            left, right = line.replace("通过", "").strip().split("/")
            try:
                return int(left), int(right), proc.returncode
            except ValueError:
                break
    return -1, -1, proc.returncode


def main() -> int:
    quick = "--quick" in sys.argv
    # --only=关键字：只跑名字里带这个关键字的变异。
    # 改完引擎想确认「新加的几条有没有防护」时，不必把 131 条全跑一遍
    # （全量约 12 分钟，中途被打断过两次）。
    # 逗号分隔多个关键字，方便分批（一次全跑 131 条约 12 分钟，容易被打断）
    only = next(
        (a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")), None
    )
    if only:
        keys = [k for k in only.split(",") if k]
        mutations = [m for m in MUTATIONS if any(k in m[0] for k in keys)]
        if not mutations:
            print(f"没有名字里带「{only}」的变异。")
            return 1
    else:
        mutations = [m for m in MUTATIONS if m[4]] if quick else MUTATIONS

    recovered = _marker_recover()
    if recovered:
        print(f".. {recovered}\n")

    ambiguous = _preflight()
    if ambiguous:
        # 不阻断：历史上有几条本来就命中多处，替换第一处仍然有效。
        # 但必须**喊出来** —— 静默打偏正是 T4 失效那一轮的样子。
        print(f".. 有 {len(ambiguous)} 条变异的原文不是唯一命中，替换会打在第一处：")
        for item in ambiguous:
            print(f"   - {item}")
        print()

    baseline_pass, baseline_total, baseline_rc = run_selftest()
    if baseline_total <= 0:
        print("跑不出自检结果，先修好项目再跑变异测试。")
        return 1
    print(f"基线：{baseline_pass}/{baseline_total} 通过\n")
    if baseline_pass != baseline_total or baseline_rc != 0:
        print("基线就是红的，先修红再跑变异测试。")
        return 1

    failures: list[str] = []
    print(f"{'变异':<40} {'变红':>4}  判定")
    print("-" * 66)
    for name, rel, old, new, _q in mutations:
        # src/ 下的文件写相对名即可，根目录文件（README）写全名
        path = (SRC / rel) if (SRC / rel).is_file() else (ROOT / rel)
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"{name:<40} {'?':>4}  跳过（原文已变，需更新 mutate.py）")
            failures.append(name)
            continue
        _BACKUPS[path] = original
        # 改坏之前先落标记（内含原文），被 SIGTERM 打断时下次启动能自动还原
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.write_text(
            json.dumps({"name": name, "file": rel, "backup": original},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            path.write_text(
                original.replace(old, new, 1), encoding="utf-8", newline="\n"
            )
            passed, total, rc = run_selftest()
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")
            _MARKER.unlink(missing_ok=True)
            _BACKUPS.pop(path, None)

        if total <= 0:
            # 自检整个崩了：改动确实被发现了，但方式是「一整屏 traceback」，
            # 对用户毫无帮助。算抓住，但单独标出来督促补一条体面的断言。
            print(f"{name:<40} {'崩':>4}  崩溃（算抓住，但提示很难看）")
            continue
        red = total - passed
        if red == 0 and rc == 0:
            verdict = "P0 无防护"
            failures.append(name)
        elif red == 0:
            verdict = "OK（退出码非 0）"
        else:
            verdict = "OK"
        print(f"{name:<40} {red:>4}  {verdict}")

    print()
    if failures:
        print(f"!! {len(failures)}/{len(mutations)} 处改坏后自检没有反应：")
        for f in failures:
            print(f"   - {f}")
        return 1
    print(f"全部 {len(mutations)} 处变异都被自检抓住。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
