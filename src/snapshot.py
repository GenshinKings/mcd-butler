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
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_DIR = Path.home() / ".mcd-butler"
SNAPSHOT_FILE = SNAPSHOT_DIR / "snapshots.json"

FMT = "%Y-%m-%d %H:%M"

MAX_ENTRIES = 50


def _ensure_dir() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # 快照里含门店和消费记录，属于个人数据 —— 目录只让自己能读。
    # Windows 上 chmod 基本不生效，但 Linux/macOS 上必须收紧，
    # 否则同一台机器的其他账号能直接读出你常去哪家店、每次花多少。
    try:
        os.chmod(SNAPSHOT_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass


def _read() -> tuple[list[dict], bool]:
    """读快照文件，第二个返回值表示**文件是否损坏**。

    必须区分「还没有快照」和「文件坏了」：
    原来的实现两种都返回 []，于是 save() 会把 [] 当成现有内容追加后写回，
    用户攒了半年的历史一瞬间全没了，而且没有任何提示。
    """
    if not SNAPSHOT_FILE.exists():
        return [], False
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return [], True
    if not isinstance(data, list):
        return [], True
    return data, False


def _write_atomic(entries: list[dict]) -> None:
    """先写临时文件再原子替换。

    直接 write_text 的话，写到一半断电/被杀进程，文件就是半个 JSON，
    下次读直接损坏。原子替换保证文件要么是旧的，要么是新的，不会是残缺的。
    """
    _ensure_dir()
    payload = json.dumps(entries, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(SNAPSHOT_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, SNAPSHOT_FILE)
        try:
            os.chmod(SNAPSHOT_FILE, 0o600)
        except (OSError, NotImplementedError):
            pass
    except BaseException:
        # 写失败就把临时文件收走，别在目录里留垃圾
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(entry: dict) -> Path:
    """追加一条快照，返回文件路径。"""
    entries, broken = _read()
    if broken:
        # 文件坏了：先把坏文件挪走留底，绝不直接覆盖 —— 用户也许还想捞回来
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = SNAPSHOT_FILE.with_name(f"snapshots.corrupt-{stamp}.json")
        try:
            SNAPSHOT_FILE.replace(backup)
            print(f"⚠️ 快照文件损坏，已备份到 {backup}，本次将重新开始记录。")
        except OSError:
            print(f"⚠️ 快照文件损坏且无法备份（{SNAPSHOT_FILE}），本次将重新开始记录。")
        entries = []

    entry = dict(entry)
    entry.setdefault("saved_at", datetime.now().strftime(FMT))
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]  # 只留最近 50 条，别无限膨胀
    _write_atomic(entries)
    return SNAPSHOT_FILE


def load_all() -> list[dict]:
    entries, broken = _read()
    if broken:
        print(f"⚠️ 快照文件读不出来（{SNAPSHOT_FILE}），本次按空处理，未做任何写入。")
    return entries


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
