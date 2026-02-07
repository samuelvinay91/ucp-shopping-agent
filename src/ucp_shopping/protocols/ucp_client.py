"""UCP protocol client for communicating with UCP-compliant merchants.

Handles discovery (``/.well-known/ucp``), capability negotiation, product
search, and the full checkout lifecycle.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ucp_shopping.models import MerchantInfo, ProductResult, ShippingOption

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 15.0
_MAX_RETRIES = 2


class UCPClientError(Exception):
    """Raised when a UCP request fails after retries."""


class UCPClient:
    """Async HTTP client for the Universal Commerce Protocol."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialise the shared ``httpx.AsyncClient``."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Request helper with retry
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an HTTP request with retries and error mapping."""
        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                )
                response.raise_for_status()
                return response.json()
            except httpx.TimeoutException as exc:
                last_error = exc
                logger.warning(
                    "ucp_request_timeout",
                    url=url,
                    attempt=attempt + 1,
                )
            except httpx.HTTPStatusError as exc:
                # Don't retry 4xx errors
                if 400 <= exc.response.status_code < 500:
                    raise UCPClientError(
                        f"UCP request failed ({exc.response.status_code}): {exc.response.text}"
                    ) from exc
                last_error = exc
                logger.warning(
                    "ucp_request_http_error",
                    url=url,
                    status=exc.response.status_code,
                    attempt=attempt + 1,
                )
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning(
                    "ucp_request_error",
                    url=url,
                    error=str(exc),
                    attempt=attempt + 1,
                )

        raise UCPClientError(
            f"UCP request to {url} failed after {self._max_retries + 1} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self, merchant_url: str) -> MerchantInfo:
        """Fetch and parse a merchant's ``/.well-known/ucp`` manifest.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant (e.g. ``http://localhost:8020/merchants/techzone``).

        Returns
        -------
        MerchantInfo
            Parsed merchant information.
        """
        url = f"{merchant_url.rstrip('/')}/.well-known/ucp"
        data = await self._request("GET", url)

        capabilities = [
            cap.get("id", "") for cap in data.get("capabilities", [])
        ]
        extensions = [
            ext.get("id", "") for ext in data.get("extensions", [])
        ]
        endpoints = data.get("endpoints", {})

        # Detect free-shipping threshold from metadata
        free_shipping = data.get("metadata", {}).get("free_shipping_threshold")

        return MerchantInfo(
            id=data.get("merchant_id", ""),
            name=data.get("merchant_name", "Unknown"),
            url=merchant_url,
            capabilities=capabilities,
            extensions=extensions,
            status="active",
            base_url=data.get("base_url", merchant_url),
            endpoints=endpoints,
            free_shipping_threshold=free_shipping,
            metadata=data.get("metadata", {}),
        )

    # ------------------------------------------------------------------
    # Negotiation
    # ------------------------------------------------------------------

    async def negotiate(
        self,
        merchant_url: str,
        agent_capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Negotiate capabilities with a merchant.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        agent_capabilities:
            The shopping agent's capability profile.

        Returns
        -------
        dict
            Negotiation result from the merchant.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/negotiate"
        body = agent_capabilities or {
            "agent_id": "ucp-shopping-agent",
            "agent_name": "UCP Shopping Agent",
            "requested_capabilities": ["catalog.search", "checkout"],
            "supported_payment_handlers": ["mock_payment"],
        }
        return await self._request("POST", url, json_body=body)

    # ------------------------------------------------------------------
    # Catalog / product search
    # ------------------------------------------------------------------

    async def search_products(
        self,
        merchant_url: str,
        query: str,
        filters: dict[str, Any] | None = None,
        limit: int = 20,
    ) -> list[ProductResult]:
        """Search a merchant's product catalog.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        query:
            Free-text search query.
        filters:
            Optional filters (category, brand, price range).
        limit:
            Max results to return.

        Returns
        -------
        list[ProductResult]
            Products matching the query.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/catalog/products"
        params: dict[str, Any] = {"q": query, "limit": limit}
        if filters:
            if "category" in filters:
                params["category"] = filters["category"]
            if "min_price" in filters:
                params["min_price"] = filters["min_price"]
            if "max_price" in filters:
                params["max_price"] = filters["max_price"]

        data = await self._request("GET", url, params=params)
        products_data = data.get("products", [])

        results: list[ProductResult] = []
        for p in products_data:
            # Parse shipping options
            shipping_options: list[ShippingOption] = []
            for so in p.get("shipping_options", []):
                shipping_options.append(
                    ShippingOption(
                        id=so.get("id", "standard"),
                        name=so.get("name", "Standard"),
                        price=so.get("price", 5.99),
                        estimated_days_min=so.get("estimated_days_min", 3),
                        estimated_days_max=so.get("estimated_days_max", 7),
                        is_free=so.get("is_free", False),
                    )
                )

            # Parse price (could be nested Money object or flat)
            price_val = p.get("price", 0)
            if isinstance(price_val, dict):
                price_val = price_val.get("amount", 0)

            results.append(
                ProductResult(
                    product_id=p.get("id", ""),
                    name=p.get("name", ""),
                    description=p.get("description", ""),
                    price=float(price_val),
                    merchant_id=p.get("merchant_id", ""),
                    merchant_name=p.get("merchant_name", ""),
                    category=p.get("category", ""),
                    brand=p.get("brand", ""),
                    shipping_options=shipping_options,
                    in_stock=p.get("in_stock", p.get("stock", 0) > 0),
                    stock_quantity=p.get("stock", 0),
                    url=p.get("url", ""),
                    specs=p.get("specs", {}),
                    rating=p.get("rating", 0.0),
                )
            )

        return results

    # ------------------------------------------------------------------
    # Checkout lifecycle
    # ------------------------------------------------------------------

    async def create_checkout(
        self,
        merchant_url: str,
        line_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create a new checkout session at the merchant.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        line_items:
            List of ``{"product_id": ..., "quantity": ...}`` dicts.

        Returns
        -------
        dict
            The created checkout session.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/checkout/sessions"
        return await self._request("POST", url, json_body={"line_items": line_items})

    async def update_checkout(
        self,
        merchant_url: str,
        session_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an existing checkout session (address, shipping, etc.).

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        session_id:
            ID of the checkout session.
        updates:
            Fields to update.

        Returns
        -------
        dict
            The updated checkout session.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/checkout/sessions/{session_id}"
        return await self._request("PUT", url, json_body=updates)

    async def complete_checkout(
        self,
        merchant_url: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Complete a checkout session and place the order.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        session_id:
            ID of the checkout session.

        Returns
        -------
        dict
            The completed order details.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/checkout/sessions/{session_id}/complete"
        return await self._request("POST", url)

    async def get_order(
        self,
        merchant_url: str,
        order_id: str,
    ) -> dict[str, Any]:
        """Retrieve order details from a merchant.

        Parameters
        ----------
        merchant_url:
            Base URL of the merchant.
        order_id:
            ID of the order.

        Returns
        -------
        dict
            Order details.
        """
        url = f"{merchant_url.rstrip('/')}/api/v1/orders/{order_id}"
        return await self._request("GET", url)
