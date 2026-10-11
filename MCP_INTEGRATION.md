# MCP 集成说明

## 使用的 MCP Server

| 项 | 值 |
|---|---|
| 名称 | `mcd-mcp` |
| 提供方 | 麦当劳中国官方 MCP 开放平台 |
| 申请地址 | https://open.mcd.cn/mcp |
| 服务地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer <TOKEN>` |
| 限流 | 每 Token 600 次/分钟 |

本项目**不自建服务端**，仅作为 MCP 客户端消费官方能力。Token 只通过环境变量 `MCD_MCP_TOKEN` 注入，不写入任何文件、不进版本库（参见 `.gitignore` 与 `CONTEST_DECLARATION.md` 的信息安全声明）。

## 调用的工具

> 口径说明：官方共暴露 35 个工具。本表列出的是**实际在用的**（11 个），
> 另有若干已核对入参但尚未接入的，均明确标注「预留」——
> 不把「试过」写成「用过」。

| 工具 | 用途 | 在本项目中的角色 |
|---|---|---|
| `query-nearby-stores` | 按城市+关键词检索到店/得来速门店 | 管线第一步，产出 `storeCode` |
| `query-meals` | 门店在售餐品与价格 | 构建可选池 |
| `query-meal-detail` | 餐品组成、套餐替换项、特调选项 | **预留未接入**（入参是 `code` 不是 `productCode`，后续用于套餐替换与券名↔菜单名对齐） |
| `query-store-coupons` | **指定门店+订单类型**下可用券 | 券价引擎的券池来源 |
| `calculate-price` | 含券实算价格 | 券价引擎的**黑盒求值器** |
| `order-list` | 历史订单 | replay 的输入 |
| `available-coupons` / `auto-bind-coupons` | 可领券列表 / 一键领取 | 资产管理（P1） |
| `query-my-account` | 积分账户（含"本月将过期"） | 过期提醒（P1） |
| `campaign-calendar` | 当月营销活动日历 | **预留未接入**（后续用于「今天有什么活动」） |
| `query-my-coupons` | 卡包内的券 | 仅做展示；**不做渠道校验**，故不用于决策 |
| `list-nutrition-foods` | 餐品营养信息 | 减脂模式（P2） |
| `now-time-info` | 服务器当前时间 | 黄历的时段判定与「今日到期」基准——本机时区错了整份黄历会错，故以服务端时间为准 |

其余工具（派对预约、麦麦商城、抽奖、订单查询/取消、配送地址、团餐）已核对入参，预留接口位。

## 调用流程

```
query-nearby-stores ──► storeCode
        │
        ├─► query-meals ──────────► 可选池
        ├─► query-store-coupons ──► 券池（couponId / couponCode / products[])
                    │
                    ▼
        ┌───────────────────────────────┐
        │  券价引擎（本项目核心）        │
        │  贪心构造 + 局部搜索          │
        │  每步都调用 calculate-price   │
        └───────────────────────────────┘
                    │
                    ▼
              最优组合 + 到手价
```

`calculate-price` 只算价，不产生订单；`create-order` 即便调用也只到支付链接为止，绝不触碰支付环节。

## 关键约束（实测得出）

1. **`storeCode` 是前置门禁**。`calculate-price`、`create-order`、`query-meal-detail` 均强制 `storeCode` + `orderType` + `beType`，因此管线必须先定位门店。
2. **`beType` 四值**：`1` 到店取餐 / `2` 麦乐送到家 / `5` 得来速 / `6` 企业团餐。
3. **到店与外送是两条独立链路**：`query-nearby-stores` 仅支持 `beType` 1 与 5（传 2 返回 `600046`）；麦乐送必须先有配送地址，再走 `delivery-query-stores`。
4. **价格字段单位是「分」**，展示需除以 100。
5. **接口不返回券的折扣金额**，只有标题和适用商品，因此券的收益无法离线估算——这正是本项目把算价接口当作黑盒求值器、用搜索而非公式求解的原因。
6. `create-order` 到店场景必传 `takeWayCode`，其值来自 `calculate-price` 返回的 `takeWayList[].code`。
7. **菜单 `currentPrice` 可能是条件价**。实测「龙焰鸡腿堡四件套」`currentPrice=29`、`originalPrice=57`、`discountType=随单购麦金卡优惠` —— 不随单购就按 57 结算。必须同时保留 `originalPrice`。
8. **节省金额不能用 `discount` 字段**。随单购会把卡费计入 `originalPrice`（实测 97.50 → 107.40，多出的 9.90 即卡费），`discount` 因此虚高。本项目统一以「裸买价 − 到手价」为真实节省。
9. **券会被随单购作废**。随单购后商品价已降至券面价，此时挂券不再产生额外减免（实测用券与不用券同为 ¥70.30），引擎需能识别这种冗余。

## 业务价值

官方 App 是**货架逻辑**（把券和商品摆出来，用户自己挑、自己算）。本项目是**管家逻辑**：把「哪张券能挂到哪个商品上」建模成二分匹配问题，用真实算价接口做黑盒求解，直接给出最便宜的合法组合。

对用户的价值：不用再自己惦记 6 折能不能叠加 0 元券、哪张券今天过期、这次改规则亏了多少。
