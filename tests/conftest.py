import os

import pytest

from pg_multi_mcp.postgres.sql import reset_postgres_version_cache


@pytest.fixture(scope="session")
def test_postgres_connection_string() -> tuple[str, str]:
    """Real database for integration tests, taken from PG_MULTI_MCP_TEST_DATABASE_URL (skipped when unset).

    Integration tests CREATE/DROP their own tables: point this to a disposable database.
    """
    url = os.environ.get("PG_MULTI_MCP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PG_MULTI_MCP_TEST_DATABASE_URL not set")
    return url, "local"


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield
