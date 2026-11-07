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
from dataclasses import dataclass
from typing import Any

from fd_erp_vertical_templates.fulfillment import (DemoLogisticsAdapter,
                                                   FulfillmentOrchestrator)
from fd_erp_vertical_templates.masterdata import MasterData
from fd_erp_vertical_templates.orchestration import (OrderOrchestrator,
                                                     ProcurementOrchestrator)
from fd_erp_vertical_templates.pricing import PricingRuleBook
from fd_erp_vertical_templates.reports import ThresholdEvaluator
from fd_erp_vertical_templates.wms_sync import DemoWmsAdapter, WmsSnapshotAdapter

from .config import Settings
from .errors import ApiError


@dataclass
class Drivers:
    """REST 面可见的全部模板层驱动（单租户，进程内单例）。"""

    tenant_id: str
    master: MasterData
    pricing: PricingRuleBook
    orders: OrderOrchestrator
    procurement: ProcurementOrchestrator
    fulfillment: FulfillmentOrchestrator
    wms: WmsSnapshotAdapter
    disposal: ThresholdEvaluator


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
    procurement = ProcurementOrchestrator(settings.tenant_id, data_dir, master)
    logistics = DemoLogisticsAdapter()
    wms = DemoWmsAdapter(load_wms_snapshot(settings.wms_snapshot_file))
    fulfillment = FulfillmentOrchestrator(settings.tenant_id, data_dir, master,
                                          wms, logistics)
    disposal = ThresholdEvaluator(settings.tenant_id, data_dir)
    return Drivers(tenant_id=settings.tenant_id, master=master, pricing=pricing,
                   orders=orders, procurement=procurement, fulfillment=fulfillment,
                   wms=wms, disposal=disposal)


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
