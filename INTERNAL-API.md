# 内部面 API 契约（内部页集 `supply-chain.fi` · 不进客户端载荷）

> 本文件声明**内部页集 `supply-chain.fi`** 的服务端面：它不属于任何对外页集（ue/se/sp/cp），
> 也不进 miniapp。声明在这里而不是 `templates/supply-chain/pagesets/*/api-contract.md`，
> 是因为页面集契约那一层被覆盖矩阵度量（功能点对账表、域计数、已提交快照），
> 把内部面塞进去会改变一个封闭空间的度量——而内部面没有面向客户的页面。
>
> **同源纪律：本文件声明的端点没有平行取数路径**，四张报表都是分录的投影。

## 0. 通用约定

### 0.1 四道闸，顺序不能换

一次财务请求要过四道，**顺序**是判定的一部分：

| 顺序 | 判什么 | 失败码 | 为什么这个顺序 |
|---|---|---|---|
| 1 | 租户头 `X-ERP-Tenant` | `TENANT_DENIED` | 一进程一租户，最外层 |
| 2 | 页集 `supply-chain.fi` 已授权 | `PAGESET_FORBIDDEN` | 未授权时连「有没有配名单」都不该泄露 |
| 3 | 该端点 grant 在册 | `GRANT_REQUIRED` | 部署层面的事实先答（先部署、后人员） |
| 4 | 调用者在 `ERP_FINANCE_STAFF` 对应档 | `STAFF_NOT_AUTHORIZED` | 见 0.2 |

缺身份头 `X-ERP-Staff` → `AUTH_REQUIRED`（第 4 道之前判）。

### 0.2 第 4 道：财务后台按人授权（change: add-fi-staff-authorization）

**为什么必须有这一道**：第 2/3 道判的是**部署**（这个部署允许哪些 grant），
`PagesetAuth` 是一份静态配置，**它不认识调用者**；而 fi 面没有任何数据范围
（财务就是看全公司的账）。少了第 4 道，「把 `supply-chain.fi` 写进授权配置」就等于
把财务后台发给**所有已登录用户**，而 `X-ERP-Staff` 留痕还会让他们看起来像有授权的经办人。

对外四个页集没有这个问题的原因：它们的身份头会**改变能看到的数据**
（`x-erp-customer` 决定只看自己那几单），越权由车间的 `DATA_SCOPE_DENIED` 兜住。

配置（部署侧，带外 Secret 注入）：

```
ERP_FINANCE_STAFF={"read":["cfo@example.com"],"write":["cfo@example.com","clerk@example.com"],"confirm":["cfo@example.com"]}
ERP_FINANCE_STAFF_FILE=   # 或给文件路径；内联优先
```

- 键可写全名（`finance.read`）或短名（`read`），两者等价。
- 值是人员标识，与网关注入的 `x-erp-staff` **逐字比较**（去空白、大小写不敏感）。
- **单调闭包**：`confirm` 名单里的人自动具备 `write` 与 `read`；`write` 自动具备 `read`。
  更强的档不需要重复登记——「CFO 能确认但不许看报表」是配置陷阱，不是我们要的语义。
- **fail-closed**：未配置 / 不可解析 / 名单为空 ⇒ **没有人**能进（不是所有人）。
- **程序身份不靠名单放行**：`system` / `agent` / `asset-engine` / `billing-engine` /
  `posting-engine` / `bad-debt-engine` 在 REST 面一律 `INVALID_ARGUMENT`（须人工经办），
  即使有人把它们写进名单也不放行。要接自动化月结，得先有服务账号机制（独立凭据 + 独立审计），
  而不是把引擎名混进人的名单。

网关侧的前提（`paas/server/routes/erp-proxy.js`）：无会话 → 401；客户端送来的一切
`X-ERP-*` 一律剥离；`x-erp-staff` 由会话邮箱/绑定表**重新注入**——所以这个头伪造不了，
它才敢被当作授权依据。

### 0.3 其它

- **租户头 `X-ERP-Tenant` 是常量，不是凭据**，不能单独放行——只凭它开财报等于裸奔。
- 页集 `supply-chain.fi` 是**内部页集**（`pagesets.INTERNAL_PAGESET_KEYS`）：
  服务端能授权、能守门、能审计，但**不出现在 `GET /pagesets` 载荷**里。
  理由：客户端 `KNOWN_KEYS` 硬编码四个 key，遇到未知 key 会整包作废，连带打掉四个业务页集。
