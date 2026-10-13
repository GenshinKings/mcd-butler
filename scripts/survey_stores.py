"""调查真实券池规模：引擎在真实账号上到底面对多大的搜索空间。

bench.py 假设 6 件商品 7 张券（解空间 11 万）。但真实账号在一家门店
能挂的券可能只有一两张 —— 如果真是这样，「是不是最优解」这个问题的
答案就完全不同：小到能穷举，就不该谈「逼近」。

只读调用：query-nearby-stores / query-store-coupons / query-meals。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cli import _parse_coupons, _parse_menu  # noqa: E402
from src.mcd_client import McdMcpClient, load_dotenv  # noqa: E402

# (city, keyword) —— 空关键词接口会直接报失败，必须给地标词
SPOTS = [
    ("上海", "人民广场"),
    ("上海", "陆家嘴"),
    ("北京", "王府井"),
    ("广州", "体育西路"),
    ("深圳", "福田"),
    ("成都", "春熙路"),
]


def main() -> int:
    load_dotenv()
    client = McdMcpClient()
    rows = []
    for city, kw in SPOTS:
        try:
            stores = client.data(
                "query-nearby-stores",
                {"beType": 1, "searchType": 2, "city": city, "keyword": kw},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{city}: 取门店失败 {exc}")
            continue
        if not isinstance(stores, list):
            continue
        for s in stores[:3]:
            code = s.get("storeCode")
            if not code:
                continue
            try:
                cps = _parse_coupons(
                    client.data(
                        "query-store-coupons",
                        {"storeCode": code, "orderType": 1, "beType": 1},
                    )
                )
                menu = _parse_menu(
                    client.data("query-meals", {"storeCode": code, "orderType": 1, "beType": 1})
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {city}/{code}: 取数失败 {exc}")
                continue
            covered = len({pc for c in cps for pc in c.product_codes if any(
                i.product_code == pc for i in menu)})
            rows.append(
                (city, str(s.get("storeName"))[:16], code, len(cps), len(menu), covered)
            )
            print(
                f"  {city} {str(s.get('storeName'))[:16]:<18} {code}: "
                f"券 {len(cps)} 张 / 菜单 {len(menu)} 件 / 券覆盖商品 {covered} 件"
            )

    print(f"\n{'=' * 60}")
    if not rows:
        print("一家店都没取到。")
        return 1
    ns = sorted(r[3] for r in rows)
    print(f"取样 {len(rows)} 家门店")
    print(f"  券池张数：最少 {ns[0]}，中位 {ns[len(ns) // 2]}，最多 {ns[-1]}")
    print(f"  券数分布：{[n for n in sorted(set(ns))]}")
    big = [r for r in rows if r[3] >= 4]
    print(f"  券 ≥4 张的门店：{len(big)}/{len(rows)} 家")
    for r in big[:5]:
        print(f"      {r[0]} {r[1]} {r[2]}: {r[3]} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
