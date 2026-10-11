"""变异测试：故意把核心逻辑改坏，看自检会不会翻红。

为什么要有这个脚本：
    自检全绿**不能证明**自检有效。上一轮把 60 处核心逻辑逐个改坏，
    其中 33 处改坏之后自检依然全绿 —— 那些断言是装饰品，
    它们守的是运气（恰好没人改那行），不是行为。

    这个脚本把「改坏 → 跑自检 → 数有几条变红」固化成一条命令，
    每次补完断言都能立刻验证补的是不是真的。

用法：
    python scripts/mutate.py            # 跑全部变异
    python scripts/mutate.py --quick    # 只跑最快的几条

判定：
    改坏后 0 条变红 = 该处没有回归防护（P0）。
"""

from __future__ import annotations

import io
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# (名字, 文件, 原文, 改坏成, 是否快速档)
MUTATIONS: list[tuple[str, str, str, str, bool]] = [
    (
        "401 降级为业务性失败",
        "mcd_client.py",
        'raise McpFatalError("Token 无效或已过期（HTTP 401）。", detail) from exc',
        'raise McpError("Token 无效或已过期（HTTP 401）。", detail) from exc',
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
        "卡包把限流降级为空",
        "assets.py",
        "    except McpFatalError:\n        raise",
        "    except Exception:\n        return {}",
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
        "vals = [float(m.group(1)) for m in _PRICE_RE.finditer(_strip_discounts(title))]",
        "vals = [float(m.group(1)) for m in _PRICE_RE.finditer(title)]",
        False,
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
        "N1-429 退避判据放宽成「任何错误都退避 5~30s」",
        "mcd_client.py",
        '    if last_exc is not None and "触发限流" in str(last_exc):',
        '    if last_exc is not None:',
        True,
    ),
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
        "N12-黄历时段边界写反（10:00 归早市、5:00 归深夜）",
        "almanac.py",
        "        if start <= h < end:",
        "        if start < h <= end:",
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
        "                if i1 not in table.get(c2, []) or i2 not in table.get(c1, []):",
        "                if i1 not in table.get(c1, []) or i2 not in table.get(c2, []):",
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
        "完整演示一共**六段**",
        "完整演示一共**七段**",
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
]


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
    mutations = [m for m in MUTATIONS if m[4]] if quick else MUTATIONS

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
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            passed, total, rc = run_selftest()
        finally:
            path.write_text(original, encoding="utf-8")

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
