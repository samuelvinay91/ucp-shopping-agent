"""Main LangGraph StateGraph for the shopping workflow.

Nodes
-----
plan       -- LLM parses the query into a structured shopping plan
discover   -- Fetch ``/.well-known/ucp`` from candidate merchants
search     -- Parallel product search across discovered merchants
compare    -- Build a price/shipping comparison matrix
optimize   -- Split-order optimization across merchants
present    -- Prepare the human-readable summary
wait       -- Human-in-the-loop confirmation gate
checkout   -- Execute checkouts at selected merchants
complete   -- Finalize orders and emit completion event

Edges (with conditional routing)
------
plan -> discover
discover -> search (if merchants found) | fail
search -> compare
compare -> optimize (if multi-item) | present (single item)
optimize -> present
present -> wait
wait -> checkout (if confirmed) | fail
checkout -> complete
"""

from __future__ import annotations

import structlog
from langgraph.graph import END, StateGraph

from ucp_shopping.agents.checkout_agent import CheckoutAgent
from ucp_shopping.agents.comparison_agent import ComparisonAgent
from ucp_shopping.agents.discovery_agent import DiscoveryAgent
from ucp_shopping.agents.optimizer import SplitOrderOptimizer
from ucp_shopping.agents.search_agent import SearchAgent
from ucp_shopping.config import Settings
from ucp_shopping.models import ShoppingSessionState
from ucp_shopping.orchestrator.planner import ShoppingPlanner
from ucp_shopping.orchestrator.state import ShoppingGraphState
from ucp_shopping.streaming import (
    EVENT_AWAITING_CONFIRMATION,
    EVENT_CHECKING_OUT,
    EVENT_COMPARING,
    EVENT_COMPARISON_READY,
    EVENT_COMPLETED,
    EVENT_ERROR,
    EVENT_MERCHANTS_DISCOVERED,
    EVENT_OPTIMIZING,
    EVENT_OPTIMIZATION_READY,
    EVENT_PLANNING,
    EVENT_PRODUCTS_FOUND,
    EVENT_SEARCHING,
    ShoppingEventStream,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Node factories
# ---------------------------------------------------------------------------


def _make_plan_node(planner: ShoppingPlanner, stream: ShoppingEventStream):
    """Create the *plan* node function."""

    async def plan_node(state: ShoppingGraphState) -> ShoppingGraphState:
        """Parse the shopping query into a structured plan."""
        session_id = state.get("session_id", "")
        request = state["request"]

        await stream.emit(session_id, EVENT_PLANNING, message="Analyzing your shopping request...")

        try:
            plan = await planner.plan(request.query)
            item_names = [item.name for item in plan.items]
            await stream.emit(
                session_id,
                EVENT_PLANNING,
                data={"items": item_names, "reasoning": plan.reasoning},
                message=f"Found {len(plan.items)} item(s) to search for: {', '.join(item_names)}",
            )
            return {
                **state,
                "shopping_plan": plan,
                "current_state": ShoppingSessionState.DISCOVERING,
                "error": None,
                "messages": state.get("messages", [])
                + [{"role": "system", "content": f"Plan: {plan.reasoning}"}],
            }
        except Exception as exc:
            logger.exception("plan_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Planning failed: {exc}")
            return {**state, "error": f"Planning failed: {exc}"}

    return plan_node


def _make_discover_node(discovery: DiscoveryAgent, stream: ShoppingEventStream):
    """Create the *discover* node function."""

    async def discover_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        await stream.emit(session_id, EVENT_SEARCHING, message="Discovering UCP merchants...")

        try:
            merchants, failed = await discovery.discover_merchants_with_failures()
            await stream.emit(
                session_id,
                EVENT_MERCHANTS_DISCOVERED,
                data={
                    "merchants": [m.name for m in merchants],
                    "failed": failed,
                },
                message=f"Discovered {len(merchants)} merchant(s).",
            )
            return {
                **state,
                "discovered_merchants": merchants,
                "failed_merchants": failed,
                "current_state": (
                    ShoppingSessionState.SEARCHING
                    if merchants
                    else ShoppingSessionState.FAILED
                ),
                "error": None if merchants else "No merchants discovered.",
            }
        except Exception as exc:
            logger.exception("discover_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Discovery failed: {exc}")
            return {
                **state,
                "discovered_merchants": [],
                "failed_merchants": [],
                "error": f"Discovery failed: {exc}",
            }

    return discover_node


def _make_search_node(search_agent: SearchAgent, stream: ShoppingEventStream):
    """Create the *search* node function."""

    async def search_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        merchants = state.get("discovered_merchants", [])
        plan = state.get("shopping_plan")

        if not plan or not plan.items:
            return {**state, "error": "No shopping plan available."}

        queries = []
        for item in plan.items:
            if item.keywords:
                queries.append(" ".join(item.keywords))
            else:
                queries.append(item.name)

        await stream.emit(
            session_id,
            EVENT_SEARCHING,
            data={"queries": queries, "merchant_count": len(merchants)},
            message=f"Searching {len(merchants)} merchant(s) for {len(queries)} item(s)...",
        )

        try:
            results = await search_agent.search_all_merchants(merchants, queries)
            total_products = sum(len(v) for v in results.values())

            # Merge all results into a flat list
            merged: list = []
            for product_list in results.values():
                merged.extend(product_list)

            await stream.emit(
                session_id,
                EVENT_PRODUCTS_FOUND,
                data={"total_products": total_products, "per_merchant": {k: len(v) for k, v in results.items()}},
                message=f"Found {total_products} product(s) across {len(results)} merchant(s).",
            )
            return {
                **state,
                "search_results": results,
                "merged_results": merged,
                "current_state": ShoppingSessionState.COMPARING,
                "error": None,
            }
        except Exception as exc:
            logger.exception("search_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Search failed: {exc}")
            return {**state, "error": f"Search failed: {exc}"}

    return search_node


def _make_compare_node(comparison: ComparisonAgent, stream: ShoppingEventStream):
    """Create the *compare* node function."""

    async def compare_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        search_results = state.get("search_results", {})
        plan = state.get("shopping_plan")

        item_names = [item.name for item in (plan.items if plan else [])]

        await stream.emit(
            session_id,
            EVENT_COMPARING,
            message="Building price comparison matrix...",
        )

        try:
            matrix = await comparison.build_comparison(search_results, item_names)
            await stream.emit(
                session_id,
                EVENT_COMPARISON_READY,
                data={
                    "entries": len(matrix.entries),
                    "total_products": matrix.total_products_found,
                },
                message=f"Comparison ready: {len(matrix.entries)} item(s) compared.",
            )
            return {
                **state,
                "comparison_matrix": matrix,
                "current_state": ShoppingSessionState.OPTIMIZING,
                "error": None,
            }
        except Exception as exc:
            logger.exception("compare_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Comparison failed: {exc}")
            return {**state, "error": f"Comparison failed: {exc}"}

    return compare_node


def _make_optimize_node(optimizer: SplitOrderOptimizer, stream: ShoppingEventStream):
    """Create the *optimize* node function."""

    async def optimize_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        matrix = state.get("comparison_matrix")
        preferences = state.get("preferences")

        if not matrix:
            return {**state, "error": "No comparison matrix available."}

        await stream.emit(
            session_id,
            EVENT_OPTIMIZING,
            message="Optimizing purchase across merchants...",
        )

        try:
            plan = await optimizer.optimize(matrix, preferences)
            await stream.emit(
                session_id,
                EVENT_OPTIMIZATION_READY,
                data={
                    "grand_total": plan.grand_total,
                    "merchants_used": plan.merchants_used,
                    "savings": plan.savings_vs_single,
                },
                message=(
                    f"Optimization complete: ${plan.grand_total:.2f} total "
                    f"across {plan.merchants_used} merchant(s). "
                    f"Savings: ${plan.savings_vs_single:.2f}"
                ),
            )
            return {
                **state,
                "optimization_plan": plan,
                "current_state": ShoppingSessionState.AWAITING_CONFIRMATION,
                "error": None,
            }
        except Exception as exc:
            logger.exception("optimize_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Optimization failed: {exc}")
            return {**state, "error": f"Optimization failed: {exc}"}

    return optimize_node


def _make_present_node(stream: ShoppingEventStream):
    """Create the *present* node -- prepares the summary for the user."""

    async def present_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        plan = state.get("optimization_plan")

        summary_lines: list[str] = []
        if plan:
            summary_lines.append(f"Total: ${plan.grand_total:.2f}")
            for item in plan.items:
                summary_lines.append(
                    f"  - {item.product_name} from {item.merchant_name}: "
                    f"${item.price:.2f} + ${item.shipping_cost:.2f} shipping"
                )
            if plan.savings_vs_single > 0:
                summary_lines.append(
                    f"  Savings vs single merchant: ${plan.savings_vs_single:.2f}"
                )

        await stream.emit(
            session_id,
            EVENT_AWAITING_CONFIRMATION,
            data={"summary": summary_lines, "plan": plan.model_dump() if plan else {}},
            message="Please review and confirm your order.",
        )
        return {
            **state,
            "current_state": ShoppingSessionState.AWAITING_CONFIRMATION,
        }

    return present_node


def _make_wait_node(stream: ShoppingEventStream):
    """Create the *wait_for_confirmation* node.

    In a real system this would block until the user confirms.  Here we
    simply read the ``user_confirmed`` flag from state, which gets set
    externally via the ``/confirm`` API endpoint.
    """

    async def wait_node(state: ShoppingGraphState) -> ShoppingGraphState:
        confirmed = state.get("user_confirmed", False)
        session_id = state.get("session_id", "")

        if not confirmed:
            await stream.emit(
                session_id,
                EVENT_AWAITING_CONFIRMATION,
                message="Waiting for your confirmation...",
            )
        return {**state, "user_confirmed": confirmed}

    return wait_node


def _make_checkout_node(checkout_agent: CheckoutAgent, stream: ShoppingEventStream):
    """Create the *checkout* node function."""

    async def checkout_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        plan = state.get("optimization_plan")
        merchants = state.get("discovered_merchants", [])

        if not plan:
            return {**state, "error": "No optimization plan for checkout."}

        await stream.emit(
            session_id,
            EVENT_CHECKING_OUT,
            message=f"Checking out with {plan.merchants_used} merchant(s)...",
        )

        try:
            merchants_map = {m.id: m for m in merchants}
            orders = await checkout_agent.execute_checkouts(plan, merchants_map, stream, session_id)
            return {
                **state,
                "completed_orders": orders,
                "current_state": ShoppingSessionState.COMPLETED,
                "error": None,
            }
        except Exception as exc:
            logger.exception("checkout_node_error")
            await stream.emit(session_id, EVENT_ERROR, message=f"Checkout failed: {exc}")
            return {**state, "error": f"Checkout failed: {exc}"}

    return checkout_node


def _make_complete_node(stream: ShoppingEventStream):
    """Create the *complete* node -- emits the final completion event."""

    async def complete_node(state: ShoppingGraphState) -> ShoppingGraphState:
        session_id = state.get("session_id", "")
        orders = state.get("completed_orders", [])

        await stream.emit(
            session_id,
            EVENT_COMPLETED,
            data={
                "order_count": len(orders),
                "orders": [o.model_dump() for o in orders] if orders else [],
            },
            message=f"Shopping complete! {len(orders)} order(s) placed.",
        )
        return {
            **state,
            "current_state": ShoppingSessionState.COMPLETED,
        }

    return complete_node


async def _fail_node(state: ShoppingGraphState) -> ShoppingGraphState:
    """Terminal failure node."""
    return {
        **state,
        "current_state": ShoppingSessionState.FAILED,
    }


# ---------------------------------------------------------------------------
# Conditional routing functions
# ---------------------------------------------------------------------------


def _after_discover(state: ShoppingGraphState) -> str:
    """Route after discovery: search if we found merchants, else fail."""
    merchants = state.get("discovered_merchants", [])
    if merchants:
        return "search"
    return "fail"


def _after_compare(state: ShoppingGraphState) -> str:
    """Route after comparison: optimize if multi-item, else present."""
    matrix = state.get("comparison_matrix")
    if matrix and len(matrix.entries) > 1:
        return "optimize"
    return "optimize"  # Always optimize even for single items for consistency


def _after_wait(state: ShoppingGraphState) -> str:
    """Route after human confirmation gate."""
    if state.get("user_confirmed", False):
        return "checkout"
    return "fail"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_shopping_graph(
    settings: Settings,
    stream: ShoppingEventStream,
) -> StateGraph:
    """Construct the LangGraph shopping workflow.

    Parameters
    ----------
    settings:
        Application settings.
    stream:
        SSE event stream for real-time updates.

    Returns
    -------
    StateGraph
        An uncompiled graph.  Call ``.compile()`` before invoking.
    """
    # Instantiate agents
    planner = ShoppingPlanner(settings)
    discovery = DiscoveryAgent(settings)
    search_agent = SearchAgent(settings)
    comparison = ComparisonAgent()
    optimizer = SplitOrderOptimizer()
    checkout_agent = CheckoutAgent(settings)

    # Build graph
    graph = StateGraph(ShoppingGraphState)

    # Add nodes
    graph.add_node("plan", _make_plan_node(planner, stream))
    graph.add_node("discover", _make_discover_node(discovery, stream))
    graph.add_node("search", _make_search_node(search_agent, stream))
    graph.add_node("compare", _make_compare_node(comparison, stream))
    graph.add_node("optimize", _make_optimize_node(optimizer, stream))
    graph.add_node("present", _make_present_node(stream))
    graph.add_node("wait_for_confirmation", _make_wait_node(stream))
    graph.add_node("checkout", _make_checkout_node(checkout_agent, stream))
    graph.add_node("complete", _make_complete_node(stream))
    graph.add_node("fail", _fail_node)

    # Entry point
    graph.set_entry_point("plan")

    # Edges
    graph.add_edge("plan", "discover")

    graph.add_conditional_edges(
        "discover",
        _after_discover,
        {"search": "search", "fail": "fail"},
    )

    graph.add_edge("search", "compare")

    graph.add_conditional_edges(
        "compare",
        _after_compare,
        {"optimize": "optimize", "present": "present"},
    )

    graph.add_edge("optimize", "present")
    graph.add_edge("present", "wait_for_confirmation")

    graph.add_conditional_edges(
        "wait_for_confirmation",
        _after_wait,
        {"checkout": "checkout", "fail": "fail"},
    )

    graph.add_edge("checkout", "complete")

    # Terminal nodes
    graph.add_edge("complete", END)
    graph.add_edge("fail", END)

    return graph


def compile_shopping_graph(
    settings: Settings,
    stream: ShoppingEventStream,
):
    """Build and compile the shopping graph into a runnable."""
    graph = build_shopping_graph(settings, stream)
    return graph.compile()
