"""驱动装配：REST 面全部业务读写经模板层驱动（零旁路，无平行取数路径）。

组件与模板包一一对应：
- MasterData            主数据三档 + 商品池（建档/授权走确认车道）
- PricingRuleBook       取价引擎（规则链 provenance，无出处规则不参与）
- OrderOrchestrator     订单受理编排（信用校验→取价→reserve_credit→订单提案）
- ProcurementOrchestrator 采购编排（SP 页面集回流：接单回执/发货反馈/到货预告）
- FulfillmentOrchestrator 交付编排（直发/仓内路径 + 运单轨迹只读适配器
                          + CP 页面集回流回执：承运接单/月台/提货/POD）
- WmsSnapshotAdapter    库存展示 read 面（快照只读；缺快照源如实降级）
- ThresholdEvaluator    处置任务车道（对账单异议进入内部处置车道）

本模块不触碰任何账务事实：账务真源在内核（经 KernelClient 工具面）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from fd_erp_vertical_templates.fulfillment import (DemoLogisticsAdapter,
                                                   FulfillmentOrchestrator)
from fd_erp_vertical_templates.freight import FreightStatementLane
from fd_erp_vertical_templates.inventory import InventoryLane
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.aging import PayableAging, ReceivableAging
from fd_erp_vertical_templates.bad_debt import BadDebtLane
from fd_erp_vertical_templates.consolidation import ConsolidationBook
from fd_erp_vertical_templates.closing import PeriodClosing
from fd_erp_vertical_templates.month_end import MonthEndChecklist
from .periods import PeriodBook
from fd_erp_vertical_templates.assets import FixedAssetLane
from fd_erp_vertical_templates.budget import BudgetBook
from fd_erp_vertical_templates.invoice_receipt import InvoiceReceiptLane
from fd_erp_vertical_templates.cost import CostEngine
from fd_erp_vertical_templates.policy import PolicyBook
from fd_erp_vertical_templates.statements import StatementBook
from fd_erp_vertical_templates.orchestration import (AfterSalesOrchestrator,
                                                     CrmOrchestrator,
                                                     InvoiceRequestLane,
                                                     OrderOrchestrator,
                                                     ProcurementOrchestrator)
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.reports import ThresholdEvaluator
from fd_erp_vertical_templates.wms_sync import DemoWmsAdapter, WmsSnapshotAdapter

from .config import Settings
from .errors import ApiError

#: 内核的对象维度「显式未知」值（与内核 events.UNKNOWN / 模板层 orchestration.UNKNOWN
#: 同一个字符串）。三处必须一致，否则同一批货记在一个维度、查在另一个维度。
UNKNOWN_DIMENSION = "__unknown__"

#: 过渡期双写开关（change: add-carrier-freight-dimension 任务 2.2 / design D5）。
#: 默认**关**：关=只写本地归档，既有写面一个字节不变；开=本地归档照写，同时把
#: 提货出库事实写进事件层（`goods_issued`，draft，带 waybill/carrier），两份逐日
#: 对账（`scripts/recon_delivery_events.py`）。
#: 直接读环境而非进 `Settings`：这是随本单拆除的过渡开关，不是长期配置项；同类先例
#: 见 `logs.py` 的访问日志开关。
EMIT_EVENTS_ENV = "ERP_FULFILLMENT_EMIT_EVENTS"


def _emit_events_enabled() -> bool:
    raw = (os.environ.get(EMIT_EVENTS_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass
class Drivers:
    """REST 面可见的全部模板层驱动（单租户，进程内单例）。"""

    tenant_id: str
    master: MasterData
    pricing: PricingRuleBook
    orders: OrderOrchestrator
    aftersales: AfterSalesOrchestrator
    crm: CrmOrchestrator
    invoices: InvoiceRequestLane
    procurement: ProcurementOrchestrator
    fulfillment: FulfillmentOrchestrator
    freight: FreightStatementLane
    #: 库存作业车道（入库/出库/调拨/盘点）：事实走拟稿车道，拍板后由 API 侧过门再投影。
    inventory: InventoryLane
    #: 口径册（成本/预算/资产/合并的管理口径唯一出处）：变更走确认车道。
    policy: PolicyBook
    #: 成本计价（对已确认事实重放；方法由口径选）。
    cost: CostEngine
    #: 固定资产车道：登记→折旧计划→期末计提（pending proposal 走确认门）→净值。
    assets: FixedAssetLane
    #: 预算控制车道：承诺由本层记（带维度），实际从内核分录按科目只读汇总。
    budget: BudgetBook
    #: 开票回执车道：票开出来之后登记号码/金额/渠道，并答「谁还没开票」。
    invoice_receipts: InvoiceReceiptLane
    #: 财务报表：试算平衡 / 利润表 / 资产负债表 / 按维度费用（全部从分录现算）。
    statements: StatementBook
    #: 应收账龄：按客户（`customer` 维度）FIFO 核销后分桶。
    receivables: ReceivableAging
    #: 应付账龄：同一个引擎的镜像绑定（开放项＝贷方），默认 `2000 应付账款`。
    payables: PayableAging
    #: 期间关账：关/重开/查状态/复算核对（期间语义全在内核，本层只绑定）。
    periods: PeriodBook
    #: 应收减值：按账龄分档计提坏账准备（增量入账，口径在册、费率人工登记）。
    bad_debts: BadDebtLane
    #: 合并报表：按 entity 维度分主体取数 + 内部往来抵销 + 平衡断言（工作底稿）。
    consolidation: ConsolidationBook
    #: 期末损益结转：逐科目结平 + 差额入 3100（只出待确认；分录带 close: 前缀）。
    closing: PeriodClosing
    #: 月结清单：结转成本 → 折旧 → 减值三步准备 + 结转损益（只出待确认），无待办才关账。
    month_end: MonthEndChecklist
    wms: WmsSnapshotAdapter
    disposal: ThresholdEvaluator
    #: 内核工具面实现（形态无关）。数量面只读查询直接经它——库存余额是内核的派生
    #: 结果，REST 面不另建一条取数路径（零旁路纪律）。
    kernel: Any = None


def load_wms_snapshot(path: str | None) -> list[dict[str, Any]]:
    """从 JSON 文件读 WMS 快照位置清单（缺文件/损坏 → 空快照，如实降级不猜）。"""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    return body if isinstance(body, list) else []


def build_drivers(settings: Settings, kernel) -> Drivers:
    """装配模板层驱动（kernel 为 KernelClient 协议实现，本模块不感知形态）。"""
    data_dir = settings.data_path()
    master = MasterData(settings.tenant_id, data_dir)
    pricing = PricingRuleBook(settings.tenant_id, data_dir, master)
    orders = OrderOrchestrator(settings.tenant_id, data_dir, kernel, master,
                               pricing, base_currency=settings.base_currency)
    procurement = ProcurementOrchestrator(settings.tenant_id, data_dir, master,
                                          kernel=kernel)
    logistics = DemoLogisticsAdapter()
    wms = DemoWmsAdapter(load_wms_snapshot(settings.wms_snapshot_file))
    aftersales = AfterSalesOrchestrator(settings.tenant_id, data_dir, master,
                                        orders=orders)
    crm = CrmOrchestrator(settings.tenant_id, data_dir, master)
    invoices = InvoiceRequestLane(settings.tenant_id, kernel)
    # 注入内核 = 本层可以把交付事实写进事件层；是否真写由双写开关决定（默认关）。
    # 不注入内核则本层与事件层零耦合（单测与纯编排场景）。
    fulfillment = FulfillmentOrchestrator(settings.tenant_id, data_dir, master,
                                          wms, logistics, kernel=kernel,
                                          emit_events=_emit_events_enabled())
    # 运费单车道：归集读的是内核费用行（`list_fees` 每行的 `refs`），确认走内核
    # 自平核对 proposal；本层不另存一份运费单状态（第二份事实源）。
    freight = FreightStatementLane(settings.tenant_id, kernel,
                                   base_currency=settings.base_currency)
    # 库存作业车道：入库/出库/调拨/盘点落内核数量面事实。模板层不过确认门
    # （内核工具面契约把 decide_event 列为模板「故意不覆盖」），拍板由本层端点完成。
    inventory = InventoryLane(settings.tenant_id, data_dir, kernel)
    # 口径册与成本计价：口径是唯一出处，成本按口径重放事件——不建成本表、不改迁移。
    policy = PolicyBook(settings.tenant_id, data_dir)
    cost = CostEngine(settings.tenant_id, data_dir, kernel, policy,
                      base_currency=settings.base_currency)
    # 固定资产车道：计提只出待确认 proposal（借费用 / 贷 1602），过门后由内核落账。
    assets = FixedAssetLane(settings.tenant_id, data_dir, kernel, policy)
    # 预算控制：维度/粒度/控制方式全读册；实际值只到科目层（内核分录无维度列）。
    budget = BudgetBook(settings.tenant_id, data_dir, kernel, policy)
    # 开票回执：申请状态读内核投影；回执不落内核（票据本体不进账本）。
    invoice_receipts = InvoiceReceiptLane(settings.tenant_id, data_dir, kernel)
    # 三表从分录现算：不建表、不缓存（资产负债表自带平衡断言）。
    statements = StatementBook(settings.tenant_id, kernel,
                               base_currency=settings.base_currency)
    # 账龄：客户归属走 customer 维度；未标客户单列，不摊给任何客户。
    receivables = ReceivableAging(settings.tenant_id, kernel,
                                  base_currency=settings.base_currency)
    # 应付账龄与应收共用同一份核销分桶实现（开放项＝贷方），不是镜像复制。
    payables = PayableAging(settings.tenant_id, kernel,
                            base_currency=settings.base_currency)
    period_book = PeriodBook(kernel)
    # 坏账计提：基数取应收账龄（同一套 FIFO/维度），费率本层登记，方法读口径册。
    bad_debts = BadDebtLane(settings.tenant_id, data_dir, kernel, policy,
                            aging=receivables)
    # 合并：主体结构人工登记，取数按 entity 维度，抵销按内部往来科目对。
    consolidation = ConsolidationBook(settings.tenant_id, data_dir, kernel, policy,
                                      base_currency=settings.base_currency)
    # 期末结转：关账台账一并注入——已关期间的结转在提交前就被拒，不用等 DB 触发器。
    closing = PeriodClosing(settings.tenant_id, kernel, periods=period_book,
                            base_currency=settings.base_currency)
    # 月结清单：四条车道按顺序准备，关账入口注入内核 periods 绑定（模板层不实现关账）。
    month_end = MonthEndChecklist(settings.tenant_id, kernel, cost=cost, assets=assets,
                                  bad_debts=bad_debts, closing=closing,
                                  closer=period_book)
    disposal = ThresholdEvaluator(settings.tenant_id, data_dir)
    return Drivers(tenant_id=settings.tenant_id, master=master, pricing=pricing,
                   orders=orders, aftersales=aftersales, crm=crm,
                   invoices=invoices, procurement=procurement,
                   fulfillment=fulfillment, freight=freight,
                   inventory=inventory, policy=policy, cost=cost, assets=assets,
                   budget=budget, invoice_receipts=invoice_receipts,
                   statements=statements, receivables=receivables,
                   payables=payables, periods=period_book,
                   bad_debts=bad_debts, consolidation=consolidation,
                   closing=closing, month_end=month_end,
                   wms=wms, disposal=disposal, kernel=kernel)


def stock_on_hand(drivers: Drivers, sku: str, *,
                  warehouse: str | None = None,
                  lot: str | None = None) -> dict[str, Any]:
    """对象数量面只读查询（establish-erp-event-kernel 7.2）。

    读的是内核的对象余额（数量面唯一真相），不是 WMS 快照——两者是不同事实：
      · WMS 快照 = 外部系统的库位视图，可缺失、可落后（`stock_visibility`）；
      · 对象余额 = 本板块自己收下的事实经事件投影出来的数量，带水位与重算校验。
    维度缺省按 `__unknown__` 查：到货预告不带库位时库存就记在未知库位维度上，
    这里用同一个未知值才查得到那批货（用 None 会查成空维度、答「没有」）。
    """
    if drivers.kernel is None:
        raise ApiError("NOT_WIRED", "内核工具面未装配：数量面不可读")
    dimension = {"warehouse": warehouse or UNKNOWN_DIMENSION,
                 "lot": lot or UNKNOWN_DIMENSION}
    out = drivers.kernel.query_object_balance("sku", sku, dimension)
    return {"sku": sku, "dimension": dimension,
            "quantity": out.get("quantity"), "unit": out.get("unit"),
            "as_of_event_id": out.get("as_of_event_id"),
            "drift": out.get("drift"),
            "completeness": out.get("completeness"),
            # 读数来源必须标注（change: add-inventory-operations-lane 任务 2.2）：
            # 这个数是本板块收下并经确认的事实投影，不是外部 WMS 的实时库存。
            # 两者不一致是常态（外部台账答「仓库怎么作业」，本板块答「我方认可多少」），
            # 标错来源会让人拿它当仓库实时库存做作业决策。
            "source": "board-confirmed-facts",
            "caliber_note": "数量面＝本板块已确认事实的投影，不是外部 WMS 实时库存；"
                            "未过确认门的事实不参与任何余额，差异进差异清单"}


def stock_visibility(drivers: Drivers, product_code: str,
                     warehouse: str | None = None) -> dict[str, Any]:
    """商品详情库存可见性：WMS 快照 read 面（只读；缺快照源如实降级不伪造）。"""
    positions = drivers.wms.read_inventory_snapshot(warehouse)
    hits = [p for p in positions if p.get("product_code") == product_code]
    total = sum(float(p.get("qty") or 0) for p in hits)
    return {
        "visible": bool(hits),
        "qty_available": f"{total:g}" if hits else "0",
        "positions": hits,
        "note": None if hits else
        "无 WMS 快照或该商品无库位（库存可见性如实降级，不伪造可售量）",
        "adapter": drivers.wms.describe(),
    }
