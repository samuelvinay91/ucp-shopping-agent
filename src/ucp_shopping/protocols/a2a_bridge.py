"""A2A integration bridge for discovering and communicating with A2A agents.

Maps between A2A ``AgentCard`` capabilities and UCP ``MerchantInfo`` so that
A2A-only merchants can participate in the shopping workflow.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import structlog

from ucp_shopping.models import MerchantInfo

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 10.0


class A2ABridge:
    """Bridge between the A2A protocol and the shopping agent's UCP world-view."""

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
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
    # Agent discovery
    # ------------------------------------------------------------------

    async def discover_agents(self, urls: list[str]) -> list[MerchantInfo]:
        """Discover A2A agents at the given URLs.

        Fetches ``/.well-known/agent.json`` from each URL, parses the
        AgentCard, and converts it into a :class:`MerchantInfo`.

        Parameters
        ----------
        urls:
            Base URLs of candidate A2A agents.

        Returns
        -------
        list[MerchantInfo]
            Discovered agents mapped to the UCP merchant schema.
        """
        client = await self._get_client()
        merchants: list[MerchantInfo] = []

        for base_url in urls:
            url = f"{base_url.rstrip('/')}/.well-known/agent.json"
            try:
                response = await client.get(url)
                response.raise_for_status()
                card = response.json()

                # Map A2A AgentCard to MerchantInfo
                capabilities = self._extract_capabilities(card)
                merchant = MerchantInfo(
                    id=card.get("id", str(uuid.uuid4())),
                    name=card.get("name", "Unknown A2A Agent"),
                    url=base_url,
                    capabilities=capabilities,
                    extensions=["a2a"],
                    status="active",
                    base_url=base_url,
                    metadata={
                        "source": "a2a",
                        "description": card.get("description", ""),
                        "skills": card.get("skills", []),
                    },
                )
                merchants.append(merchant)
                logger.info("a2a_agent_discovered", name=merchant.name, url=base_url)

            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "a2a_discovery_http_error",
                    url=url,
                    status=exc.response.status_code,
                )
            except httpx.RequestError as exc:
                logger.warning(
                    "a2a_discovery_request_error",
                    url=url,
                    error=str(exc),
                )
            except Exception as exc:
                logger.warning(
                    "a2a_discovery_unexpected_error",
                    url=url,
                    error=str(exc),
                )

        return merchants

    # ------------------------------------------------------------------
    # Task communication
    # ------------------------------------------------------------------

    async def send_task(
        self,
        agent_url: str,
        message: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a task to an A2A agent.

        Parameters
        ----------
        agent_url:
            Base URL of the A2A agent.
        message:
            The task message text.
        task_id:
            Optional pre-assigned task ID.
        metadata:
            Optional metadata for the task.

        Returns
        -------
        dict
            The created task record from the agent.
        """
        client = await self._get_client()
        url = f"{agent_url.rstrip('/')}/api/v1/a2a/tasks"
        payload = {
            "message": message,
            "task_id": task_id or str(uuid.uuid4()),
            "metadata": metadata or {},
        }

        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("a2a_send_task_failed", agent_url=agent_url, error=str(exc))
            return {
                "error": str(exc),
                "task_id": payload["task_id"],
                "status": "failed",
            }

    async def get_task_status(
        self,
        agent_url: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Poll task status from an A2A agent.

        Parameters
        ----------
        agent_url:
            Base URL of the A2A agent.
        task_id:
            ID of the task to check.

        Returns
        -------
        dict
            Current task status.
        """
        client = await self._get_client()
        url = f"{agent_url.rstrip('/')}/api/v1/a2a/tasks/{task_id}"

        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("a2a_get_task_failed", agent_url=agent_url, error=str(exc))
            return {
                "error": str(exc),
                "task_id": task_id,
                "status": "unknown",
            }

    # ------------------------------------------------------------------
    # Capability mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_capabilities(agent_card: dict[str, Any]) -> list[str]:
        """Map A2A AgentCard skills to UCP-style capability IDs.

        Parameters
        ----------
        agent_card:
            Parsed AgentCard JSON.

        Returns
        -------
        list[str]
            UCP-compatible capability IDs.
        """
        capabilities: list[str] = []
        skills = agent_card.get("skills", [])

        # Map known skill types to UCP capabilities
        skill_mapping = {
            "product_search": "catalog.search",
            "catalog": "catalog.search",
            "checkout": "checkout",
            "payment": "checkout",
            "order_tracking": "orders",
            "shipping": "fulfillment",
        }

        for skill in skills:
            skill_id = skill if isinstance(skill, str) else skill.get("id", "")
            skill_lower = skill_id.lower()
            for key, ucp_cap in skill_mapping.items():
                if key in skill_lower and ucp_cap not in capabilities:
                    capabilities.append(ucp_cap)

        return capabilities or ["catalog.search"]
