# 麦当劳 MCP 开发实战：35 个工具全量实测与避坑清单（附零依赖 Python 实现）

> 提示：CSDN 编辑器支持 Markdown 导入。直接粘贴本文即可，代码块会保留高亮。
> 建议发文时间：工作日上午 9:00-11:00 或晚上 20:00-22:00。

---

## 写在前面

麦当劳中国在 `open.mcd.cn` 开放了官方 MCP Server，一共 35 个工具，覆盖门店、菜单、券、算价、下单、派对、商城、积分、营养。

我最近基于它做了个项目，过程里把 35 个工具挨个调了一遍。结果发现：**入参文档写得挺清楚，但「返回体到底长什么样、哪些字段会骗人」只能靠真跑。**

这篇文章是把踩过的坑整理出来的清单。如果你也在基于这套 MCP 开发，应该能省你几小时。

先说结论里最反直觉的三条——它们不是 Bug，而是**口径问题**，但如果你不看清楚，代码算出来的数字就是错的。

---

## 一、三个「看起来对、其实是坑」的数据陷阱

### 陷阱 1：菜单标价可能是条件价

实测某个四件套：

```json
{
  "currentPrice": 29,
  "originalPrice": 57,
  "discountType": "随单购麦金卡优惠"
}
```

货架上写 ¥29，接口 `currentPrice` 也确实是 29。但注意 `discountType`——**这是「随单购麦金卡」的专属价，不随单购就按 `originalPrice` ¥57 结算。**

差 ¥28。

如果你的代码只取 `currentPrice` 去展示或者做预算，算出来的数会和实际支付差一大截。正确做法是**同时保留 `originalPrice` 和 `discountType`**，判断当前是否满足条件价。

### 陷阱 2：「省了多少」不能信 `discount` 字段

这是我踩得最深的一个。

同一单实测：

```
裸买原价      ¥97.50
随单购后原价  ¥107.40   ← 多出来的 ¥9.90 是卡费，被算进了原价
接口 discount 报 ¥37.10
实际到手      ¥70.30
```

问题在于：随单购会把**卡费计入 `originalPrice`**（97.50 → 107.40）。于是 `discount` = 107.40 − 70.30 = 37.10，看起来省了 37 块。

但你真正省的是 **97.50 − 70.30 = ¥27.20**。虚高了近 ¥10。

> 这不是 Bug，是口径差异。`discount` 的基准是「含卡费的原价」，而用户关心的基准是「不买卡、不用券的裸买价」。

正确做法：**以裸买价为基准，把两个数字都算出来**。我在项目里是这么处理的：

```python
baseline = optimizer.oracle(cart, Assignment(mapping={}, with_order=None))
saved = baseline.price_fen - best.price.price_fen
print(f"裸买价 ¥{FEN(baseline.price_fen)}  →  到手 ¥{FEN(best.price.price_fen)}")
print(f"真实省下 ¥{FEN(saved)}（接口 discount 报 ¥{FEN(best.price.discount_fen)}）")
```

### 陷阱 3：券会互相作废

随单购之后，某款美式已经是 ¥9.9 了。这时候再挂一张「9.9 元冰美式」券，**不产生任何额外减免**——实测挂与不挂都是 ¥70.30。

所以「把券全用上」是错的思路。引擎必须能识别这种冗余券。

还有一个相关的现象：领了 6 张券，但 `query-store-coupons` 只返回 **1 张**。其余是早餐/小食时段券，在非对应时段自动失效。

**你以为你有 6 张，此刻能用的只有 1 张。** 这就是「券迷宫」体感的来源。

---

## 二、返回体其实是三种格式，不是一种

这是最容易翻车的地方。你以为都是 JSON，其实不是。

### A 类：Markdown 说明 + 内嵌 JSON 报文（多数）

长这样：

```
# API Response Information
Below is the response from an API call...
## Response Structure
- **data.availablePoint**: 可用积分 (Type: string)
## Original Response
{"success":true,"code":200,"message":"请求成功","data":{...}}
```

**解析这里有连环坑：**

- ❌ `\{.*\}` 贪婪正则 —— `## Response Structure` 里有 `data[].title` 这类占位符，会被当成报文开头
- ❌ 括号计数扫描 —— 占位符 `{data[].title}` 里的 `data[].title` **不带引号**，计数被带偏，真正的报文被吞进一个永不闭合的对象

我第一版和第二版都栽在这。最终方案是：**扫描每一个 `{`，用 `json.JSONDecoder.raw_decode` 试解，以「含 `success` 键」为判据**：

```python
def extract_payload(text: str):
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        if '"' not in text[i : i + 12]:
            # 真 JSON 的键一定带引号，占位符不带
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "success" in obj:
            found = obj   # 取最后一个
    return found
```

关键点：**判据只能是内容，不能是位置**。报文有时在末尾，有时夹在两段 Markdown 中间。

### B 类：纯 Markdown，压根没有 JSON（3 个）

| 工具 | 返回结构 |
|---|---|
| `query-my-coupons` | `# 您的优惠券列表` → `## 券名` → `- **有效期**: ...` |
| `available-coupons` | `- 优惠券标题：X \ 状态：已领取`（行尾 `\` 是转义） |
| `campaign-calendar` | `#### 2026年10月11日 今日` → `- **活动标题**：...` |

