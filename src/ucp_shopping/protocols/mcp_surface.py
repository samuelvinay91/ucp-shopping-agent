"""MCP tool surface for the UCP Shopping Agent.

Exposes the shopping agent's capabilities as MCP-compatible tools that can
be discovered and invoked by LLM tool-use or MCP clients.
"""

from __future__ import annotations

from typing import Any

import structlog

from ucp_shopping.models import MCPToolDefinition, MCPToolResult

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SHOPPING_TOOLS: list[MCPToolDefinition] = [
    MCPToolDefinition(
        name="shop",
        description=(
            "Submit a natural-language shopping request. Discovers merchants, "
            "searches for products, compares prices, and creates an optimized "
            "purchase plan."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language shopping query (e.g. 'Find me a mechanical keyboard under $150')",
                },
                "budget": {
                    "type": "number",
                    "description": "Maximum budget in USD (optional)",
                },
                "prefer_single_merchant": {
                    "type": "boolean",
                    "description": "If true, prefer buying everything from one merchant",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    ),
    MCPToolDefinition(
        name="compare_prices",
        description=(
            "Compare prices for a specific product across all known merchants."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "product_query": {
                    "type": "string",
                    "description": "Product to search for (e.g. 'wireless mouse')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results per merchant",
                    "default": 5,
                },
            },
            "required": ["product_query"],
        },
    ),
    MCPToolDefinition(
        name="find_best_deal",
        description=(
            "Find the single best deal for a product considering price, "
            "shipping cost, and delivery time."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "product_query": {
                    "type": "string",
                    "description": "Product to find the best deal for",
                },
                "max_shipping_days": {
                    "type": "integer",
                    "description": "Maximum acceptable shipping days (optional)",
                },
                "prefer_free_shipping": {
                    "type": "boolean",
                    "description": "Prefer options with free shipping",
                    "default": False,
                },
            },
            "required": ["product_query"],
        },
    ),
    MCPToolDefinition(
        name="discover_merchants",
        description=(
            "Discover UCP-compliant merchants at the given URLs and add "
            "them to the known merchant list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of merchant base URLs to discover",
                },
            },
            "required": ["urls"],
        },
    ),
    MCPToolDefinition(
        name="track_orders",
        description="Get the status of all recent orders or a specific order.",
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Specific order ID to track (optional, omit for all orders)",
                },
            },
            "required": [],
        },
    ),
    MCPToolDefinition(
        name="get_shopping_status",
        description="Get the current status of an active shopping session.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Shopping session ID",
                },
            },
            "required": ["session_id"],
        },
    ),
]


def list_tools() -> list[dict[str, Any]]:
    """Return all MCP tool definitions as dicts."""
    return [tool.model_dump() for tool in SHOPPING_TOOLS]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


