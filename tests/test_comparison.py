"""Tests for price comparison and split-order optimization."""

import pytest

from ucp_shopping.agents.comparison_agent import ComparisonAgent
from ucp_shopping.agents.optimizer import SplitOrderOptimizer
from ucp_shopping.models import (
    ProductResult,
    ShippingOption,
    ShoppingPreferences,
)


@pytest.fixture
def sample_search_results():
    """Sample search results from 3 merchants."""
    return {
        "techzone": [
            ProductResult(
                product_id="tz-kb-001",
                name="Mechanical Keyboard Pro",
                price=89.99,
                merchant_id="techzone",
                merchant_name="TechZone",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=5.99,
                                   estimated_days_min=5, estimated_days_max=7),
                ],
                in_stock=True,
            ),
            ProductResult(
                product_id="tz-hub-001",
                name="USB-C Hub 7-in-1",
                price=45.99,
                merchant_id="techzone",
                merchant_name="TechZone",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=5.99,
                                   estimated_days_min=5, estimated_days_max=7),
                ],
                in_stock=True,
            ),
        ],
        "homegoods": [
            ProductResult(
                product_id="hg-kb-001",
                name="Ergonomic Mechanical Keyboard",
                price=99.99,
                merchant_id="homegoods",
                merchant_name="HomeGoods",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=0.00,
                                   estimated_days_min=5, estimated_days_max=7,
                                   is_free=True),
                ],
                in_stock=True,
            ),
            ProductResult(
                product_id="hg-hub-001",
                name="USB-C Docking Hub",
                price=34.99,
                merchant_id="homegoods",
                merchant_name="HomeGoods",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=0.00,
                                   estimated_days_min=5, estimated_days_max=7,
                                   is_free=True),
                ],
                in_stock=True,
            ),
        ],
        "megamart": [
            ProductResult(
                product_id="mm-kb-001",
                name="Gaming Mechanical Keyboard",
                price=69.99,
                merchant_id="megamart",
                merchant_name="MegaMart",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=8.99,
                                   estimated_days_min=3, estimated_days_max=5),
                ],
                in_stock=True,
            ),
            ProductResult(
                product_id="mm-hub-001",
                name="USB-C Hub Pro",
                price=39.99,
                merchant_id="megamart",
                merchant_name="MegaMart",
                shipping_options=[
                    ShippingOption(id="standard", name="Standard", price=8.99,
                                   estimated_days_min=3, estimated_days_max=5),
                ],
                in_stock=True,
            ),
        ],
    }


class TestComparisonAgent:
    async def test_build_comparison(self, sample_search_results):
        agent = ComparisonAgent()
        matrix = await agent.build_comparison(
            sample_search_results,
            item_names=["keyboard", "usb hub"],
        )
        assert len(matrix.entries) == 2
        for entry in matrix.entries:
            assert len(entry.merchant_results) > 0
            assert entry.recommended is not None

    async def test_best_price_identified(self, sample_search_results):
        agent = ComparisonAgent()
        matrix = await agent.build_comparison(
            sample_search_results,
            item_names=["keyboard"],
        )
        entry = matrix.entries[0]
        assert entry.best_price is not None
        # MegaMart at $69.99 should be cheapest for keyboard
        assert entry.best_price.price == 69.99

    async def test_empty_results(self):
        agent = ComparisonAgent()
        matrix = await agent.build_comparison({}, item_names=["nothing"])
        assert len(matrix.entries) == 1
        assert len(matrix.entries[0].merchant_results) == 0


class TestSplitOrderOptimizer:
    async def test_optimize_finds_cheapest(self, sample_search_results):
        optimizer = SplitOrderOptimizer()
        agent = ComparisonAgent()
        matrix = await agent.build_comparison(
            sample_search_results,
            item_names=["keyboard", "usb hub"],
        )
        plan = await optimizer.optimize(matrix, ShoppingPreferences())
        assert plan is not None
        assert len(plan.items) == 2
        assert plan.grand_total > 0

    async def test_savings_calculated(self, sample_search_results):
        optimizer = SplitOrderOptimizer()
        agent = ComparisonAgent()
        matrix = await agent.build_comparison(
            sample_search_results,
            item_names=["keyboard", "usb hub"],
        )
        plan = await optimizer.optimize(matrix, ShoppingPreferences())
        # savings_vs_single should be >= 0
        assert plan.savings_vs_single >= 0

    async def test_prefer_single_merchant(self, sample_search_results):
        optimizer = SplitOrderOptimizer()
        agent = ComparisonAgent()
        matrix = await agent.build_comparison(
            sample_search_results,
            item_names=["keyboard", "usb hub"],
        )
        prefs = ShoppingPreferences(prefer_single_merchant=True)
        plan = await optimizer.optimize(matrix, prefs)
        # All items should be from the same merchant
        merchants = {item.merchant_name for item in plan.items}
        assert len(merchants) == 1
