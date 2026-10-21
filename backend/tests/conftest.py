import os

import pytest


@pytest.fixture
def database_url() -> str:
    """The test database that make check starts from tests/compose.test.yaml."""
    url = os.environ.get("TITAN_TEST_DATABASE_URL")
    if not url:
        pytest.fail("TITAN_TEST_DATABASE_URL is not set; run the tests with make check")
    return url