- fail-closed 双向：未配置 `supply-chain.fi` 授权、或未配置 `ERP_FINANCE_STAFF`，
  端点一律 403。启用是**配置变更**（两处都要配齐），不是代码变更。
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

## 1f. 账龄与坏账准备（催收与期末减值）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/receivables/aging` | `asOf`、`dimension`（customer/supplier/…）、`buckets`（逗号分隔，如 `30,60,90`） | 按对象的账龄表 + `totals` + `totalOutstanding` + `advanceReceipt`（多收单列）+ `accountNetBalance` + `consistent`（不平带 `error`） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/payables/aging` | 同上 | 同上（应付口径，多付为 `advancePayment`） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/bad-debts/schedule` | — | 当前分档费率 + `registered`（未登记为 false） | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/bad-debts/schedule` | JSON：`bands`（`[{maxDays, rate}]`，最后一档 `maxDays` 留空） | 新版本号；`registeredBy` 取 `X-ERP-Staff`，**system/agent 一律拒** | `finance.write` |
| GET | `/erp/supply-chain/v1/fi/bad-debts/status` | `asOf`（缺省今天） | `outstanding` / `allowance` / `netReceivable` / `coverage` / `bands` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/reports/bad-debt-preview` | `period`、`asOf`（可选） | 预演：`target`/`existing`/`delta`/`rows` + `proposed=false`、`preview=true`、`blockers`（真计提会被什么挡住）、`canSubmit` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/bad-debts/{period}/accrue` | — | 待确认提案（借 `6602` / 贷 `1231`；减少记反向）+ `proposed=true` + `stableId` | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/bad-debts/{period}/void` | JSON：`reason`（**必填**） | 作废结果；重提要换稳定键（`bad-debt:{period}#N`） | `finance.write` |

**预演只算不写**：不生成提案、不落事件档。真计提会撞上的三件事（同期间已确认且金额不同＝跨期调整
不允许／同期间待确认且金额不同须先作废／应计提额与账上准备一致＝noop）以 `blockers` 报出，
**不抛错**——看一眼数字不该被挡住。

**计提额是增量**：`应计提 = Σ(账龄桶 × 费率)`，落账金额永远是 `应计提 − 账上已有准备`，
为负记反向（冲回）。这样做重跑幂等、跨期只影响各自差额。

**费率只能人工登记**：分档与费率是会计估计，要写进报表附注对审计解释；程序车道提交一律拒。
未登记费率时计提被拒（不猜默认费率）。

**账龄不平如实报出**：`各桶合计 − 多收/多付` 必须等于科目净额，不等时 `consistent=false`
并带 `error`——端点**不调桶凑平**。

## 1g. 固定资产（台账 / 资本化门槛 / 月度折旧）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/assets` | — | 固定资产清单：原值 / 残值 / 累计折旧 / 净值 / 剩余月数 / 折旧方法及其出处 | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/assets` | JSON：`assetId`、`name`、`cost`、`acquiredOn`、`usefulLifeMonths`、`salvage`、`currency`、`category`、`expenseAccount`、`method`、`unitsTotal` | 登记结果；同标识幂等；低于资本化门槛即拒并指明应费用化（`treatment=expense`） | `finance.write` |
| GET | `/erp/supply-chain/v1/fi/assets/summary` | — | 规模（项数 / 原值 / 累计折旧 / 净值）+ 资本化门槛 + 折旧方法与台账归属口径 | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/assets/{assetId}` | — | 单项资产；不存在 → `NOT_FOUND`（不返回零对象） | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/assets/{assetId}/plan` | — | 完整折旧计划：每期金额 / 累计 / 期末净值 + `booked` 已计提标记 | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/assets/{assetId}/book-value` | `asOf`（`YYYY-MM` 或 `YYYY-MM-DD`，可选） | 原值 / 累计折旧 / 净值 / `fullyDepreciated` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/assets/threshold` | JSON：`amount`（**键必须出现**，`null`＝取消门槛）、`currency` | 门槛结果；`approvedBy` 取 `X-ERP-Staff`，**system/asset-engine 等程序身份一律拒** | `finance.write` |
| GET | `/erp/supply-chain/v1/fi/reports/depreciation-preview` | `period` | 预演：逐资产计提额 + `total` + `proposed=false`、`preview=true`、`blockers`、`canSubmit` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/assets/depreciation/{period}` | JSON 可选：`units`（工作量法资产当期产量，`{assetId: 数量}`） | 待确认提案（借费用 / 贷 `1602`）+ `proposed=true` + `stableId` | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/assets/depreciation/{period}/void` | JSON：`reason`（**必填**） | 作废结果；已过门落账的须先在内核冲正 | `finance.write` |

