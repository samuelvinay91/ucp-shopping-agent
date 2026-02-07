"""Pydantic models for the UCP Shopping Agent.

Covers shopping requests, sessions, merchant information, product results,
comparison matrices, split-order plans, checkout status, order summaries,
SSE events, and MCP tool definitions.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shopping preferences and requests
# ---------------------------------------------------------------------------


class ShoppingPreferences(BaseModel):
    """User preferences that influence search, comparison, and optimization."""

    prefer_single_merchant: bool = False
    max_shipping_days: int | None = None
    prefer_free_shipping: bool = False
    max_results_per_merchant: int = 10
    preferred_brands: list[str] = Field(default_factory=list)
    min_rating: float | None = None


class ShoppingRequest(BaseModel):
    """Top-level shopping request submitted by a user or agent."""

    query: str
    budget: Decimal | None = None
    preferences: ShoppingPreferences = Field(default_factory=ShoppingPreferences)


# ---------------------------------------------------------------------------
# Session state machine
# ---------------------------------------------------------------------------


class ShoppingSessionState(str, enum.Enum):
    """Lifecycle states of a shopping session."""

    PLANNING = "planning"
    DISCOVERING = "discovering"
    SEARCHING = "searching"
    COMPARING = "comparing"
    OPTIMIZING = "optimizing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CHECKING_OUT = "checking_out"
    COMPLETED = "completed"
    FAILED = "failed"


class ShoppingSession(BaseModel):
    """Full state of a shopping session."""

    id: str
    request: ShoppingRequest
    state: ShoppingSessionState = ShoppingSessionState.PLANNING
    merchants: list[MerchantInfo] = Field(default_factory=list)
    search_results: dict[str, list[ProductResult]] = Field(default_factory=dict)
    comparison: ComparisonMatrix | None = None
    optimization_plan: SplitOrderPlan | None = None
    orders: list[OrderSummary] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Merchant information
# ---------------------------------------------------------------------------


class MerchantInfo(BaseModel):
    """Parsed UCP merchant manifest."""

    id: str
    name: str
    url: str
    capabilities: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    status: str = "active"
    base_url: str = ""
    endpoints: dict[str, str] = Field(default_factory=dict)
    free_shipping_threshold: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Product results
# ---------------------------------------------------------------------------


class ShippingOption(BaseModel):
    """A shipping method available for a product."""

    id: str = "standard"
    name: str = "Standard Shipping"
    price: float = 5.99
    estimated_days_min: int = 3
    estimated_days_max: int = 7
    is_free: bool = False


class ProductResult(BaseModel):
    """A single product result returned from a merchant."""

    product_id: str
    name: str
    description: str = ""
    price: float
    currency: str = "USD"
    merchant_id: str
    merchant_name: str
    category: str = ""
    brand: str = ""
    shipping_options: list[ShippingOption] = Field(default_factory=list)
    in_stock: bool = True
    stock_quantity: int = 0
    url: str = ""
    image_url: str = ""
    specs: dict[str, str] = Field(default_factory=dict)
    rating: float = 0.0
    score: float = 0.0


# ---------------------------------------------------------------------------
# Comparison matrix
# ---------------------------------------------------------------------------


class ComparisonEntry(BaseModel):
    """Comparison of a single product query across merchants."""

    product_query: str
    merchant_results: list[ProductResult] = Field(default_factory=list)
    best_price: ProductResult | None = None
    best_shipping: ProductResult | None = None
    recommended: ProductResult | None = None


class ComparisonMatrix(BaseModel):
    """Multi-item comparison matrix across merchants."""

    entries: list[ComparisonEntry] = Field(default_factory=list)
    total_merchants: int = 0
    total_products_found: int = 0
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Split-order optimization
# ---------------------------------------------------------------------------


class SplitOrderItem(BaseModel):
    """A single item in a split-order plan."""

    product_name: str
    product_id: str
    merchant_name: str
    merchant_id: str
    merchant_url: str = ""
    price: float
    shipping_cost: float
    total: float = 0.0

    def model_post_init(self, __context: Any) -> None:
        """Compute item total if not provided."""
        if self.total == 0.0:
            self.total = round(self.price + self.shipping_cost, 2)


class SplitOrderPlan(BaseModel):
    """Optimized purchase plan that may split across merchants."""

    items: list[SplitOrderItem] = Field(default_factory=list)
    total_product_cost: float = 0.0
    total_shipping_cost: float = 0.0
    grand_total: float = 0.0
    savings_vs_single: float = 0.0
    merchants_used: int = 0
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Checkout and orders
# ---------------------------------------------------------------------------


class CheckoutStatus(BaseModel):
    """Status of a checkout at a single merchant."""

    merchant_id: str
    merchant_name: str
    session_id: str | None = None
    status: str = "pending"
    error: str | None = None
    order_id: str | None = None


class OrderSummary(BaseModel):
    """Summary of a completed order at a single merchant."""

    merchant_name: str
    merchant_id: str
    order_id: str
    items: list[SplitOrderItem] = Field(default_factory=list)
    total: float = 0.0
    status: str = "confirmed"
    tracking_url: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------


class ShoppingEvent(BaseModel):
    """Server-Sent Event pushed during shopping workflow execution."""

    event_type: str
    session_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------


class MCPToolDefinition(BaseModel):
    """MCP tool definition with JSON-Schema input specification."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # noqa: N815


class MCPToolResult(BaseModel):
    """Result of executing an MCP tool."""

    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Shopping plan (output of the LLM planner)
# ---------------------------------------------------------------------------


class ShoppingPlanItem(BaseModel):
    """A single item the planner extracted from the user query."""

    name: str
    keywords: list[str] = Field(default_factory=list)
    budget: Decimal | None = None
    brand_preference: str | None = None
    features: list[str] = Field(default_factory=list)


class ShoppingPlan(BaseModel):
    """LLM-parsed shopping intent with structured items and constraints."""

    items: list[ShoppingPlanItem] = Field(default_factory=list)
    overall_budget: Decimal | None = None
    preferences: ShoppingPreferences = Field(default_factory=ShoppingPreferences)
    reasoning: str = ""


# Rebuild forward references so nested models resolve correctly
ShoppingSession.model_rebuild()