这三个调上面的 `extract_payload` 必然返回 `None`，下游 `payload["data"]` 直接 `TypeError`。

### C 类：`data` 是字符串（最阴的）

`list-nutrition-foods` 的 `success=true`，但 `data` 是**一个 str**：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  猪柳蛋麦满分,null,1618,387,23,21,25,846,243
```

自定义的类 CSV：首行 `[条数]{字段清单}:` 声明结构，后面每行一条。

**而且营养表里没有 `productCode`**，只能按品名和菜单匹配。匹配不上就丢，宁可少选也不能拿错数据给建议。

---

## 三、参数地雷清单

| 现象 | 实测结论 |
|---|---|
| `calculate-price` 报参数缺失 | `storeCode` + `orderType` + `beType` **三个都要**，`create-order`、`query-meal-detail` 同样。管线必须先定位门店 |
| `query-nearby-stores` 传 `beType=2` | 报 `600046`。**只支持 1（到店）和 5（得来速）**。麦乐送要另走 `delivery-query-addresses` → `delivery-query-stores` |
| `query-nearby-stores` 只传 keyword | 查不到。**city 和 keyword 必须同时给** |
| `query-meal-assistance` 一直失败 | 报 `610501 仅支持企业团餐场景下使用助餐服务` —— **只对 `beType=6` 生效**，普通门店调了也是白调 |
| 金额差 100 倍 | 价格字段单位是**分** |
| 积分显示成 0 | `availablePoint` 是**字符串小数 `"9.9"`**，`int("9.9")` 直接抛异常。要用 `float()` |

`beType` 共四值：`1` 到店取餐 / `2` 麦乐送到家 / `5` 得来速 / `6` 企业团餐。

---

## 四、核心难点：为什么不能用公式算最优券组合

麦当劳的 `calculate-price` 允许购物车里**每个商品各自挂一张券**。所以这不是「选一张最猛的券」，而是一个**二分匹配**：

```
约束 1：一张券最多用一次
约束 2：一个商品最多挂一张券
约束 3：券只能挂到它 products[] 里声明过的商品上
```

难处在于：**接口不返回券的折扣金额**，只有标题和适用商品。收益无法离线估算，任何公式都派不上用场。

我把 `calculate-price` 当成了**黑盒求值器**，用搜索求解：

1. **贪心构造** —— 按可挂商品数升序安排券，最受约束的先占坑
2. **局部搜索** —— 交替执行 删除 / 新增 / 交换 三种邻域动作，**每步都用真实接口验证**，只接受变便宜的改动
3. **预算收敛** —— 调用次数达上限或不再改进即停（默认 40 次，接口限流 600 次/分钟）

实测 4 次调用就收敛了。这比穷举更贴近真实约束：调用次数可控，且不依赖任何未在文档中承诺的字段。

---

## 五、有副作用的工具，调试时别乱调

这些会写进你的**真实会员账号**：

| 工具 | 后果 |
|---|---|
| `auto-bind-coupons` | 真领券 |
| `create-order` | 真下单 |
| `cancel-order` | 取消真实订单 |
| `draw-lottery` | 消耗积分抽奖 |
| `mall-create-order` | 商城真下单 |
| `delivery-create-address` | 写入真实收货地址 |
| `party-order-create` | 创建派对预约 |

我的处理是**默认只读**：资产巡检只查不写，必须显式传 `--claim` 才真领券。

---

## 六、项目

上面这些坑的代码处理都在项目里，**零第三方依赖**（只用 Python 标准库 `urllib`），clone 下来不用 `pip install` 就能跑，而且不需要 Token 也能跑完整离线演示：

```bash
git clone https://github.com/GenshinKings/mcd-butler.git
cd mcd-butler
python -m src.cli demo
```

几个入口：

```bash
python -m src.cli demo                 # 离线演示，无需 Token
python -m src.cli optimize             # 真实账号：定位门店 → 算价 → 券价寻优
python -m src.cli assets               # 资产巡检（默认只读，--claim 才领券）
python -m src.cli nutrition --kcal 600 # 热量预算搭配
python -m src.cli replay               # 复刻上一单，报价格变动
```

仓库：<https://github.com/GenshinKings/mcd-butler>

另外我把完整的实测笔记单独整理成了 `FINDINGS.md`，包含全部 35 个工具的返回格式分类和实测返回值速查表，需要的话可以直接翻。

---

## 小结

三句话版本：

1. **别只看 `currentPrice`**，它可能是条件价，不随单购就要按 `originalPrice` 付
2. **别信 `discount`** 当「省了多少」，它把卡费算进了原价，虚高约 ¥10
3. **返回体有三种格式**，其中 3 个工具压根没有 JSON，1 个工具的 `data` 是字符串

这些都不是文档里会写的东西，只能真跑。

---

*本文为麦当劳程序员创意开发大赛参赛期间的技术记录，项目非麦当劳官方产品。餐品信息、价格与供应状态以麦当劳官方渠道实时结果为准。接口行为可能随版本变化，请以实际返回为准。*
