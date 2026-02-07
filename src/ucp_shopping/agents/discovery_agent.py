"""Merchant discovery specialist agent.

Parallelises UCP manifest fetches across candidate merchant URLs and ranks
the results by capability match.
"""

from __future__ import annotations

import asyncio

import structlog

from ucp_shopping.config import Settings
from ucp_shopping.models import MerchantInfo
from ucp_shopping.protocols.ucp_client import UCPClient, UCPClientError

logger = structlog.get_logger(__name__)


class DiscoveryAgent:
    """Discovers and validates UCP merchants."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ucp_client = UCPClient(timeout=settings.discovery_timeout)

    async def discover_merchants(
        self,
        urls: list[str] | None = None,
    ) -> list[MerchantInfo]:
        """Discover UCP merchants from the provided or default URLs.

        Parameters
        ----------
        urls:
            Merchant base URLs to probe.  Falls back to
            ``settings.known_merchant_urls``.

        Returns
        -------
        list[MerchantInfo]
            Successfully discovered merchants, ranked by capability match.
        """
        target_urls = urls or self._settings.known_merchant_urls
        # Limit to configured max
        target_urls = target_urls[: self._settings.max_merchants]

        tasks = [self._discover_one(url) for url in target_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merchants: list[MerchantInfo] = []
        for url, result in zip(target_urls, results):
            if isinstance(result, MerchantInfo):
                merchants.append(result)
            else:
                logger.warning(
                    "merchant_discovery_failed",
                    url=url,
                    error=str(result),
                )

        # Rank by number of capabilities (more capable merchants first)
        merchants.sort(key=lambda m: len(m.capabilities), reverse=True)

        logger.info(
            "merchants_discovered",
            total=len(merchants),
            names=[m.name for m in merchants],
        )
        return merchants

    async def discover_merchants_with_failures(
        self,
        urls: list[str] | None = None,
    ) -> tuple[list[MerchantInfo], list[str]]:
        """Discover merchants and also return the list of failed URLs.

        Returns
        -------
        tuple[list[MerchantInfo], list[str]]
            A pair of (discovered_merchants, failed_urls).
        """
        target_urls = urls or self._settings.known_merchant_urls
        target_urls = target_urls[: self._settings.max_merchants]

        tasks = [self._discover_one(url) for url in target_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merchants: list[MerchantInfo] = []
        failed: list[str] = []

        for url, result in zip(target_urls, results):
            if isinstance(result, MerchantInfo):
                merchants.append(result)
            else:
                failed.append(url)
                logger.warning(
                    "merchant_discovery_failed",
                    url=url,
                    error=str(result),
                )

        merchants.sort(key=lambda m: len(m.capabilities), reverse=True)
        return merchants, failed

    async def _discover_one(self, url: str) -> MerchantInfo:
        """Fetch and validate a single merchant manifest."""
        try:
            merchant = await self._ucp_client.discover(url)
            # Validate minimum capabilities
            if not merchant.id:
                merchant.id = url.rstrip("/").split("/")[-1]
            if not merchant.name:
                merchant.name = merchant.id
            return merchant
        except UCPClientError:
            raise
        except Exception as exc:
            raise UCPClientError(f"Unexpected error discovering {url}: {exc}") from exc

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._ucp_client.close()
