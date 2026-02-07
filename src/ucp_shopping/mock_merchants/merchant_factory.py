"""Mock merchant factory.

Creates pre-configured mock UCP merchant sub-applications loaded from
the bundled JSON catalog files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ucp_shopping.mock_merchants.merchant_app import MockMerchantApp

_CATALOG_DIR = Path(__file__).parent / "catalogs"


class MerchantFactory:
    """Factory for creating mock UCP merchant sub-applications."""

    @staticmethod
    def create_merchant(
        name: str,
        catalog_file: str,
        merchant_id: str,
        base_path: str = "",
        free_shipping_threshold: float = 100.0,
        shipping_options: list[dict[str, Any]] | None = None,
        discount_codes: list[dict[str, Any]] | None = None,
    ) -> MockMerchantApp:
        """Create a single mock merchant from a JSON catalog file.

        Parameters
        ----------
        name:
            Human-readable merchant name.
        catalog_file:
            Filename of the JSON catalog inside the ``catalogs/`` directory.
        merchant_id:
            Unique identifier for the merchant.
        base_path:
            URL path prefix where the app will be mounted.
        free_shipping_threshold:
            Order subtotal above which shipping is free.
        shipping_options:
            Custom shipping methods (uses defaults if ``None``).
        discount_codes:
            Custom discount codes (uses defaults if ``None``).

        Returns
        -------
        MockMerchantApp
            A configured mock merchant with its FastAPI sub-app.
        """
        catalog_path = _CATALOG_DIR / catalog_file
        if not catalog_path.exists():
            raise FileNotFoundError(f"Catalog file not found: {catalog_path}")

        with open(catalog_path) as f:
            products = json.load(f)

        return MockMerchantApp(
            name=name,
            merchant_id=merchant_id,
            products=products,
            base_path=base_path,
            free_shipping_threshold=free_shipping_threshold,
            shipping_options=shipping_options,
            discount_codes=discount_codes,
        )

    @classmethod
    def create_all_merchants(
        cls,
        base_url: str = "http://localhost:8020",
    ) -> dict[str, MockMerchantApp]:
        """Create the three built-in demo merchants.

        Returns
        -------
        dict[str, MockMerchantApp]
            Mapping of mount path suffix -> MockMerchantApp.
            Keys: ``"techzone"``, ``"homegoods"``, ``"megamart"``.
        """
        merchants: dict[str, MockMerchantApp] = {}

        # TechZone -- electronics specialist
        merchants["techzone"] = cls.create_merchant(
            name="TechZone Electronics",
            catalog_file="techzone.json",
            merchant_id="techzone",
            base_path=f"{base_url}/merchants/techzone",
            free_shipping_threshold=150.0,
            shipping_options=[
                {
                    "id": "standard",
                    "name": "Standard Shipping",
                    "description": "5-7 business days",
                    "price": 6.99,
                    "estimated_days_min": 5,
                    "estimated_days_max": 7,
                    "is_free": False,
                },
                {
                    "id": "express",
                    "name": "Express Shipping",
                    "description": "2-3 business days",
                    "price": 14.99,
                    "estimated_days_min": 2,
                    "estimated_days_max": 3,
                    "is_free": False,
                },
                {
                    "id": "overnight",
                    "name": "Overnight Shipping",
                    "description": "Next business day",
                    "price": 29.99,
                    "estimated_days_min": 1,
                    "estimated_days_max": 1,
                    "is_free": False,
                },
            ],
        )

        # HomeGoods -- home/office specialist
        merchants["homegoods"] = cls.create_merchant(
            name="HomeGoods Office",
            catalog_file="homegoods.json",
            merchant_id="homegoods",
            base_path=f"{base_url}/merchants/homegoods",
            free_shipping_threshold=75.0,
            shipping_options=[
                {
                    "id": "standard",
                    "name": "Standard Shipping",
                    "description": "5-8 business days",
                    "price": 4.99,
                    "estimated_days_min": 5,
                    "estimated_days_max": 8,
                    "is_free": False,
                },
                {
                    "id": "express",
                    "name": "Express Shipping",
                    "description": "2-4 business days",
                    "price": 9.99,
                    "estimated_days_min": 2,
                    "estimated_days_max": 4,
                    "is_free": False,
                },
            ],
        )

        # MegaMart -- general retailer (overlapping products at different prices)
        merchants["megamart"] = cls.create_merchant(
            name="MegaMart",
            catalog_file="megamart.json",
            merchant_id="megamart",
            base_path=f"{base_url}/merchants/megamart",
            free_shipping_threshold=50.0,
            shipping_options=[
                {
                    "id": "standard",
                    "name": "Free Standard Shipping",
                    "description": "5-7 business days (free over $50)",
                    "price": 3.99,
                    "estimated_days_min": 5,
                    "estimated_days_max": 7,
                    "is_free": False,
                },
                {
                    "id": "express",
                    "name": "Express Shipping",
                    "description": "2-3 business days",
                    "price": 11.99,
                    "estimated_days_min": 2,
                    "estimated_days_max": 3,
                    "is_free": False,
                },
                {
                    "id": "overnight",
                    "name": "Overnight",
                    "description": "Next business day",
                    "price": 19.99,
                    "estimated_days_min": 1,
                    "estimated_days_max": 1,
                    "is_free": False,
                },
            ],
        )

        return merchants
