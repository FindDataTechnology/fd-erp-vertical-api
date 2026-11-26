"""SE 对账单推送 / SP 发票上传（change establish-ai-native-ecommerce-stack 3.2）。

判据：
1. 未接线（运行时未配置）→ 501 NOT_WIRED 如实失败，不回落自建直连；
2. 接线后经运行时动作：`inapp-notify.send_broadcast`（受众选择器透传、
   留痕=网关回执）/ `file-service.create_upload_url → PUT → confirm_upload`；
3. 失败如实：缺字段 400 指名；越域/不存在 404；文件服务不可达如实 5xx。

**不连数据库**：SE 对账单派生经注入的假 service；SP 发票只走文件服务车道。
"""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.cart import CartStore
from fd_erp_vertical_api.config import Settings
from fd_erp_vertical_api.errors import ApiError

TENANT = "api-e2e"
GRANTS = json.dumps({
    "supply-chain.se": ["catalog.read", "statement.confirm.proxy", "ai.chat"],
    "supply-chain.sp": ["po.read", "statement.read", "invoice.upload", "ai.chat"],
})
STAFF = {"X-ERP-Staff": "S-001", "X-ERP-Tenant": TENANT}
SUPPLIER = {"X-ERP-Supplier": "SUP-001", "X-ERP-Tenant": TENANT}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    settings = Settings(tenant_id=TENANT, data_dir=str(tmp_path / "data"),
                        pageset_auth_json=GRANTS)
    client = TestClient(create_app(settings, cart_store=CartStore()))
    master = client.app.state.drivers.master
    # SE：一个归属 S-001 的客户（越域探针用它）
    cust = master.create_entity("customer", {"code": "C-001", "name": "客户甲",
                                             "sales_rep": "S-001"})
    master.confirm_request(cust["request_id"])
    # SP：一个供应商档案
    sup = master.create_entity("supplier", {"code": "SUP-001", "name": "供应商甲"})
    master.confirm_request(sup["request_id"])
    return client, cust


def _install_statement(client, *, found: bool, stmt: dict | None = None):
    """把 statement service 的取单换成假实现（SE 面只依赖 get_statement）。"""
    def get_statement(customer_id, statement_id):
        if not found:
            raise ApiError("NOT_FOUND", "对账单不存在")
        return stmt or {"statement_id": statement_id, "period": "2026-10",
                        "amount_total": "250", "currency": "CNY"}
    client.app.state.statements.get_statement = get_statement


class _FakeNotify:
    calls: list[dict] = []

    def __init__(self, *a, **k):
        pass

    def send_broadcast(self, *, audience, title, body, wake="none"):
        _FakeNotify.calls.append({"audience": audience, "title": title,
                                  "body": body})
        return {"broadcast": {"id": "BC-1", "hitCount": 3},
                "broadcast_id": "BC-1", "hit_count": 3}


class _FakeFiles:
    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):
        pass

    def create_upload_url(self, **kw):
        _FakeFiles.calls.append(("create", kw))
        return {"id": "F1", "upload_url": "http://storage.test/x",
                "method": "PUT", "dedup": False}

    def confirm_upload(self, *, owner, file_id):
        _FakeFiles.calls.append(("confirm", {"owner": owner, "file_id": file_id}))
        return {"id": file_id, "status": "ready"}

    def list_files(self, *, owner, mime_prefix=None, limit=50):
        _FakeFiles.calls.append(("list", {"owner": owner}))
        return [{"id": "F1", "filename": "a.pdf", "mime": "application/pdf",
                 "size": 10, "created_at": "2026-11-26T00:00:00"}]


# ---- SE 推送 ----

def test_se_push_without_runtime_is_not_wired(app_client):
    client, cust = app_client
    _install_statement(client, found=True)
    r = client.post(f"/erp/supply-chain/v1/se/statements/ST-1/push",
                    json={"audience": {"kind": "all"}}, headers=STAFF)
    assert r.status_code == 501, r.text
    assert r.json()["error"]["code"] == "NOT_WIRED"


def test_se_push_requires_audience_selector(app_client, monkeypatch):
    client, cust = app_client
    _install_statement(client, found=True)
    r = client.post(f"/erp/supply-chain/v1/se/statements/ST-1/push",
                    json={}, headers=STAFF)
    assert r.status_code == 400
    assert r.json()["error"]["detail"] == {"missing": "audience"}


