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
        "演示券写死过期日期",
        "demo_data.py",
        "_DEMO_WITH_ORDER_DISCOUNT = {\"\": 0, \"麦金卡（随单购）\": 600}",
        "_DEMO_WITH_ORDER_DISCOUNT = {\"\": 0, \"麦金卡（随单购）\": 600}\n"
        "DEMO_COUPONS[0] = Coupon(coupon_id='C0', coupon_code='x', "
        "title='过期的券', product_codes=frozenset(), valid_until='2020-01-01')",
        False,
    ),
]


def run_selftest() -> tuple[int, int]:
    """跑一次自检，返回 (通过数, 总数)。"""
    env = {"PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli", "selftest"],
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
                return int(left), int(right)
            except ValueError:
                break
    return -1, -1


def main() -> int:
    quick = "--quick" in sys.argv
    mutations = [m for m in MUTATIONS if m[4]] if quick else MUTATIONS

    baseline_pass, baseline_total = run_selftest()
    if baseline_total <= 0:
        print("跑不出自检结果，先修好项目再跑变异测试。")
        return 1
    print(f"基线：{baseline_pass}/{baseline_total} 通过\n")
    if baseline_pass != baseline_total:
        print("基线就是红的，先修红再跑变异测试。")
        return 1

    failures: list[str] = []
    print(f"{'变异':<38} {'变红':>4}  判定")
    print("-" * 62)
    for name, rel, old, new, _q in mutations:
        path = SRC / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"{name:<38} {'—':>4}  跳过（原文已变，需更新 mutate.py）")
            failures.append(name)
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            passed, total = run_selftest()
        finally:
            path.write_text(original, encoding="utf-8")
        red = total - passed
        verdict = "P0 无防护" if red == 0 else "OK"
        print(f"{name:<38} {red:>4}  {verdict}")
        if red == 0:
            failures.append(name)

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
