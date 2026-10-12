"""麦乐送（外送）链路。

为什么单独一个模块
------------------
到店和麦乐送是**两套独立链路**：门店接口不同（`query-nearby-stores` vs
`delivery-query-stores`）、价格口径不同（外送价 + 配送费 + 打包费）。
本项目到店那条已经跑通并实测过，外送这条一直没接 —— 于是「只点外送的人」
打开这个工具，全程只能算到店价，等于产品对他不可用。

为什么不直接估算
----------------
外送价必须绑定**收货地址**才取得到真值（`delivery-query-stores` 要 addressId）。
实测作者账号 `delivery-query-addresses` 返回 `{"addresses": []}` —— 没有地址，
就没有任何真值可算。这时候拿「到店价 + 一个猜的配送费」凑一个数给用户，
是把估算冒充实测，这个项目从第一天起就不干这件事。

所以这个模块干的是：把「能不能算外送」这件事查清楚、说清楚，
能算就列出可配送门店，不能算就明说卡在哪一步、让用户知道该去哪儿补。
"""

from __future__ import annotations

import re

from .mcd_client import McdMcpClient, McpFatalError

_DIGIT_TAIL = re.compile(r"[0-9０-９]+[^0-9０-９]*$")


def mask_address(text: str) -> str:
    """收货地址脱敏：保留门牌号之前的区段，尾号打码。

    收货地址是能定位到一个人的数据。终端输出会被截图、被贴到 issue 里，
    命令行工具默认就该少露 —— 只留到「XX 小区 3 号楼」这一级。
    """
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return s[0] + "***" if s else ""
    head = s[:6]
    tail = s[6:]
    # 尾部的纯数字段（门牌/房号）整段打码，字母中文保留可做区分
    masked = _DIGIT_TAIL.sub(lambda m: "*" * len(m.group(0)), tail)
    return head + masked


def fetch_addresses(client: McdMcpClient) -> list[dict]:
    """收货地址列表。任何异常结构都当「没有地址」，不崩。"""
    try:
        payload = client.call_json("delivery-query-addresses")
    except McpFatalError:
        raise
    except Exception:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    raw = data.get("addresses")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "id": str(it.get("addressId") or it.get("id") or ""),
                "name": str(it.get("name") or it.get("receiverName") or ""),
                "address": str(it.get("address") or it.get("detailAddress") or ""),
                "tag": str(it.get("tag") or it.get("label") or ""),
            }
        )
    return out


def fetch_delivery_stores(client: McdMcpClient, address_id: str) -> list[dict]:
    """某个收货地址周边**可配送**的门店。"""
    try:
        payload = client.call_json(
            "delivery-query-stores", {"addressId": address_id}
        )
    except McpFatalError:
        raise
    except Exception:
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("stores") or data.get("list") or []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "code": str(it.get("storeCode") or ""),
                "name": str(it.get("storeName") or ""),
                "distance": it.get("distance"),
                "status": it.get("businessStatus"),
            }
        )
    return [s for s in out if s["code"]]


def describe(client: McdMcpClient) -> list[str]:
    """把「外送这条链路现在通不通」说清楚。返回可直接打印的行。"""
    lines: list[str] = []
    addrs = fetch_addresses(client)
    if not addrs:
        lines.append("收货地址：0 个")
        lines.append("")
        lines.append("麦乐送（外送）的价格要绑定收货地址才取得到真值 ——")
        lines.append("您的账号现在一个地址都没有，所以小的算不出外送这一单。")
        lines.append("")
        lines.append("这不是推托：外送价 = 餐品外送价 + 配送费 + 打包费，")
        lines.append("三项都得按地址向接口取。没有地址就没有真值，")
        lines.append("小的宁可说「算不了」，也不拿一个猜的配送费凑数给您。")
        lines.append("")
        lines.append("想让小的能算外送：去麦当劳 App 里添加一个收货地址，")
        lines.append("再跑一次这条命令，小的就把可配送的门店列给您。")
        return lines

    lines.append(f"收货地址：{len(addrs)} 个")
    for i, a in enumerate(addrs[:5], 1):
        label = f"（{a['tag']}）" if a["tag"] else ""
        lines.append(f"  {i}. {mask_address(a['address'])}{label}")
    lines.append("")

    # 有地址才谈得上取门店。这一段在作者账号上没能实测（无地址），
    # 所以任何一步不对都如实报失败，不假装成功。
    stores = fetch_delivery_stores(client, addrs[0]["id"])
    if not stores:
        lines.append("可配送门店：没取到（接口没返回，或这个地址周边没有可配送的店）")
        lines.append("换成别的地址也取不到的话，多半是这条链路的接口变动了 ——")
        lines.append("请把这条输出贴到 https://github.com/GenshinKings/mcd-butler/issues")
        return lines

    lines.append(f"可配送门店：{len(stores)} 家")
    for s in stores[:8]:
        dist = s["distance"]
        tail = f"　{dist} 米" if isinstance(dist, (int, float)) else ""
        closed = "（当前打烊）" if s["status"] is False else ""
        lines.append(f"  · {s['name']}{tail}{closed}")
    lines.append("")
    lines.append("注意：外送价与配送费仍要按具体商品逐单算，")
    lines.append("这一步只是确认「您的地址送得到、能送到哪些店」。")
    return lines
