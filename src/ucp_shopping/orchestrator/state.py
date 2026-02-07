"""LangGraph state schema for the shopping workflow.

The ``ShoppingGraphState`` TypedDict describes every piece of data that flows
through the graph.  Nodes read from and write to this shared state.
"""

from __future__ import annotations

from typing import Any, TypedDict

from ucp_shopping.models import (
    CheckoutStatus,
    ComparisonMatrix,
    MerchantInfo,
    OrderSummary,
    ProductResult,
    ShoppingPlan,
    ShoppingPreferences,
    ShoppingRequest,
    ShoppingSessionState,
    SplitOrderPlan,
)


class ShoppingGraphState(TypedDict, total=False):
    """Typed dictionary describing the full state flowing through the graph."""

    # --- Input ----------------------------------------------------------------
    request: ShoppingRequest
    session_id: str
    preferences: ShoppingPreferences

    # --- Session state --------------------------------------------------------
    current_state: ShoppingSessionState

    # --- Planning -------------------------------------------------------------
    shopping_plan: ShoppingPlan | None

    # --- Merchant discovery ---------------------------------------------------
    discovered_merchants: list[MerchantInfo]
    failed_merchants: list[str]

    # --- Product search -------------------------------------------------------
    search_results: dict[str, list[ProductResult]]
    merged_results: list[ProductResult]

    # --- Comparison -----------------------------------------------------------
    comparison_matrix: ComparisonMatrix | None

    # --- Optimization ---------------------------------------------------------
    optimization_plan: SplitOrderPlan | None

    # --- Human-in-the-loop ----------------------------------------------------
    user_confirmed: bool

    # --- Checkout -------------------------------------------------------------
    active_checkouts: list[CheckoutStatus]
    completed_orders: list[OrderSummary]

    # --- Error handling -------------------------------------------------------
    error: str | None

    # --- Message log (for debugging / LLM context) ----------------------------
    messages: list[dict[str, Any]]
