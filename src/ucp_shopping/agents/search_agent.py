"""Product search specialist agent.

Executes parallel searches across all discovered merchants and normalises
the results into a consistent format.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ucp_shopping.config import Settings
from ucp_shopping.models import MerchantInfo, ProductResult
from ucp_shopping.protocols.ucp_client import UCPClient, UCPClientError

logger = structlog.get_logger(__name__)


class SearchAgent:
    """Searches products across multiple UCP merchants in parallel."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ucp_client = UCPClient(timeout=settings.comparison_timeout)

    async def search_all_merchants(
        self,
        merchants: list[MerchantInfo],
        queries: list[str],
        filters: dict[str, Any] | None = None,
    ) -> dict[str, list[ProductResult]]:
        """Search all merchants for the given queries.

        Executes queries in parallel across all merchants.  Results are
        keyed by merchant ID.

        Parameters
        ----------
        merchants:
            Discovered merchants to search.
        queries:
            Search queries (one per item in the shopping plan).
        filters:
            Optional global filters (category, price range).

        Returns
        -------
        dict[str, list[ProductResult]]
            Mapping of merchant_id -> list of product results.
        """
        tasks: list[asyncio.Task[tuple[str, list[ProductResult]]]] = []

        for merchant in merchants:
            for query in queries:
                task = asyncio.create_task(
                    self._search_one_merchant(merchant, query, filters)
                )
                tasks.append(task)

        results_tuples = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate results by merchant
        merchant_results: dict[str, list[ProductResult]] = {}
        for result in results_tuples:
            if isinstance(result, Exception):
                logger.warning("search_task_failed", error=str(result))
                continue
            merchant_id, products = result
            if merchant_id not in merchant_results:
                merchant_results[merchant_id] = []
            merchant_results[merchant_id].extend(products)

        # Deduplicate within each merchant
        for merchant_id in merchant_results:
            merchant_results[merchant_id] = self._deduplicate(
                merchant_results[merchant_id]
            )

        logger.info(
            "search_complete",
            merchants=len(merchant_results),
            total_products=sum(len(v) for v in merchant_results.values()),
        )
        return merchant_results

    async def _search_one_merchant(
        self,
        merchant: MerchantInfo,
        query: str,
        filters: dict[str, Any] | None = None,
    ) -> tuple[str, list[ProductResult]]:
        """Search a single merchant for a single query.

        Returns
        -------
        tuple[str, list[ProductResult]]
            The merchant ID and the list of matching products.
        """
        try:
            products = await self._ucp_client.search_products(
                merchant.url,
                query,
                filters=filters,
                limit=self._settings.max_results_per_merchant,
            )

            # Ensure merchant info is populated on each result
            for product in products:
                if not product.merchant_id:
                    product.merchant_id = merchant.id
                if not product.merchant_name:
                    product.merchant_name = merchant.name

            logger.debug(
                "merchant_search_complete",
                merchant=merchant.name,
                query=query,
                results=len(products),
            )
            return merchant.id, products

        except UCPClientError as exc:
            logger.warning(
                "merchant_search_failed",
                merchant=merchant.name,
                query=query,
                error=str(exc),
            )
            return merchant.id, []

    @staticmethod
    def _deduplicate(products: list[ProductResult]) -> list[ProductResult]:
        """Remove duplicate products (by product_id) keeping the first occurrence."""
        seen: set[str] = set()
        unique: list[ProductResult] = []
        for product in products:
            key = f"{product.merchant_id}:{product.product_id}"
            if key not in seen:
                seen.add(key)
                unique.append(product)
        return unique

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._ucp_client.close()
