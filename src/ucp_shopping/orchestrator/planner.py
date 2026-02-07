"""LLM-powered shopping planner.

Parses a free-text shopping query into a structured :class:`ShoppingPlan`
containing individual items, keywords, budget constraints, and preferences.
Falls back to simple keyword extraction when no LLM key is configured.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import structlog

from ucp_shopping.config import Settings
from ucp_shopping.models import ShoppingPlan, ShoppingPlanItem, ShoppingPreferences

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a shopping assistant planner. Given the user's natural-language shopping \
query, extract a structured shopping plan.

Return ONLY valid JSON with the following schema (no markdown fences):
{
  "items": [
    {
      "name": "<product name>",
      "keywords": ["keyword1", "keyword2"],
      "budget": <number or null>,
      "brand_preference": "<brand or null>",
      "features": ["feature1", "feature2"]
    }
  ],
  "overall_budget": <number or null>,
  "preferences": {
    "prefer_single_merchant": false,
    "max_shipping_days": <number or null>,
    "prefer_free_shipping": false
  },
  "reasoning": "<brief explanation of your interpretation>"
}

Rules:
- Extract every distinct product the user wants.
- Separate budget from preferences.
- If the user mentions a brand, capture it in brand_preference.
- keywords should be search-engine-friendly terms for the product.
- If no budget is stated, set budget fields to null.
"""


class ShoppingPlanner:
    """Parses natural-language shopping queries into structured plans."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._openai_client: object | None = None
        self._anthropic_client: object | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(self, query: str) -> ShoppingPlan:
        """Parse *query* into a :class:`ShoppingPlan`.

        Attempts LLM-based parsing first (OpenAI, then Anthropic).  Falls
        back to simple heuristic keyword extraction if no API key is
        available or the LLM call fails.
        """
        # Try LLM-based planning
        if self._settings.openai_api_key:
            try:
                return await self._plan_with_openai(query)
            except Exception:
                logger.warning("openai_planning_failed", exc_info=True)

        if self._settings.anthropic_api_key:
            try:
                return await self._plan_with_anthropic(query)
            except Exception:
                logger.warning("anthropic_planning_failed", exc_info=True)

        # Fallback to keyword extraction
        logger.info("using_fallback_planner")
        return self._plan_with_keywords(query)

    # ------------------------------------------------------------------
    # OpenAI-based planner
    # ------------------------------------------------------------------

    async def _plan_with_openai(self, query: str) -> ShoppingPlan:
        """Use the OpenAI chat completions API to parse the query."""
        from openai import AsyncOpenAI

        if self._openai_client is None:
            self._openai_client = AsyncOpenAI(api_key=self._settings.openai_api_key)

        client: AsyncOpenAI = self._openai_client  # type: ignore[assignment]
        response = await client.chat.completions.create(
            model=self._settings.default_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            temperature=0.2,
            max_tokens=1024,
        )

        raw = response.choices[0].message.content or "{}"
        return self._parse_plan_json(raw, query)

    # ------------------------------------------------------------------
    # Anthropic-based planner
    # ------------------------------------------------------------------

    async def _plan_with_anthropic(self, query: str) -> ShoppingPlan:
        """Use the Anthropic messages API to parse the query."""
        from anthropic import AsyncAnthropic

        if self._anthropic_client is None:
            self._anthropic_client = AsyncAnthropic(
                api_key=self._settings.anthropic_api_key
            )

        client: AsyncAnthropic = self._anthropic_client  # type: ignore[assignment]
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": query}],
        )

        raw = response.content[0].text if response.content else "{}"
        return self._parse_plan_json(raw, query)

    # ------------------------------------------------------------------
    # JSON parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_plan_json(raw: str, original_query: str) -> ShoppingPlan:
        """Parse LLM JSON output into a :class:`ShoppingPlan`."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```json?\s*", "", raw)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("plan_json_parse_failed", raw_snippet=raw[:200])
            return ShoppingPlanner._plan_with_keywords(original_query)

        items: list[ShoppingPlanItem] = []
        for item_data in data.get("items", []):
            budget_val = item_data.get("budget")
            items.append(
                ShoppingPlanItem(
                    name=item_data.get("name", ""),
                    keywords=item_data.get("keywords", []),
                    budget=Decimal(str(budget_val)) if budget_val is not None else None,
                    brand_preference=item_data.get("brand_preference"),
                    features=item_data.get("features", []),
                )
            )

        overall_budget_val = data.get("overall_budget")
        prefs_data = data.get("preferences", {})

        return ShoppingPlan(
            items=items,
            overall_budget=(
                Decimal(str(overall_budget_val))
                if overall_budget_val is not None
                else None
            ),
            preferences=ShoppingPreferences(
                prefer_single_merchant=prefs_data.get("prefer_single_merchant", False),
                max_shipping_days=prefs_data.get("max_shipping_days"),
                prefer_free_shipping=prefs_data.get("prefer_free_shipping", False),
            ),
            reasoning=data.get("reasoning", ""),
        )

    # ------------------------------------------------------------------
    # Keyword-based fallback planner
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_with_keywords(query: str) -> ShoppingPlan:
        """Extract structured items using simple heuristics.

        Splits on common delimiters (``and``, commas, semicolons) and treats
        each fragment as an individual product query.
        """
        # Detect budget mentions like "$500", "under 200", "budget 300"
        budget_match = re.search(
            r"(?:under|below|budget|max|up\s+to)\s*\$?\s*(\d+(?:\.\d{1,2})?)", query, re.IGNORECASE
        )
        overall_budget = Decimal(budget_match.group(1)) if budget_match else None

        # Remove budget phrase from the query to avoid it becoming a keyword
        cleaned = query
        if budget_match:
            cleaned = cleaned[: budget_match.start()] + cleaned[budget_match.end() :]

        # Split on delimiters
        fragments = re.split(r"\band\b|,|;", cleaned, flags=re.IGNORECASE)
        items: list[ShoppingPlanItem] = []

        for frag in fragments:
            frag = frag.strip()
            if not frag or len(frag) < 3:
                continue
            keywords = [w.lower() for w in frag.split() if len(w) > 2]
            items.append(
                ShoppingPlanItem(
                    name=frag,
                    keywords=keywords,
                )
            )

        # If nothing was extracted, treat the whole query as one item
        if not items:
            items.append(
                ShoppingPlanItem(
                    name=query.strip(),
                    keywords=[w.lower() for w in query.split() if len(w) > 2],
                )
            )

        return ShoppingPlan(
            items=items,
            overall_budget=overall_budget,
            reasoning="Fallback keyword extraction (no LLM available).",
        )
