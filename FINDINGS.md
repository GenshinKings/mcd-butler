# 麦当劳 MCP 实测避坑笔记

> 这不是文档翻译，是**把 35 个工具挨个调了一遍之后的记录**。
> 写它的原因：官方只给了入参说明，但**返回体长什么样、哪些字段会骗人**，
> 只有真跑才知道。下面每一条都带实测证据。
>
> 如果你也在参赛，这份笔记能帮你少踩几小时的坑。
> 觉得有用就点个 star，让更多人少踩点 —— 仓库：<https://github.com/GenshinKings/mcd-butler>

---

## 一、35 个工具的返回体，其实是三种格式

这是最大的坑。你以为都是 JSON，其实不是。实测分类：

### A 类：Markdown 说明 + 内嵌 JSON 报文（多数）

结构长这样：

```
# API Response Information
Below is the response from an API call...
## Response Structure
- **data.availablePoint**: 可用积分 (Type: string)
## Original Response
{"success":true,"code":200,"message":"请求成功","data":{...}}
```

**解析难点**：`## Response Structure` 里会写 `- **data[].title**` 这类占位符。
这意味着：

- ❌ `\{.*\}` 贪婪正则 —— 占位符会被当成报文开头，粘成一坨
- ❌ 括号计数扫描 —— `{data[].title}` 里的 `data[].title` 不带引号，
  计数被带偏，真正的报文被吞进一个永不闭合的对象里
- ✅ 正解：扫描每一个 `{`，用 `json.JSONDecoder.raw_decode` 试解，
  **以「含 `success` 键」为判据**，取最后一个

```python
def extract_payload(text: str):
    decoder = json.JSONDecoder()
    found = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        if '"' not in text[i : i + 12]:   # 真 JSON 的键一定带引号，占位符不带
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "success" in obj:
            found = obj
    return found
```

**不要偷懒**：报文位置不固定，有时在末尾，有时夹在两段 Markdown 中间。
判据只能是内容，不能是位置。

### B 类：纯 Markdown，压根没有 JSON（3 个，最容易踩）

| 工具 | 返回结构 |
|---|---|
| `query-my-coupons` | `# 您的优惠券列表` → `## 券名` → `- **有效期**: ...`、`- **标签**: ...` |
| `available-coupons` | `### 麦麦省优惠券列表：` → `- 优惠券标题：X \ 状态：已领取`（行尾 `\` 是转义，要剥掉） |
| `campaign-calendar` | `#### 2026年10月11日 今日` → `- **活动标题**：...` |

这三个调 `extract_payload` 必然返回 `None`。
如果你的代码里写了 `data = payload["data"]`，这里直接 `TypeError`。

`query-my-coupons` 的解析思路：按 `##` 切块，每块内用字段正则取值：

```python
_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
```

注意有效期文本是 `2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29` ——
**后面那半段是重复的可用餐时段说明**，正则只取前两个时间点，别把「周日 05:00-10:29」也吃掉。

### C 类：`data` 字段不是对象也不是数组，是字符串（最阴的坑）

`list-nutrition-foods` 的 `success=true`，`data` 却是一个 **str**：

```
"[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  猪柳蛋麦满分,null,161..."
```

自定义的类 CSV 格式，160 条营养数据塞在一个字符串里，字段清单用 `[160]{...}` 前缀声明。
`data["xxx"]` 或 `for x in data` 都会以意想不到的方式失败。

---

## 二、参数地雷

