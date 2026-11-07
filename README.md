# fd-erp-vertical-api

ERP 行业垂直 **REST 服务面**（OpenSpec change `add-supply-chain-vertical` 任务 6.5）：
UE 订货台 19 + SE 业务员台 15 + SP 供应商 13 + CP 承运商 12 契约端点 +
pageset 授权端点（共 60 路由）。契约真源：
`fd-erp-vertical-templates/templates/supply-chain/pagesets/{ue-commerce,se-sales,sp-supplier,cp-carrier}/api-contract.md`。

## 边界纪律（同 fd-erp-vertical-templates）

1. **零旁路**：全部业务读写经 `fd_erp_vertical_templates` 驱动
   （masterdata / pricing / orchestration / fulfillment / wms_sync / reports）
   + KernelClient 工具面 —— 无平行取数路径；
2. **包源码不 import 任何 DB 驱动、不写内核表名 SQL**（tests 目录豁免，与模板包
   conftest 同口径：真库约束层不可 mock）；
3. **落账仍走内核确认门**：REST 不直接落账。下单经内核 `reserve_credit` 记账；
   对账单确认只提交自平核对 proposal（pending）进确认门，绝不假报已确认；
4. **租户模型照内核**：每进程 `ERP_TENANT_ID` 绑定唯一租户；每请求带
   `X-ERP-Tenant` 头过租户闸，跨租户一律 403 `TENANT_DENIED`；
   身份头（壹座底座 PAT 在网关侧落地前的演示/测试形态）：UE=`X-ERP-Customer`
   （客户联系人所属客户档案）、SE=`X-ERP-Staff`（业务员工号，归属=客户主数据
   `sales_rep`）、SP=`X-ERP-Supplier`（供应商档案 id/编码）、CP=`X-ERP-Carrier`
   （承运商标识，运单 carrier 域）。未知/越域身份一律 404（不泄露存在性）；
   例外：SE 代客下单归属越权 403 `DATA_SCOPE_DENIED`（契约明示，可解释拒绝）；
5. **BYOK**：外部凭证只经环境变量注入，零落库零日志；DSN 已从 repr 隐去
   （`field(repr=False)`），任何日志路径不外泄口令。

## 运行

```bash
pip install -e .            # 或 uv pip install -e .
fd-erp-vertical-api serve --host 127.0.0.1 --port 8100
fd-erp-vertical-api check   # 自检：授权配置 + 错误码封闭集（不回显敏感值）
```

环境变量：`ERP_TENANT_ID`、`ERP_LEDGER_DATABASE_URL`、`ERP_BASE_CURRENCY`、
`ERP_API_DATA_DIR`（模板层 JSONL 事件档目录）、`ERP_KERNEL_TRANSPORT`
（`inprocess` 缺省 / `mcp`）、`ERP_KERNEL_URL`（mcp 形态）、
`ERP_PAGESET_AUTH`（内联 JSON）或 `ERP_PAGESET_AUTH_FILE`（文件）、
`ERP_WMS_SNAPSHOT_FILE`（WMS 快照 JSON，缺省无快照源如实降级）。

生产形态 `ERP_KERNEL_TRANSPORT=mcp` 时经
`fd_erp_vertical_templates.kernel_client.McpKernelClient` 调内核 MCP 工具面；
缺省 InProcess 形态与模板包测试同构（服务进程内委托内核模块，同形签名）。

## pageset 授权端点（形状被 paas P0 客户端固定）

`GET /erp/supply-chain/v1/pagesets` →
`{"ok":true, "data":{"pagesets":[...]}, "pagesets":[{"key":"supply-chain.ue","grants":["catalog.read",...]},...], "rows_returned":1}`

- 与 `paas/miniapp/src/pagesets/authorize.ts` `parsePagesetAuth` 对齐：key 白名单
  逐字同（supply-chain.ue/se/sp/cp）、grants 全字符串、顶层 `pagesets` 可解析
  （客户端 `res.json()` 原始体直读，故顶层镜像字段在统一壳之外附加）；
