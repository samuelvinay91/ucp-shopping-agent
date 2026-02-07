"""Split-order optimizer.

Determines the cheapest way to purchase all items in the comparison matrix,
potentially splitting the order across multiple merchants to minimise total
cost (product price + shipping).
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from ucp_shopping.models import (
    ComparisonMatrix,
    ProductResult,
    ShoppingPreferences,
    SplitOrderItem,
    SplitOrderPlan,
)

logger = structlog.get_logger(__name__)


class SplitOrderOptimizer:
    """Computes optimal purchase plans across merchants."""

    async def optimize(
        self,
        matrix: ComparisonMatrix,
        preferences: ShoppingPreferences | None = None,
    ) -> SplitOrderPlan:
        """Build an optimized split-order plan.

        Algorithm
        ---------
        1. For each item in the comparison matrix, select the cheapest
           option considering price + shipping.
        2. Group selected items by merchant and check free-shipping
           thresholds: if the subtotal from one merchant exceeds the
           threshold, zero out the shipping cost.
        3. Compare against a single-merchant baseline (buying everything
           from the cheapest single merchant) to calculate savings.

        Parameters
        ----------
        matrix:
            The comparison matrix containing scored options per item.
        preferences:
            User preferences that may override optimisation decisions.

        Returns
        -------
        SplitOrderPlan
        """
        prefs = preferences or ShoppingPreferences()

        if prefs.prefer_single_merchant:
            return self._optimize_single_merchant(matrix, prefs)

        return self._optimize_split(matrix, prefs)

    # ------------------------------------------------------------------
    # Split-order optimisation
    # ------------------------------------------------------------------

    def _optimize_split(
        self,
        matrix: ComparisonMatrix,
        prefs: ShoppingPreferences,
    ) -> SplitOrderPlan:
        """Pick the cheapest option per item regardless of merchant."""
        items: list[SplitOrderItem] = []

        for entry in matrix.entries:
            if not entry.merchant_results:
                continue

            # Filter by preferences
            candidates = self._apply_preference_filters(
                entry.merchant_results, prefs
            )
            if not candidates:
                candidates = entry.merchant_results

            # Find cheapest total (price + cheapest shipping)
            best = min(
                candidates,
                key=lambda p: p.price + self._cheapest_shipping_cost(p),
            )
            shipping = self._cheapest_shipping_cost(best)

            items.append(
                SplitOrderItem(
                    product_name=best.name,
                    product_id=best.product_id,
                    merchant_name=best.merchant_name,
                    merchant_id=best.merchant_id,
                    merchant_url=self._get_merchant_url(best),
                    price=best.price,
                    shipping_cost=shipping,
                )
            )

        # Apply free-shipping thresholds
        items = self._apply_free_shipping_thresholds(items, matrix)

        # Calculate totals
        total_product = round(sum(i.price for i in items), 2)
        total_shipping = round(sum(i.shipping_cost for i in items), 2)
        grand_total = round(total_product + total_shipping, 2)

        # Calculate savings vs single-merchant baseline
        single_plan = self._optimize_single_merchant(matrix, prefs)
        savings = round(max(0.0, single_plan.grand_total - grand_total), 2)

        # Count distinct merchants
        merchant_ids = {i.merchant_id for i in items}

        plan = SplitOrderPlan(
            items=items,
            total_product_cost=total_product,
            total_shipping_cost=total_shipping,
            grand_total=grand_total,
            savings_vs_single=savings,
            merchants_used=len(merchant_ids),
            reasoning=self._build_reasoning(items, savings),
        )

        logger.info(
            "split_order_optimized",
            items=len(items),
            merchants=len(merchant_ids),
            grand_total=grand_total,
            savings=savings,
        )
        return plan

    # ------------------------------------------------------------------
    # Single-merchant optimisation
    # ------------------------------------------------------------------

    def _optimize_single_merchant(
        self,
        matrix: ComparisonMatrix,
        prefs: ShoppingPreferences,
    ) -> SplitOrderPlan:
        """Find the best single merchant to fulfil all items."""
        # Group available products by merchant
        merchant_items: dict[str, list[tuple[str, ProductResult]]] = defaultdict(list)

        for entry in matrix.entries:
            for result in entry.merchant_results:
                merchant_items[result.merchant_id].append(
                    (entry.product_query, result)
                )

        # Evaluate each merchant that can fulfil all items
        num_items = len(matrix.entries)
        best_plan: SplitOrderPlan | None = None

        for merchant_id, available in merchant_items.items():
            # Pick cheapest option per item from this merchant
            item_map: dict[str, ProductResult] = {}
            for item_query, product in available:
                if item_query not in item_map or product.price < item_map[item_query].price:
                    item_map[item_query] = product

            # Only consider merchants that can cover all items
            if len(item_map) < num_items:
                continue

            items: list[SplitOrderItem] = []
            for item_query, product in item_map.items():
                shipping = self._cheapest_shipping_cost(product)
                items.append(
                    SplitOrderItem(
                        product_name=product.name,
                        product_id=product.product_id,
                        merchant_name=product.merchant_name,
                        merchant_id=product.merchant_id,
                        merchant_url=self._get_merchant_url(product),
                        price=product.price,
                        shipping_cost=shipping,
                    )
                )

            total_product = round(sum(i.price for i in items), 2)
            total_shipping = round(sum(i.shipping_cost for i in items), 2)
            grand_total = round(total_product + total_shipping, 2)

            plan = SplitOrderPlan(
                items=items,
                total_product_cost=total_product,
                total_shipping_cost=total_shipping,
                grand_total=grand_total,
                savings_vs_single=0.0,
                merchants_used=1,
                reasoning=f"All items from {items[0].merchant_name if items else 'unknown'}.",
            )

            if best_plan is None or grand_total < best_plan.grand_total:
                best_plan = plan

        if best_plan is not None:
            return best_plan

        # If no single merchant can fulfil all items, fall back to split
        # but mark it as a single-merchant attempt
        return SplitOrderPlan(
            items=[],
            reasoning="No single merchant can fulfil all items.",
        )

    # ------------------------------------------------------------------
    # Free shipping threshold logic
    # ------------------------------------------------------------------

    def _apply_free_shipping_thresholds(
        self,
        items: list[SplitOrderItem],
        matrix: ComparisonMatrix,
    ) -> list[SplitOrderItem]:
        """Zero out shipping for merchants where subtotal exceeds threshold.

        This is a simplified model: if the total from a merchant exceeds a
        known free-shipping threshold, all shipping from that merchant is
        waived.
        """
        # Group items by merchant and compute subtotals
        merchant_subtotals: dict[str, float] = defaultdict(float)
        for item in items:
            merchant_subtotals[item.merchant_id] += item.price

        # Check thresholds (stored in metadata on comparison results)
        # For now, use a heuristic: free shipping if subtotal >= 100
        free_shipping_threshold = 100.0

        updated: list[SplitOrderItem] = []
        for item in items:
            if merchant_subtotals[item.merchant_id] >= free_shipping_threshold:
                updated.append(
                    item.model_copy(
                        update={
                            "shipping_cost": 0.0,
                            "total": round(item.price, 2),
                        }
                    )
                )
            else:
                updated.append(item)

        return updated

    # ------------------------------------------------------------------
    # Preference filters
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_preference_filters(
        products: list[ProductResult],
        prefs: ShoppingPreferences,
    ) -> list[ProductResult]:
        """Filter products based on user preferences."""
        filtered = list(products)

        # Filter by max shipping days
        if prefs.max_shipping_days is not None:
            filtered = [
                p
                for p in filtered
                if any(
                    so.estimated_days_max <= prefs.max_shipping_days
                    for so in p.shipping_options
                )
                or not p.shipping_options
            ]

        # Filter by free shipping preference
        if prefs.prefer_free_shipping:
            free_options = [
                p
                for p in filtered
                if any(so.is_free or so.price == 0 for so in p.shipping_options)
            ]
            if free_options:
                filtered = free_options

        # Filter by preferred brands
        if prefs.preferred_brands:
            brand_lower = {b.lower() for b in prefs.preferred_brands}
            brand_matches = [
                p for p in filtered if p.brand.lower() in brand_lower
            ]
            if brand_matches:
                filtered = brand_matches

        # Filter by minimum rating
        if prefs.min_rating is not None:
            rated = [p for p in filtered if p.rating >= prefs.min_rating]
            if rated:
                filtered = rated

        return filtered

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cheapest_shipping_cost(product: ProductResult) -> float:
        """Return the cheapest shipping cost for a product."""
        if not product.shipping_options:
            return 5.99
        return min(so.price for so in product.shipping_options)

    @staticmethod
    def _get_merchant_url(product: ProductResult) -> str:
        """Derive the merchant URL from product metadata."""
        return product.url.rsplit("/api", 1)[0] if "/api" in product.url else ""

    @staticmethod
    def _build_reasoning(items: list[SplitOrderItem], savings: float) -> str:
        """Build a human-readable explanation of the plan."""
        if not items:
            return "No items to purchase."

        merchant_names = sorted({i.merchant_name for i in items})
        if len(merchant_names) == 1:
            reasoning = f"All {len(items)} item(s) purchased from {merchant_names[0]}."
        else:
            reasoning = (
                f"Order split across {len(merchant_names)} merchants "
                f"({', '.join(merchant_names)}) for optimal pricing."
            )

        if savings > 0:
            reasoning += f" Saves ${savings:.2f} compared to single-merchant purchase."

        return reasoning
