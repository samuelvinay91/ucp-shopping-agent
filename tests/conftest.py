"""Shared test fixtures for UCP Shopping Agent."""

import pytest

from ucp_shopping.config import Settings
from ucp_shopping.main import build_app


@pytest.fixture
def settings():
    """Create test settings."""
    return Settings(
        environment="testing",
        openai_api_key="test-key",
        human_confirmation_required=False,
    )


@pytest.fixture
def app(settings):
    """Create FastAPI app for testing (with mock merchants mounted)."""
    return build_app(settings)