def test_se_push_passes_audience_and_defaults_from_statement(app_client, monkeypatch):
    client, cust = app_client
    _install_statement(client, found=True)
    _FakeNotify.calls = []
    monkeypatch.setattr("fd_erp_vertical_api.faces.InappNotify", _FakeNotify)
    r = client.post("/erp/supply-chain/v1/se/statements/ST-1/push",
                    json={"audience": {"kind": "org", "id": "org-9"}},
                    headers=STAFF)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["broadcast_id"] == "BC-1" and data["hit_count"] == 3
    (call,) = _FakeNotify.calls
    assert call["audience"] == {"kind": "org", "id": "org-9"}
    assert "2026-10" in call["body"] and "250" in call["body"]


def test_se_push_unknown_statement_is_404(app_client):
    client, cust = app_client
    _install_statement(client, found=False)
    r = client.post(f"/erp/supply-chain/v1/se/statements/ST-404/push",
                    json={"audience": {"kind": "all"}}, headers=STAFF)
    assert r.status_code == 404


# ---- SP 发票 ----

def test_sp_invoice_upload_without_runtime_is_not_wired(app_client):
    client, _cust = app_client
    r = client.post("/erp/supply-chain/v1/sp/invoices", headers=SUPPLIER,
                    json={"fileName": "a.pdf", "mime": "application/pdf",
                          "contentBase64": base64.b64encode(b"%PDF-1.4 x").decode()})
    assert r.status_code == 501, r.text
    assert r.json()["error"]["code"] == "NOT_WIRED"


def test_sp_invoice_upload_passes_through_file_service(app_client, monkeypatch):
    client, _cust = app_client
    _FakeFiles.calls = []
    monkeypatch.setattr("fd_erp_vertical_api.faces.FileServiceUpload", _FakeFiles)
    puts: list[tuple[str, bytes]] = []
    monkeypatch.setattr("fd_erp_vertical_api.faces._put_presigned",
                        lambda g, data, fn, mime: puts.append((g["upload_url"], data)))
    payload = b"%PDF-1.4 invoice"
    r = client.post("/erp/supply-chain/v1/sp/invoices", headers=SUPPLIER,
                    json={"fileName": "a.pdf", "mime": "application/pdf",
                          "contentBase64": base64.b64encode(payload).decode()})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["invoice_id"] == "F1" and data["sizeBytes"] == len(payload)
    assert data["verification"]["state"] == "unverified"   # 查验车道未接入，如实
    (action, kw), (confirm, kw2) = _FakeFiles.calls
    assert action == "create"
    assert kw["owner"] == f"erp:{TENANT}:sp-invoice" and kw["size"] == len(payload)
    assert action == "create" and kw["sha256"]
    assert confirm == "confirm" and kw2["file_id"] == "F1"
    assert puts == [("http://storage.test/x", payload)]


def test_sp_invoice_upload_rejects_bad_input(app_client):
    client, _cust = app_client
    r = client.post("/erp/supply-chain/v1/sp/invoices", headers=SUPPLIER,
                    json={"fileName": "a.pdf"})
    assert r.status_code == 400
    assert set(r.json()["error"]["detail"]["missing"]) == {"mime", "contentBase64"}
    r = client.post("/erp/supply-chain/v1/sp/invoices", headers=SUPPLIER,
                    json={"fileName": "a.pdf", "mime": "application/pdf",
                          "contentBase64": "%%not-base64%%"})
    assert r.status_code == 400


def test_sp_invoice_list_projects_file_service_rows(app_client, monkeypatch):
    client, _cust = app_client
    _FakeFiles.calls = []
    monkeypatch.setattr("fd_erp_vertical_api.faces.FileServiceUpload", _FakeFiles)
    r = client.get("/erp/supply-chain/v1/sp/invoices", headers=SUPPLIER)
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert rows[0]["invoice_id"] == "F1" and rows[0]["fileName"] == "a.pdf"
    assert rows[0]["verification"]["state"] == "unverified"
    (action, kw), = _FakeFiles.calls
    assert action == "list" and kw["owner"] == f"erp:{TENANT}:sp-invoice"
