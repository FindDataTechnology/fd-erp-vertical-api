"""三表装配（change: add-financial-statements 任务 2.1）。

判据：用月度闭环那批真分录查三表——资产负债表 `balanced=true`，且三表数字与账本余额咬合。
这是「报表能不能当账用」的判据：报表若自成一派（自己算一遍余额），这一条就会红。
"""
from __future__ import annotations

from decimal import Decimal

from .conftest import TENANT


def _approve_arrival_rule(settings) -> None:
    from fd_erp_ledger_mcp import posting
    from fd_erp_ledger_mcp.db import connection
    with connection(settings) as conn:
        drafted = posting.submit_posting_rule(
            conn, tenant_id=TENANT, event_type="goods_received",
            rule={"lines": [
                {"account_code": "1200", "direction": "debit",
                 "amount_from": "payload.total_cost"},
                {"account_code": "2000", "direction": "credit",
                 "amount_from": "payload.total_cost"}]},
            actor="cfg-eng")
        posting.approve_posting_rule(conn, tenant_id=TENANT,
                                     event_type="goods_received",
                                     version=drafted["version"], approved_by="cfo")


def _seed_one_month(d) -> None:
    """到货 1000 → 出库 400 结转 → 费用 1000（带维度）→ 折旧 1000。"""
    settings = d.kernel._s if hasattr(d.kernel, "_s") else None
    rc = d.inventory.receive(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "10", "unit": "个", "cost": "100"}])
    for line in d.inventory.document(rc["ref"])["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="ok")
    d.inventory.project(rc["ref"])

    io = d.inventory.issue(warehouse="WH-A", lines=[
        {"sku": "SKU-001", "qty": "4", "unit": "个"}])
    for line in d.inventory.document(io["ref"])["lines"]:
        d.kernel.decide_event(line["stable_id"], "accepted", note="ok")
    d.inventory.project(io["ref"])

    cogs = d.cost.post_cogs("2026-11")
    d.kernel.confirm_proposal(proposal_id=cogs["proposal"]["proposal_id"],
                              confirmed_by="cfo")

    exp = d.kernel.submit_proposal("exp:stmt-1", [
        {"account_code": "5200", "direction": "debit", "amount": "1000",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1000",
         "currency": "CNY"}], dimensions={"cost_center": "CC-1"})
    d.kernel.confirm_proposal(proposal_id=exp["proposal_id"], confirmed_by="cfo")

    d.assets.register("FA-01", "包装机", cost="12000", acquired_on="2026-01-15",
                      useful_life_months=12, expense_account="5200")
    dep = d.assets.depreciate("2026-11")
    d.kernel.confirm_proposal(proposal_id=dep["proposal"]["proposal_id"],
                              confirmed_by="cfo")
    return settings


def test_statements_balance_on_real_entries(client_faces):
    d = client_faces.app.state.drivers
    _approve_arrival_rule(client_faces.app.state.settings)
    _seed_one_month(d)

    tb = d.statements.trial_balance("2026-11")
    assert tb["balanced"] is True
    assert tb["totals"]["debit"] == tb["totals"]["credit"]

    inc = d.statements.income_statement("2026-11")
    assert inc["costOfSales"] == "400.00"
    assert inc["otherOpex"] == "2000.00"      # 费用 1000 + 折旧 1000
    assert inc["operatingProfit"] == "-2400.00"

    bs = d.statements.balance_sheet("2026-11")
    assert bs["balanced"] is True, bs.get("error")
    # 与账本余额咬合：1200 = 600、1602 = −1000、5200 = 2000
    balances = {r["account_code"]: r["balance"]
                for r in d.kernel.query_balance()["data"]}
    assert balances["1200"] == "600.000000"
    assert balances["1602"] == "-1000.000000"
    assert Decimal(bs["assets"]["total"]) == Decimal("600") + Decimal("-1000") \
        + Decimal(balances["1000"])
    assert bs["unclosedProfit"] == inc["operatingProfit"]

    dim = d.statements.expenses_by_dimension("2026-11", "cost_center")
    rows = {r["dimensionValue"]: r["amount"] for r in dim["rows"]}
    assert rows["CC-1"] == "1000.00"
    # 未标注维度的费用有两笔：成本结转 400（5000）与折旧 1000（5200）
    assert rows["unattributed"] == "1400.00"
    assert dim["total"] == "2400.00"
