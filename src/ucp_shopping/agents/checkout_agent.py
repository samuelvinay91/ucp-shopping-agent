"""Multi-merchant checkout specialist agent.

Executes parallel checkouts across merchants, handling partial failures
gracefully and aggregating order confirmations.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import structlog

from ucp_shopping.config import Settings
from ucp_shopping.models import (
    MerchantInfo,
    OrderSummary,
    SplitOrderItem,
    SplitOrderPlan,
)
from ucp_shopping.protocols.ucp_client import UCPClient, UCPClientError
from ucp_shopping.streaming import EVENT_CHECKOUT_PROGRESS, ShoppingEventStream

logger = structlog.get_logger(__name__)


class CheckoutAgent:
    """Orchestrates checkouts across multiple merchants."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ucp_client = UCPClient(timeout=settings.checkout_timeout)

    async def execute_checkouts(
        self,
        plan: SplitOrderPlan,
        merchants: dict[str, MerchantInfo],
        stream: ShoppingEventStream | None = None,
        session_id: str = "",
    ) -> list[OrderSummary]:
        """Execute checkouts at all merchants in the plan.

        Groups items by merchant, creates checkout sessions, updates them
        with shipping details, and completes the orders.  Handles partial
        failures so that one merchant's error does not block others.

        Parameters
        ----------
        plan:
            The optimized split-order plan.
        merchants:
            Mapping of merchant_id -> MerchantInfo.
        stream:
            Optional SSE stream for progress events.
        session_id:
            Shopping session ID for SSE events.

        Returns
        -------
        list[OrderSummary]
            Completed orders.
        """
        # Group items by merchant
        merchant_items: dict[str, list[SplitOrderItem]] = defaultdict(list)
        for item in plan.items:
            merchant_items[item.merchant_id].append(item)

        # Execute checkouts in parallel
        tasks: list[asyncio.Task[OrderSummary | None]] = []
        for merchant_id, items in merchant_items.items():
            merchant = merchants.get(merchant_id)
            if merchant is None:
                logger.warning(
                    "checkout_merchant_not_found",
                    merchant_id=merchant_id,
                )
                continue

            task = asyncio.create_task(
                self._checkout_one_merchant(
                    merchant=merchant,
                    items=items,
                    stream=stream,
                    session_id=session_id,
                )
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        orders: list[OrderSummary] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("checkout_task_failed", error=str(result))
                continue
            if result is not None:
                orders.append(result)

        logger.info(
            "checkouts_complete",
            total_merchants=len(merchant_items),
            successful_orders=len(orders),
        )
        return orders

    async def _checkout_one_merchant(
        self,
        merchant: MerchantInfo,
        items: list[SplitOrderItem],
        stream: ShoppingEventStream | None = None,
        session_id: str = "",
    ) -> OrderSummary | None:
        """Execute the full checkout lifecycle for one merchant.

        Steps:
        1. Create checkout session with line items.
        2. Update session with shipping address.
        3. Complete the checkout.
        4. Return an OrderSummary.
        """
        merchant_url = merchant.url

        try:
            # 1. Create checkout session
            line_items = [
                {
                    "product_id": item.product_id,
                    "quantity": 1,
                }
                for item in items
            ]

            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={"merchant": merchant.name, "step": "creating_session"},
                    message=f"Creating checkout at {merchant.name}...",
                )

            checkout = await self._ucp_client.create_checkout(
                merchant_url, line_items
            )
            checkout_session_id = checkout.get("id", "")

            # 2. Update with shipping address (mock address for demo)
            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={"merchant": merchant.name, "step": "updating_shipping"},
                    message=f"Setting shipping details at {merchant.name}...",
                )

            await self._ucp_client.update_checkout(
                merchant_url,
                checkout_session_id,
                {
                    "shipping_address": {
                        "full_name": "Demo User",
                        "line1": "123 AI Street",
                        "city": "San Francisco",
                        "state": "CA",
                        "postal_code": "94105",
                        "country": "US",
                    },
                    "selected_shipping_id": "standard",
                },
            )

            # 3. Complete checkout
            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={"merchant": merchant.name, "step": "completing"},
                    message=f"Completing order at {merchant.name}...",
                )

            completion = await self._ucp_client.complete_checkout(
                merchant_url, checkout_session_id
            )

            order_id = completion.get("order_id", completion.get("id", checkout_session_id))
            total = sum(i.total for i in items)

            order = OrderSummary(
                merchant_name=merchant.name,
                merchant_id=merchant.id,
                order_id=order_id,
                items=items,
                total=round(total, 2),
                status="confirmed",
                tracking_url=completion.get("tracking_url"),
                created_at=datetime.now(tz=timezone.utc),
            )

            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={
                        "merchant": merchant.name,
                        "step": "completed",
                        "order_id": order_id,
                    },
                    message=f"Order {order_id} confirmed at {merchant.name}.",
                )

            logger.info(
                "merchant_checkout_complete",
                merchant=merchant.name,
                order_id=order_id,
                total=total,
            )
            return order

        except UCPClientError as exc:
            logger.error(
                "merchant_checkout_failed",
                merchant=merchant.name,
                error=str(exc),
            )
            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={"merchant": merchant.name, "step": "failed", "error": str(exc)},
                    message=f"Checkout failed at {merchant.name}: {exc}",
                )
            return None

        except Exception as exc:
            logger.error(
                "merchant_checkout_unexpected_error",
                merchant=merchant.name,
                error=str(exc),
            )
            if stream:
                await stream.emit(
                    session_id,
                    EVENT_CHECKOUT_PROGRESS,
                    data={"merchant": merchant.name, "step": "failed", "error": str(exc)},
                    message=f"Checkout error at {merchant.name}: {exc}",
                )
            return None

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._ucp_client.close()