**预演只算不写**：不生成提案、不落事件档。已计提过的期间照样可用——可提额如实为 0 并说明
「这期不会再提」，而不是拿「已计提」把人挡在门外。

**资本化门槛是闸不是备注**：门槛由人工登记（程序身份一律拒），低于门槛的资产登记被拒并在
detail 里指明应费用化。`amount: null` 是**显式取消**门槛——`amount` 键必须出现，少写一个键
不该把闸悄悄清掉。

**折旧口径在车道里**：三种方法（直线 / 双倍余额递减含末期改直线 / 工作量法）、末期封顶、
已提足判断、作废后重提换稳定键，全在 `FixedAssetLane`；端点只做投影与身份注入。
工作量法缺预计总产量或当期产量时被拒并点名资产，**不用时间折旧顶替**。

## 1h. 月结清单（一串有顺序的动作，不是 48 个按钮）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/month-end/{period}` | — | 逐项判据（`ok`/`blocker`/`warning`）+ 期间内待确认提案 + `nextOwners`（谁做下一步）+ `readyToRun` | `finance.read` |
| POST | `/erp/supply-chain/v1/fi/month-end/{period}/run` | — | 顺序准备：结转成本 → 折旧 → 减值 → 结转损益；各步 `prepared`/`noop`/`deferred`/`failed` + `nextActions` | `finance.write` |
| POST | `/erp/supply-chain/v1/fi/month-end/{period}/close` | — | 先跑再关账；前置不满足则 `closed=false` 并把原因写进 `nextActions` | **`finance.confirm`** |

**为什么要有它**：漏跑一步没人会知道——漏折旧时账本一切正常（只是这期少一笔费用），
漏结转时营业成本永远是 0，两个错在报表上都「看起来合理」，通常等跨期才发现。
清单把凭记忆的顺序变成代码。

**失败即停**（不跳过）：跳过会让「少了一笔费用」看起来像「这期本来没有」。

**结转损益会 `deferred`**：它的基数只认**已落账**分录。前序三步刚出的待确认提案还没落账时，
现在结转就少结转一笔费用而报表看起来正常——所以宁可推迟并列出挡住它的提案。

**关账前置 = 各步成功 + 期间内没有任何待确认提案**：有待办时关账，等于让「还没人看过的一笔
分录」变成既成事实。已关账期间重跑会被拒并提示先重开（带理由）。

**没有 `partial` 逃生阀**：成本未知时整期拒是车道设计（结转偏小的营业成本会让毛利虚高）。

## 1i. 三本账（账 → 凭证）

| 方法 | 路径 | 请求要点 | 响应要点 | 权限 |
|---|---|---|---|---|
| GET | `/erp/supply-chain/v1/fi/books/balances` | `period` | 三栏式逐科目「期初 + 本期借 − 本期贷 = 期末」+ `totals` + `balanced` + 表外科目 `unclassified` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/books/ledger/{accountCode}` | `period`（可选，缺省从建账起）、`limit`（只截显示行） | 逐笔流水 + 逐行 `balance` + `totals` + `closingBalance` + `truncated`/`rowCount` | `finance.read` |
| GET | `/erp/supply-chain/v1/fi/books/party-statement` | `dimension`、`value`（均必填）、`period`、`accounts`（逗号分隔，给了才对总账） | 跨科目小计 + 逐行余额 + `reconciliation`（本单 + 未标注 vs 总账）+ `consistent` | `finance.read` |

**三张表之外还要有账**：查一笔钱去哪了，靠的不是资产负债表，是「科目余额表 → 明细账 → 那张凭证」。

**期间口径与报表同源**（业务日期优先的月度期间）：账簿若另用落账时刻分窗，同一期的账簿与报表
会各自对不上而每本都「自洽」。所以这三本账必须由这一层投影，不能另写一套。

**明细账 `limit` 只截显示行**：`totals` 与 `closingBalance` 永远按全量算，截断时标 `truncated`
与 `rowCount`——只截合计会让「最后一行的余额」骗人。

**往来对账单只认显式维度**：`dimensions[dimension] == value`。靠科目名或摘要文字猜归属，
迟早把两家的往来记到一家头上。指定 `accounts` 时把「该科目未标该维度」的净额单列并对回总账，
差额如实报出、**不摊给某个往来方**。

**逐行 `balance` 是按科目走的**：各科目相加恒为零（每笔分录借贷必平），那种「余额」看着一路
归零、对查账无用。看往来余额请看 `subtotals` 里应收/应付那一行。
