# workbuddy.md · 麦门管家开发上下文

> 本文件用于核验「使用 WorkBuddy 开发」这一联动活动奖励条件。
> 文件名为官方规定，不可改动。
> 项目：麦门管家（mcd-butler）　仓库：https://github.com/GenshinKings/mcd-butler

## 一、开发环境

| 项 | 值 |
|---|---|
| 开发工具 | WorkBuddy（本次活动官方合作伙伴） |
| MCP 接入方式 | WorkBuddy 左侧栏「专家·技能·连接器」→「连接器」→ 自定义连接器 → 配置 MCP |
| MCP Server | `https://mcp.mcd.cn`（streamablehttp，`Authorization: Bearer <Token>`） |
| 运行时 | Python 3.13，零第三方依赖（仅标准库 `urllib`） |
| Token 处理 | 仅存于环境变量 `MCD_MCP_TOKEN` 与本地 MCP 配置，**不入库、不打印** |

## 二、开发过程（WorkBuddy 对话驱动）

### 阶段 1 · 方案校准：用真接口推翻文档假设

参赛者提供了一份自撰方案文档。WorkBuddy 不直接照做，而是**先调真接口逐条验证**，
结果推翻了多处文档描述：

| 文档写的 | 实测结果 |
|---|---|
| 36 个工具 | **35 个** |
| `query-partystore-date` / `query-partystore-session` | 实为 `query-party-store-date` / `query-party-store-session` |
| 未提及 | 遗漏 `query-survey-coupon`、`query-promotions` |
| 未提及 | `calculate-price` / `create-order` / `query-meal-detail` **强制要求 `storeCode` + `orderType` + `beType`** |
| `beType` 两种 | **四种**：1 到店取餐 / 2 麦乐送到家 / 5 得来速 / 6 企业团餐 |
| 未提及 | `query-nearby-stores` 仅支持 beType 1/5；city 与 keyword 必须同时给 |
| 未提及 | 价格单位是**分**，不是元 |

这一步决定了后续所有实现都建立在实测而非文档之上。

### 阶段 2 · MCP 客户端：三次重写才能解析返回体

麦当劳 MCP 的返回体是「字段说明 + Markdown + 业务报文」的混合体，解析踩了三个坑：

1. `\{.*\}` 贪婪正则失败 —— 字段说明里自带 `{data[].title}` 这类占位示例；
2. 改为括号计数扫描，仍然失败 —— 占位符 `{data[].title}` 不带引号，把计数带偏，
   真正的报文被吞进一个永不闭合的对象；
3. 最终方案：扫描每一个 `{`，用 `json.JSONDecoder.raw_decode` 试解，
   **以「含 `success` 键」为判据**而非以位置为判据，取最后一个。

同时兼容 SSE 流（`data:` 行）、HTTP 401/429 的差异化报错、`Mcp-Session-Id` 会话保持。

### 阶段 3 · 券价引擎：把官方算价接口当黑盒求值器

核心难点：**`calculate-price` 不返回券的折扣金额**，只有标题和适用商品，
收益无法离线估算，任何公式都派不上用场。

WorkBuddy 据此把问题重新建模：

- 不是「挑一张最猛的券」，而是**二分匹配**（一张券最多一次 / 一个商品最多一张 /
  券只能挂到 `products[]` 声明过的商品上）；
- 把 `calculate-price` 当作**黑盒求值器**，用「贪心构造（最受约束的先占坑）+
  局部搜索（删除/新增/交换三种邻域，每步真调接口验证）」求解；
- 设 `max_calls` 预算收敛（默认 40，接口限流 600 次/分钟）。

### 阶段 4 · 真跑才发现的三个坑（本项目存在的理由）

1. **菜单标价可能是条件价。** 四件套货架写 ¥29，`currentPrice` 也确实是 29，
   但 `discountType` 是「随单购麦金卡优惠」，不随单购就按 `originalPrice` **¥57** 结算，差 ¥28。
   → 为此给 `CartItem` 增加了 `list_price_fen` / `needs_with_order` / `effective_price_fen()`。