| 现象 | 实测结论 |
|---|---|
| `calculate-price` 报参数缺失 | **`storeCode` + `orderType` + `beType` 三个都要**。`create-order`、`query-meal-detail` 同样。管线必须先定位门店 |
| `query-nearby-stores` 传 `beType=2` | 报 `600046`。**只支持 1（到店）和 5（得来速）**。麦乐送要另走 `delivery-query-addresses` → `delivery-query-stores` |
| `query-nearby-stores` 只传 keyword | 查不到。**city 和 keyword 必须同时给** |
| `query-meal-assistance` 一直失败 | 报 `610501 "仅支持企业团餐场景下使用助餐服务"` —— **这个工具只对 beType=6 生效**，普通门店调了也是白调 |
| 金额算出来差 100 倍 | 价格字段单位是**分**，不是元 |
| 积分显示成 0 | `availablePoint` 是**字符串小数 `"9.9"`**，`int("9.9")` 抛异常。要用 `float()` |

`beType` 共四值：`1` 到店取餐 / `2` 麦乐送到家 / `5` 得来速 / `6` 企业团餐。

---

## 三、数据陷阱：字段会骗人

这三条是本项目存在的理由，也是官方 App 不会告诉你的。

### 1. 菜单标价可能是条件价

实测「龙焰鸡腿堡四件套」：

```json
{ "currentPrice": 29, "originalPrice": 57, "discountType": "随单购麦金卡优惠" }
```

货架上写 ¥29，接口 `currentPrice` 也确实是 29 —— 但**不随单购就按 `originalPrice` ¥57 结算**。
差 **¥28**。

→ 解析菜单时必须同时保留 `originalPrice` 和 `discountType`，不能只拿 `currentPrice` 去展示。

### 2. 「省了多少」不能信 `discount`

随单购会把**卡费算进 `originalPrice`**：

```
裸买原价  ¥97.50
随单购后  ¥107.40   ← 多出的 ¥9.90 是卡费，被算进了原价
接口 discount 报 ¥37.10
而你真正省的是 97.50 − 70.30 = ¥27.20
```

`discount` 虚高了近 **¥10**。

→ 正确基准是 **裸买价 − 到手价**，两者都该亮出来。

### 3. 券会互相作废

随单购之后美式已经是 ¥9.9，此时再挂「9.9 元冰美式」券**不再产生任何减免**
（实测挂与不挂都是 ¥70.30）。

→ 券不是越多越好，引擎必须能识别冗余券。这也是为什么「挑一张最猛的券」这个思路本身就不对。

### 4. 券池不是你以为的那个数

领了 6 张券，`query-store-coupons` 只返回 **1 张**。
其余是早餐/小食时段券，在非对应时段自动失效。

「券迷宫」的体感就是这么来的：**你以为你有 6 张，此刻能用的只有 1 张。**

---

## 四、有副作用的工具，别在调试时乱调

这些会写进你的**真实会员账号**：

| 工具 | 后果 |
|---|---|
| `auto-bind-coupons` | 真领券 |
| `create-order` | 真下单（只到支付链接，但订单已建） |
| `cancel-order` | 取消真实订单 |
| `draw-lottery` | 消耗积分抽奖 |
| `mall-create-order` | 商城真下单 |
| `delivery-create-address` | 写入真实收货地址 |
| `party-order-create` | 创建派对预约 |

本项目的处理：**资产巡检默认只读**，必须显式传 `--claim` 才领券。

---

## 五、实测返回值速查

门店 `1450713`（上海人民广场华旭国际大厦餐厅），`orderType=1, beType=1`：

| 工具 | 返回 | `data` 形态 |
|---|---|---|
| `query-meals` | JSON | `dict[categories, meals]`，68 个品类 |
| `query-store-coupons` | JSON | `list`，1 张 |
| `query-promotions` | JSON | `list`，12 条 |
| `query-meal-detail` | JSON | `dict[code, name, modification, rounds, supportModify]` |
| `query-my-account` | JSON | `dict`，积分是字符串小数 |
| `query-my-prizes` | JSON | `dict[prizes, hasMore, pageNum, ...]` |
| `query-lottery-info` | JSON | `dict[activityCode, activityName, ...]` |
| `mall-points-products` | JSON | `list`，50 条 |
| `now-time-info` | JSON | `dict[timestamp, datetime, dayOfWeek, timezone, utc, ...]` |
| `delivery-query-addresses` | JSON | `dict[addresses]` |
| `order-list` | JSON | **空 `{}`** —— 没下过单的账号永远为空 |
| `query-my-coupons` | **纯 Markdown** | 无 |
| `available-coupons` | **纯 Markdown** | 无 |
| `campaign-calendar` | **纯 Markdown** | 无 |
| `list-nutrition-foods` | JSON | **str**（类 CSV，160 条） |
| `query-meal-assistance` | JSON | `success=false, code=610501`（非团餐场景） |

