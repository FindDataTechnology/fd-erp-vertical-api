"""FastAPI 应用工厂：UE 订货台 19 + SE 业务员台 15 + SP 供应商 13 + CP 承运商
12 契约端点 + pageset 授权端点（共 60 路由）。

请求闸序（每请求）：
1. 租户闸：X-ERP-Tenant 必带且须等于本进程绑定租户（跨租户 TENANT_DENIED 403）；
2. 页面集闸：页面集 key（supply-chain.ue/se/sp/cp）须在租户授权配置内
   （fail-closed 403）；
3. grant 闸：端点所需权限码（契约权限列）须已授权（403）；
4. 身份/数据域闸：UE=X-ERP-Customer（本租户已建档客户）；SE=X-ERP-Staff
   （业务员工号，归属=客户主数据 sales_rep）；SP=X-ERP-Supplier（供应商档案）；
   CP=X-ERP-Carrier（运单 carrier 域）。越域/未知一律 404，不泄露存在性
   （例外：SE 代客下单归属越权 403 DATA_SCOPE_DENIED，契约明示）。

统一响应壳 {ok, data, error:{code,message,hint,detail}, rows_returned}；
错误码封闭集（errors.ERROR_CATALOG）；分页/体积上限沿内核 result.py 语义。
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fd_erp_vertical_templates.errors import TemplateError

from .cart import CartStore
from .config import Settings
from .drivers import build_drivers, stock_visibility
from .envelope import error_shell, list_shell, ok_shell, parse_page_params
from .errors import ApiError, adapt_exception, http_status_for
from .faces import register_cp_face, register_se_face, register_sp_face
from .kernel_client import build_kernel_client
from .pagesets import PagesetAuth, load_pageset_auth, require_grant
from .statements import StatementService

PAGESET_UE = "supply-chain.ue"
TENANT_HEADER = "X-ERP-Tenant"
CUSTOMER_HEADER = "X-ERP-Customer"
# 探针路径：不走租户闸（见 tenant_gate 注释）
HEALTH_PATHS = ("/health", "/healthz")


def _err_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=error_shell(exc))


def _resolve_product_ref(drivers, product_ref: str) -> dict[str, Any] | None:
    """productId 兼容实体 id 与商品编码（先实体 id 后 code，越域 404）。"""
    snap = drivers.master.get_entity(product_ref)
    if snap is not None and snap["kind"] == "product":
        return snap
    return drivers.master.find_by_code("product", product_ref)


def _visible_product(drivers, product_ref: str, customer_id: str,
                     channel_id: str | None, org_node: str | None):
    """商品可见性：商品池授权过滤（可见可售集外的商品一律 404 不泄露存在性）。"""
    snap = _resolve_product_ref(drivers, product_ref)
    if snap is None:
        return None
    visible = drivers.master.visible_product_codes(
        customer_id=customer_id, channel_id=channel_id, org_node=org_node)
    code = str(snap["payload"].get("code") or "")
    if code not in visible or snap.get("state") != "active":
        return None
    return snap


def _request_price(drivers, customer_id: str, product_code: str, qty: Any,
                   unit: str | None) -> dict[str, Any]:
    """当次取价（pricing.resolve_price，规则链 provenance）。"""
    if unit:
        drivers.master.convert_qty(product_code, qty, unit, unit)
    from datetime import date
    return drivers.pricing.resolve_price(
        drivers.tenant_id, customer_id, product_code,
        date.today().isoformat(), qty)


def _line_request(body: dict[str, Any]) -> tuple[str, Any, str | None, str | None]:
    """cart/price-preview 行请求解析：productId+qty 必带。"""
    code = str(body.get("productId") or body.get("product_code") or "").strip()
    if not code:
        raise ApiError("INVALID_ARGUMENT", "行请求缺 productId")
    qty = body.get("qty")
    try:
        if qty is None or float(qty) <= 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiError("INVALID_ARGUMENT", "数量 qty 须为正数",
                       detail={"qty": qty}) from exc
    return code, qty, body.get("unit") or None, body.get("skuId") or None


def create_app(settings: Settings, kernel=None, *,
               pageset_auth: PagesetAuth | None = None,
               cart_store: CartStore | None = None) -> FastAPI:
    """应用工厂（测试可注入 kernel/pageset_auth；serve 形态全部按 Settings 装配）。"""
    app = FastAPI(title="fd-erp-vertical-api", version="0.1.0",
                  description="ERP 行业垂直 REST 服务面（UE 订货台）")
    app.state.settings = settings
    app.state.kernel = kernel or build_kernel_client(settings)
    app.state.pageset_auth = pageset_auth or load_pageset_auth(
        inline_json=settings.pageset_auth_json,
        file_path=settings.pageset_auth_file)
    app.state.drivers = build_drivers(settings, app.state.kernel)
    app.state.cart = cart_store or CartStore()
    app.state.statements = StatementService(app.state.drivers, app.state.kernel)

    # ---- 请求闸（中间件：租户闸全域生效；其余在依赖里按端点粒度生效）----

    @app.middleware("http")
    async def tenant_gate(request: Request, call_next):
        # 探针豁免：/health 是给 k8s readiness/liveness 用的，探针不带任何业务
        # 身份（没有 X-ERP-Tenant），若一并过租户闸，Pod 永远不会 Ready。
        # 该端点只回服务名与承载形态，不碰任何业务数据、不泄露租户标识。
        if request.url.path in HEALTH_PATHS:
            return await call_next(request)
        try:
            requested = request.headers.get(TENANT_HEADER)
            if not requested:
                raise ApiError("AUTH_REQUIRED", f"缺少租户上下文头 {TENANT_HEADER}")
            if requested != settings.tenant_id:
                raise ApiError("TENANT_DENIED",
                               f"跨租户访问被拒绝：本进程仅服务租户 {settings.tenant_id}",
                               detail={"requested_tenant": requested,
                                       "cell_tenant": settings.tenant_id})
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 统一归一化为封闭错误码
            return _err_response(adapt_exception(exc))

    # 请求体/参数校验失败（FastAPI 默认 422）→ 统一壳 400 封闭码
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return _err_response(ApiError("INVALID_ARGUMENT",
                                      "请求体/参数形态非法（须为 JSON 对象）",
                                      detail={"errors": exc.errors()[:5]}))

    def ue_context(request: Request) -> dict[str, Any]:
        """UE 端点统一闸：页面集 → grant → 客户身份（数据域）。"""
        grant = request.scope.get("ue_grant")
        if grant is None:  # pragma: no cover - 路由声明保证
            raise ApiError("INTERNAL", "路由未声明所需 grant")
        require_grant(app.state.pageset_auth, PAGESET_UE, grant)
        customer_id = request.headers.get(CUSTOMER_HEADER, "").strip()
        if not customer_id:
            raise ApiError("AUTH_REQUIRED",
                           f"缺少客户身份头 {CUSTOMER_HEADER}（UE 身份=客户联系人所属客户档案）")
        snap = app.state.drivers.master.get_entity(customer_id)
        if snap is None or snap["kind"] != "customer":
            # 越域/未知身份一律 404，不泄露存在性（契约 0. 通用约定）
            raise ApiError("NOT_FOUND", "客户档案不存在",
                           detail={"customer_id": customer_id})
        return {"customer_id": customer_id, "customer": snap}

    def route(grant: str):
        """路由工厂：把该端点所需 grant 注入 scope，供 ue_context 消费。"""
        def _dep(request: Request) -> dict[str, Any]:
            request.scope["ue_grant"] = grant
            return ue_context(request)
        return _dep

    # ---- 0) 页面集授权端点（形状已被 paas P0 客户端固定：store.ts 以
    #      res.json() 原始体过 parsePagesetAuth，要求顶层 payload.pagesets；
    #      故在统一壳外附顶层 pagesets 镜像字段，解析两侧都成立）----

    @app.get("/health")
    @app.get("/healthz")
    async def health():
        """承载探针：只报服务名与内核承载形态，不碰业务数据、不回显租户与 DSN。

        单副本 Deployment 的 readiness/liveness 都打这里；探针不能携带业务身份，
        故本路径在租户闸里被显式豁免（HEALTH_PATHS）。
        """
        return {"ok": True, "service": "fd-erp-vertical-api",
                "kernel_transport": settings.kernel_transport}

    @app.get("/erp/supply-chain/v1/pagesets")
    async def list_pagesets():
        pagesets = app.state.pageset_auth.wire_payload()["pagesets"]
        return {"ok": True, "data": {"pagesets": pagesets},
                "pagesets": pagesets, "rows_returned": len(pagesets)}

    # ---- 1) 商品域（grant: catalog.read）----

    @app.get("/erp/supply-chain/v1/ue/products")
    async def list_products(request: Request, ctx: dict = Depends(route("catalog.read"))):
        q = request.query_params
        keyword = (q.get("keyword") or "").strip().lower()
        category = (q.get("categoryId") or "").strip()
        sort = (q.get("sort") or "code").strip()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        page = int(q["page"]) if q.get("page") else None  # 1-based 便捷参数
        if page is not None and page > 0:
            offset, limit = (page - 1) * limit, limit
        channel_id = (q.get("channelId") or "").strip() or None
        org_node = (q.get("orgNode") or "").strip() or None
        d = app.state.drivers
        visible = d.master.visible_product_codes(
            customer_id=ctx["customer_id"], channel_id=channel_id, org_node=org_node)
        cards: list[dict[str, Any]] = []
        for code in visible:
            snap = d.master.find_by_code("product", code)
            if snap is None:
                continue
            payload = snap["payload"]
            if keyword and keyword not in str(payload.get("name") or "").lower() \
                    and keyword not in code.lower():
                continue
            if category and str(payload.get("category") or "") != category:
                continue
            cards.append({
                "productId": code,
                "entity_id": snap["entity_id"],
                "title": payload.get("name"),
                "category": payload.get("category"),
                "units": payload.get("units") or [],
                "images": payload.get("images") or [],
                "list_price": payload.get("base_price"),
                "visibility_note": "商品池授权过滤后可见（masterdata.visible_product_codes）",
            })
        reverse = sort.startswith("-")
        cards.sort(key=lambda c: _sort_key(c, sort), reverse=reverse)
        return list_shell(cards, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/product-categories")
    async def list_categories(ctx: dict = Depends(route("catalog.read"))):
        d = app.state.drivers
        visible = d.master.visible_product_codes(customer_id=ctx["customer_id"])
        tree: dict[str, dict[str, Any]] = {}
        for code in visible:
            snap = d.master.find_by_code("product", code)
            payload = snap["payload"] if snap else {}
            cat = str(payload.get("category") or "未分类")
            node = tree.setdefault(cat, {"categoryId": cat, "product_count": 0,
                                         "stop_selling_count": 0})
            node["product_count"] += 1
        # 禁售/限售标注：租户全量商品中 stop_selling 者按类计数（如实标注）
        for snap in d.master.list_entities(kind="product"):
            if snap.get("state") == "stop_selling":
                cat = str(snap["payload"].get("category") or "未分类")
                node = tree.setdefault(cat, {"categoryId": cat, "product_count": 0,
                                             "stop_selling_count": 0})
                node["stop_selling_count"] += 1
        out = [tree[k] for k in sorted(tree)]
        return list_shell(out, offset=0, limit=len(out) or 1)

    @app.get("/erp/supply-chain/v1/ue/products/{product_id}")
    async def product_detail(product_id: str,
                             ctx: dict = Depends(route("catalog.read"))):
        d = app.state.drivers
        snap = _visible_product(d, product_id, ctx["customer_id"], None, None)
        if snap is None:
            raise ApiError("NOT_FOUND", f"商品不存在或不可见：{product_id}",
                           detail={"productId": product_id})
        payload = snap["payload"]
        quote = _request_price(d, ctx["customer_id"], str(payload.get("code")),
                               1, None)
        return ok_shell({
            "productId": payload.get("code"),
            "entity_id": snap["entity_id"],
            "title": payload.get("name"),
            "category": payload.get("category"),
            "state": snap.get("state"),
            "units": payload.get("units") or [],
            "images": payload.get("images") or [],
            "supplier_id": payload.get("supplier_id"),
            "price": quote,  # 取价引擎结果：客户价/活动价/券 + 规则链出处与生效期
            "stock": stock_visibility(d, str(payload.get("code"))),
        })

    # ---- 2) 下单域（grant: catalog.read / order.write）----

    @app.get("/erp/supply-chain/v1/ue/cart")
    async def get_cart(ctx: dict = Depends(route("catalog.read"))):
        lines = app.state.cart.lines(settings.tenant_id, ctx["customer_id"])
        return ok_shell({"lines": lines, "line_count": len(lines)})

    @app.post("/erp/supply-chain/v1/ue/cart/lines")
    async def add_cart_line(body: dict[str, Any],
                            ctx: dict = Depends(route("catalog.read"))):
        code, qty, unit, sku_id = _line_request(body)
        d = app.state.drivers
        snap = _visible_product(d, code, ctx["customer_id"], None, None)
        if snap is None:
            raise ApiError("NOT_FOUND", f"商品不存在或不可见：{code}",
                           detail={"productId": code})
        quote = _request_price(d, ctx["customer_id"], str(snap["payload"].get("code")),
                               qty, unit)
        line = app.state.cart.add_line(settings.tenant_id, ctx["customer_id"], {
            "product": str(snap["payload"].get("code")),
            "qty": qty,
            "unit": unit or "",
            "skuId": sku_id,
            "title": snap["payload"].get("name"),
            "quote": quote,
        })
        return ok_shell(line)

    @app.delete("/erp/supply-chain/v1/ue/cart/lines/{line_id}")
    async def remove_cart_line(line_id: str,
                               ctx: dict = Depends(route("catalog.read"))):
        lines = app.state.cart.remove_line(settings.tenant_id,
                                           ctx["customer_id"], line_id)
        return ok_shell({"lines": lines, "line_count": len(lines)})

    @app.post("/erp/supply-chain/v1/ue/cart/price-preview")
    async def price_preview(body: dict[str, Any],
                            ctx: dict = Depends(route("catalog.read"))):
        """试算：行价/优惠/合计 + 信用额度预估占用；纯读，不占用额度。"""
        rows = body.get("lines")
        if not isinstance(rows, list) or not rows:
            raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
        d = app.state.drivers
        priced: list[dict[str, Any]] = []
        total = 0.0
        for row in rows:
            code, qty, unit, _sku = _line_request(row if isinstance(row, dict) else {})
            snap = _visible_product(d, code, ctx["customer_id"], None, None)
            if snap is None:
                raise ApiError("NOT_FOUND", f"商品不存在或不可见：{code}",
                               detail={"productId": code})
            quote = _request_price(d, ctx["customer_id"],
                                   str(snap["payload"].get("code")), qty, unit)
            total += float(quote["line_total"])
            priced.append(quote)
        credit = app.state.kernel.query_credit(ctx["customer_id"]) or {}
        try:
            available = float(credit.get("available") or 0)
        except (TypeError, ValueError):
            available = 0.0
        return ok_shell({
            "lines": priced,
            "amount_total": f"{total:.6f}".rstrip("0").rstrip("."),
            "currency": settings.base_currency,
            "credit_preview": {
                "available": credit.get("available"),
                "requested": f"{total:.6f}".rstrip("0").rstrip("."),
                "sufficient": total <= available,
                "note": "纯读试算：不占用额度；实际占用在下单经内核 reserve_credit 记账",
            },
        })

    @app.post("/erp/supply-chain/v1/ue/orders")
    async def create_order(body: dict[str, Any],
                           ctx: dict = Depends(route("order.write"))):
        """下单：编排层 accept_order（信用校验→取价→reserve_credit→订单提案）。

        额度不足 fail-closed 拒单：CREDIT_EXCEEDED + 占用明细（可解释拒绝）。
        落账仍走内核确认门 —— REST 不直接落账。
        """
        rows = body.get("lines")
        if not isinstance(rows, list) or not rows:
            raise ApiError("INVALID_ARGUMENT", "lines 须为非空数组")
        from datetime import date
        lines = []
        for row in rows:
            code, qty, unit, _sku = _line_request(row if isinstance(row, dict) else {})
            entry: dict[str, Any] = {"product_code": code, "qty": qty}
            if unit:
                entry["unit"] = unit
            if isinstance(row, dict) and row.get("note"):
                entry["note"] = str(row["note"])
            lines.append(entry)
        result = app.state.drivers.orders.accept_order(
            ctx["customer_id"], lines, date.today().isoformat())
        if not result.get("accepted"):
            raise ApiError("CREDIT_EXCEEDED",
                           "订单金额超出可用信用额度（fail-closed 拒单）",
                           detail=result.get("explanation") or {})
        # 收货信息/备注：受理回执回显（编排层 accept_order 暂不承载收货字段 ——
        # 持久化挂账见 README；不伪造已保存）
        result["shipping_receipt"] = body.get("shipping") or {}
        result["note_receipt"] = body.get("note") or ""
        return ok_shell(result)

    # ---- 3) 查单域（grant: order.write）----

    def _owned_order(order_ref: str, customer_id: str) -> dict[str, Any]:
        """自有订单当前态（越域一律 404，不泄露存在性）。"""
        d = app.state.drivers
        hits = [e for e in d.orders._events()
                if e.get("record") == "order" and e.get("ref") == order_ref]
        if not hits:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_ref}",
                           detail={"orderId": order_ref})
        first = hits[0]
        if (first.get("payload") or {}).get("customer_id") != customer_id:
            raise ApiError("NOT_FOUND", f"订单不存在：{order_ref}",
                           detail={"orderId": order_ref})
        latest = hits[-1]
        payload = dict(first.get("payload") or {})
        payload.update(latest.get("payload") or {})
        return {"order_ref": order_ref, "phase": latest.get("event"),
                "status": latest.get("status"), "payload": payload}

    @app.get("/erp/supply-chain/v1/ue/orders")
    async def list_orders(request: Request,
                          ctx: dict = Depends(route("order.write"))):
        q = request.query_params
        status = (q.get("status") or "").strip() or None
        date_from = (q.get("from") or "").strip() or None
        date_to = (q.get("to") or "").strip() or None
        keyword = (q.get("keyword") or "").strip().lower()
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        d = app.state.drivers
        seen: dict[str, dict[str, Any]] = {}
        for ev in d.orders._events():
            if ev.get("record") != "order":
                continue
            ref = ev.get("ref")
            payload = ev.get("payload") or {}
            if ev["event"] == "proposed":
                if payload.get("customer_id") == ctx["customer_id"]:
                    seen[ref] = {"ref": ref, "event": "proposed",
                                 "status": ev.get("status"), "payload": dict(payload)}
            elif ref in seen and ev["event"] in ("confirmed", "cancelled"):
                merged = dict(seen[ref]["payload"])
                merged.update(payload)
                seen[ref] = {"ref": ref, "event": ev["event"],
                             "status": ev.get("status"), "payload": merged}
        rows: list[dict[str, Any]] = []
        for item in seen.values():
            payload = item["payload"]
            day = str(payload.get("day") or "")
            if date_from and day < date_from:
                continue
            if date_to and day > date_to:
                continue
            if status and item["status"] != status:
                continue
            if keyword and keyword not in item["ref"].lower() and not any(
                    keyword in str(ln.get("product") or "").lower()
                    for ln in payload.get("lines") or []):
                continue
            rows.append({
                "orderId": item["ref"],
                "status": item["status"],
                "phase": item["event"],
                "status_note": "pending=待确认（确认车道）；confirmed=已确认移交履约；"
                               "cancelled=已取消（信用占用经内核回冲）",
                "day": payload.get("day"),
                "amount_total": payload.get("amount_total"),
                "currency": payload.get("currency"),
                "customer_id": payload.get("customer_id"),
            })
        rows.sort(key=lambda r: (str(r.get("day") or ""), r["orderId"]), reverse=True)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/orders/{order_id}")
    async def order_detail(order_id: str,
                           ctx: dict = Depends(route("order.write"))):
        item = _owned_order(order_id, ctx["customer_id"])
        payload = item["payload"]
        return ok_shell({
            "orderId": item["order_ref"],
            "status": item["status"],
            "phase": item["phase"],
            "status_note": "pending=待确认（确认车道）；confirmed=已确认移交履约；"
                           "cancelled=已取消",
            "day": payload.get("day"),
            "amount_total": payload.get("amount_total"),
            "currency": payload.get("currency"),
            "lines": payload.get("lines") or [],
            "credit_reservation": payload.get("credit_reservation"),
            "request_id": payload.get("request_id"),
        })

    @app.get("/erp/supply-chain/v1/ue/orders/{order_id}/fulfillment")
    async def order_fulfillment(order_id: str,
                                ctx: dict = Depends(route("order.write"))):
        _owned_order(order_id, ctx["customer_id"])  # 越域闸
        d = app.state.drivers
        try:
            view = d.fulfillment.fulfillment_view(order_id)
        except TemplateError as exc:
            if exc.code != "NOT_FOUND":
                raise
            # 无履约计划：区分「未确认（无移交记录）→ 404」与「已确认待编排
            # （有移交记录）→ 如实报 handoff_pending 中间态，不伪造计划」
            handoff = next((e for e in d.orders._events()
                            if e.get("record") == "fulfillment_handoff"
                            and (e.get("payload") or {}).get("order_ref") == order_id),
                           None)
            if handoff is None:
                raise ApiError("NOT_FOUND",
                               f"订单无履约编排（未确认订单不产生履约移交）：{order_id}",
                               detail={"orderId": order_id,
                                       "hint": "订单经确认车道确认后移交交付编排"}) from exc
            return ok_shell({
                "order_ref": order_id, "plans": [],
                "handoff_ref": handoff.get("ref"),
                "handoff_pending": True,
                "note": "订单已确认并移交履约（handoff 记录在编排档）；"
                        "交付编排计划生成后此处返回 plans 明细",
            })
        return ok_shell(view)

    # ---- 4) 对账域（grant: statement.confirm）----

    @app.get("/erp/supply-chain/v1/ue/statements")
    async def list_statements(request: Request,
                              ctx: dict = Depends(route("statement.confirm"))):
        q = request.query_params
        period = (q.get("period") or "").strip() or None
        status = (q.get("status") or "").strip() or None
        offset, limit = parse_page_params(q.get("offset"), q.get("limit"))
        rows = app.state.statements.list_statements(
            ctx["customer_id"], status=status, period=period)
        return list_shell(rows, offset=offset, limit=limit)

    @app.get("/erp/supply-chain/v1/ue/statements/{statement_id}")
    async def statement_detail(statement_id: str,
                               ctx: dict = Depends(route("statement.confirm"))):
        return ok_shell(app.state.statements.get_statement(
            ctx["customer_id"], statement_id))

    @app.post("/erp/supply-chain/v1/ue/statements/{statement_id}/confirm")
    async def confirm_statement(statement_id: str, body: dict[str, Any],
                                ctx: dict = Depends(route("statement.confirm"))):
        """确认门：确认→内核自平核对 proposal（pending）；异议→内部处置车道。

        超时/失败不假报成功：响应如实携带 proposal_status=pending。
        """
        decision = str(body.get("decision") or "").strip()
        if decision not in ("confirmed", "disputed"):
            raise ApiError("INVALID_ARGUMENT",
                           "decision 须为 confirmed / disputed",
                           detail={"decision": decision})
        if decision == "confirmed":
            return ok_shell(app.state.statements.confirm(
                ctx["customer_id"], statement_id))
        return ok_shell(app.state.statements.dispute(
            ctx["customer_id"], statement_id, str(body.get("disputeNote") or "")))

    # ---- 5) 开票域（grant: invoice.write）—— 后端能力未就绪，如实 501 ----

    @app.get("/erp/supply-chain/v1/ue/invoice-requests")
    async def list_invoice_requests(ctx: dict = Depends(route("invoice.write"))):
        raise ApiError("NOT_IMPLEMENTED",
                       "开票申请后端能力未就绪：模板层无开票车道，金蝶 provider 无开票"
                       "直连 action（docs/connector-onboarding-supply-chain.md §1.2）；"
                       "禁止假实现", detail={"planned_lane": "后台审核车道 + 金蝶/线下开票通道"})

    @app.post("/erp/supply-chain/v1/ue/invoice-requests")
    async def create_invoice_request(body: dict[str, Any],
                                     ctx: dict = Depends(route("invoice.write"))):
        raise ApiError("NOT_IMPLEMENTED",
                       "开票申请后端能力未就绪：模板层无开票车道，金蝶 provider 无开票"
                       "直连 action；禁止假实现",
                       detail={"planned_lane": "后台审核车道 + 金蝶/线下开票通道"})

    # ---- 6) 售后域（grant: aftersale.write）—— 后端能力未就绪，如实 501 ----

    @app.post("/erp/supply-chain/v1/ue/aftersale-requests")
    async def create_aftersale(body: dict[str, Any],
                               ctx: dict = Depends(route("aftersale.write"))):
        raise ApiError("NOT_IMPLEMENTED",
                       "售后流后端能力未就绪：编排层无售后处置车道（tasks 6.2 契约面"
                       "已交付、后端未实施）；禁止假实现",
                       detail={"planned_lane": "售后处置车道（待编排层扩展）"})

    @app.get("/erp/supply-chain/v1/ue/aftersale-requests")
    async def list_aftersales(request: Request,
                              ctx: dict = Depends(route("aftersale.write"))):
        raise ApiError("NOT_IMPLEMENTED", "售后流后端能力未就绪（同 POST）",
                       detail={"planned_lane": "售后处置车道（待编排层扩展）"})

    @app.get("/erp/supply-chain/v1/ue/aftersale-requests/{request_id}")
    async def aftersale_detail(request_id: str,
                               ctx: dict = Depends(route("aftersale.write"))):
        raise ApiError("NOT_IMPLEMENTED", "售后流后端能力未就绪（同 POST）",
                       detail={"planned_lane": "售后处置车道（待编排层扩展）"})

    # ---- AI 助手入口：契约 §7 明确无独立 AI 端点（复用底座 chat/WS 面）----

    # ---- SE 业务员台 / SP 供应商 / CP 承运商三页面集（faces.py，同闸序同壳）----
    register_se_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.statements, app.state.pageset_auth)
    register_sp_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.pageset_auth)
    register_cp_face(app, settings, app.state.drivers, app.state.kernel,
                     app.state.pageset_auth)

    return app


def _sort_key(card: dict[str, Any], sort: str) -> tuple:
    """商品卡排序键：code/name/price（- 前缀表示倒序，由 reverse 承载）；
    非法 sort 如实 INVALID_ARGUMENT。"""
    field = sort.lstrip("-")
    if field == "name":
        return (str(card.get("title") or ""),)
    if field == "price":
        try:
            return (float(card.get("list_price") or 0),)
        except (TypeError, ValueError):
            return (0.0,)
    if field != "code":
        raise ApiError("INVALID_ARGUMENT", "sort 须为 code/name/-name/price/-price",
                       detail={"sort": sort})
    return (str(card.get("productId") or ""),)


