"""Reusable mock UCP merchant mini-application.

Creates a self-contained FastAPI sub-app that implements the core UCP
endpoints (discovery, catalog search, checkout lifecycle, orders) using
in-memory state.  Designed to be mounted inside the main shopping agent
application for demo and testing purposes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request / response models (lightweight, internal to the mock)
# ---------------------------------------------------------------------------


class CreateCheckoutRequest(BaseModel):
    line_items: list[dict[str, Any]]


class UpdateCheckoutRequest(BaseModel):
    shipping_address: dict[str, Any] | None = None
    billing_address: dict[str, Any] | None = None
    selected_shipping_id: str | None = None
    discount_code: str | None = None


# ---------------------------------------------------------------------------
# Mock merchant mini-app
# ---------------------------------------------------------------------------


class MockMerchantApp:
    """A self-contained mock UCP merchant.

    Parameters
    ----------
    name:
        Human-readable merchant name.
    merchant_id:
        Unique merchant identifier.
    products:
        Product catalog loaded from JSON.
    base_path:
        URL path prefix where this app is mounted (for generating URLs).
    free_shipping_threshold:
        Order subtotal above which shipping is free.
    shipping_options:
        Shipping methods offered by this merchant.
    discount_codes:
        Valid discount codes.
    """

    def __init__(
        self,
        name: str,
        merchant_id: str,
        products: list[dict[str, Any]],
        base_path: str = "",
        free_shipping_threshold: float = 100.0,
        shipping_options: list[dict[str, Any]] | None = None,
        discount_codes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.merchant_id = merchant_id
        self.products = products
        self.base_path = base_path
        self.free_shipping_threshold = free_shipping_threshold

        self.shipping_options = shipping_options or [
            {
                "id": "standard",
                "name": "Standard Shipping",
                "description": "Delivered in 5-7 business days",
                "price": 5.99,
                "estimated_days_min": 5,
                "estimated_days_max": 7,
                "is_free": False,
            },
            {
                "id": "express",
                "name": "Express Shipping",
                "description": "Delivered in 2-3 business days",
                "price": 12.99,
                "estimated_days_min": 2,
                "estimated_days_max": 3,
                "is_free": False,
            },
            {
                "id": "overnight",
                "name": "Overnight Shipping",
                "description": "Next business day delivery",
                "price": 24.99,
                "estimated_days_min": 1,
                "estimated_days_max": 1,
                "is_free": False,
            },
        ]

        self.discount_codes = discount_codes or [
            {
                "code": "SAVE10",
                "description": "10% off your order",
                "discount_type": "percentage",
                "value": 10,
                "min_order": 50.0,
            },
            {
                "code": "FREESHIP",
                "description": "Free standard shipping",
                "discount_type": "free_shipping",
                "value": 0,
            },
        ]

        # In-memory state
        self._checkout_sessions: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, dict[str, Any]] = {}

        # Enrich products with merchant info and shipping
        for product in self.products:
            product.setdefault("merchant_id", self.merchant_id)
            product.setdefault("merchant_name", self.name)
            product.setdefault("shipping_options", self.shipping_options)
            product.setdefault("in_stock", product.get("stock", 0) > 0)
            product.setdefault("rating", round(3.5 + (hash(product.get("id", "")) % 15) / 10, 1))

        self.app = self._build_app()

    # ------------------------------------------------------------------
    # App builder
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        """Construct the FastAPI sub-app with all UCP endpoints."""
        app = FastAPI(title=f"Mock Merchant: {self.name}")

        merchant = self  # capture for closures

        # -- Discovery -------------------------------------------------

        @app.get("/.well-known/ucp")
        async def ucp_manifest() -> dict[str, Any]:
            """Serve the UCP discovery manifest."""
            return {
                "spec_version": "0.1.0",
                "merchant_name": merchant.name,
                "merchant_domain": f"{merchant.merchant_id}.example.com",
                "merchant_id": merchant.merchant_id,
                "base_url": merchant.base_path,
                "capabilities": [
                    {
                        "id": "catalog.search",
                        "version": "1.0",
                        "description": "Full-text product search",
                    },
                    {
                        "id": "catalog.browse",
                        "version": "1.0",
                        "description": "Browse product catalog",
                    },
                    {
                        "id": "checkout",
                        "version": "1.0",
                        "description": "Checkout session management",
                    },
                    {
                        "id": "orders",
                        "version": "1.0",
                        "description": "Order tracking",
                    },
                ],
                "extensions": [
                    {
                        "id": "discounts",
                        "version": "1.0",
                        "description": "Discount code support",
                    },
                    {
                        "id": "fulfillment",
                        "version": "1.0",
                        "description": "Multiple shipping options",
                    },
                ],
                "payment_handlers": [
                    {
                        "id": "mock_payment",
                        "name": "Mock Payment",
                        "description": "Simulated payment for demo",
                        "handler_url": f"{merchant.base_path}/api/v1/payments",
                        "supported_currencies": ["USD"],
                    },
                ],
                "endpoints": {
                    "catalog": f"{merchant.base_path}/api/v1/catalog/products",
                    "checkout": f"{merchant.base_path}/api/v1/checkout/sessions",
                    "orders": f"{merchant.base_path}/api/v1/orders",
                    "negotiate": f"{merchant.base_path}/api/v1/negotiate",
                },
                "metadata": {
                    "free_shipping_threshold": merchant.free_shipping_threshold,
                    "currency": "USD",
                    "return_policy": "30-day returns",
                },
            }

        # -- Negotiation -----------------------------------------------

        @app.post("/api/v1/negotiate")
        async def negotiate(body: dict[str, Any]) -> dict[str, Any]:
            """Simplified capability negotiation."""
            return {
                "negotiation_id": str(uuid.uuid4()),
                "agent_id": body.get("agent_id", ""),
                "agreed_capabilities": [
                    {"id": "catalog.search", "version": "1.0"},
                    {"id": "checkout", "version": "1.0"},
                ],
                "agreed_extensions": [
                    {"id": "discounts", "version": "1.0"},
                ],
                "agreed_payment_handlers": [
                    {"id": "mock_payment", "name": "Mock Payment"},
                ],
                "session_endpoint": f"{merchant.base_path}/api/v1/checkout/sessions",
            }

        # -- Catalog ---------------------------------------------------

        @app.get("/api/v1/catalog/products")
        async def search_products(
            q: str = Query("", description="Search query"),
            category: str | None = Query(None),
            min_price: float | None = Query(None),
            max_price: float | None = Query(None),
            limit: int = Query(20, ge=1, le=100),
            offset: int = Query(0, ge=0),
        ) -> dict[str, Any]:
            """Search the product catalog."""
            results = list(merchant.products)

            # Full-text search (simple keyword matching)
            if q:
                keywords = q.lower().split()
                filtered = []
                for product in results:
                    text = (
                        f"{product['name']} {product.get('description', '')} "
                        f"{product.get('category', '')} {product.get('brand', '')}"
                    ).lower()
                    if any(kw in text for kw in keywords):
                        filtered.append(product)
                results = filtered

            # Category filter
            if category:
                results = [
                    p for p in results if p.get("category", "").lower() == category.lower()
                ]

            # Price range filters
            if min_price is not None:
                results = [p for p in results if p.get("price", 0) >= min_price]
            if max_price is not None:
                results = [p for p in results if p.get("price", 0) <= max_price]

            total = len(results)
            page = results[offset : offset + limit]

            return {
                "products": page,
                "total": total,
                "offset": offset,
                "limit": limit,
                "query": q or None,
            }

        @app.get("/api/v1/catalog/products/{product_id}")
        async def get_product(product_id: str) -> dict[str, Any]:
            """Retrieve a single product by ID."""
            for product in merchant.products:
                if product["id"] == product_id:
                    return product
            raise HTTPException(status_code=404, detail=f"Product {product_id} not found")

        # -- Checkout --------------------------------------------------

        @app.post("/api/v1/checkout/sessions")
        async def create_checkout(req: CreateCheckoutRequest) -> dict[str, Any]:
            """Create a new checkout session."""
            session_id = str(uuid.uuid4())

            # Resolve line items
            line_items = []
            subtotal = 0.0
            for item_req in req.line_items:
                product_id = item_req.get("product_id", "")
                quantity = item_req.get("quantity", 1)
                product = None
                for p in merchant.products:
                    if p["id"] == product_id:
                        product = p
                        break
                if product is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Product {product_id} not found",
                    )
                price = product["price"]
                line_total = round(price * quantity, 2)
                subtotal += line_total
                line_items.append(
                    {
                        "product_id": product_id,
                        "product_name": product["name"],
                        "quantity": quantity,
                        "unit_price": {"amount": price, "currency": "USD"},
                        "total_price": {"amount": line_total, "currency": "USD"},
                    }
                )

            session = {
                "id": session_id,
                "state": "incomplete",
                "line_items": line_items,
                "subtotal": {"amount": round(subtotal, 2), "currency": "USD"},
                "tax": {"amount": round(subtotal * 0.0875, 2), "currency": "USD"},
                "shipping_cost": {"amount": 5.99, "currency": "USD"},
                "discount_amount": {"amount": 0.0, "currency": "USD"},
                "total": {
                    "amount": round(subtotal + subtotal * 0.0875 + 5.99, 2),
                    "currency": "USD",
                },
                "shipping_address": None,
                "selected_shipping": None,
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            merchant._checkout_sessions[session_id] = session
            return session

        @app.put("/api/v1/checkout/sessions/{session_id}")
        async def update_checkout(
            session_id: str, req: UpdateCheckoutRequest
        ) -> dict[str, Any]:
            """Update an existing checkout session."""
            session = merchant._checkout_sessions.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Checkout session not found")

            if req.shipping_address:
                session["shipping_address"] = req.shipping_address

            if req.selected_shipping_id:
                for opt in merchant.shipping_options:
                    if opt["id"] == req.selected_shipping_id:
                        session["selected_shipping"] = opt
                        session["shipping_cost"] = {
                            "amount": opt["price"],
                            "currency": "USD",
                        }
                        break

            # Check free shipping threshold
            subtotal = session["subtotal"]["amount"]
            if subtotal >= merchant.free_shipping_threshold:
                session["shipping_cost"] = {"amount": 0.0, "currency": "USD"}

            # Recalculate total
            tax = session["tax"]["amount"]
            shipping = session["shipping_cost"]["amount"]
            discount = session["discount_amount"]["amount"]
            session["total"] = {
                "amount": round(subtotal + tax + shipping - discount, 2),
                "currency": "USD",
            }

            # Advance state if we have enough info
            if session["shipping_address"] and session["state"] == "incomplete":
                session["state"] = "ready_for_complete"

            session["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
            return session

        @app.post("/api/v1/checkout/sessions/{session_id}/complete")
        async def complete_checkout(session_id: str) -> dict[str, Any]:
            """Complete a checkout session and create an order."""
            session = merchant._checkout_sessions.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Checkout session not found")

            if session["state"] not in ("ready_for_complete", "incomplete"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot complete session in state: {session['state']}",
                )

            # Create order
            order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            order = {
                "id": order_id,
                "checkout_session_id": session_id,
                "state": "confirmed",
                "line_items": session["line_items"],
                "shipping_address": session.get("shipping_address"),
                "subtotal": session["subtotal"],
                "tax": session["tax"],
                "shipping_cost": session["shipping_cost"],
                "total": session["total"],
                "tracking_number": f"TRK{uuid.uuid4().hex[:10].upper()}",
                "tracking_url": f"https://tracking.example.com/TRK{uuid.uuid4().hex[:10].upper()}",
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "history": [
                    {
                        "event_type": "order_confirmed",
                        "message": "Order has been confirmed and is being processed.",
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    }
                ],
            }

            merchant._orders[order_id] = order

            # Update session state
            session["state"] = "completed"
            session["order_id"] = order_id
            session["completed_at"] = datetime.now(tz=timezone.utc).isoformat()

            return {
                "id": session_id,
                "state": "completed",
                "order_id": order_id,
                "total": session["total"],
                "tracking_url": order["tracking_url"],
            }

        # -- Orders ----------------------------------------------------

        @app.get("/api/v1/orders/{order_id}")
        async def get_order(order_id: str) -> dict[str, Any]:
            """Retrieve order details."""
            order = merchant._orders.get(order_id)
            if order is None:
                raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
            return order

        @app.get("/api/v1/orders")
        async def list_orders() -> dict[str, Any]:
            """List all orders."""
            return {
                "orders": list(merchant._orders.values()),
                "total": len(merchant._orders),
            }

        return app