2. **「省了多少」接口会骗你。** 随单购把卡费计入 `originalPrice`（97.50 → 107.40，
   多出的 9.90 是卡费），于是 `discount` 报 ¥37.10，而真实只省 **¥27.20**。
   → 统一改用「裸买价 − 到手价」为基准，并把两个数字都亮出来。
3. **券会互相作废。** 随单购之后美式已是 ¥9.9，「9.9 元冰美式」券就冗余了
   （实测挂与不挂都是 ¥70.30）。→ 引擎自动识别冗余，不硬塞券。

### 阶段 5 · 资产巡检与到期预警

领券实测暴露了「券迷宫」的体感来源：**领了 6 张，当前门店券池里只有 1 张能用**，
其余是早餐/小食时段券，在非对应时段自动失效。

巡检功能因此把「今天内到期」单独标红。设计上坚持**默认只读**：
领券会写进真实会员账号，必须显式加 `--claim` 才执行。

解析同样踩坑，三个接口三种格式：

- `query-my-account`：JSON，但积分是**字符串小数 `"9.9"`**，`int()` 直接炸（第一版把积分全算成 0）；
- `query-my-coupons`：**纯 Markdown 文档**，没有内嵌 JSON，`extract_payload` 必然返回 None；
- `available-coupons`：另一种 Markdown（`- 优惠券标题：X \ 状态：已领取`）。

三套解析器各自容错，格式变了也不崩。

### 阶段 6 · 复刻上一单：为「没有历史订单」的账号换思路

`order-list` 对本项目账号返回空的 `data: {}`。参赛者住处附近没有门店，
这个账号**永远**不会有真实订单，「复刻历史订单」按原设计就彻底废了。

WorkBuddy 据此改了设计：**上一次算过的那单，就是你的上一单。**
`optimize` 跑完把购物车和成交价存到本地 `~/.mcd-butler/`（不进仓库，属个人数据），
`replay` 时拉今天的菜单和券重算；账号一旦有真实订单则自动优先用真实订单。

比较口径统一为「上次实付 vs 今天同款的最优实付」——
不能用「上次实付」比「今天菜单标价」，那是拿用券后的价去比不用的价，自己在骗自己。

## 三、WorkBuddy 实际产出的文件

```
src/mcd_client.py      MCP 客户端（零第三方依赖，SSE/JSON 双兼容）
src/coupon_engine.py   券价最优组合引擎 ← 项目核心
src/demo_data.py       离线演示数据层（无需 Token 即可完整演示）
src/assets.py          资产巡检：卡包到期预警 + 积分过期（默认只读）
src/snapshot.py        本地订单快照
src/cli.py             命令行入口（demo / tools / optimize / assets / replay）
README.md              项目介绍、实测证据、快速上手
MCP_INTEGRATION.md     使用的 MCP Server、Tool、调用流程与业务价值
CONTEST_DECLARATION.md 参赛声明（官方原文，未改动）
```

## 四、验证记录（真实接口，非模拟）

```
定位门店：上海 / 人民广场 → 1450713（华旭国际大厦餐厅）
菜单载入 68 个品类　门店可用券 1 张

裸买价 ¥97.50  →  到手 ¥70.30
真实省下 ¥27.20（接口 discount 报 ¥37.10，含卡费口径）
算价接口共调用 4 次

资产巡检：卡包 5 张，其中 2 张今天内到期；可用积分 9.9
```

## 五、合规红线（全程遵守）

- Token 只走环境变量，不入库、不打印、不出现在任何提交中
- `calculate-price` 只算价；`create-order` 只到支付链接，**绝不触碰支付环节**
- 领券类写操作默认关闭，需显式 `--claim`
- 个人数据（门店、商品、快照）存于 `~/.mcd-butler/`，不入仓库
- 非商业：不售卖、不付费分发、不引流变现
- 本项目为参赛作品，**非麦当劳官方产品**；餐品信息、价格与供应状态以官方渠道实时结果为准
