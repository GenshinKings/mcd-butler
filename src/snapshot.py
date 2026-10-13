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

def _home_dir() -> Path:
    """家目录。不能放在模块级求值 —— HOME 未设置时 Path.home() 会抛
    RuntimeError，而且是 **import 阶段** 就炸，用户连「读不到家目录」
    这句提示都来不及看到（brag / forget / replay --list 会一起死）。"""
    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


SNAPSHOT_DIR = _home_dir() / ".mcd-butler"
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
            # 不打印绝对路径：家目录含本机用户名。文件名本身够了。
            print(f"⚠️ 账本文件损坏，坏的那份已另存为 {backup.name}，本次从头开始记。")
        except OSError:
            print("⚠️ 账本文件损坏且无法备份，本次从头开始记。")
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
        print("⚠️ 账本文件读不出来，本次按空处理，没有改动原来的文件。")
    return entries


def has_order_key(key: str) -> bool:
    """这笔真实订单记过了吗？

    `replay` 每次跑都会复刻同一笔历史订单 —— 不查重的话，
    把 replay 跑十遍就能把段位刷十遍，那和「把 optimize 多跑几次」是一回事。
    """
    if not key:
        return False
    return any(e.get("order_key") == key for e in load_all())


def latest(store_code: str | None = None) -> dict | None:
    """取最近一条**真实成交**；一笔真实成交都没有，才退回最近一条测算。

    为什么必须挑，不能简单取 `entries[-1]`：replay 曾经每跑一次就往账本里
    追加一条测算，于是"最近一条"永远是他自己上一次写的测算价。
    第二次跑 replay 就变成了「上次算出来的价」比「这次算出来的价」——
    两个测算值相减，永远是 ¥0.00。**真正的涨价只能被检出一次，之后一律"持平"**，
    而用户是被这个数字说服去/不去那家店的。
    """
    entries = load_all()
    if store_code:
        entries = [e for e in entries if e.get("store_code") == store_code]
    for e in reversed(entries):
        if e.get("kind") == "order":
            return e
    return entries[-1] if entries else None


def describe(entry: dict) -> str:
    """一行人类可读摘要，用于列出快照。"""
    when = entry.get("saved_at", "?")
    store = entry.get("store_name") or entry.get("store_code") or "?"
    paid = entry.get("paid_fen")
    paid_str = f"¥{paid / 100:.2f}" if isinstance(paid, int) else "?"
    n = len(entry.get("items") or [])
    tag = "成交" if entry.get("kind") == "order" else "测算"
    return f"{when}　{store}　{n} 件　{tag} {paid_str}"


# ---------------------------------------------------------------- 撤回
def delete_last(n: int = 1) -> int:
    """撤回最近 n 条快照，返回实际删除条数；**账本读不出来时返回 -1**。

    为什么要区分 -1 和 0：两者都表示"一条没删"，但意思完全相反 ——
    0 是"账本本来就是空的"，-1 是"账本读不出来，我没敢动它"。
    合并成 0 会让调用方打印"账本是空的，没有可撤回的记录"，
    用户于是以为自己没数据、甚至以为清空成功了，而数据其实还在盘上
    （只是解析失败）。把"没查到"说成"没有"，是最伤人的那一类谎。

    为什么要有这个：optimize 默认会把每一次测算写进账本，
    于是「输错店名随手一跑」「--budget 1 探测一下」也都被记成省钱记录。
    用户把 brag 生成的卡片发朋友圈之前，必须能把自己不认的条目删掉 ——
    否则那张卡片是一份他自己都解释不清的假成绩单。
    """
    entries, broken = _read()
    if broken:
        return -1
    if not entries:
        return 0
    n = max(int(n), 0)
    keep = entries[:-n] if 0 < n < len(entries) else ([] if n >= len(entries) else entries)
    removed = len(entries) - len(keep)
    if removed:
        _write_atomic(keep)
    return removed


def clear_all() -> int:
    """清空整本账，返回被清掉的条数。"""
    entries, _ = _read()
    if entries:
        _write_atomic([])
    return len(entries)


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
    kind: str = "estimate",
    order_key: str = "",
) -> dict:
    """把 optimize 的结果打包成快照条目。

    `kind` 区分两种性质完全不同的记录，混在一起就是撒谎：
        "estimate" 测算 —— 引擎算出「这么点能省多少」，用户未必真买了
        "order"    成交 —— 真实下单记录（来自 `order-list` 的历史订单）

    `order_key` 是订单号的 **哈希**，不是订单号本身。
    订单号能在客服系统反查到手机号与支付流水，屏幕上和磁盘里都不该有原文；
    但要防止同一笔订单被 `replay` 反复记账（那样段位又能刷了），
    所以留一个不可逆的指纹用于去重。
    """
    return {
        "saved_at": datetime.now().strftime(FMT),
        "source": "optimize" if kind != "order" else "order-list",
        "kind": kind,
        "order_key": str(order_key),
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
