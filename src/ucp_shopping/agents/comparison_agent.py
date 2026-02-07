"""Price comparison specialist agent.

Builds a comparison matrix that scores products across merchants on price,
shipping cost, delivery speed, and stock availability.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from ucp_shopping.models import (
    ComparisonEntry,
    ComparisonMatrix,
    ProductResult,
)

logger = structlog.get_logger(__name__)

# Scoring weights
_WEIGHT_PRICE = 0.50
_WEIGHT_SHIPPING = 0.25
_WEIGHT_AVAILABILITY = 0.15
_WEIGHT_DELIVERY = 0.10


class ComparisonAgent:
    """Builds and scores a product comparison matrix."""

    async def build_comparison(
        self,
        search_results: dict[str, list[ProductResult]],
        item_names: list[str],
    ) -> ComparisonMatrix:
        """Create a comparison matrix for the requested items.

        For each item name, gathers matching products from all merchants,
        scores them, and identifies the best price, best shipping, and
        overall recommended option.

        Parameters
        ----------
        search_results:
            Mapping of merchant_id -> list of products.
        item_names:
            The item names from the shopping plan.

        Returns
        -------
        ComparisonMatrix
        """
        # Flatten all products
        all_products: list[ProductResult] = []
        for products in search_results.values():
            all_products.extend(products)

        entries: list[ComparisonEntry] = []

        for item_name in item_names:
            # Find products that match this item name
            matching = self._find_matching_products(all_products, item_name)

            if not matching:
                entries.append(
                    ComparisonEntry(
                        product_query=item_name,
                        merchant_results=[],
                    )
                )
                continue

            # Score each product
            scored = self._score_products(matching)

            # Sort by score descending
            scored.sort(key=lambda p: p.score, reverse=True)

            # Find best price (lowest product price)
            best_price = min(scored, key=lambda p: p.price) if scored else None

            # Find best shipping (lowest cheapest shipping cost)
            best_shipping = min(
                scored,
                key=lambda p: self._cheapest_shipping(p),
            ) if scored else None

            # Recommended is highest overall score
            recommended = scored[0] if scored else None

            entries.append(
                ComparisonEntry(
                    product_query=item_name,
                    merchant_results=scored,
                    best_price=best_price,
                    best_shipping=best_shipping,
                    recommended=recommended,
                )
            )

        total_products = sum(len(e.merchant_results) for e in entries)
        merchant_ids: set[str] = set()
        for entry in entries:
            for result in entry.merchant_results:
                merchant_ids.add(result.merchant_id)

        matrix = ComparisonMatrix(
            entries=entries,
            total_merchants=len(merchant_ids),
            total_products_found=total_products,
            generated_at=datetime.now(tz=timezone.utc),
        )

        logger.info(
            "comparison_built",
            items=len(entries),
            total_products=total_products,
            merchants=len(merchant_ids),
        )
        return matrix

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _find_matching_products(
        products: list[ProductResult],
        item_name: str,
    ) -> list[ProductResult]:
        """Find products that match the item name using keyword overlap.

        Uses a simple token-overlap heuristic: a product matches if at
        least one keyword from the item name appears in the product's
        name, description, or category.
        """
        keywords = {w.lower() for w in item_name.split() if len(w) > 2}
        if not keywords:
            keywords = {item_name.lower()}

        matching: list[ProductResult] = []
        for product in products:
            product_text = (
                f"{product.name} {product.description} {product.category} {product.brand}"
            ).lower()
            overlap = sum(1 for kw in keywords if kw in product_text)
            if overlap > 0:
                matching.append(product)

        return matching

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_products(self, products: list[ProductResult]) -> list[ProductResult]:
        """Assign a composite score to each product.

        Score components:
        - price_score:        lower price is better
        - shipping_score:     lower cheapest shipping is better
        - availability_score: in-stock products score higher
        - delivery_score:     faster delivery is better
        """
        if not products:
            return []

        # Compute ranges for normalisation
        prices = [p.price for p in products]
        min_price = min(prices)
        max_price = max(prices) if max(prices) != min_price else min_price + 1

        shipping_costs = [self._cheapest_shipping(p) for p in products]
        min_ship = min(shipping_costs)
        max_ship = max(shipping_costs) if max(shipping_costs) != min_ship else min_ship + 1

        delivery_days = [self._fastest_delivery(p) for p in products]
        min_days = min(delivery_days)
        max_days = max(delivery_days) if max(delivery_days) != min_days else min_days + 1

        scored: list[ProductResult] = []
        for product in products:
            # Normalise to 0-1 (inverted: lower is better)
            price_score = 1.0 - (product.price - min_price) / (max_price - min_price)
            ship_cost = self._cheapest_shipping(product)
            shipping_score = 1.0 - (ship_cost - min_ship) / (max_ship - min_ship)
            days = self._fastest_delivery(product)
            delivery_score = 1.0 - (days - min_days) / (max_days - min_days)
            availability_score = 1.0 if product.in_stock else 0.0

            composite = (
                _WEIGHT_PRICE * price_score
                + _WEIGHT_SHIPPING * shipping_score
                + _WEIGHT_AVAILABILITY * availability_score
                + _WEIGHT_DELIVERY * delivery_score
            )

            product_copy = product.model_copy(update={"score": round(composite, 4)})
            scored.append(product_copy)

        return scored

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cheapest_shipping(product: ProductResult) -> float:
        """Return the cheapest shipping cost for a product."""
        if not product.shipping_options:
            return 5.99  # default assumption
        return min(so.price for so in product.shipping_options)

    @staticmethod
    def _fastest_delivery(product: ProductResult) -> int:
        """Return the fastest delivery days for a product."""
        if not product.shipping_options:
            return 5  # default assumption
        return min(so.estimated_days_min for so in product.shipping_options)
