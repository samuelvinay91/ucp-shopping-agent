"""Tests for UCP Shopping Agent API."""

import pytest
from httpx import ASGITransport, AsyncClient

from ucp_shopping.main import build_app
from ucp_shopping.config import Settings


@pytest.fixture
def settings():
    return Settings(
        environment="testing",
        openai_api_key="test-key",
        human_confirmation_required=False,
    )


@pytest.fixture
def app(settings):
    return build_app(settings)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_health_check(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "ucp-shopping-agent"


class TestMockMerchants:
    async def test_techzone_discovery(self, client):
        resp = await client.get("/merchants/techzone/.well-known/ucp")
        assert resp.status_code == 200
        data = resp.json()
        assert "capabilities" in data
        assert data["merchant_name"] == "TechZone Electronics"

    async def test_homegoods_discovery(self, client):
        resp = await client.get("/merchants/homegoods/.well-known/ucp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["merchant_name"] == "HomeGoods Office"

    async def test_megamart_discovery(self, client):
        resp = await client.get("/merchants/megamart/.well-known/ucp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["merchant_name"] == "MegaMart"

    async def test_techzone_catalog(self, client):
        resp = await client.get("/merchants/techzone/api/v1/catalog/products")
        assert resp.status_code == 200
        data = resp.json()
        assert "products" in data
        assert data["total"] > 0

    async def test_search_across_merchants(self, client):
        for merchant in ["techzone", "homegoods", "megamart"]:
            resp = await client.get(
                f"/merchants/{merchant}/api/v1/catalog/products",
                params={"q": "keyboard"},
            )
            assert resp.status_code == 200

    async def test_techzone_negotiate(self, client):
        resp = await client.post(
            "/merchants/techzone/api/v1/negotiate",
            json={"agent_id": "test-agent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "negotiation_id" in data
        assert data["agent_id"] == "test-agent"
        assert "agreed_capabilities" in data

    async def test_techzone_checkout_lifecycle(self, client):
        # Get a product
        resp = await client.get(
            "/merchants/techzone/api/v1/catalog/products",
            params={"limit": 1},
        )
        products = resp.json()["products"]
        assert len(products) >= 1
        product_id = products[0]["id"]

        # Create checkout session
        resp = await client.post(
            "/merchants/techzone/api/v1/checkout/sessions",
            json={"line_items": [{"product_id": product_id, "quantity": 1}]},
        )
        assert resp.status_code == 200
        session = resp.json()
        assert session["state"] == "incomplete"
        session_id = session["id"]

        # Update with address
        resp = await client.put(
            f"/merchants/techzone/api/v1/checkout/sessions/{session_id}",
            json={"shipping_address": {
                "line1": "123 Main St",
                "city": "SF",
                "state": "CA",
                "postal_code": "94105",
                "country": "US",
            }},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "ready_for_complete"

        # Complete checkout
        resp = await client.post(
            f"/merchants/techzone/api/v1/checkout/sessions/{session_id}/complete",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "completed"
        assert "order_id" in data


class TestMerchantDiscovery:
    async def test_list_merchants(self, client):
        resp = await client.get("/api/v1/merchants")
        assert resp.status_code == 200
        data = resp.json()
        assert "merchants" in data
        # The discovery agent tries to reach the known_merchant_urls
        # which point to the mock merchants. In test env they may or may not
        # be discovered depending on routing. Just check the response shape.
        assert "total" in data

    async def test_discover_merchants(self, client):
        resp = await client.post("/api/v1/merchants/discover", json={
            "urls": ["http://test/merchants/techzone"]
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "discovered" in data


class TestShopping:
    async def test_submit_shopping_request(self, client):
        resp = await client.post("/api/v1/shop", json={
            "query": "Find me a mechanical keyboard under $100",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "planning"
        assert "stream_url" in data

    async def test_get_shopping_session(self, client):
        create_resp = await client.post("/api/v1/shop", json={
            "query": "laptop",
        })
        session_id = create_resp.json()["session_id"]
        resp = await client.get(f"/api/v1/shop/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == session_id

    async def test_cancel_shopping(self, client):
        create_resp = await client.post("/api/v1/shop", json={
            "query": "mouse",
        })
        session_id = create_resp.json()["session_id"]
        resp = await client.post(f"/api/v1/shop/{session_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"

    async def test_get_nonexistent_session(self, client):
        resp = await client.get("/api/v1/shop/nonexistent-id")
        assert resp.status_code == 404


class TestOrders:
    async def test_list_orders(self, client):
        resp = await client.get("/api/v1/orders")
        assert resp.status_code == 200
        data = resp.json()
        assert "orders" in data
        assert "total" in data

    async def test_get_order_not_found(self, client):
        resp = await client.get("/api/v1/orders/nonexistent")
        assert resp.status_code == 404


class TestMCPTools:
    async def test_list_tools(self, client):
        resp = await client.post("/api/v1/mcp/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        tool_names = [t["name"] for t in data["tools"]]
        assert "shop" in tool_names
        assert "compare_prices" in tool_names
        assert "find_best_deal" in tool_names
        assert "discover_merchants" in tool_names
        assert "track_orders" in tool_names
        assert "get_shopping_status" in tool_names

    async def test_execute_shop_tool(self, client):
        resp = await client.post("/api/v1/mcp/tools/shop/execute", json={
            "arguments": {"query": "keyboard"}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["tool_name"] == "shop"
        assert "session_id" in data["result"]

    async def test_execute_unknown_tool(self, client):
        resp = await client.post("/api/v1/mcp/tools/nonexistent/execute", json={
            "arguments": {}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] is not None
