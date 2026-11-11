"""KernelClient 装配：生产走 MCP 工具面，同进程走 InProcess 同形客户端。

- InProcessKernelClient：与 fd-erp-vertical-templates/tests/conftest.py 的同形
  客户端同构 —— 委托 fd_erp_ledger_mcp 内核模块函数（惰性导入，签名同形）。
  服务进程内直连内核模块是模板测试同构形态；生产形态 ERP_KERNEL_TRANSPORT=mcp
  时改用 templates.kernel_client.McpKernelClient（fastmcp 工具面）。
- 两种形态对模板层都只暴露 KernelClient 协议，不给 SQL/DB —— 零旁路纪律不变形。

fail-closed：内核侧信用/计费工具未上线时如实报 NOT_WIRED（不伪造成功）。
对账两个方法（run_reconciliation / get_recon_report）是**长任务代理**：提交立刻拿
task_id，报告靠轮询；内核不可达时抛 UPSTREAM_UNAVAILABLE，由 REST 面按 spec 降级
成 501 + planned_lane（不用 500、不伪造结果）。
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .errors import ERROR_CATALOG, ApiError


class InProcessKernelClient:
    """内核工具面同进程实现（fd_erp_ledger_mcp 模块委托，签名与协议同形）。

    内核惰性导入：模块导入期不触碰任何 DB 依赖；仅调用时刻解析内核包。
    """

    def __init__(self, settings: Settings):
        self._s = settings
        self._tenant = settings.tenant_id

    # ---- 内核模块惰性解析 ----

    def _kernel_mod(self, name: str):
        """惰性解析内核子模块：导入期零 DB 依赖，仅调用时刻触碰内核包。"""
        try:
            import importlib
            return importlib.import_module(f"fd_erp_ledger_mcp.{name}")
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核包 fd_erp_ledger_mcp 不可用（InProcess 形态需可导入内核）",
                           detail={"module": name}) from exc

    def _connection(self):
        db = self._kernel_mod("db")
        return db.connection(self._s)

    # ---- 8 核心工具（同形签名）----

    def submit_proposal(self, stable_id, lines, source_ref=None, kind="journal",
                        reversal_of=None, lane_fleet=None, lane_post=None,
                        lane_title=None, lane_target=None, tenant_id=None,
                        submitted_by="agent"):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.submit_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                stable_id=stable_id, lines=lines, source_ref=source_ref,
                kind=kind, reversal_of=reversal_of, lane_fleet=lane_fleet,
                lane_post=lane_post, lane_title=lane_title,
                lane_target=lane_target, submitted_by=submitted_by)

    def list_proposals(self, status=None, tenant_id=None, offset=0, limit=100):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            rows = proposals.list_proposals(conn, self._tenant, status)
            return {"proposals": rows[offset:offset + limit],
                    "rows_returned": len(rows)}

    def confirm_proposal(self, proposal_id=None, stable_id=None, tenant_id=None,
                         confirmed_by="human"):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.confirm_proposal(
                conn, settings=self._s, tenant_id=self._tenant,
                proposal_id=proposal_id, stable_id=stable_id,
                confirmed_by=confirmed_by)

    def reject_proposal(self, proposal_id=None, stable_id=None, reason=None,
                        tenant_id=None, rejected_by="human"):
        proposals = self._kernel_mod("proposals")
        with self._connection() as conn:
            return proposals.reject_proposal(
                conn, tenant_id=self._tenant, proposal_id=proposal_id,
                stable_id=stable_id, reason=reason, rejected_by=rejected_by)

    def query_balance(self, account_code=None, tenant_id=None):
        ledger = self._kernel_mod("ledger")
        with self._connection() as conn:
            data = ledger.compute_balance(conn, self._tenant, account_code,
                                          settings=self._s)
            return {"data": data, "rows_returned": len(data)}

    def query_entries(self, account_code=None, txn_id=None, tenant_id=None,
                      offset=0, limit=100):
        ledger = self._kernel_mod("ledger")
        with self._connection() as conn:
            rows = ledger.query_entries(conn, self._tenant,
                                        account_code=account_code, txn_id=txn_id)
            return {"entries": rows[offset:offset + limit],
                    "rows_returned": len(rows)}

    # ---- 对账长任务（design D13：提交立即返回 task_id，报告靠轮询）----
    #
    # 通道结论（任务 6.6）：内核对这个工具的设计本来就是「立即返回 task_id +
    # get_recon_report 轮询」（fd_erp_ledger_mcp/recon/engine.py：写 queued 行后启动
    # daemon 线程，dt 内完成）。也就是说通道本身能承载长任务——承载方式是任务化，
    # 不是同步等待。因此这里**原样代理**：REST 面不做「等服务端跑完再回」的同步
    # 包装（那等于把内核已解决的长任务超时问题搬到 REST 面，design D13 已否掉）。
    #
    # 错误归一（404 / 501 降级）不在本类做：MCP 形态返回的是错误信封、InProcess 形态
    # 抛 ErpError，两形态分叉会养出「测试绿、产上 500」的问题（本仓已被同类分叉咬过
    # 两次）。统一收口见模块级 ReconProxy。

    def _recon_tenant(self, tenant_id: str | None) -> str:
        """查询闸同内核工具面：跨租户请求拒绝并留痕（tenancy.resolve_tenant）。"""
        tenancy = self._kernel_mod("tenancy")
        with self._connection() as conn:
            return tenancy.resolve_tenant(conn, self._s, tenant_id or self._tenant)

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        """提交两源对账（只读滩头）：返回 {"task_id", "status": "queued", ...}。

        内核在后台线程跑，落库态可轮询（服务重启后仍可查）；幂等/重试语义由内核
        retry_task_id 承担（仅失败条目重试）。
        """
        engine = self._kernel_mod("recon.engine")
        tenant = self._recon_tenant(tenant_id)
        return engine.enqueue(self._s, tenant, left_source, right_source, retry_task_id)

    def get_recon_report(self, task_id, tenant_id=None):
        """按 task_id 查状态/进度/差异（四类差异 + 双侧引用），任务不存在 404。"""
        engine = self._kernel_mod("recon.engine")
        tenant = self._recon_tenant(tenant_id)
        return engine.get_report(self._s, tenant, task_id)

    # ---- 信用额度扩展（占用/释放/回冲由账本记账）----

    def _credit_mod(self):
        try:
            import fd_erp_ledger_mcp.credit as credit  # 内核侧信用记账模块
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核信用工具未上线（fail-closed，不伪造成功）") from exc
        return credit

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None, expires_at=None):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.reserve_credit(
                conn, settings=self._s, tenant_id=self._tenant,
                customer_id=customer_id, stable_id=stable_id, amount=amount,
                currency=currency, source_ref=source_ref, expires_at=expires_at)

    def release_credit(self, customer_id, stable_id, released_by="system", reason=None):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.release_credit(conn, settings=self._s, tenant_id=self._tenant,
                                      customer_id=customer_id, stable_id=stable_id,
                                      released_by=released_by, reason=reason)

    def query_credit(self, customer_id):
        mod = self._credit_mod()
        with self._connection() as conn:
            # MCP 工具面 query_credit = credit.credit_position（同形语义）
            return mod.credit_position(conn, settings=self._s,
                                       tenant_id=self._tenant,
                                       customer_id=customer_id)

    # ---- 事件层扩展（establish-erp-event-kernel 7.1）----
    #
    # 与模板层的 KernelClient 协议同名同形：到货预告这类外部原始事实经这里进内核，
    # 数量面与分录都由内核派生——REST 面不直连库表、不算金额、不猜科目。

    def _events_mod(self):
        try:
            import fd_erp_ledger_mcp.events as events
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核事件工具未上线（fail-closed，不伪造事实）",
                           detail={"module": "fd_erp_ledger_mcp.events"}) from exc
        return events

    def ingest_event(self, event_type, stable_id, occurred_at, payload,
                     source_ref=None, provenance=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.append_event(
                conn, settings=self._s, tenant_id=self._tenant,
                event_type=event_type, stable_id=stable_id,
                occurred_at=occurred_at, payload=payload, lane="ingest",
                source_ref=source_ref, provenance=provenance)

    def project_event(self, event_id, object_type, object_key, quantity_delta,
                      unit, dimension=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.project_object_event(
                conn, tenant_id=self._tenant, event_id=event_id,
                object_type=object_type, object_key=object_key,
                quantity_delta=quantity_delta, unit=unit, dimension=dimension)

    def query_object_balance(self, object_type, object_key, dimension=None):
        mod = self._events_mod()
        with self._connection() as conn:
            return mod.query_object_balance(
                conn, tenant_id=self._tenant, object_type=object_type,
                object_key=object_key, dimension=dimension)

    def post_event(self, event_id):
        try:
            import fd_erp_ledger_mcp.posting as posting
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核过账工具未上线（fail-closed，不伪造分录）",
                           detail={"module": "fd_erp_ledger_mcp.posting"}) from exc
        with self._connection() as conn:
            return posting.post_event(conn, settings=self._s,
                                      tenant_id=self._tenant, event_id=event_id)


def _is_transport_failure(exc: Exception) -> bool:
    """传输层失败（内核包缺失 / DB 连接不可达 / MCP 连接异常），与业务错误区分。"""
    if isinstance(exc, (ImportError, ConnectionError, TimeoutError)):
        return True
    mod = (type(exc).__module__ or "").split(".")[0]
    if mod.startswith(("psycopg", "psycopg2")):
        return type(exc).__name__ in ("OperationalError", "InterfaceError")
    # MCP 形态：fastmcp/httpx 的连接类异常（内核进程没起、URL 打不通、超时）
    if mod in ("httpx", "httpcore", "anyio", "fastmcp", "mcp", "sse", "h11"):
        return True
    if "connect" in type(exc).__name__.lower() or "timeout" in type(exc).__name__.lower():
        return True
    # fastmcp 的连接失败被包成 builtins.RuntimeError("Client failed to connect: …")，
    # 类型上完全看不出来（实测：内核进程停掉后就是这个形态）；只能按消息判。
    msg = str(exc).lower()
    return any(sig in msg for sig in (
        "failed to connect", "connection refused", "connection attempts failed",
        "connection error", "connection reset", "timed out", "timeout"))


def _raise_recon_error(exc: Exception, action: str):
    """内核异常 → REST 面封闭错误码（本函数必定抛出）。

    - RECON_TASK_NOT_FOUND（内核专属码）→ NOT_FOUND 404，不落成 500；
    - 其余封闭集码原样透出（NOT_WIRED / INVALID_ARGUMENT / TENANT_DENIED …）；
    - 传输层失败 → UPSTREAM_UNAVAILABLE（REST 层按 spec 降级为 501 + planned_lane）；
    - 其它未知异常原样抛（→ INTERNAL 500），绝不当成「内核不可用」掩盖缺陷。
    """
    if isinstance(exc, ApiError):
        raise exc
    code = getattr(exc, "code", None)
    message = str(getattr(exc, "message", None) or exc)
    detail = getattr(exc, "detail", None)
    detail = detail if isinstance(detail, dict) else None
    if code == "RECON_TASK_NOT_FOUND":
        raise ApiError("NOT_FOUND", message, detail=detail) from exc
    if isinstance(code, str) and code in ERROR_CATALOG:
        raise ApiError(code, message, detail=detail) from exc
    if _is_transport_failure(exc):
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       f"内核对账引擎不可达（{type(exc).__name__}）",
                       detail={"kernel": "recon", "action": action}) from exc
    raise exc


def _recon_result(out: Any, action: str) -> Any:
    """MCP 工具面失败是「错误信封」而非异常：统一翻成 ApiError（否则会当数据返回）。"""
    if isinstance(out, dict) and out.get("ok") is False:
        err = out.get("error") or {}
        code = str(err.get("code") or "INTERNAL")
        message = str(err.get("message") or "内核对账工具报错")
        detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
        if code == "RECON_TASK_NOT_FOUND":
            raise ApiError("NOT_FOUND", message, detail=detail)
        if code in ERROR_CATALOG:
            raise ApiError(code, message, detail=detail)
        raise ApiError("UPSTREAM_UNAVAILABLE",
                       f"内核对账工具返回未登记错误码：{code}",
                       detail={**detail, "kernel_code": code, "action": action})
    return out


class ReconProxy:
    """对账两方法在**两种 transport** 上的统一收口（一层，不各写一遍）。

    为什么必须有：InProcess 形态委托内核模块 → 失败**抛** ErpError；MCP 形态经
    fastmcp 调工具面 → 内核 `@safe_tool` 把失败包成 `{"ok": false, "error": …}`
    **返回**。只在一侧做归一，另一侧就会把错误信封当成功数据吐给客户，或把
    「任务不存在」变成 200 / 把「内核不可达」变成 500 —— 产上实测踩到过（见
    docs/optimize-erp-solution 6.6 记录）。故两种形态都过这层。

    未覆盖的方法原样透传（信用/计费/账本工具面行为不变）。
    """

    def __init__(self, inner):
        self._inner = inner

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        try:
            out = self._inner.run_reconciliation(
                left_source, right_source, retry_task_id=retry_task_id,
                tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 见 _raise_recon_error
            _raise_recon_error(exc, "run_reconciliation")
        return _recon_result(out, "run_reconciliation")

    def get_recon_report(self, task_id, tenant_id=None):
        try:
            out = self._inner.get_recon_report(task_id, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 见 _raise_recon_error
            _raise_recon_error(exc, "get_recon_report")
        return _recon_result(out, "get_recon_report")

    def __getattr__(self, name):  # 其余工具面方法透传（不改变既有行为）
        return getattr(self._inner, name)


def ensure_recon_proxy(client):
    """幂等包装：已经是 ReconProxy 就原样返回。"""
    return client if isinstance(client, ReconProxy) else ReconProxy(client)


def build_kernel_client(settings: Settings):
    """按 Settings.kernel_transport 装配 KernelClient（生产 MCP / 同进程 InProcess）。"""
    if settings.kernel_transport == "mcp":
        if not settings.kernel_url:
            raise ApiError("INVALID_ARGUMENT",
                           "ERP_KERNEL_TRANSPORT=mcp 需设置 ERP_KERNEL_URL")
        from fd_erp_vertical_templates.kernel_client import McpKernelClient
        return ensure_recon_proxy(
            McpKernelClient(settings.kernel_url, settings.tenant_id))
    return ensure_recon_proxy(InProcessKernelClient(settings))