---

## 六、券的三件没人说的事（券包体检的来源）

`query-store-coupons` 给了券的标题和适用商品，到此为止。下面三件事它一个字都不提：

**① 门槛不被告知。** 「满 20 减 3」你拿一件 ¥10 的商品去挂，`calculate-price` 不报错、
不提示、不加任何字段，**就是不减**。判断门槛只有一个办法：拿便宜的挂一次、拿贵的挂一次，
分界处就是门槛区间。这就是 `src/audit.py` 里 `works_from_fen` / `blocked_fen` 的来历。

**② 券面额 ≠ 券价值。** 「立减 8 元」是印在券上的面额；挂到真实商品上接口真给你减多少，
是另一回事（实测差 ¥3）。少了这一栏，用户会以为「我有张 8 元的券」，
实际它只值 5 元。

**③ 死券不会自己说话。** 券印着「麦咖啡新品」，而这家店的在售菜单里根本没有咖啡 ——
它在那儿就是一张废纸。判断方法：`coupon.applies_to()` 扫一遍全菜单，命中 0 件即为死券。

顺带两条接口现状（2026-10-11 实测，上海多家门店一致）：

- `query-store-coupons` **只返回 1 张券**。所以「可用券少」不是算漏，是接口口径如此；
  卡包里的券（`query-my-coupons`）只回 Markdown、不含券码，挂不进算价。
- `query-promotions` 的 schema 里 `beType` 固定枚举 `6`（企业团餐）、`orderType` 固定 `2`，
  **到店取餐场景（1/1）用不上**，别照着名字想当然。

## 七、活动日历的两个坑

`campaign-calendar` 返回 Markdown（不是 JSON），按 `#### 2026年10月11日 今日` 分块，
块内是「活动标题 / 活动内容介绍 / 活动图片介绍」。两个实测踩到的坑：

**① 摊销价会冒充活动价。** 「学生专属！49元150天麦金学期卡」的活动正文写
「每天低至 0.33 元」。直接取最小值会得出「今天有 ¥0.33 的活动」—— 纯属误导。
处理：标题优先、正文兜底，且过滤掉 ¥1 以下的值。

**② 今天那天的活动特别多。** 接口把所有「正在跑」的活动都挂在今天（实测 14 个），
未来某天的活动往往是今天的子集。所以「等几天更划算」这句话成立的前提是
**那天有今天吃不到的新活动** —— 否则就是劝人干等。`src/campaign.py` 只提示前者。

另外，标题里的数字可能是**数学粗体**（`𝟗.𝟗元`，U+1D7D8 起），
正则 `\d` 与 `float()` 都能吃，但写 fixture 时容易把码位记错。

## 八、复用

解析相关的坑，代码都在本项目里，零第三方依赖，可直接抄：

- `src/mcd_client.py` — `extract_payload`（A 类解析，三次重写后的终版）
- `src/assets.py` — `parse_coupon_markdown`（B 类）、`_num()`（字符串小数积分）
- `src/coupon_engine.py` — 把 `calculate-price` 当黑盒求值器的搜索实现
- `src/audit.py` — 券的真实价值 / 废券 / 门槛反推
- `src/campaign.py` — 活动日历 Markdown 解析与「择日」判据

---

## 免责

本文为参赛期间实测记录，**非麦当劳官方文档**。接口行为可能随版本变化，
一切以官方渠道实时结果为准。项目与麦当劳官方无关。
