"""财务后台报表端点（change: add-cn-finance-report-endpoints 任务 3.1）。

判据：真库里走一遍四端点与三层闸——

1. **fail-closed**：配置里没有 `supply-chain.fi` → 四个端点全 `PAGESET_FORBIDDEN`，
   且不泄露任何报表数据；
2. **身份不能只靠租户头**：授权后仍缺 `X-ERP-Staff` → `AUTH_REQUIRED`；
3. **三张表可读且带对照列**：`balance-sheet` 有 `opening`、`income-statement` 与
   `cash-flow` 有 `priorYear` / `cumulative`，`chart` 回显准则；
4. **载荷隔离**：同时授权内部页集与对外页集时，`GET /pagesets` 只吐对外 key
   ——多吐一个会让 miniapp 整包作废，连带打掉四个业务页集。
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fd_erp_vertical_api.pagesets import (INTERNAL_PAGESET_KEYS, PUBLISHED_PAGESET_KEYS,
                                          load_pageset_auth)

FI_ONLY = {"supply-chain.fi": ["finance.read"]}
FI_AND_UE = {"supply-chain.fi": ["finance.read"],
             "supply-chain.ue": ["catalog.read"]}

TENANT_HEADER = {"X-ERP-Tenant": "tnt-demo"}
STAFF_HEADER = {"X-ERP-Staff": "cfo@example"}

ENDPOINTS = ("balance-sheet", "income-statement", "cash-flow", "vat-return")


def _app(tmp_path, dsn, grants):
    from fastapi.testclient import TestClient

    from fd_erp_vertical_api.app import create_app
    from fd_erp_vertical_api.cart import CartStore
    from fd_erp_vertical_api.config import Settings

    payload = json.dumps(grants)
    settings = Settings(tenant_id="tnt-demo", database_url=dsn,
                        base_currency="CNY", data_dir=str(tmp_path / "data"),
                        pageset_auth_json=payload)
    from fd_erp_vertical_api.kernel_client import InProcessKernelClient
    kernel = InProcessKernelClient(settings)
    app = create_app(settings, kernel,
                     pageset_auth=load_pageset_auth(inline_json=payload),
                     cart_store=CartStore())
    return TestClient(app)


def _seed(client) -> None:
    """一个含现销与成本结转的小账本：11 月收现 2000、成本 800。"""
    d = client.app.state.drivers
    for ref, lines, when in (
        ("fi:sale", [{"account_code": "1000", "direction": "debit",
                      "amount": "2000", "currency": "CNY"},
                     {"account_code": "4000", "direction": "credit",
                      "amount": "2000", "currency": "CNY"}], "2026-11-10"),
        ("fi:cost", [{"account_code": "5000", "direction": "debit",
                      "amount": "800", "currency": "CNY"},
                     {"account_code": "1000", "direction": "credit",
                      "amount": "800", "currency": "CNY"}], "2026-11-12"),
    ):
        prop = d.kernel.submit_proposal(ref, lines, booked_on=when)
        d.kernel.confirm_proposal(proposal_id=prop["proposal_id"],
                                  confirmed_by="cfo")


@pytest.fixture()
def fi_client(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, FI_ONLY)
    _seed(client)
    return client


@pytest.fixture()
def fi_client_no_staff_ok(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, FI_AND_UE)
    _seed(client)
    return client


# ---- 1) 未授权：四个端点全拒 ----

def test_every_report_endpoint_is_forbidden_without_the_pageset(reset_db, tmp_path, dsn):
    client = _app(tmp_path, dsn, {"supply-chain.ue": ["catalog.read"]})
    for name in ENDPOINTS:
        res = client.get(f"/erp/supply-chain/v1/fi/reports/{name}?period=2026-11",
                         headers={**TENANT_HEADER, **STAFF_HEADER})
        assert res.status_code == 403, name
        body = res.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "PAGESET_FORBIDDEN", name
        assert "data" not in body or body.get("data") is None


# ---- 2) 授权后仍缺身份头 ----

def test_a_missing_staff_header_is_refused_even_with_the_grant(fi_client):
    res = fi_client.get("/erp/supply-chain/v1/fi/reports/balance-sheet?period=2026-11",
                        headers=TENANT_HEADER)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"


def test_the_tenant_header_alone_never_opens_the_reports(fi_client):
    for name in ENDPOINTS:
        res = fi_client.get(f"/erp/supply-chain/v1/fi/reports/{name}?period=2026-11",
                            headers=TENANT_HEADER)
        assert res.status_code in (401, 403), name


# ---- 3) 三张表可读且带对照列 ----

def test_the_balance_sheet_comes_with_the_opening_column(fi_client):
    res = fi_client.get("/erp/supply-chain/v1/fi/reports/balance-sheet?period=2026-11",
                        headers={**TENANT_HEADER, **STAFF_HEADER})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["chart"] == "cn-small-business"
    assert data["balanced"] is True
    assert "opening" in data and data["opening"]["asOf"] == "2025-12-31"
    cash = next(l for l in data["assets"] if l["key"] == "cash")
    assert Decimal(cash["amount"]) == Decimal("1200.00")


def test_the_income_statement_comes_with_prior_year_and_cumulative(fi_client):
    res = fi_client.get(
        "/erp/supply-chain/v1/fi/reports/income-statement?period=2026-11",
        headers={**TENANT_HEADER, **STAFF_HEADER})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["totals"]["netProfit"] == "1200.00"
    assert data["priorYear"]["period"] == "2025-11"
    assert data["cumulative"]["totals"]["netProfit"] == "1200.00"


def test_the_cash_flow_comes_with_both_self_proofs(fi_client):
    res = fi_client.get("/erp/supply-chain/v1/fi/reports/cash-flow?period=2026-11",
                        headers={**TENANT_HEADER, **STAFF_HEADER})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["consistent"] is True, data.get("note") or data.get("error")
    assert data["sumCheck"]["ok"] is True
    assert data["balanceCheck"]["ok"] is True
    assert data["netIncrease"] == "1200.00"
    assert data["cumulative"]["netIncrease"] == "1200.00"
    assert data["priorYear"]["period"] == "2025-11"


def test_the_vat_return_is_readable_and_says_it_was_not_cross_checked(fi_client):
    res = fi_client.get("/erp/supply-chain/v1/fi/reports/vat-return?period=2026-11",
                        headers={**TENANT_HEADER, **STAFF_HEADER})
    assert res.status_code == 200
    data = res.json()["data"]
    assert "未提供独立申报数" in json.dumps(data, ensure_ascii=False)


# ---- 4) 载荷隔离 ----

def test_the_internal_pageset_never_reaches_the_published_payload(fi_client_no_staff_ok):
    res = fi_client_no_staff_ok.get("/erp/supply-chain/v1/pagesets",
                                    headers=TENANT_HEADER)
    assert res.status_code == 200
    keys = [p["key"] for p in res.json()["pagesets"]]
    assert "supply-chain.ue" in keys
    assert all(k in PUBLISHED_PAGESET_KEYS for k in keys)
    assert not any(k in INTERNAL_PAGESET_KEYS for k in keys)


def test_the_internal_key_is_still_grantable_server_side():
    auth = load_pageset_auth(inline_json=json.dumps(FI_ONLY))
    assert auth.grants_of("supply-chain.fi") == frozenset({"finance.read"})
    assert auth.wire_payload()["pagesets"] == []
