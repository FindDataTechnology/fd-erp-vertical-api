"""KernelClient 装配：生产走 MCP 工具面，同进程走 InProcess 同形客户端。

- InProcessKernelClient：与 fd-erp-vertical-templates/tests/conftest.py 的同形
  客户端同构 —— 委托 fd_erp_ledger_mcp 内核模块函数（惰性导入，签名同形）。
  服务进程内直连内核模块是模板测试同构形态；生产形态 ERP_KERNEL_TRANSPORT=mcp
  时改用 templates.kernel_client.McpKernelClient（fastmcp 工具面）。
- 两种形态对模板层都只暴露 KernelClient 协议，不给 SQL/DB —— 零旁路纪律不变形。

fail-closed：内核侧信用/计费工具未上线时如实报 NOT_WIRED（不伪造成功）。
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .errors import ApiError


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

    def run_reconciliation(self, left_source, right_source, retry_task_id=None,
                           tenant_id=None):
        raise ApiError("NOT_WIRED",
                       "内核对账长任务由内核自身工具面承担（本服务不代理）")

    def get_recon_report(self, task_id, tenant_id=None):
        raise ApiError("NOT_WIRED", "同 run_reconciliation")

    # ---- 信用额度扩展（占用/释放/回冲由账本记账）----

    def _credit_mod(self):
        try:
            import fd_erp_ledger_mcp.credit as credit  # 内核侧信用记账模块
        except ImportError as exc:
            raise ApiError("NOT_WIRED",
                           "内核信用工具未上线（fail-closed，不伪造成功）") from exc
        return credit

    def reserve_credit(self, customer_id, stable_id, amount, currency,
                       source_ref=None):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.reserve_credit(
                conn, settings=self._s, tenant_id=self._tenant,
                customer_id=customer_id, stable_id=stable_id, amount=amount,
                currency=currency, source_ref=source_ref)

    def release_credit(self, customer_id, stable_id):
        mod = self._credit_mod()
        with self._connection() as conn:
            return mod.release_credit(conn, settings=self._s, tenant_id=self._tenant,
                                      customer_id=customer_id, stable_id=stable_id)

    def query_credit(self, customer_id):
        mod = self._credit_mod()
        with self._connection() as conn:
            # MCP 工具面 query_credit = credit.credit_position（同形语义）
            return mod.credit_position(conn, settings=self._s,
                                       tenant_id=self._tenant,
                                       customer_id=customer_id)


def build_kernel_client(settings: Settings):
    """按 Settings.kernel_transport 装配 KernelClient（生产 MCP / 同进程 InProcess）。"""
    if settings.kernel_transport == "mcp":
        if not settings.kernel_url:
            raise ApiError("INVALID_ARGUMENT",
                           "ERP_KERNEL_TRANSPORT=mcp 需设置 ERP_KERNEL_URL")
        from fd_erp_vertical_templates.kernel_client import McpKernelClient
        return McpKernelClient(settings.kernel_url, settings.tenant_id)
    return InProcessKernelClient(settings)
