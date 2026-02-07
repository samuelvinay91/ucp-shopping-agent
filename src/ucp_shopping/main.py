"""Entry point for the UCP Shopping Agent service.

Creates the main FastAPI application, mounts the three mock merchant
sub-apps, configures logging, and starts the uvicorn server.
"""

from __future__ import annotations

import structlog
import uvicorn

from common import setup_logging

from ucp_shopping.api import create_app
from ucp_shopping.config import Settings, get_settings
from ucp_shopping.mock_merchants.merchant_factory import MerchantFactory

logger = structlog.get_logger(__name__)


def build_app(settings: Settings | None = None) -> object:
    """Construct the fully-configured application with mock merchants.

    Returns the FastAPI application with mock merchant sub-apps mounted
    at ``/merchants/techzone``, ``/merchants/homegoods``, and
    ``/merchants/megamart``.
    """
    settings = settings or get_settings()
    setup_logging(settings.log_level)

    # Create main API application
    app = create_app(settings)

    # Create and mount mock merchants
    base_url = f"http://{settings.host}:{settings.port}"
    if settings.host == "0.0.0.0":
        base_url = f"http://localhost:{settings.port}"

    merchants = MerchantFactory.create_all_merchants(base_url=base_url)

    for slug, merchant_app in merchants.items():
        mount_path = f"/merchants/{slug}"
        app.mount(mount_path, merchant_app.app, name=f"merchant-{slug}")
        logger.info(
            "mock_merchant_mounted",
            merchant=merchant_app.name,
            path=mount_path,
            products=len(merchant_app.products),
        )

    logger.info(
        "application_ready",
        service=settings.service_name,
        version=settings.service_version,
        merchants_mounted=len(merchants),
        docs_url=f"{base_url}/docs",
    )

    return app


def main() -> None:
    """Launch the UCP Shopping Agent server."""
    settings = get_settings()
    setup_logging(settings.log_level)

    app = build_app(settings)

    uvicorn.run(
        app,  # type: ignore[arg-type]
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