- 授权配置源：`ERP_PAGESET_AUTH`（内联 JSON）优先于 `ERP_PAGESET_AUTH_FILE`
  （形如 `{"supply-chain.ue": ["catalog.read", ...]}`）；**fail-closed**：
  缺失/损坏/形态非法 → 空授权（无页面集可见、所有 /ue/* 端点 403）；
- grant 码封闭集（`pagesets.KNOWN_GRANTS`）= paas registry 四页面集
  requiredGrants 并集；集外码一律丢弃 —— 绝不输出会让客户端整体作废的形态；
- 端点闸序：租户闸 → 页面集授权（`PAGESET_FORBIDDEN`）→ grant
  （`GRANT_REQUIRED`）→ 客户身份域（404）。

## 端点覆盖（契约 19 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 商品 | GET /ue/products · /ue/product-categories · /ue/products/{id} | masterdata 商品档 + 商品池授权过滤（visible_product_codes）；详情带 pricing.resolve_price 取价（规则链 provenance）+ wms_sync 快照库存可见性 | 实现 |
| 下单 | GET/POST/DELETE /ue/cart(/lines) · POST /ue/cart/price-preview | 进货单=进程内暂存 UI 态（当次取价快照，非账务事实）；预览纯读不占额度 | 实现 |
| 下单 | POST /ue/orders | orchestration.accept_order：信用校验（fail-closed `CREDIT_EXCEEDED`+占用明细）→ 取价 → 内核 reserve_credit → 订单提案（确认车道） | 实现 |
| 查单 | GET /ue/orders · /ue/orders/{id} · /ue/orders/{id}/fulfillment | 编排层事件档 + fulfillment_view；未确认→404；已确认未编排→如实 `handoff_pending` | 实现 |
| 对账 | GET /ue/statements(/{id}) · POST /ue/statements/{id}/confirm | 订单按月派生 + 内核 proposal 面状态真源；确认→自平核对 proposal（pending 确认门，幂等回放）；异议→ThresholdEvaluator 处置车道 | 实现 |
| 开票 | GET/POST /ue/invoice-requests | — | **501 NOT_IMPLEMENTED**（模板层无开票车道；金蝶 provider 无开票直连 action，见 docs/connector-onboarding-supply-chain.md §1.2） |
| 售后 | POST/GET /ue/aftersale-requests(/{id}) | — | **501 NOT_IMPLEMENTED**（编排层无售后处置车道） |

计数：**实现 14/19；501 如实 5/19**（开票 2 + 售后 3），全部带 `planned_lane`
说明，禁止假实现。AI 助手入口按契约 §7 无独立端点（复用底座 chat/WS 面）。
WS 事件面（订单状态推送/确认门回执）属底座通道接入，随 paas P1 逐页联调，挂账。

## 端点覆盖（SE 业务员台，契约 15 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /se/workspace | 名下客户（主数据 `sales_rep`）+ 订单/对账/处置任务聚合卡 | 实现 |
| CRM | GET /se/customers(/{id})(/orders) | 主数据客户档 + kernel query_credit（账本同源）+ 对账单派生摘要；未登记信用额度如实缺省 | 实现 |
| CRM | GET/POST /se/leads · POST/GET /se/visits | — | **501 NOT_IMPLEMENTED**（模板层无 CRM 线索/拜访车道） |
| 代客下单 | POST /se/orders/proxy · GET /se/orders(/{id}) | 与 POST /ue/orders 同一 accept_order 链路（取价+信用校验同源）；业务员-客户归属主数据校验（越权 403）；代理身份以 `requested_by=se:{工号}` 留痕订单事件档（请求/服务端日志/单据三处） | 实现 |
| 预警处置 | GET /se/alerts · POST /se/alerts/{id}/handle | reports 处置任务确认车道（与 owner 席位同一确认门实现）；accept→拍板生效，reject→拒绝留痕不落账 | 实现 |
| 对账协助 | GET /se/statements | StatementService 按名下客户聚合 | 实现 |
| 对账协助 | POST /se/statements/{id}/push | — | **501 NOT_IMPLEMENTED**（端内消息通道归底座通知面；推送须留痕；客户确认仍走 UE 端确认门，SE 端不可代拍） |

计数：**实现 10/15；501 如实 5/15**。

## 端点覆盖（SP 供应商，契约 13 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /sp/workspace | 推送给我方供应商档案的采购单/到货预告聚合计数 | 实现 |
| 接单 | GET /sp/purchase-orders(/{id}) · POST /{poId}/accept | 采购编排事件档（数据域=已推送给我方供应商档案的单据）；接单回执写入采购单**同一状态机**（accepted/rejected 事件；rejected 须附 note，终态拒绝留痕）；越域 404 | 实现 |
| 发货反馈 | POST /{poId}/delivery-feedback · GET/POST /sp/arrival-notices | 编排层 delivery_feedback 留痕 + **自动生成到货预告**（与入库任务联动）；凭证影像仅存引用（中台文件服务另行接入） | 实现 |
| 对账 | GET/POST /sp/statements(/{id})(/confirm) | — | **501 NOT_IMPLEMENTED**（契约数据源=金蝶 provider 往来面拉取+账本内核计算，任务 3.1 挂账；采购编排单据无金额无日期，无法如实派生对账单） |
| 发票 | GET/POST /sp/invoices | — | **501 NOT_IMPLEMENTED**（中台文件服务上传契约 + 发票查验/勾兑车道未接入；金蝶回写 contract-only） |
| 结算 | GET /sp/settlements | — | **501 NOT_IMPLEMENTED**（与金蝶往来面同源挂账；**零支付操作面红线 D4 不变**） |

计数：**实现 7/13；501 如实 6/13**。

## 端点覆盖（CP 承运商，契约 12 条）

| 域 | 端点 | 数据源 | 状态 |
|---|---|---|---|
| 工作台 | GET /cp/workspace | 运单/月台/POD 回执聚合计数 | 实现 |
| 运单 | GET /cp/waybills(/{id}) · POST /{waybillId}/accept · GET /{waybillId}/events | 交付编排运单事件档（数据域=carrier 命中的运单）；承运回执写同一事件档（rejected 终态留痕，改派由租户侧重下运单）；事件流与客户侧全单物流视图同源；轨迹不可得如实置空 | 实现 |
| 月台 | POST /{waybillId}/dock-reservation | 预约/改约/取消回执留痕（slotId 为仓库侧不透明引用，与仓库作业编排联动） | 实现 |
| 月台 | GET /cp/dock-slots | — | **501 NOT_IMPLEMENTED**（月台资源主数据/可约时段计算归仓库作业编排，未建模） |
| 提货 | POST /{waybillId}/pickup-checkin | 提货打卡回执（驱动在途状态）；定位/影像为**可选采集**（隐私声明过审后启用） | 实现 |
| 签收 | POST /{waybillId}/pod | POD 回执（驱动结单/计费事件——计费规则引擎同源，本端零落账）；异常签收进处置车道（确认门派单留痕） | 实现 |
| 运费对账 | GET/POST /cp/freight-statements(/{id})(/confirm) | — | **501 NOT_IMPLEMENTED**（运费单 read 面需计费 fee_details 按承运商归集工具，KernelClient 现无该 read 面） |

计数：**实现 8/12；501 如实 4/12**。

**三面合计：实现 25/40；501 如实 15/40**，全部带 `planned_lane`，禁止假实现。

## 统一响应壳与错误

- 成功 `{"ok":true,"data":...,"rows_returned":n}`（数组按行计，对象/写成功 0 ——
  内核 result.stamp 语义）；
- 失败 `{"ok":false,"error":{code,message,hint,detail},"rows_returned":0}`；
- 错误码封闭集（`errors.ERROR_CATALOG`）：复用内核语义码
  （NOT_FOUND/INVALID_ARGUMENT/TENANT_DENIED/CREDIT_EXCEEDED/NOT_WIRED/...）
  + 新码 AUTH_REQUIRED / PAGESET_FORBIDDEN / GRANT_REQUIRED / NOT_IMPLEMENTED
  / DATA_SCOPE_DENIED（SE 代客归属越权 403）
  （全部登记封闭集，`error_codes_selfcheck.assert_closed_catalog` 断言四类语义）；
- 分页 `offset/limit`（+`page` 便捷参数）沿内核 result.py 语义：上限
  `RESULT_MAX_ROWS=500` 不放开、truncated 标记 + `continuation:{method:"offset",
  next_offset}` 续读。

## 测试

```bash
ERP_LEDGER_DATABASE_URL=... .venv/bin/python -m pytest tests/ -q
# 缺省读 secrets/erp-demo.env 并把库名 erp_demo→erp_test_b（口令绝不打印）
```

18 用例（12 UE + 6 三面）：pagesets 授权形状对齐 → 商品检索/详情（池过滤+库存
可见性）→ 进货单 → 取价试算（额度预估）→ 下单（信用占用真记账）→ 查单/越域
404 → 履约（handoff_pending → plan → view）→ 对账单确认门（pending→confirmed）→
异议处置车道 → 跨租户 403 → fail-closed 无授权 403 → grant 缺失 403 →
5×501 如实 → 分页/上限/壳/校验错误 400；
SE 面：工作台/名下客户/360/代客下单全链（归属越权 403 + 额度不足 422 + 代理
身份留痕）/预警处置拍板（accept/reject）/CRM 域 501；
SP 面：推送可见域 → 接单（重复裁决 409、拒单须 note）→ 发货反馈（自动到货
预告）→ 补录 → 入库任务联动 → 越权 404 → 对账/发票/结算 501；
CP 面：运单域 → 承运回执 → 月台（预留/改约/取消）→ 提货打卡 → 正常/异常 POD
（异常进处置车道）→ 事件流 → dock-slots/运费单 501；
分页/统一壳/租户闸对三面同壳复验。

## 如实挂账

- 对账单「与金蝶往来面同源」：金蝶 live 接通属任务 3.1 挂账（待客户凭证），
  现行行项目源 = 编排层订单事件 + 内核信用占用（响应内 `source_note` 如实标注）；
  SP 应付对账单因此 501（采购单无金额无日期，不伪造派生）；
- 下单收货信息/备注：受理回执回显（shipping_receipt/note_receipt），编排层
  accept_order 尚不持久化收货字段；
- SE 代理身份持久化：订单事件 `requested_by=se:{工号}`（编排 JSONL append-only，
  即服务端留痕）；独立审计流随底座 PAT 落地；
- SP 接单/发货反馈与 CP 承运/月台/提货/POD 回执车道：本波在模板编排层新增的
  最小回流车道（同一状态机事件档，additive，模板包 99 用例零破坏）；
- CP 运单结单态/计费事件、运费单 read 面（fee_details 按承运商归集）、
  月台时段主数据、SP 发票查验/勾兑车道、CRM 线索/拜访车道、端内消息通道：
  均如实 501（planned_lane 见端点覆盖表）；
- 运单轨迹/电子回单：DemoLogisticsAdapter（合成 read 面），真实承运商回流随
  连接器 onboarding；
- WS 事件面与 paas P1 UE/SE/SP/CP 逐页实施随编排层联调（tasks 6.5 注记）。
