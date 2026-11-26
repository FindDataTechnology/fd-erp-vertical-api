"""店面在线收款（PayPal）测试（change establish-ai-native-ecommerce-stack 0.13）。

判据（任务验证原文）：
1. **无真实商户号时以 NOT_WIRED 如实失败（501），不回落任何自建直连**；
2. 捕获完成 → 与人工登记**同一条** record_payment_received 车道落到账事实，
   幂等键 = `paypal:{capture_id}`（同一笔捕获重复确认只落一条事实）；
3. 捕获未完成 → 不登记到账、订单停留待付款（0.12「未到账不倒流」）；
4. 跨店/非零售单一律 404（不泄露存在性）。

**不连数据库**：店面族只走主数据与事件档，任何机器可跑（同 test_storefront_public）。
真 PayPal 车道在无商户号时本就 NOT_WIRED，happy path 用注入的假车道（真链路的
接线形态由模板仓 tests/test_paypal_checkout.py 对 RuntimeClient 引用求值）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_erp_vertical_api.app import create_app
from fd_erp_vertical_api.cart import CartStore
from fd_erp_vertical_api.config import Settings

TENANT = "api-e2e"
CHANNEL = "retail-online"
SHIP = {"name": "收货甲", "phone": "13800000001", "address": "示例地址"}


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """店面客户端 + 一件可售商品；运行时环境变量始终未配置（无商户号的实况）。"""
    monkeypatch.delenv("ERP_CONNECTOR_BASE_URL", raising=False)
    settings = Settings(tenant_id=TENANT, data_dir=str(tmp_path / "data"),
                        retail_channel=CHANNEL)
    client = TestClient(create_app(settings, cart_store=CartStore()))
    master = client.app.state.drivers.master
    pricing = client.app.state.drivers.pricing
    req = master.create_entity("product", {
        "code": "SKU-P", "name": "PayPal 可购商品", "category": "饮料",
        "base_price": "10", "units": [{"unit": "件", "kind": "base", "rate": "1"}]})
    master.confirm_request(req["request_id"])
    pool = master.create_entity("product_pool", {
        "name": "零售池", "visibility": "authorized", "org_path": "/零售",
        "product_codes": ["SKU-P"]})
    master.confirm_request(pool["request_id"])
    master.confirm_request(
        master.authorize_pool(pool["entity_id"], channels=[CHANNEL])["request_id"])
    pricing.confirm_rule(pricing.add_rule({
        "layer": "retail", "scope": {"product_code": "SKU-P", "channel_id": CHANNEL},
        "price": "99.00", "valid_from": "2026-01-01", "valid_to": "2099-12-31",
        "rate_source": "retail/price-list/paypal-test"})["request_id"])

    def new_order() -> str:
        r = client.post(f"/shop/{TENANT}/orders",
                        json={"lines": [{"productId": "SKU-P", "qty": 1}],
                              "shipTo": SHIP}, headers={})
        assert r.status_code == 200, r.text
        return r.json()["data"]["order_ref"]

    return client, new_order


class _FakeLane:
    """假 PayPal 车道：记录调用，按脚本回放。"""

    scripts: dict[str, object] = {}

    def __init__(self, *a, **k):
        self.calls: list[tuple[str, dict]] = []

    def create_order(self, *, order_ref, amount, currency):
        self.calls.append(("create_order", {"order_ref": order_ref,
                                            "amount": amount, "currency": currency}))
        out = self.scripts.get("create")
        if isinstance(out, Exception):
            raise out
        return out

    def capture_order(self, *, paypal_order_id):
        self.calls.append(("capture_order", {"paypal_order_id": paypal_order_id}))
        out = self.scripts.get("capture")
        if isinstance(out, Exception):
            raise out
        return out


APPROVE = {"paypal_order_id": "PAY-1", "status": "CREATED",
           "approve_url": "https://www.paypal.com/checkoutnow?token=PAY-1"}
CAPTURED = {"paypal_order_id": "PAY-1", "status": "COMPLETED", "completed": True,
            "capture_id": "CAP-1", "amount": "99.00", "currency": "CNY"}


def _install(monkeypatch):
    lane = _FakeLane()
    monkeypatch.setattr("fd_erp_vertical_api.storefront.PayPalCheckout",
                        lambda *a, **k: lane)
    return lane


def test_create_without_merchant_account_is_not_wired(shop):
    """无商户号（运行时未配置）→ 501 NOT_WIRED，如实失败，无回落。"""
    client, new_order = shop
    ref = new_order()
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    assert r.status_code == 501, r.text
    err = r.json()["error"]
    assert err["code"] == "NOT_WIRED"
    assert "自建直连" in err["message"]


def test_create_takes_amount_from_order_fact_not_buyer(shop, monkeypatch):
    """金额/币种取自订单事实；买家侧没有报价格的入口。"""
    client, new_order = shop
    lane = _install(monkeypatch)
    lane.scripts["create"] = APPROVE
    ref = new_order()
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["approve_url"].startswith("https://www.paypal.com/")
    (action, kwargs), = lane.calls
    assert action == "create_order"
    assert kwargs == {"order_ref": ref, "amount": "99", "currency": "CNY"}


def test_capture_completed_records_payment_on_same_lane(shop, monkeypatch):
    """捕获完成 → 到账事实与人工登记同车道；订单翻 received；履约可继续。"""
    client, new_order = shop
    lane = _install(monkeypatch)
    lane.scripts["create"] = APPROVE
    lane.scripts["capture"] = CAPTURED
    ref = new_order()
    assert client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal",
                       headers={}).status_code == 200
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                    json={"paypalOrderId": "PAY-1"}, headers={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["payment_state"] == "received"
    assert data["capture_id"] == "CAP-1"

    orders = client.app.state.drivers.orders
    facts = [e for e in orders._events()
             if e.get("record") == "payment_received" and e.get("order_ref") == ref]
    assert len(facts) == 1
    assert facts[0]["payload"]["proof_ref"] == "paypal:CAP-1"
    assert facts[0]["payload"]["requires_posting"] is False  # 只记事实不落分录
    # 买家侧查单看得到已到账
    got = client.get(f"/shop/{TENANT}/orders/{ref}", headers={})
    assert got.json()["data"]["payment_state"] == "received"


def test_capture_is_idempotent_by_capture_id(shop, monkeypatch):
    """同一笔捕获重复确认：返回首次结果，事实层不新增。"""
    client, new_order = shop
    lane = _install(monkeypatch)
    lane.scripts["create"] = APPROVE
    lane.scripts["capture"] = CAPTURED
    ref = new_order()
    client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    a = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                    json={"paypalOrderId": "PAY-1"}, headers={}).json()["data"]
    b = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                    json={"paypalOrderId": "PAY-1"}, headers={}).json()["data"]
    assert b["duplicate"] is True and b["payment_ref"] == a["payment_ref"]
    orders = client.app.state.drivers.orders
    assert sum(1 for e in orders._events()
               if e.get("record") == "payment_received") == 1


def test_capture_incomplete_records_nothing_and_order_stays_awaiting(shop, monkeypatch):
    """捕获未完成（402 PAYMENT_NOT_CONFIRMED）→ 零到账事实，订单停留待付款。"""
    client, new_order = shop
    lane = _install(monkeypatch)
    lane.scripts["create"] = APPROVE
    lane.scripts["capture"] = {"paypal_order_id": "PAY-1", "status": "DECLINED",
                               "completed": False, "capture_id": None,
                               "amount": None, "currency": None}
    ref = new_order()
    client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                    json={"paypalOrderId": "PAY-1"}, headers={})
    assert r.status_code == 402, r.text
    assert r.json()["error"]["code"] == "PAYMENT_NOT_CONFIRMED"
    orders = client.app.state.drivers.orders
    assert not [e for e in orders._events()
                if e.get("record") == "payment_received"]
    got = client.get(f"/shop/{TENANT}/orders/{ref}", headers={}).json()["data"]
    assert got["payment_state"] == "awaiting_payment"


def test_paid_order_refuses_second_paypal_create(shop, monkeypatch):
    """已到账的单不允许再次发起收款。"""
    client, new_order = shop
    lane = _install(monkeypatch)
    lane.scripts["create"] = APPROVE
    lane.scripts["capture"] = CAPTURED
    ref = new_order()
    client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                json={"paypalOrderId": "PAY-1"}, headers={})
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal", headers={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WORKFLOW_STATE"


def test_paypal_routes_404_for_cross_tenant_and_foreign_orders(shop):
    """跨店单、B2B 单、不存在的单：一律 404，不泄露存在性。"""
    client, new_order = shop
    assert client.post("/shop/intruder/orders/X/payment/paypal",
                       headers={}).status_code == 404
    for path in ("payment/paypal", "payment/paypal/capture"):
        r = client.post(f"/shop/{TENANT}/orders/SO-NOT-RETAIL/{path}",
                        json={"paypalOrderId": "PAY-1"}, headers={})
        assert r.status_code == 404, (path, r.text)


def test_capture_requires_paypal_order_id(shop):
    client, new_order = shop
    ref = new_order()
    r = client.post(f"/shop/{TENANT}/orders/{ref}/payment/paypal/capture",
                    json={}, headers={})
    assert r.status_code == 400
    assert r.json()["error"]["detail"] == {"missing": "paypalOrderId"}
