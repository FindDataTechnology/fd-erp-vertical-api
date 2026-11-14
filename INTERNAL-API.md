# 内部面 API 契约（无页集 · 不进客户端载荷）

> 本文件声明**没有页面集**的服务端内部面：它们不是任何对外页集（ue/se/sp/cp）的一部分，
> 也不属于 miniapp。声明在这里而不是 `templates/supply-chain/pagesets/*/api-contract.md`，
> 是因为页面集契约那一层被覆盖矩阵度量（功能点对账表、域计数、已提交快照），
> 把内部面塞进去会改变一个封闭空间的度量——而内部面根本没有页面。
>
> **同源纪律：本文件声明的端点没有平行取数路径**，四张报表都是分录的投影。

## 0. 通用约定

- 鉴权三件齐备：页集已授权 + 该页集含 `finance.read` + 请求头 `X-ERP-Staff`。
  缺页集 → `PAGESET_FORBIDDEN`；缺 grant → `GRANT_REQUIRED`；缺身份头 → `AUTH_REQUIRED`。
- **租户头 `X-ERP-Tenant` 是常量，不是凭据**，不能单独放行——只凭它开财报等于裸奔。
- 页集 `supply-chain.fi` 是**内部页集**（`pagesets.INTERNAL_PAGESET_KEYS`）：
  服务端能授权、能守门、能审计，但**不出现在 `GET /pagesets` 载荷**里。
  理由：客户端 `KNOWN_KEYS` 硬编码四个 key，遇到未知 key 会整包作废，连带打掉四个业务页集。
- fail-closed：未配置 `supply-chain.fi` 授权时四个端点一律 403。启用是配置变更
  （`ERP_PAGESET_AUTH_JSON` 加 `"supply-chain.fi": ["finance.read"]`），不是代码变更。
- 参数：`period` 为 `YYYY-MM`；响应为统一壳 `{"ok": true, "data": ..., "rows_returned": 0}`。

## 1. 法定报表（capability `erp-cn-localization`）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/reports/balance-sheet` | `period` | 期末三表区 + `opening`（年初数，含 `asOf`）+ `totals` + `balanced` + `complete` + `unmapped` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/income-statement` | `period` | `lines` + `totals`（本期）+ `priorYear`（同月去年，回显 `period`）+ `cumulative`（本年 1 月 1 日起） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/cash-flow` | `period` | 直接法三类 + `netIncrease` + 期初期末现金 + 自证 `sumCheck`/`balanceCheck` + `priorYear` + `cumulative` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/vat-return` | `period` | `ledger`（账面口径）与 `declared`（申报口径）；未提供申报数时明说「不能当作已核对」 | `finance.read` |

## 2. 报表自带的判据（本层不重算）

- 资产负债表：`balanced`（平不平）与 `complete`（该有的在不在）是**两个**判断——漏项的报表照样平。
- 现金流量表：`sumCheck`（三类净额之和 = 净增加额）与 `balanceCheck`（期末现金 = 货币资金）；
  `consistent=false` 时 `error` 点名未归类科目。
- 利润表：`priorYear` 取**同月去年**（不是上一期）；`cumulative` 从 1 月 1 日起算（不是账本第一天）。

## 3. 不做的（明确）

报表导出 PDF/Excel、报表缓存与预聚合、报表侧写账或调账——投影口径下都属于另一层，
且缓存会让「报表是分录的投影」不再成立。

## 1b. 申报动作（读与写分档）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/reports/surcharges` | `period` | 附加税预演：`base`（应纳增值税）、三项 `items`、`total`、`proposed=false`（只算不写） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/income-tax-prepay` | `period` | 所得税预缴预演：`cumulativeProfit`、`target`、`alreadyPrepaid`、`due`、`proposed=false` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/filing-summary` | `period` | 增值税 + 三项附加税 + 所得税预缴一张表，并与账面 5403 / 5801 逐项对比 | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/filings/vat-return` | JSON：`period`、`source`（必填）、`sales`/`outputVat`/`inputVat`/`inputTransferOut`/`openingCredit` | 登记结果：`declared`、`source`；同期间后一次为准 | `finance.write` |

**读与写必须分档**：`finance.read` 能看预演与汇总，但 MUST NOT 能改申报数；
登记端点要求 `finance.write`，只有读权限调用 MUST 被拒（`GRANT_REQUIRED`）。

**预演不写账**：两个预演端点的返回体 `proposed=false`，且内核待确认提案集合调用前后不变。
「看一眼要缴多少」不得在账上留提案——谁点的、点两次是否重复，事后都说不清。
