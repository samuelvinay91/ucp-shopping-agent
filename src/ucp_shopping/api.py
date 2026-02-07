"""FastAPI application for the UCP Shopping Agent.

Exposes REST endpoints for:
- Shopping session management (create, status, confirm, cancel)
- SSE streaming of shopping workflow progress
- Price comparison and split-order optimization
- Merchant discovery and catalog browsing
- Order tracking
- MCP tool surface
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from common import ErrorResponse, HealthResponse

from ucp_shopping.agents.comparison_agent import ComparisonAgent
from ucp_shopping.agents.discovery_agent import DiscoveryAgent
from ucp_shopping.agents.optimizer import SplitOrderOptimizer
from ucp_shopping.agents.search_agent import SearchAgent
from ucp_shopping.config import Settings
from ucp_shopping.models import (
    ComparisonMatrix,
    MerchantInfo,
    OrderSummary,
    ShoppingPreferences,
    ShoppingRequest,
    ShoppingSession,
    ShoppingSessionState,
    SplitOrderPlan,
)
from ucp_shopping.orchestrator.graph import compile_shopping_graph
from ucp_shopping.orchestrator.state import ShoppingGraphState
from ucp_shopping.protocols.mcp_surface import MCPToolHandler, list_tools
from ucp_shopping.streaming import ShoppingEventStream

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ShopRequest(BaseModel):
    """Incoming shopping request."""

    query: str
    budget: float | None = None
    preferences: ShoppingPreferences = Field(default_factory=ShoppingPreferences)


class CompareRequest(BaseModel):
    """Price comparison request."""

    product_query: str
    max_results_per_merchant: int = 10


class OptimizeRequest(BaseModel):
    """Split-order optimization request."""

    session_id: str


class DiscoverRequest(BaseModel):
    """Merchant discovery request."""

    urls: list[str]


class ToolExecuteRequest(BaseModel):
    """MCP tool execution request."""

    arguments: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Session manager (in-memory)
# ---------------------------------------------------------------------------


class SessionManager:
    """In-memory shopping session store."""

    def __init__(self) -> None:
        self._sessions: dict[str, ShoppingSession] = {}
        self._graph_tasks: dict[str, asyncio.Task[Any]] = {}

    async def create_session(self, request: ShoppingRequest) -> ShoppingSession:
        """Create a new shopping session."""
        session_id = str(uuid.uuid4())
        session = ShoppingSession(
            id=session_id,
            request=request,
            state=ShoppingSessionState.PLANNING,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> ShoppingSession | None:
        """Retrieve a session by ID."""
        return self._sessions.get(session_id)

    def update_session(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> ShoppingSession | None:
        """Update session fields."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        for key, value in kwargs.items():
            if hasattr(session, key):
                setattr(session, key, value)
        session.updated_at = datetime.now(tz=timezone.utc)
        return session

    def list_sessions(self) -> list[ShoppingSession]:
        """Return all sessions."""
        return list(self._sessions.values())


# ---------------------------------------------------------------------------
# Application state container
# ---------------------------------------------------------------------------