class MCPToolHandler:
    """Dispatches MCP tool calls to the appropriate shopping agent function.

    The handler holds a reference to shared application state (sessions,
    merchants, orders) so that tool invocations can read and mutate it.
    """

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Execute the named tool with the provided arguments.

        Parameters
        ----------
        tool_name:
            One of the registered tool names.
        arguments:
            Tool arguments matching the JSON schema.

        Returns
        -------
        MCPToolResult
            Execution result.
        """
        handler_map = {
            "shop": self._handle_shop,
            "compare_prices": self._handle_compare_prices,
            "find_best_deal": self._handle_find_best_deal,
            "discover_merchants": self._handle_discover_merchants,
            "track_orders": self._handle_track_orders,
            "get_shopping_status": self._handle_get_shopping_status,
        }

        handler = handler_map.get(tool_name)
        if handler is None:
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Unknown tool: {tool_name}",
            )

        try:
            result = await handler(arguments)
            return MCPToolResult(
                tool_name=tool_name,
                success=True,
                result=result,
            )
        except Exception as exc:
            logger.error("mcp_tool_execution_failed", tool=tool_name, error=str(exc))
            return MCPToolResult(
                tool_name=tool_name,
                success=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Individual tool handlers
    # ------------------------------------------------------------------

    async def _handle_shop(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``shop`` tool -- create a shopping session."""
        from ucp_shopping.models import ShoppingPreferences, ShoppingRequest

        query = arguments["query"]
        budget = arguments.get("budget")
        prefs = ShoppingPreferences(
            prefer_single_merchant=arguments.get("prefer_single_merchant", False),
        )
        request = ShoppingRequest(
            query=query,
            budget=budget,
            preferences=prefs,
        )

        # Access the session manager from app state
        session_mgr = self._state.session_manager
        session = await session_mgr.create_session(request)
        return {
            "session_id": session.id,
            "status": session.state.value,
            "message": f"Shopping session created for: {query}",
        }

    async def _handle_compare_prices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``compare_prices`` tool."""
        from ucp_shopping.agents.comparison_agent import ComparisonAgent
        from ucp_shopping.agents.discovery_agent import DiscoveryAgent
        from ucp_shopping.agents.search_agent import SearchAgent

        product_query = arguments["product_query"]
        settings = self._state.settings

        # Quick discovery + search + compare pipeline
        discovery = DiscoveryAgent(settings)
        merchants = await discovery.discover_merchants()

        search_agent = SearchAgent(settings)
        results = await search_agent.search_all_merchants(merchants, [product_query])

        comparison = ComparisonAgent()
        matrix = await comparison.build_comparison(results, [product_query])

        return {
            "query": product_query,
            "merchants_searched": len(merchants),
            "total_results": matrix.total_products_found,
            "entries": [
                {
                    "product_query": entry.product_query,
                    "options": [
                        {
                            "name": r.name,
                            "price": r.price,
                            "merchant": r.merchant_name,
                            "in_stock": r.in_stock,
                        }
                        for r in entry.merchant_results
                    ],
                    "best_price": (
                        {
                            "name": entry.best_price.name,
                            "price": entry.best_price.price,
                            "merchant": entry.best_price.merchant_name,
                        }
                        if entry.best_price
                        else None
                    ),
                }
                for entry in matrix.entries
            ],
        }

    async def _handle_find_best_deal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``find_best_deal`` tool."""
        # Reuse compare_prices and pick the best option
        compare_result = await self._handle_compare_prices(
            {"product_query": arguments["product_query"]}
        )

        best_deal: dict[str, Any] | None = None
        for entry in compare_result.get("entries", []):
            bp = entry.get("best_price")
            if bp and (best_deal is None or bp["price"] < best_deal["price"]):
                best_deal = bp

        return {
            "query": arguments["product_query"],
            "best_deal": best_deal,
            "total_options_compared": compare_result.get("total_results", 0),
        }

    async def _handle_discover_merchants(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``discover_merchants`` tool."""
        from ucp_shopping.agents.discovery_agent import DiscoveryAgent

        urls = arguments["urls"]
        discovery = DiscoveryAgent(self._state.settings)
        merchants = await discovery.discover_merchants(urls)

        return {
            "discovered": len(merchants),
            "merchants": [
                {"id": m.id, "name": m.name, "url": m.url, "capabilities": m.capabilities}
                for m in merchants
            ],
        }

    async def _handle_track_orders(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``track_orders`` tool."""
        order_id = arguments.get("order_id")
        orders = self._state.orders

        if order_id:
            order = orders.get(order_id)
            if order:
                return {"order": order.model_dump()}
            return {"error": f"Order {order_id} not found"}

        return {
            "orders": [o.model_dump() for o in orders.values()],
            "total": len(orders),
        }

    async def _handle_get_shopping_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Handle the ``get_shopping_status`` tool."""
        session_id = arguments["session_id"]
        session_mgr = self._state.session_manager
        session = session_mgr.get_session(session_id)

        if session is None:
            return {"error": f"Session {session_id} not found"}

        return {
            "session_id": session.id,
            "state": session.state.value,
            "merchants": len(session.merchants),
            "search_results": {k: len(v) for k, v in session.search_results.items()},
            "has_comparison": session.comparison is not None,
            "has_plan": session.optimization_plan is not None,
            "orders": len(session.orders),
            "error": session.error,
        }
