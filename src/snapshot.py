"""本地订单快照。

为什么需要它：
`order-list` 对很多账号返回的是空的 `data: {}` —— 尤其是从没在麦当劳下过单
的账号（比如住处附近没有门店的人）。这类账号**永远**拿不到历史订单，
「复刻上一单」这个功能就彻底废了。

所以改个思路：**上一次算过的那单，就是你的上一单。**
`optimize` 跑完之后把购物车和成交价存到本地，下次 `replay` 时按门店重新拉
今天的菜单和券，重算一遍，告诉你这单是涨了还是跌了。

存储位置固定在 `~/.mcd-butler/`，**不进仓库**。
快照里含门店和商品，属于个人数据，不能跟着代码一起提交。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path.home() / ".mcd-butler"
SNAPSHOT_FILE = SNAPSHOT_DIR / "snapshots.json"

FMT = "%Y-%m-%d %H:%M"


def _ensure_dir() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def save(entry: dict) -> Path:
    """追加一条快照，返回文件路径。"""
    _ensure_dir()
    entries = load_all()
    entry = dict(entry)
    entry.setdefault("saved_at", datetime.now().strftime(FMT))
    entries.append(entry)
    entries = entries[-50:]  # 只留最近 50 条，别无限膨胀
    SNAPSHOT_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return SNAPSHOT_FILE


def load_all() -> list[dict]:
    if not SNAPSHOT_FILE.exists():
        return []
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def latest(store_code: str | None = None) -> dict | None:
    """取最近一条快照；指定 store_code 时只在该门店里找。"""
    entries = load_all()
    if store_code:
        entries = [e for e in entries if e.get("store_code") == store_code]
    return entries[-1] if entries else None


def describe(entry: dict) -> str:
    """一行人类可读摘要，用于列出快照。"""
    when = entry.get("saved_at", "?")
    store = entry.get("store_name") or entry.get("store_code") or "?"
    paid = entry.get("paid_fen")
    paid_str = f"¥{paid / 100:.2f}" if isinstance(paid, int) else "?"
    n = len(entry.get("items") or [])
    return f"{when}　{store}　{n} 件　成交 {paid_str}"


def entry_from_cart(
    cart: list[Any],
    paid_fen: int,
    baseline_fen: int,
    discount_fen: int,
    store_code: str,
    store_name: str,
    city: str,
    keyword: str,
    coupon_titles: list[str],
    with_order_label: str,
) -> dict:
    """把 optimize 的结果打包成快照条目。"""
    return {
        "saved_at": datetime.now().strftime(FMT),
        "source": "optimize",
        "store_code": str(store_code),
        "store_name": store_name,
        "city": city,
        "keyword": keyword,
        "items": [
            {
                "code": i.product_code,
                "name": i.name,
                "qty": i.quantity,
            }
            for i in cart
        ],
        "paid_fen": int(paid_fen),
        "baseline_fen": int(baseline_fen),
        "discount_fen": int(discount_fen),
        "coupon_titles": list(coupon_titles),
        "with_order_label": with_order_label,
    }