class AppState:
    """Shared application state accessible from route handlers and MCP tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session_manager = SessionManager()
        self.event_stream = ShoppingEventStream()
        self.merchants: dict[str, MerchantInfo] = {}
        self.orders: dict[str, OrderSummary] = {}
        self.graph_confirmations: dict[str, asyncio.Event] = {}


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = settings or Settings()

    app = FastAPI(
        title="UCP Shopping Agent",
        description=(
            "AI-powered shopping assistant that discovers UCP merchants, "
            "compares prices, and orchestrates multi-merchant purchases."
        ),
        version=settings.service_version,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Shared state
    state = AppState(settings)
    app.state.app_state = state
    app.state.settings = settings

    mcp_handler = MCPToolHandler(state)

    # -------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="healthy",
            service=settings.service_name,
            version=settings.service_version,
        )

    # -------------------------------------------------------------------
    # Shopping session endpoints
    # -------------------------------------------------------------------

    @app.post("/api/v1/shop", tags=["shopping"])
    async def create_shopping_session(req: ShopRequest) -> dict[str, Any]:
        """Submit a new shopping request.

        Creates a session and kicks off the LangGraph workflow
        asynchronously.  Use the ``/stream`` endpoint to follow progress.
        """
        budget = Decimal(str(req.budget)) if req.budget is not None else None
        shopping_req = ShoppingRequest(
            query=req.query,
            budget=budget,
            preferences=req.preferences,
        )
        session = await state.session_manager.create_session(shopping_req)

        # Run the graph asynchronously
        confirmation_event = asyncio.Event()
        state.graph_confirmations[session.id] = confirmation_event

        async def _run_graph() -> None:
            """Execute the shopping graph, pausing at confirmation gate."""
            compiled = compile_shopping_graph(settings, state.event_stream)

            initial: ShoppingGraphState = {
                "request": shopping_req,
                "session_id": session.id,
                "preferences": req.preferences,
                "current_state": ShoppingSessionState.PLANNING,
                "shopping_plan": None,
                "discovered_merchants": [],
                "failed_merchants": [],
                "search_results": {},
                "merged_results": [],
                "comparison_matrix": None,
                "optimization_plan": None,
                "user_confirmed": False,
                "active_checkouts": [],
                "completed_orders": [],
                "error": None,
                "messages": [],
            }

            # Run up to the confirmation gate
            result = await compiled.ainvoke(initial)

            # Persist intermediate state on the session
            current_state = result.get("current_state", ShoppingSessionState.FAILED)
            state.session_manager.update_session(
                session.id,
                state=current_state,
                merchants=result.get("discovered_merchants", []),
                search_results=result.get("search_results", {}),
                comparison=result.get("comparison_matrix"),
                optimization_plan=result.get("optimization_plan"),
                error=result.get("error"),
            )

            # Store discovered merchants globally
            for m in result.get("discovered_merchants", []):
                state.merchants[m.id] = m

            if current_state == ShoppingSessionState.AWAITING_CONFIRMATION:
                if settings.human_confirmation_required:
                    # Wait for user confirmation
                    logger.info("awaiting_confirmation", session_id=session.id)
                    await confirmation_event.wait()
                    logger.info("confirmation_received", session_id=session.id)

                # Re-run with confirmation
                result["user_confirmed"] = True
                result = await compiled.ainvoke(result)

                # Persist final state
                final_state = result.get(
                    "current_state", ShoppingSessionState.COMPLETED
                )
                orders = result.get("completed_orders", [])
                state.session_manager.update_session(
                    session.id,
                    state=final_state,
                    orders=orders,
                    error=result.get("error"),
                )
                for order in orders:
                    state.orders[order.order_id] = order

        task = asyncio.create_task(_run_graph())
        state.session_manager._graph_tasks[session.id] = task

        return {
            "session_id": session.id,
            "status": session.state.value,
            "message": f"Shopping session created for: {req.query}",
            "stream_url": f"/api/v1/shop/{session.id}/stream",
        }

    @app.get("/api/v1/shop/{session_id}", tags=["shopping"])
    async def get_shopping_session(session_id: str) -> dict[str, Any]:
        """Get the current state of a shopping session."""
        session = state.session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return session.model_dump()

    @app.get("/api/v1/shop/{session_id}/stream", tags=["shopping"])
    async def stream_shopping_session(session_id: str) -> EventSourceResponse:
        """SSE stream of shopping workflow events."""
        session = state.session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        async def event_generator():  # type: ignore[no-untyped-def]
            async for event in state.event_stream.subscribe(session_id):
                yield {
                    "event": event.event_type,
                    "data": json.dumps(event.model_dump(), default=str),
                }

        return EventSourceResponse(event_generator())

    @app.post("/api/v1/shop/{session_id}/confirm", tags=["shopping"])
    async def confirm_shopping_session(session_id: str) -> dict[str, Any]:
        """Confirm the shopping plan and proceed to checkout."""
        session = state.session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        if session.state != ShoppingSessionState.AWAITING_CONFIRMATION:
            raise HTTPException(
                status_code=400,
                detail=f"Session is in state '{session.state.value}', not awaiting confirmation.",
            )

        # Signal the graph to continue
        confirmation_event = state.graph_confirmations.get(session_id)
        if confirmation_event:
            confirmation_event.set()

        return {
            "session_id": session_id,
            "status": "confirmed",
            "message": "Order confirmed. Proceeding to checkout.",
        }

    @app.post("/api/v1/shop/{session_id}/cancel", tags=["shopping"])
    async def cancel_shopping_session(session_id: str) -> dict[str, Any]:
        """Cancel a shopping session."""
        session = state.session_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        # Cancel the graph task
        task = state.session_manager._graph_tasks.get(session_id)
        if task and not task.done():
            task.cancel()

        state.session_manager.update_session(
            session_id,
            state=ShoppingSessionState.FAILED,
            error="Cancelled by user.",
        )
        state.event_stream.close(session_id)

        return {
            "session_id": session_id,
            "status": "cancelled",
            "message": "Shopping session cancelled.",
        }

    # -------------------------------------------------------------------
    # Comparison and optimization endpoints
    # -------------------------------------------------------------------

    @app.post("/api/v1/compare", tags=["comparison"])
    async def compare_prices(req: CompareRequest) -> dict[str, Any]:
        """Compare prices for a product across all known merchants."""
        discovery = DiscoveryAgent(settings)
        merchants = await discovery.discover_merchants()

        search_agent = SearchAgent(settings)
        results = await search_agent.search_all_merchants(
            merchants, [req.product_query]
        )

        comparison = ComparisonAgent()
        matrix = await comparison.build_comparison(results, [req.product_query])

        return matrix.model_dump()

    @app.post("/api/v1/optimize", tags=["comparison"])
    async def optimize_order(req: OptimizeRequest) -> dict[str, Any]:
        """Run split-order optimization on an existing session's comparison."""
        session = state.session_manager.get_session(req.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.comparison is None:
            raise HTTPException(
                status_code=400, detail="No comparison matrix available for this session."
            )

        optimizer = SplitOrderOptimizer()
        plan = await optimizer.optimize(
            session.comparison, session.request.preferences
        )
        state.session_manager.update_session(
            req.session_id, optimization_plan=plan
        )
        return plan.model_dump()

    # -------------------------------------------------------------------
    # Merchant endpoints
    # -------------------------------------------------------------------

    @app.get("/api/v1/merchants", tags=["merchants"])
    async def list_merchants() -> dict[str, Any]:
        """List all known merchants."""
        # Do a fresh discovery if none are cached
        if not state.merchants:
            discovery = DiscoveryAgent(settings)
            merchants = await discovery.discover_merchants()
            for m in merchants:
                state.merchants[m.id] = m

        return {
            "merchants": [m.model_dump() for m in state.merchants.values()],
            "total": len(state.merchants),
        }

    @app.post("/api/v1/merchants/discover", tags=["merchants"])
    async def discover_merchants(req: DiscoverRequest) -> dict[str, Any]:
        """Discover new UCP merchants at the given URLs."""
        discovery = DiscoveryAgent(settings)
        merchants = await discovery.discover_merchants(req.urls)

        for m in merchants:
            state.merchants[m.id] = m

        return {
            "discovered": len(merchants),
            "merchants": [m.model_dump() for m in merchants],
        }

    @app.get("/api/v1/merchants/{merchant_id}/catalog", tags=["merchants"])
    async def browse_merchant_catalog(
        merchant_id: str,
        q: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Browse a specific merchant's product catalog."""
        merchant = state.merchants.get(merchant_id)
        if merchant is None:
            raise HTTPException(
                status_code=404, detail=f"Merchant {merchant_id} not found"
            )

        search_agent = SearchAgent(settings)
        results = await search_agent.search_all_merchants(
            [merchant], [q or ""], filters=None
        )

        products = results.get(merchant_id, [])
        return {
            "merchant_id": merchant_id,
            "merchant_name": merchant.name,
            "products": [p.model_dump() for p in products[:limit]],
            "total": len(products),
        }

    # -------------------------------------------------------------------
    # Order endpoints
    # -------------------------------------------------------------------

    @app.get("/api/v1/orders", tags=["orders"])
    async def list_orders() -> dict[str, Any]:
        """List all completed orders."""
        return {
            "orders": [o.model_dump() for o in state.orders.values()],
            "total": len(state.orders),
        }

    @app.get("/api/v1/orders/{order_id}", tags=["orders"])
    async def get_order(order_id: str) -> dict[str, Any]:
        """Get details of a specific order."""
        order = state.orders.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
        return order.model_dump()

    # -------------------------------------------------------------------
    # MCP tool endpoints
    # -------------------------------------------------------------------

    @app.post("/api/v1/mcp/tools", tags=["mcp"])
    async def mcp_list_tools() -> dict[str, Any]:
        """List all available MCP tools."""
        tools = list_tools()
        return {"tools": tools, "total": len(tools)}

    @app.post("/api/v1/mcp/tools/{tool_name}/execute", tags=["mcp"])
    async def mcp_execute_tool(
        tool_name: str, req: ToolExecuteRequest
    ) -> dict[str, Any]:
        """Execute an MCP tool by name."""
        result = await mcp_handler.execute(tool_name, req.arguments)
        return result.model_dump()

    # -------------------------------------------------------------------
    # Error handlers
    # -------------------------------------------------------------------

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "unhandled_exception", error=str(exc), path=request.url.path
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="Internal server error",
                detail=str(exc),
                status_code=500,
            ).model_dump(),
        )

    return app
