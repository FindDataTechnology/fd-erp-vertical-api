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

## 1c. 发票台账（录入与查询）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/invoices` | `direction`（可选 sale/purchase） | 票据列表（含 `tier`、认证状态、`redOf`） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/invoices/{number}` | — | 单票详情；不存在 → `NOT_FOUND` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/invoices/deduction` | `period` | 进项三档（可抵 / 待认证 / 不可抵，税额合计 + 逐票） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/invoices/reconciliation` | `period` | 账上 `2221.01.02` 借方发生额 vs 台账可抵税额，差额与提示 | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/invoice-summary` | `period` | 台账口径的期间汇总 | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/invoices` | JSON：`direction`、`kind`、`number`、`net`、`rate`/`tax`、`issuedOn` | 登记结果；票种不在封闭集或税额与「金额 × 税率」不符即拒 | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/invoices/{number}/certify` | JSON 可选：`certifiedOn` | 认证结果；经办人取财务身份头，**system/agent 一律拒** | `finance.write` |

**抵扣判断不在这一层**：某张票属于可抵 / 待认证 / 不可抵由台账决定（票种 + 认证共同决定）。
端点只做投影与录入——在这一层再写一遍判断就是第二份口径。

**认证必须人工经办**：`by` 取自 `X-ERP-Staff`，`system`/`agent`/空一律拒。系统不得自证抵扣。

**不做的**：过账（`purchase`/`post_red` 会提提案）与红字票登记属于确认门那一侧，
与提案收件箱一起做——现在开一个只能提不能确认的入口只会积压。

## 1d. 提案收件箱与拍板（确认门缺的那一侧）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/proposals` | `status`（可选 pending/confirmed/rejected） | 提案列表（含 id、kind、lane_title、stable_id、状态、提交人） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/proposals/{id}` | — | 单条提案（含分录行）；不存在 → `NOT_FOUND` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/proposals/{id}/confirm` | — | 确认结果；`confirmed_by` 取 `X-ERP-Staff` | `finance.confirm` |
| POST | `/erp/supply-chain/v1/fi/proposals/{id}/reject` | JSON：`reason`（**必填**） | 退回结果；不带理由即拒 | `finance.confirm` |
| POST | `/erp/supply-chain/v1/fi/invoices/red` | JSON：`direction`、`kind`、`number`、`net`、`redOf`（必填）、`reason`、`issuedOn` | 红字票（负数金额，原票不动）；票种与税率由原票决定，不收 `rate`/`tax` | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/invoices/{number}/post` | JSON：`ref`、`amount`、`costAccount`、`settleAccount`、`gross`、`bookedOn` | 待确认提案（**不直接写分录**） | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/invoices/{number}/post-red` | JSON：`ref`、`amount`、`revenueAccount`、`settleAccount`、`bookedOn` | 待确认提案（与现销逐行反向） | `finance.write` |

**权限三档互不覆盖**：`finance.read` 看提案；`finance.write` 登记票与过账（只提提案）；
**`finance.confirm` 才能确认/退回**。「能登记一张票」与「能让这笔分录成为事实」是两件事，
后者是账本不可逆的一步。

**机器不得拍板**：`confirmed_by`/`rejected_by` 取自 `X-ERP-Staff`，`system`/`agent`/空一律拒
——与发票认证同一纪律。

**退回必须带理由**：不带理由的退回等于没退回，提出方不知道要改什么。

**为什么不做批量确认**：一次确认几十条会让「逐笔看过」这句话失去意义，而确认是唯一入口。

## 1e. 月结三件（期间 / 期初 / 结转）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/periods` | — | 期间清单（含关账状态） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/periods/{period}` | — | 单期间状态：是否关账、关账人/时间/备注 | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/periods/{period}/report` | — | 关账快照复算（封账那刻的数 vs 现在重算） | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/periods/{period}/close` | JSON 可选：`note` | 关账结果；`actor` 取 `X-ERP-Staff` | **`finance.confirm`** |
| POST | `/erp/supply-chain/v1/fi/periods/{period}/reopen` | JSON：`reason`（**必填**） | 重开结果 | **`finance.confirm`** |
| GET | `/erp/supply-chain/v1/fi/opening` | — | 建账期初的 `status` + `preview` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/opening` | JSON：`lines`、`asOf`、`allowWithEntries`、`reason` | 待确认提案；只在空账可录 | `finance.write` |
| GET | `/erp/supply-chain/v1/fi/reports/closing-preview` | `period` | 结哪些科目、净额、搬进权益多少（不写账） | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/closing/{period}/transfer` | — | 待确认提案；已关账期间拒绝穿入 | `finance.write` |

**关账/重开用 `finance.confirm` 而不是 write**：关账会挡住该期间后续落账、重开会放开已封的期间，
这是时间轴上的动作，与「登记一张票」不是同一类。顺序关账、只重开最近一期、快照复算
全部在内核判定，端点只转发——在这一层重写一遍就是第二份口径。

**期初只在空账上可录**（车道强制）：事后插入会改写全部历史报表；确需补录必须
`allowWithEntries` + `reason`，受权偏离要留痕。
