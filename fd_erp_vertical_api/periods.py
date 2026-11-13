"""期间关账的 API 驱动（change: add-period-close 任务 4.1）。

关账不是一个「竖井」能力，而是账本的时间轴：期间台账、落账闸、关账快照都在内核里
（`fd_erp_ledger_mcp.periods`）。这里只做一件事——把内核的五个动作包成驱动对象，
让 REST 面/编排层有一个稳定的绑定位置，**不重复任何判断**（顺序关账、只重开最近一期、
快照复算全在内核）。

为什么不做模板层封装：模板层（行业垂直）面向的是主子档/订单/库存这类业务对象；
「哪一期关了」是账本自身的属性，与行业无关。再包一层只会是纯转发，还会让两个地方
都能改关账语义。
"""
from __future__ import annotations

from typing import Any, Protocol


class _PeriodKernel(Protocol):
    def close_period(self, period: str, actor: str, note: str | None = None) -> Any: ...
    def reopen_period(self, period: str, actor: str, reason: str) -> Any: ...
    def period_status(self, period: str) -> Any: ...
    def period_report(self, period: str) -> Any: ...
    def list_periods(self) -> Any: ...


class PeriodBook:
    """关账驱动：关账 / 重开 / 查状态 / 复算核对 / 期间清单。"""

    def __init__(self, kernel: _PeriodKernel):
        self._kernel = kernel

    def close(self, period: str, *, actor: str, note: str | None = None) -> Any:
        return self._kernel.close_period(period, actor, note=note)

    def reopen(self, period: str, *, actor: str, reason: str) -> Any:
        return self._kernel.reopen_period(period, actor, reason)

    def status(self, period: str) -> Any:
        return self._kernel.period_status(period)

    def report(self, period: str) -> Any:
        return self._kernel.period_report(period)

    def list(self) -> Any:
        return self._kernel.list_periods()


__all__ = ["PeriodBook"]
