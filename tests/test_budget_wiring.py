"""预算控制车道装配（change: add-budget-control-lane 任务 3.1）。

判据：
1. `drivers.budget` 可用，三个预算口径键已转 `enforced=true`；
2. 登记 → 承诺 → 查询走通，且额度按承诺递减；
3. 实际值真的来自内核分录——先落一张真实 proposal 并确认，再看预算查询里的实际额
   （证明它是读内核、不是拿承诺顶替）。
"""
from __future__ import annotations


def test_drivers_expose_budget_and_flip_the_policy_keys(client_faces):
    d = client_faces.app.state.drivers
    assert d.budget is not None
    now = d.policy.current()
    for key in ("budget_dimension", "budget_period", "budget_control"):
        assert now["keys"][key]["enforced"] is True, key

    out = d.budget.set_budget("CC-1", "5200", "2026-11", "10000",
                              approved_by="owner")
    assert out["grain"] == "monthly"                    # 默认口径：cost-center + 月
    assert out["budget"]["dimension"] == "cost-center"

    c = d.budget.commit("CC-1", "5200", "3000", ref="PO-1",
                        occurred_on="2026-11-05")
    assert c["duplicate"] is False and c["remaining"] == "7000.000000"
    assert c["overBudget"] is False                     # 默认口径 warn-only、未超


def test_budget_actual_reads_the_kernel_not_the_commitments(client_faces):
    d = client_faces.app.state.drivers
    d.budget.set_budget("CC-1", "5200", "2026-11", "10000")
    d.budget.commit("CC-1", "5200", "3000", ref="PO-1", occurred_on="2026-11-05")

    before = d.budget.status("CC-1", "2026-11")
    assert before["rows"][0]["actual"] == "0.000000"    # 承诺不算实际

    # 内核里真落一笔 5200 借方（确认门之后）
    prop = d.kernel.submit_proposal("exp:2026-11-001", [
        {"account_code": "5200", "direction": "debit", "amount": "1200",
         "currency": "CNY"},
        {"account_code": "1000", "direction": "credit", "amount": "1200",
         "currency": "CNY"},
    ])
    d.kernel.confirm_proposal(proposal_id=prop["proposal_id"], confirmed_by="owner")

    after = d.budget.status("CC-1", "2026-11")
    row = after["rows"][0]
    assert row["actual"] == "1200.000000"               # 实际来自内核分录
    assert row["committed"] == "3000.000000"            # 承诺仍在，两者分列
    assert after["totals"]["actual"] == "1200.000000"
    assert "不区分维度" in after["caliber_note"]
