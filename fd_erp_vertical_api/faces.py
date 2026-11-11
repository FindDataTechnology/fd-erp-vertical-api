"""SE 业务员台（15）/ SP 供应商（13）/ CP 承运商（12）三页面集 REST 面。

契约真源：fd-erp-vertical-templates/templates/supply-chain/pagesets/
{se-sales,sp-supplier,cp-carrier}/api-contract.md（同源纪律：全部端点与 MCP
工具面同源数据，本模块零直连取数 —— 业务读写一律经模板层驱动 + KernelClient）。

闸序（同 UE：app.ue_context 先例）：
1. 租户闸（全局中间件）；
2. 页面集闸：supply-chain.se / .sp / .cp 须在租户授权配置内（fail-closed 403）；
3. grant 闸：端点所需权限码（契约权限列）须已授权（403）；工作台端点按契约
   仅要求「页面集授权」（无独立 grant）；
4. 身份/数据域闸（网关 PAT 落地前的演示形态，沿 X-ERP-* 头形态）：
   - SE  X-ERP-Staff   业务员工号；归属关系 = 客户主数据 payload.sales_rep
     （主数据关系）；代客下单越权 403 DATA_SCOPE_DENIED；读面越域 404 不泄露；
   - SP  X-ERP-Supplier 供应商档案（实体 id 或编码），数据域 = 本租户推送给
     本供应商档案的采购单及其回流；越域 404 不泄露存在性；
   - CP  X-ERP-Carrier  承运商标识；运单域 = carrier 字段命中的在途运单
     （承运商档案不在主数据三档，如实以运单留痕为域）。

数据域与车道映射（如实，不伪造）：
- SE 代客下单复用 orchestration.accept_order（同一取价/信用链路），业务员身份
  以 requested_by 留痕于订单事件档（= 服务端日志，JSONL append-only）；
- SE 预警处置走 reports.ThresholdEvaluator 处置任务确认车道（与 owner 席位
  同一确认门实现，拒绝留痕不落账）；
- SP 接单/发货反馈写采购编排同一状态机（同 ref 事件档）；发货反馈自动生成
  到货预告（record_arrival_notice）；
- CP 承运回执/月台/提货/POD 写交付编排运单同一事件档；异常签收进处置车道；
- 契约有、编排层无车道的端点如实 501 NOT_IMPLEMENTED + planned_lane（沿 UE
  既有 5 个 501 先例）：CRM 线索/拜访、对账单推送（SE）、SP 对账/发票/结算、
  CP 可约月台时段/运费单。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import Depends, Request

from .drivers import Drivers
from .envelope import list_shell, ok_shell, parse_page_params
from .errors import ApiError
from .pagesets import PagesetAuth, require_grant

PAGESET_SE = "supply-chain.se"
PAGESET_SP = "supply-chain.sp"
PAGESET_CP = "supply-chain.cp"

STAFF_HEADER = "X-ERP-Staff"
SUPPLIER_HEADER = "X-ERP-Supplier"
CARRIER_HEADER = "X-ERP-Carrier"

ORDER_STATUS_NOTE = ("pending=待确认（确认车道）；confirmed=已确认移交履约；"
                     "cancelled=已取消（信用占用经内核回冲）")


def _not_implemented(message: str, planned_lane: str) -> ApiError:
    return ApiError("NOT_IMPLEMENTED", message,
                    detail={"planned_lane": planned_lane})


# --------------------------------------------------------------------------
# 共用 read 面辅助（编排层事件档同源回放；app.py _owned_order 同款先例）
# --------------------------------------------------------------------------

def _order_states(d: Drivers, customer_ids: set[str]) -> list[dict[str, Any]]:
    """订单当前态（proposed→confirmed/cancelled 合并），限客户集合（数据域）。"""
    seen: dict[str, dict[str, Any]] = {}
    for ev in d.orders._events():  # 编排层事件档（同源 read 面）
        if ev.get("record") != "order":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "proposed":
            if str(payload.get("customer_id") or "") in customer_ids:
                seen[ref] = {"ref": ref, "event": "proposed",
                             "status": ev.get("status"), "payload": dict(payload),
                             "requested_by": ev.get("requested_by")}
        elif ref in seen and ev["event"] in ("confirmed", "cancelled"):
            merged = dict(seen[ref]["payload"])
            merged.update(payload)
            seen[ref] = {"ref": ref, "event": ev["event"],
                         "status": ev.get("status"), "payload": merged,
                         "requested_by": seen[ref]["requested_by"]}
    return list(seen.values())


def _order_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = item["payload"]
    return {
        "orderId": item["ref"], "status": item["status"], "phase": item["event"],
        "status_note": ORDER_STATUS_NOTE,
        "day": payload.get("day"), "amount_total": payload.get("amount_total"),
        "currency": payload.get("currency"),
        "customer_id": payload.get("customer_id"),
        "requested_by": item.get("requested_by"),
    }


def _order_lines(body_lines: Any) -> list[dict[str, Any]]:
    """代客下单行请求解析（同 UE 下单行形态）。"""
    if not isinstance(body_lines, list) or not body_lines:
        raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
    lines: list[dict[str, Any]] = []
    for row in body_lines:
        row = row if isinstance(row, dict) else {}
        code = str(row.get("productId") or row.get("product_code") or "").strip()
        if not code:
            raise ApiError("INVALID_ARGUMENT", "行请求缺 productId")
        qty = row.get("qty")
        try:
            if qty is None or float(qty) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ApiError("INVALID_ARGUMENT", "数量 qty 须为正数",
                           detail={"qty": qty}) from exc
        entry: dict[str, Any] = {"product_code": code, "qty": qty}
        if row.get("unit"):
            entry["unit"] = str(row["unit"])
        if row.get("note"):
            entry["note"] = str(row["note"])
        lines.append(entry)
    return lines


def _fmt_amount(v: Any) -> str | None:
    try:
        return f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# SE 业务员台（supply-chain.se）
# --------------------------------------------------------------------------

def _owned_customers(d: Drivers, staff: str) -> list[dict[str, Any]]:
    """名下客户（归属关系 = 客户主数据 payload.sales_rep，主数据关系）。"""
    out = [snap for snap in d.master.list_entities(kind="customer")
           if str(snap["payload"].get("sales_rep") or "").strip() == staff]
    out.sort(key=lambda s: str(s["payload"].get("code") or ""))
    return out


def _credit_or_none(kernel, customer_id: str) -> dict[str, Any] | None:
    """信用额度位（kernel query_credit）；未登记信用额度的客户如实 None。

    内核对 credit_registry 无记录的客户报 NOT_FOUND —— read 面聚合时如实
    缺省（None + 注记），不让单个客户缺额度拖垮整个列表；其余错误原样上抛。
    """
    try:
        return kernel.query_credit(customer_id) or None
    except Exception as exc:  # noqa: BLE001 仅吞「未登记」语义，其余上抛
        if getattr(exc, "code", None) == "NOT_FOUND":
            return None
        raise


def _statement_summary(service, customer_id: str) -> dict[str, Any]:
    rows = service.list_statements(customer_id)
    open_count = sum(1 for r in rows if r["status"] in ("open", "confirming",
                                                        "disputed_pending"))
    latest = rows[-1]["period"] if rows else None
    return {"count": len(rows), "open_count": open_count, "latest_period": latest}


def register_se_face(app, settings, d: Drivers, kernel,
                     statements, auth: PagesetAuth) -> None:
    """SE 业务员台 15 端点（实现 10 + 如实 501 5）。"""

    def se_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_SE) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_SE}",
                                   detail={"pageset_key": PAGESET_SE})
            else:
                require_grant(auth, PAGESET_SE, grant)
            staff = request.headers.get(STAFF_HEADER, "").strip()
            if not staff:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少业务员身份头 {STAFF_HEADER}"
                               "（SE 身份=业务员工号；底座 PAT 网关落地前演示形态）")
            return {"staff": staff}
        return _dep

    def _owned_or_404(customer_id: str, staff: str) -> dict[str, Any]:
        """名下客户或 404（越域/未知一律 404，不泄露存在性）。"""
        snap = d.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer" or \
                str(snap["payload"].get("sales_rep") or "").strip() != staff:
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customerId": customer_id})
        return snap

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/se/workspace")
    async def se_workspace(ctx: dict = Depends(se_route(None))):
        owned = _owned_customers(d, ctx["staff"])
        owned_ids = {s["entity_id"] for s in owned}
        orders = _order_states(d, owned_ids)
        pending_orders = sum(1 for o in orders if o["status"] == "pending")
        stms = [_statement_summary(statements, s["entity_id"]) for s in owned]
        open_statements = sum(s["open_count"] for s in stms)
        alerts = d.disposal.pending_tasks()
        return ok_shell({
            "staff": ctx["staff"],
            "owned_customer_count": len(owned),
            "pending_orders": pending_orders,
            "open_statements": open_statements,
            "pending_alerts": len(alerts),
            "today_visit_plan": 0,
            "notes": {
                "crm": "CRM 线索/拜访车道未上线（GET/POST /se/leads、/se/visits "
                       "如实 501）：今日拜访计划暂不可得，不伪造",
                "alerts": "预警数 = 处置任务确认车道 pending 派单数"
                          "（reports 阈值→处置任务闭环的移动呈现）",
                "orders": "待办 = 名下客户订单确认车道待决数",
            },
        })

    # ---- 2) CRM 域 ----

    @app.get("/erp/supply-chain/v1/se/customers")
    async def se_customers(request: Request,
                           ctx: dict = Depends(se_route("crm.read"))):
        q = request.query_params
        keyword = (q.get("keyword") or "").strip().lower()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows: list[dict[str, Any]] = []
        for snap in _owned_customers(d, ctx["staff"]):
            payload = snap["payload"]
            if keyword and keyword not in str(payload.get("name") or "").lower() \
                    and keyword not in str(payload.get("code") or "").lower():
                continue
            credit = _credit_or_none(kernel, snap["entity_id"])
            summary = _statement_summary(statements, snap["entity_id"])
            rows.append({
                "customerId": snap["entity_id"],
                "code": payload.get("code"), "name": payload.get("name"),
                "state": snap.get("state"),
                "sales_rep": payload.get("sales_rep"),
                "credit_occupied": credit.get("reserved") if credit else None,
                "credit_available": credit.get("available") if credit else None,
                "ar_balance": credit.get("ar_balance") if credit else None,
                "open_statement_count": summary["open_count"],
                "credit_source": "kernel query_credit（账本同源）" if credit
                else "未登记信用额度（内核 credit_registry 无记录，如实缺省）",
            })
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/se/customers/{customer_id}")
    async def se_customer_360(customer_id: str,
                              ctx: dict = Depends(se_route("crm.read"))):
        snap = _owned_or_404(customer_id, ctx["staff"])
        credit = _credit_or_none(kernel, snap["entity_id"]) or {}
        orders = _order_states(d, {snap["entity_id"]})
        orders.sort(key=lambda i: str((i["payload"] or {}).get("day") or ""),
                    reverse=True)
        return ok_shell({
            "customerId": snap["entity_id"],
            "profile": dict(snap["payload"]),
            "state": snap.get("state"),
            "credit": {
                "limit": credit.get("limit"), "ar_balance": credit.get("ar_balance"),
                "reserved": credit.get("reserved"),
                "available": credit.get("available"),
                "source_note": "额度占用与账本 query_balance 同源"
                               "（kernel credit_position，账本计算）" if credit
                else "未登记信用额度（内核 credit_registry 无记录，如实缺省）",
            },
            "recent_orders": [_order_row(o) for o in orders[:5]],
            "order_count": len(orders),
            "statements": _statement_summary(statements, snap["entity_id"]),
        })

    @app.get("/erp/supply-chain/v1/se/customers/{customer_id}/orders")
    async def se_customer_orders(request: Request, customer_id: str,
                                 ctx: dict = Depends(se_route("crm.read"))):
        require_grant(auth, PAGESET_SE, "order.write.proxy")  # 契约双 grant
        _owned_or_404(customer_id, ctx["staff"])
        q = request.query_params
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = [_order_row(o) for o in _order_states(d, {customer_id})]
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]),
                  reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/se/leads")
    async def se_list_leads(ctx: dict = Depends(se_route("crm.read"))):
        raise _not_implemented(
            "CRM 线索池后端能力未就绪：模板层无 CRM 线索车道（orchestration 层"
            "无线索/拜访事件记录）；禁止假实现",
            "planned_lane: CRM 线索/拜访事件档（编排层扩展）+ 底座 chat 面线索助手")

    @app.post("/erp/supply-chain/v1/se/leads")
    async def se_create_lead(body: dict[str, Any],
                             ctx: dict = Depends(se_route("crm.visit.write"))):
        raise _not_implemented(
            "建线索后端能力未就绪：模板层无 CRM 线索车道；禁止假实现",
            "planned_lane: CRM 线索/拜访事件档（编排层扩展）")

    @app.post("/erp/supply-chain/v1/se/visits")
    async def se_create_visit(body: dict[str, Any],
                              ctx: dict = Depends(se_route("crm.visit.write"))):
        raise _not_implemented(
            "拜访登记后端能力未就绪：模板层无 CRM 拜访车道（定位等采集项还须"
            "隐私声明过审后启用）；禁止假实现",
            "planned_lane: CRM 拜访事件档（编排层扩展；定位/影像可选采集随隐私"
            "声明过审启用）")

    @app.get("/erp/supply-chain/v1/se/visits")
    async def se_list_visits(request: Request,
                             ctx: dict = Depends(se_route("crm.visit.write"))):
        raise _not_implemented(
            "拜访记录后端能力未就绪（同 POST /se/visits）",
            "planned_lane: CRM 拜访事件档（编排层扩展）")

    # ---- 3) 代客下单域（确认门 + 代理身份留痕）----

    @app.post("/erp/supply-chain/v1/se/orders/proxy")
    async def se_order_proxy(body: dict[str, Any],
                             ctx: dict = Depends(se_route("order.write.proxy"))):
        """代客下单：与 POST /ue/orders 同一取价引擎与信用校验链路（同源）。

        业务员-客户归属校验（主数据 sales_rep）越权 403；代理身份三处留痕：
        请求（X-ERP-Staff）→ 服务端日志（编排 JSONL append-only）→ 业务单据
        （订单事件 requested_by）。额度不足 fail-closed 可解释拒绝。
        """
        customer_id = str(body.get("customerId") or "").strip()
        if not customer_id:
            raise ApiError("INVALID_ARGUMENT", "缺 customerId")
        snap = d.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer":
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customerId": customer_id})
        if str(snap["payload"].get("sales_rep") or "").strip() != ctx["staff"]:
            raise ApiError("DATA_SCOPE_DENIED",
                           "代客下单越权：客户不在本人名下（业务员-客户归属"
                           "主数据校验失败；「代客」不改变数据域）",
                           detail={"customerId": customer_id,
                                   "staff": ctx["staff"]})
        requested_by = f"se:{ctx['staff']}"
        result = d.orders.accept_order(
            customer_id, _order_lines(body.get("lines")),
            date.today().isoformat(), requested_by=requested_by)
        if not result.get("accepted"):
            raise ApiError("CREDIT_EXCEEDED",
                           "订单金额超出可用信用额度（fail-closed 拒单）",
                           detail=result.get("explanation") or {})
        result["proxy"] = {
            "staff": ctx["staff"], "customer_id": customer_id,
            "requested_by": requested_by,
            "note": "代理身份已留痕（订单事件 requested_by = 编排层服务端日志"
                    " append-only）；数据域仍限本人名下客户",
        }
        result["shipping_receipt"] = body.get("shipping") or {}
        result["note_receipt"] = body.get("note") or ""
        return ok_shell(result)

    @app.get("/erp/supply-chain/v1/se/orders")
    async def se_orders(request: Request,
                        ctx: dict = Depends(se_route("order.write.proxy"))):
        q = request.query_params
        customer_id = (q.get("customerId") or "").strip()
        status = (q.get("status") or "").strip() or None
        date_from = (q.get("from") or "").strip() or None
        date_to = (q.get("to") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        owned_ids = {s["entity_id"] for s in _owned_customers(d, ctx["staff"])}
        if customer_id:
            _owned_or_404(customer_id, ctx["staff"])  # 越域 404
            owned_ids &= {customer_id}
        rows = []
        for item in _order_states(d, owned_ids):
            payload = item["payload"]
            day = str(payload.get("day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            if status and item["status"] != status:
                continue
            rows.append(_order_row(item))
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]),
                  reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/se/orders/{order_id}")
    async def se_order_detail(order_id: str,
                              ctx: dict = Depends(se_route("order.write.proxy"))):
        owned_ids = {s["entity_id"] for s in _owned_customers(d, ctx["staff"])}
        hits = [o for o in _order_states(d, owned_ids) if o["ref"] == order_id]
        if not hits:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_id}",
                           detail={"orderId": order_id})
        item = hits[0]
        payload = item["payload"]
        return ok_shell({
            "orderId": item["ref"], "status": item["status"],
            "phase": item["event"], "status_note": ORDER_STATUS_NOTE,
            "day": payload.get("day"), "amount_total": payload.get("amount_total"),
            "currency": payload.get("currency"), "lines": payload.get("lines") or [],
            "credit_reservation": payload.get("credit_reservation"),
            "requested_by": item.get("requested_by"),
            "request_id": payload.get("request_id"),
        })

    # ---- 4) 预警处置域（拍板席位移动呈现；确认门与 owner 同源）----

    def _disposal_rows(status: str | None, severity: str | None) -> list[dict[str, Any]]:
        tasks = d.disposal.list_tasks()  # 处置车道事件档（同源 read 面）
        if status and status != "all":
            tasks = [t for t in tasks if t.get("status") == status]
        elif not status:
            tasks = [t for t in tasks if t.get("status") == "pending"]
        rows = []
        for t in tasks:
            payload = t.get("payload") or {}
            if severity and str(payload.get("severity") or "") != severity:
                continue
            rows.append({
                "alertId": t.get("request_id"), "status": t.get("status"),
                "metric": payload.get("metric"), "value": payload.get("value"),
                "threshold": payload.get("threshold"),
                "severity": payload.get("severity"),
                "title": payload.get("title"),
                "assignee": payload.get("assignee"),
                "task_template": payload.get("task_template"),
                "note": "阈值预警→处置任务闭环（tasks 7.2）的移动呈现；"
                        "拍板与 owner 席位同一确认门实现",
            })
        rows.sort(key=lambda r: str(r["alertId"]))
        return rows

    @app.get("/erp/supply-chain/v1/se/alerts")
    async def se_alerts(request: Request,
                        ctx: dict = Depends(se_route("alert.handle"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        severity = (q.get("severity") or q.get("level") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        return list_shell(_disposal_rows(status, severity), offset=offset,
                          limit=limit)

    @app.post("/erp/supply-chain/v1/se/alerts/{alert_id}/handle")
    async def se_alert_handle(alert_id: str, body: dict[str, Any],
                              ctx: dict = Depends(se_route("alert.handle"))):
        """处置回执：accept→确认门拍板生效；reject→拒绝留痕不落账。

        与 owner 席位同一确认门实现（reports 处置任务 confirm/reject 车道）；
        超时/失败不假报成功：回执如实回显车道结果。
        """
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accept", "reject"):
            raise ApiError("INVALID_ARGUMENT", "decision 须为 accept / reject",
                           detail={"decision": decision})
        note = str(body.get("note") or "").strip()
        decided_by = f"se:{ctx['staff']}"
        if decision == "accept":
            out = d.disposal.confirm_disposal_task(alert_id, decided_by=decided_by)
            out["handled_by"] = decided_by
            return ok_shell(out)
        out = d.disposal.reject_disposal_task(alert_id, note, decided_by=decided_by)
        out["handled_by"] = decided_by
        return ok_shell(out)

    # ---- 5) 对账协助域 ----

    @app.get("/erp/supply-chain/v1/se/statements")
    async def se_statements(request: Request,
                            ctx: dict = Depends(se_route(
                                "statement.confirm.proxy"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows: list[dict[str, Any]] = []
        for snap in _owned_customers(d, ctx["staff"]):
            for st in statements.list_statements(snap["entity_id"], status=status):
                rows.append(st)
        rows.sort(key=lambda r: (str(r.get("period") or ""), r["statement_id"]))
        return list_shell(rows, offset=offset, limit=limit)

    @app.post("/erp/supply-chain/v1/se/statements/{statement_id}/push")
    async def se_statement_push(statement_id: str, body: dict[str, Any],
                                ctx: dict = Depends(se_route(
                                    "statement.confirm.proxy"))):
        raise _not_implemented(
            "对账单推送后端能力未就绪：端内消息通道归底座通知面（模板层无消息"
            "车道）；推送须留痕，禁止无车道假实现。客户确认仍走 UE 端确认门，"
            "SE 端不可代拍",
            "planned_lane: 底座端内消息通道（notify 车道）接入后实施推送留痕；"
            "客户确认门=UE /ue/statements/{id}/confirm（内核 proposal 车道）")

    # 契约 §6：AI 助手入口无独立端点（复用底座 chat/WS 面）。


# --------------------------------------------------------------------------
# SP 供应商（supply-chain.sp）
# --------------------------------------------------------------------------

def _resolve_supplier(d: Drivers, ref: str) -> dict[str, Any] | None:
    """供应商身份解析：实体 id 优先，其次编码（越域/未知由调用方 404）。"""
    snap = d.master.get_entity(ref)
    if snap is not None and snap["kind"] == "supplier":
        return snap
    hit = d.master.find_by_code("supplier", ref)
    if hit is not None and hit["kind"] == "supplier":
        return hit
    return None


def _po_states(d: Drivers, supplier_id: str | None = None) -> list[dict[str, Any]]:
    """采购单当前态（proposed→confirmed→pushed→supplier_accepted/rejected）。

    supplier_id 给定时只返回「已推送给该供应商」的采购单（数据域：本租户推送
    给本供应商档案的数据；未推送提案对供应商不可见）。
    """
    pos: dict[str, dict[str, Any]] = {}
    for ev in d.procurement._events():  # 采购编排事件档（同源 read 面）
        if ev.get("record") != "purchase_order":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "proposed":
            pos[ref] = {"ref": ref, "event": "proposed",
                        "status": ev.get("status"), "payload": dict(payload),
                        "pushed": False, "push_channel": None,
                        "supplier_response": None}
            continue
        item = pos.get(ref)
        if item is None:
            continue
        item["event"] = ev.get("event")
        item["status"] = ev.get("status")
        if ev["event"] == "pushed":
            item["pushed"] = True
            item["push_channel"] = payload.get("channel")
        if ev["event"] in ("supplier_accepted", "supplier_rejected"):
            item["supplier_response"] = dict(payload)
    out = []
    for item in pos.values():
        if supplier_id is not None:
            if not item["pushed"]:
                continue  # 未推送采购单对供应商不可见（数据域）
            if str(item["payload"].get("supplier_id") or "") != supplier_id:
                continue
        out.append(item)
    return out


def _po_visible(po_states: list[dict[str, Any]], po_ref: str) -> dict[str, Any]:
    hit = next((p for p in po_states if p["ref"] == po_ref), None)
    if hit is None:
        raise ApiError("NOT_FOUND", f"采购订单不存在：{po_ref}",
                       detail={"poId": po_ref})
    return hit


def _arrival_records(d: Drivers, po_refs: set[str]) -> list[dict[str, Any]]:
    return [e for e in d.procurement._events()
            if e.get("record") == "arrival_notice"
            and str((e.get("payload") or {}).get("po_ref") or "") in po_refs]


def _inbound_tasks(d: Drivers, arrival_refs: set[str]) -> list[dict[str, Any]]:
    return [e for e in d.procurement._events()
            if e.get("record") == "inbound_task"
            and str((e.get("payload") or {}).get("arrival_ref") or "") in arrival_refs]


def _po_feedbacks(d: Drivers, po_ref: str) -> list[dict[str, Any]]:
    return d.procurement.delivery_feedbacks(po_ref)


def register_sp_face(app, settings, d: Drivers, kernel,
                     auth: PagesetAuth) -> None:
    """SP 供应商 13 端点（实现 7 + 如实 501 6）。资金红线：本端零支付操作面。"""

    def sp_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_SP) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_SP}",
                                   detail={"pageset_key": PAGESET_SP})
            else:
                require_grant(auth, PAGESET_SP, grant)
            ref = request.headers.get(SUPPLIER_HEADER, "").strip()
            if not ref:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少供应商身份头 {SUPPLIER_HEADER}"
                               "（SP 身份=供应商档案 id/编码；网关 PAT 落地前演示形态）")
            snap = _resolve_supplier(d, ref)
            if snap is None:
                raise ApiError("NOT_FOUND", "供应商档案不存在",
                               detail={"supplier": ref})
            return {"supplier_id": snap["entity_id"], "supplier": snap,
                    "supplier_code": snap["payload"].get("code")}
        return _dep

    def _my_pos(supplier_id: str) -> list[dict[str, Any]]:
        return _po_states(d, supplier_id)

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/sp/workspace")
    async def sp_workspace(ctx: dict = Depends(sp_route(None))):
        supplier_id = ctx["supplier_id"]
        pos = _my_pos(supplier_id)
        po_refs = {p["ref"] for p in pos}
        arrivals = _arrival_records(d, po_refs)
        fb_po = {f["payload"].get("po_ref") for f in
                 [e for e in d.procurement._events()
                  if e.get("record") == "delivery_feedback"]}
        pending_accept = sum(1 for p in pos if p["status"] == "pushed"
                             and p["supplier_response"] is None)
        pending_ship = sum(1 for p in pos
                           if p["status"] in ("pushed", "accepted")
                           and p["ref"] not in fb_po)
        arrivals_open = sum(1 for a in arrivals if a.get("status") == "open")
        return ok_shell({
            "supplier_id": supplier_id, "supplier_code": ctx["supplier_code"],
            "po_count": len(pos),
            "po_pending_accept": pending_accept,
            "po_pending_ship": pending_ship,
            "arrival_notices_open": arrivals_open,
            "statements_pending": 0, "invoices_pending_verify": 0,
            "notes": {
                "statements": "对账域后端能力未就绪（501 如实）：应付对账单与"
                              "金蝶往来面同源（任务 3.1 挂账），计数暂不可得",
                "invoices": "发票域后端能力未就绪（501 如实）：上传/查验车道"
                            "未接入，计数暂不可得",
                "settlements": "结算查询与金蝶往来面同源（挂账）；本端零支付"
                               "操作面（红线 D4）",
            },
        })

    # ---- 2) 接单域（确认门）----

    @app.get("/erp/supply-chain/v1/sp/purchase-orders")
    async def sp_purchase_orders(request: Request,
                                 ctx: dict = Depends(sp_route("po.read"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        period = (q.get("period") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = []
        for p in _my_pos(ctx["supplier_id"]):
            if status and p["status"] != status:
                continue
            payload = p["payload"]
            response = p["supplier_response"]
            rows.append({
                "poId": p["ref"], "status": p["status"], "event": p["event"],
                "supplier_id": payload.get("supplier_id"),
                "aggregate_ref": payload.get("aggregate_ref"),
                "lines": payload.get("lines") or [],
                "push_channel": p["push_channel"],
                "supplier_response": response,
                "status_note": "pushed=已推送待接单；accepted=已接单（到货预告"
                               "编排可驱动）；rejected=已拒单（终态，拒绝留痕）",
            })
        rows.sort(key=lambda r: r["poId"])
        extra = {"source": "采购编排事件档（同源 read 面）；数据域=推送给我方"
                           "供应商档案的采购单"}
        if period:
            extra["period_note"] = ("采购编排采购单无单据日期字段，期间过滤暂不可用"
                                    "（挂账：编排层补单据日期后生效）")
        return list_shell(rows, offset=offset, limit=limit, extra=extra)

    @app.get("/erp/supply-chain/v1/sp/purchase-orders/{po_id}")
    async def sp_po_detail(po_id: str, ctx: dict = Depends(sp_route("po.read"))):
        po = _po_visible(_my_pos(ctx["supplier_id"]), po_id)
        payload = po["payload"]
        arrivals = _arrival_records(d, {po_id})
        arrival_refs = {a["ref"] for a in arrivals}
        return ok_shell({
            "poId": po_id, "status": po["status"], "event": po["event"],
            "supplier_id": payload.get("supplier_id"),
            "aggregate_ref": payload.get("aggregate_ref"),
            "lines": payload.get("lines") or [],
            "push_channel": po["push_channel"],
            "supplier_response": po["supplier_response"],
            "delivery_feedbacks": [
                {"ref": f["ref"], "event": f["event"], "status": f["status"],
                 "payload": dict(f.get("payload") or {})}
                for f in _po_feedbacks(d, po_id)],
            "arrival_notices": [
                {"ref": a["ref"], "status": a["status"],
                 "payload": dict(a.get("payload") or {})} for a in arrivals],
            "inbound_tasks": [
                {"ref": t["ref"], "status": t["status"],
                 "payload": dict(t.get("payload") or {})}
                for t in _inbound_tasks(d, arrival_refs)],
            "field_note": "交期/收货仓：采购编排提案不携带交期与收货仓字段"
                          "（如实缺省，待编排层补字段）",
        })

    @app.post("/erp/supply-chain/v1/sp/purchase-orders/{po_id}/accept")
    async def sp_po_accept(po_id: str, body: dict[str, Any],
                           ctx: dict = Depends(sp_route("po.accept"))):
        """接单回执：确认门语义；写入采购单同一状态机（不建第二状态机）。

        rejected 须附 rejectedNote（可解释拒绝纪律）；回执驱动到货预告编排。
        """
        po = _po_visible(_my_pos(ctx["supplier_id"]), po_id)  # 越域 404
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accepted", "rejected"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 accepted / rejected",
                           detail={"decision": decision})
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_supplier_response(
            po_id, decision, note=str(body.get("rejectedNote") or ""),
            responded_by=responded_by)
        out["po_status_before"] = po["status"]
        return ok_shell(out)

    # ---- 3) 发货反馈域 ----

    @app.post("/erp/supply-chain/v1/sp/purchase-orders/{po_id}/delivery-feedback")
    async def sp_delivery_feedback(po_id: str, body: dict[str, Any],
                                   ctx: dict = Depends(sp_route(
                                       "delivery.feedback"))):
        """发货反馈回执：行级明细 + 物流商/单号/凭证影像引用；自动生成到货
        预告（与入库任务联动，编排面同源）。凭证影像仅存引用（中台文件服务
        上传契约另行接入）。"""
        _po_visible(_my_pos(ctx["supplier_id"]), po_id)  # 越域 404
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "发货反馈 lines 须为非空数组")
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_delivery_feedback(
            po_id, lines,
            carrier=str(body.get("carrier") or ""),
            waybill_no=str(body.get("waybillNo") or ""),
            documents=[str(x) for x in (body.get("documents") or [])],
            expected_day=str(body.get("expectedDay") or "") or None,
            responded_by=responded_by)
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/sp/arrival-notices")
    async def sp_arrival_notices(request: Request,
                                 ctx: dict = Depends(sp_route(
                                     "delivery.feedback"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        po_refs = {p["ref"] for p in _my_pos(ctx["supplier_id"])}
        rows = []
        for a in _arrival_records(d, po_refs):
            if status and a.get("status") != status:
                continue
            payload = dict(a.get("payload") or {})
            rows.append({"ref": a["ref"], "status": a["status"],
                         "event": a["event"], **payload})
        rows.sort(key=lambda r: r["ref"])
        return list_shell(rows, offset=offset, limit=limit)

    @app.post("/erp/supply-chain/v1/sp/arrival-notices")
    async def sp_arrival_notice_create(body: dict[str, Any],
                                       ctx: dict = Depends(sp_route(
                                           "delivery.feedback"))):
        """到货预告补录（发货反馈已自动生成，本端点承载补录）。"""
        po_ref = str(body.get("poRef") or body.get("po_ref") or "").strip()
        if not po_ref:
            raise ApiError("INVALID_ARGUMENT", "缺 poRef（订单引用）")
        _po_visible(_my_pos(ctx["supplier_id"]), po_ref)  # 越域 404
        lines = body.get("lines")
        if not isinstance(lines, list) or not lines:
            raise ApiError("INVALID_ARGUMENT", "到货预告 lines 须为非空数组")
        responded_by = f"sp:{ctx['supplier_code'] or ctx['supplier_id']}"
        out = d.procurement.record_arrival_notice(
            po_ref, lines, expected_day=str(body.get("expectedDay") or "") or None,
            requested_by=responded_by)
        return ok_shell(out)

    # ---- 4) 对账域 —— 后端能力未就绪，如实 501 ----

    _SP_STATEMENT_501 = (
        "应付对账单后端能力未就绪：契约数据源=金蝶 provider 往来面拉取 + 账本"
        "内核计算（api-contract §4）；金蝶 live 接通属任务 3.1 挂账，采购编排"
        "单据无金额无日期，无法如实派生对账单 —— 禁止假实现")

    @app.get("/erp/supply-chain/v1/sp/statements")
    async def sp_statements(ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501,
                               "planned_lane: 金蝶 provider 往来面拉取"
                               "（任务 3.1）→ 账本内核计算应付 → 单据级对账单"
                               "派生 + 确认门（内核自平核对 proposal，同 UE 契约）")

    @app.get("/erp/supply-chain/v1/sp/statements/{statement_id}")
    async def sp_statement_detail(statement_id: str,
                                  ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501, "planned_lane: 同 GET /sp/statements")

    @app.post("/erp/supply-chain/v1/sp/statements/{statement_id}/confirm")
    async def sp_statement_confirm(statement_id: str, body: dict[str, Any],
                                   ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(_SP_STATEMENT_501,
                               "planned_lane: 金额同源到位后确认门=内核自平核对"
                               " proposal（超时/失败不假报成功）；异议进处置车道")

    # ---- 5) 发票域 —— 后端能力未就绪，如实 501 ----

    _SP_INVOICE_501 = (
        "发票上传后端能力未就绪：上传件中台文件服务与发票查验/勾兑车道均未接入，"
        "金蝶连接器回写为 contract-only（docs/connector-onboarding-supply-chain.md"
        " §1.2）；禁止假实现")

    @app.post("/erp/supply-chain/v1/sp/invoices")
    async def sp_invoice_upload(body: dict[str, Any],
                                ctx: dict = Depends(sp_route("invoice.upload"))):
        raise _not_implemented(
            _SP_INVOICE_501,
            "planned_lane: 中台文件服务上传契约 + 发票查验/勾兑车道 + 金蝶连接器"
            "回写（contract-only）")

    @app.get("/erp/supply-chain/v1/sp/invoices")
    async def sp_invoice_list(ctx: dict = Depends(sp_route("invoice.upload"))):
        raise _not_implemented(_SP_INVOICE_501,
                               "planned_lane: 同 POST /sp/invoices（上传历史与"
                               "核验进度 read 面）")

    # ---- 6) 结算查询域（只读；零支付操作面）—— 未就绪，如实 501 ----

    @app.get("/erp/supply-chain/v1/sp/settlements")
    async def sp_settlements(ctx: dict = Depends(sp_route("statement.read"))):
        raise _not_implemented(
            "结算进度查询后端能力未就绪：结算与金蝶往来面同源（任务 3.1 挂账）；"
            "本端零支付操作面（红线 D4：对外付款走金蝶/持牌通道）不变",
            "planned_lane: 金蝶付款/结算回写 read 面（只读，无任何支付/账户操作）")

    # 契约 §7：AI 助手入口无独立端点（复用底座 chat/WS 面）。


# --------------------------------------------------------------------------
# CP 承运商（supply-chain.cp）
# --------------------------------------------------------------------------

def _waybill_states(d: Drivers, carrier: str | None = None) -> list[dict[str, Any]]:
    """运单当前态（交付编排事件档同源回放；carrier 给定时按运单域过滤）。"""
    waybills: dict[str, dict[str, Any]] = {}
    for ev in d.fulfillment._events():  # 交付编排事件档（同源 read 面）
        if ev.get("record") != "waybill":
            continue
        ref = ev.get("ref")
        payload = ev.get("payload") or {}
        if ev["event"] == "created":
            if carrier is not None and str(payload.get("carrier") or "") != carrier:
                continue
            waybills[ref] = {"ref": ref, "event": ev["event"],
                             "status": ev.get("status"), "payload": dict(payload),
                             "carrier_response": None}
            continue
        item = waybills.get(ref)
        if item is None:
            continue
        item["event"] = ev.get("event")
        item["status"] = ev.get("status")
        if ev["event"] in ("carrier_accepted", "carrier_rejected"):
            item["carrier_response"] = dict(payload)
    return list(waybills.values())


def _waybill_by_ref(d: Drivers, waybill_id: str,
                    carrier: str | None) -> dict[str, Any]:
    """运单域解析：实体 ref 或运单号（waybill_no）均可；越域/未知 404。"""
    states = _waybill_states(d, carrier)
    hit = next((w for w in states if w["ref"] == waybill_id), None)
    if hit is None:
        hit = next((w for w in states
                    if str(w["payload"].get("waybill_no") or "") == waybill_id),
                   None)
    if hit is None:
        raise ApiError("NOT_FOUND", f"运单不存在：{waybill_id}",
                       detail={"waybillId": waybill_id})
    return hit


def _plan_of(d: Drivers, plan_ref: str) -> dict[str, Any] | None:
    hit = next((e for e in d.fulfillment._events()
                if e.get("record") == "fulfillment_plan"
                and e.get("ref") == plan_ref and e.get("event") == "created"), None)
    return dict(hit) if hit else None


def register_cp_face(app, settings, d: Drivers, kernel,
                     auth: PagesetAuth) -> None:
    """CP 承运商 12 端点（实现 8 + 如实 501 4）。计费同源：CP 端只读确认，
    不改计费口径；采集纪律：定位/影像为可选采集（隐私声明过审后启用）。"""

    def cp_route(grant: str | None):
        def _dep(request: Request) -> dict[str, Any]:
            request.state.erp_grant = grant  # 访问日志：被 GRANT 拒时能指到哪一条
            if grant is None:  # 工作台：契约权限列=页面集授权（无独立 grant）
                if auth.grants_of(PAGESET_CP) is None:
                    raise ApiError("PAGESET_FORBIDDEN",
                                   f"当前租户未授权页面集：{PAGESET_CP}",
                                   detail={"pageset_key": PAGESET_CP})
            else:
                require_grant(auth, PAGESET_CP, grant)
            carrier = request.headers.get(CARRIER_HEADER, "").strip()
            if not carrier:
                raise ApiError("AUTH_REQUIRED",
                               f"缺少承运商身份头 {CARRIER_HEADER}"
                               "（CP 身份=承运商标识；网关 PAT 落地前演示形态）")
            return {"carrier": carrier}
        return _dep

    # ---- 1) 工作台（权限=页面集授权）----

    @app.get("/erp/supply-chain/v1/cp/workspace")
    async def cp_workspace(ctx: dict = Depends(cp_route(None))):
        carrier = ctx["carrier"]
        waybills = _waybill_states(d, carrier)
        receipts: list[dict[str, Any]] = []
        for w in waybills:  # 运单关联回执（月台/提货/POD，同源 read 面）
            receipts.extend(d.fulfillment.carrier_receipts(w["ref"]))
        open_wb = [w for w in waybills if w["status"] == "open"]
        pending_accept = [w for w in open_wb if w["carrier_response"] is None]
        dock_active = sum(1 for r in receipts
                          if r["record"] == "dock_reservation"
                          and r["status"] == "reserved")
        pod_exception = sum(1 for r in receipts
                            if r["record"] == "pod" and r["event"] == "reported"
                            and str((r["payload"] or {}).get("exception_note")
                                    or "").strip())
        return ok_shell({
            "carrier": carrier,
            "waybills_total": len(waybills),
            "waybills_open": len(open_wb),
            "waybills_pending_accept": len(pending_accept),
            "dock_reservations_active": dock_active,
            "pod_exceptions": pod_exception,
            "notes": {
                "dock_slots": "可约时段目录 501 如实（月台资源主数据归仓库作业"
                              "编排，未建模）",
                "freight": "运费单 read 面 501 如实（计费 read 工具面待扩展）",
                "tasks": "今日任务=有效月台预约数（编排层无日期字段，如实不按日"
                         "过滤）",
            },
        })

    # ---- 2) 运单域（确认门）----

    @app.get("/erp/supply-chain/v1/cp/waybills")
    async def cp_waybills(request: Request,
                          ctx: dict = Depends(cp_route("waybill.read"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = []
        for w in _waybill_states(d, ctx["carrier"]):
            if status and w["status"] != status:
                continue
            payload = w["payload"]
            plan = _plan_of(d, str(payload.get("plan_ref") or ""))
            rows.append({
                "waybillId": w["ref"], "waybill_no": payload.get("waybill_no"),
                "carrier": payload.get("carrier"), "status": w["status"],
                "event": w["event"], "plan_ref": payload.get("plan_ref"),
                "order_ref": (plan or {}).get("payload", {}).get("order_ref"),
                "carrier_response": w["carrier_response"],
                "status_note": "open=在途流转（可月台/提货/签收）；"
                               "rejected=已拒单（终态，改派由租户侧重下运单）",
            })
        rows.sort(key=lambda r: r["waybillId"])
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/cp/waybills/{waybill_id}")
    async def cp_waybill_detail(waybill_id: str,
                                ctx: dict = Depends(cp_route("waybill.read"))):
        w = _waybill_by_ref(d, waybill_id, ctx["carrier"])
        payload = w["payload"]
        plan = _plan_of(d, str(payload.get("plan_ref") or ""))
        track = None
        try:
            track = d.fulfillment.track_waybill(
                str(payload.get("waybill_no") or ""))["events"]
        except Exception:
            track = None  # 轨迹不可得如实置空，不伪造
        return ok_shell({
            "waybillId": w["ref"], "waybill_no": payload.get("waybill_no"),
            "carrier": payload.get("carrier"), "status": w["status"],
            "plan_ref": payload.get("plan_ref"),
            "order_ref": (plan or {}).get("payload", {}).get("order_ref"),
            "customer_id": (plan or {}).get("payload", {}).get("customer_id"),
            "lines": (plan or {}).get("payload", {}).get("lines") or [],
            "carrier_response": w["carrier_response"],
            "track_events": track,
            "logistics_adapter": d.fulfillment._logistics.describe(),  # 适配器描述（同源 read 面）
            "note": "运单明细（货/仓/窗期/收发信息）：编排层运单记录不携带窗期"
                    "与收发货人明细（如实缺省，待编排层补字段）",
        })

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/accept")
    async def cp_waybill_accept(waybill_id: str, body: dict[str, Any],
                                ctx: dict = Depends(cp_route("waybill.accept"))):
        """承运回执：确认门语义；拒绝回显原因；回执驱动交付编排改派（改派由
        租户侧重下运单承载，本端拒绝即终态留痕）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        decision = str(body.get("decision") or "").strip()
        if decision not in ("accepted", "rejected"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 accepted / rejected",
                           detail={"decision": decision})
        responded_by = f"cp:{ctx['carrier']}"
        out = d.fulfillment.record_carrier_response(
            waybill_id, decision, note=str(body.get("rejectedNote") or ""),
            responded_by=responded_by)
        return ok_shell(out)

    # ---- 3) 月台预约域 ----

    @app.get("/erp/supply-chain/v1/cp/dock-slots")
    async def cp_dock_slots(ctx: dict = Depends(cp_route("dock.reserve"))):
        raise _not_implemented(
            "可约月台时段后端能力未就绪：月台资源主数据与可约时段计算归仓库"
            "作业编排（编排层未建模月台资源）；预约回执本身已实现"
            "（POST /cp/waybills/{id}/dock-reservation，slotId 为仓库侧不透明"
            "引用）；禁止伪造时段目录",
            "planned_lane: 仓库作业编排月台资源建模（时段目录 + 占用计算）")

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/dock-reservation")
    async def cp_dock_reserve(waybill_id: str, body: dict[str, Any],
                              ctx: dict = Depends(cp_route("dock.reserve"))):
        """预约回执（action=reserve / cancel，改约=再预约新时段）；与仓库作业
        编排联动（slotId 为仓库侧不透明引用）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        action = str(body.get("action") or "reserve").strip()
        responded_by = f"cp:{ctx['carrier']}"
        if action == "cancel":
            reservation_id = str(body.get("reservationId") or "").strip()
            if not reservation_id:
                raise ApiError("INVALID_ARGUMENT", "取消预约须带 reservationId")
            owned = any(r["ref"] == reservation_id
                        for r in d.fulfillment.carrier_receipts(waybill_id)
                        if r["record"] == "dock_reservation")
            if not owned:
                raise ApiError("NOT_FOUND",
                               f"月台预约不存在或不属于该运单：{reservation_id}",
                               detail={"reservationId": reservation_id})
            return ok_shell(d.fulfillment.cancel_dock_reservation(
                reservation_id, responded_by=responded_by))
        slot_id = str(body.get("slotId") or "").strip()
        if not slot_id:
            raise ApiError("INVALID_ARGUMENT", "预约须带 slotId")
        return ok_shell(d.fulfillment.record_dock_reservation(
            waybill_id, slot_id,
            warehouse=str(body.get("warehouse") or ""),
            slot_day=str(body.get("slotDay") or ""),
            responded_by=responded_by))

    # ---- 4) 提货域 ----

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/pickup-checkin")
    async def cp_pickup_checkin(waybill_id: str, body: dict[str, Any],
                                ctx: dict = Depends(cp_route("pickup.checkin"))):
        """打卡回执：回执驱动在途状态。扫码提货复用底座 bind-scan 同一扫码
        实现（解析目标=作业码）。定位/影像为可选采集（隐私声明过审后启用）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        location = body.get("location") if isinstance(body.get("location"), dict) \
            else None
        image_refs = [str(i) for i in (body.get("imageRefs") or [])]
        responded_by = f"cp:{ctx['carrier']}"
        return ok_shell(d.fulfillment.record_pickup_checkin(
            waybill_id, location=location, image_refs=image_refs,
            scan_code=str(body.get("scanCode") or ""),
            responded_by=responded_by))

    # ---- 5) 签收域 ----

    @app.post("/erp/supply-chain/v1/cp/waybills/{waybill_id}/pod")
    async def cp_pod_report(waybill_id: str, body: dict[str, Any],
                            ctx: dict = Depends(cp_route("pod.report"))):
        """POD 回执：驱动运单结单与计费事件（计费规则引擎同源，本端零落账）；
        异常签收进处置车道（ThresholdEvaluator 确认门派单留痕）。"""
        _waybill_by_ref(d, waybill_id, ctx["carrier"])  # 越域 404
        documents = [str(x) for x in (body.get("documents") or [])]
        exception_note = str(body.get("exceptionNote") or "").strip()
        responded_by = f"cp:{ctx['carrier']}"
        out = d.fulfillment.record_carrier_pod(
            waybill_id, documents=documents, exception_note=exception_note,
            responded_by=responded_by)
        if out["exception"]:
            alert = {"triggered": True, "metric": "waybill_pod_exception",
                     "value": out["waybill_no"], "severity": "high",
                     "disposal_task": "异常签收处置：核对异常说明、回执客户并留痕"}
            task = d.disposal.raise_disposal_task(
                alert, title=f"运单异常签收：{out['waybill_no']}"
                             f"（{exception_note[:80]}）",
                requested_by=responded_by)
            out["disposal_request_id"] = task.get("request_id")
            out["disposal_status"] = task.get("status")
        out["settlement_note"] = ("签收触发收入确认经内核 proposal 车道（账务面"
                                  "驱动）；运单结单/计费事件由计费规则引擎"
                                  "事件驱动，本端不落结单态（如实挂账）")
        return ok_shell(out)

    @app.get("/erp/supply-chain/v1/cp/waybills/{waybill_id}/events")
    async def cp_waybill_events(waybill_id: str,
                                ctx: dict = Depends(cp_route("waybill.read"))):
        """运单事件流/轨迹：交付编排事件档 + 只读适配器（与客户侧全单物流
        视图同源，零平行取数）。"""
        w = _waybill_by_ref(d, waybill_id, ctx["carrier"])
        events: list[dict[str, Any]] = []
        for ev in d.fulfillment._events():
            if ev.get("ref") != w["ref"]:
                continue
            events.append({"record": ev.get("record"), "ref": ev.get("ref"),
                           "event": ev.get("event"), "status": ev.get("status"),
                           "payload": dict(ev.get("payload") or {}),
                           "requested_by": ev.get("requested_by"),
                           "note": ev.get("note")})
        events.extend(d.fulfillment.carrier_receipts(w["ref"]))
        track = None
        try:
            track = d.fulfillment.track_waybill(
                str(w["payload"].get("waybill_no") or ""))["events"]
        except Exception:
            track = None  # 轨迹不可得如实置空，不伪造
        return ok_shell({
            "waybillId": w["ref"], "waybill_no": w["payload"].get("waybill_no"),
            "status": w["status"], "events": events,
            "track_events": track,
            "caliber_note": "与客户侧全单物流视图同源（交付编排事件档 + 只读"
                            "适配器回流，零平行取数）",
        })

    # ---- 6) 运费对账域 —— 后端能力未就绪，如实 501 ----

    _CP_FREIGHT_501 = (
        "运费单后端能力未就绪：运费单来自计费规则引擎（事件驱动 + 日终快照，"
        "tasks 7.2），按承运商归集与四层穿透溯源需计费 read 工具面扩展"
        "（KernelClient 现无 fee_details 归集查询）；禁止假实现")

    @app.get("/erp/supply-chain/v1/cp/freight-statements")
    async def cp_freight_statements(request: Request,
                                    ctx: dict = Depends(cp_route(
                                        "freight.statement.read"))):
        raise _not_implemented(
            _CP_FREIGHT_501,
            "planned_lane: 计费 read 工具面（fee_details 按承运商归集 + 规则/"
            "事件/单据/原始作业四层溯源）→ 运费单派生；确认门届时=内核自平核对 "
            "proposal（同 UE 对账契约）")

    @app.get("/erp/supply-chain/v1/cp/freight-statements/{statement_id}")
    async def cp_freight_detail(statement_id: str,
                                ctx: dict = Depends(cp_route(
                                    "freight.statement.read"))):
        raise _not_implemented(_CP_FREIGHT_501,
                               "planned_lane: 同 GET /cp/freight-statements")

    @app.post("/erp/supply-chain/v1/cp/freight-statements/{statement_id}/confirm")
    async def cp_freight_confirm(statement_id: str, body: dict[str, Any],
                                 ctx: dict = Depends(cp_route(
                                     "freight.statement.read"))):
        raise _not_implemented(
            _CP_FREIGHT_501,
            "planned_lane: 运费单 read 面到位后确认门=内核自平核对 proposal"
            "（超时/失败不假报成功）；异议进处置车道")

    # 契约 §7：AI 助手入口无独立端点（复用底座 chat/WS 面）。
