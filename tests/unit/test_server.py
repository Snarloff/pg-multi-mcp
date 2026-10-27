import inspect
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from pydantic.fields import FieldInfo
from test_connections import FakePool

import pg_multi_mcp.server as server
from pg_multi_mcp.connections import AccessMode
from pg_multi_mcp.connections import ConnectionConfig
from pg_multi_mcp.connections import ConnectionManager
from pg_multi_mcp.mongodb import MongoDriver
from pg_multi_mcp.postgres.sql import SqlDriver


async def fake_execute_query(self, query, params=None, force_readonly=False):
    """Echo which connection URL and transaction mode served the query."""
    if "generate_series" in query:
        return [SqlDriver.RowResult(cells={"n": i}) for i in range(10)]
    return [SqlDriver.RowResult(cells={"url": self.conn.connection_url, "readonly": force_readonly})]


@pytest.fixture
def manager(tmp_path):
    FakePool.instances = []
    FakePool.fail_urls = set()
    manager = ConnectionManager(config_path=tmp_path / "connections.json")
    manager.register(ConnectionConfig(name="sales_prod", url="postgresql://u@prod/sales", aliases=["sales"], persistent=False))
    manager.register(
        ConnectionConfig(name="local", url="postgresql://u:pw@localhost/dev", access_mode=AccessMode.UNRESTRICTED, persistent=False)
    )
    with (
        patch("pg_multi_mcp.connections.DbConnPool", FakePool),
        patch("pg_multi_mcp.connections.probe_connection", AsyncMock()),
        patch("pg_multi_mcp.server.probe_connection", AsyncMock()),
        patch.object(SqlDriver, "execute_query", fake_execute_query),
        patch.object(server, "connections", manager),
    ):
        yield manager


def text(response) -> str:
    return response[0].text


@pytest.mark.asyncio
async def test_execute_sql_routes_to_named_connection(manager):
    out = text(await server.execute_sql(sql="SELECT 1", connection="sales", max_rows=500))
    assert out.startswith("[conexão: sales_prod | modo: restricted]")
    assert "postgresql://u@prod/sales" in out
    assert '"readonly": true' in out

    out = text(await server.execute_sql(sql="SELECT 1", connection="local", max_rows=500))
    assert out.startswith("[conexão: local | modo: unrestricted]")
    assert '"readonly": false' in out


@pytest.mark.asyncio
async def test_execute_sql_restricted_blocks_writes(manager):
    out = text(await server.execute_sql(sql="DELETE FROM customers", connection="sales_prod", max_rows=500))
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_execute_sql_truncates_rows(manager):
    out = text(await server.execute_sql(sql="SELECT * FROM generate_series(1, 10) n", connection="local", max_rows=3))
    payload = json.loads(out.split("\n", 1)[1])
    assert payload["truncated"] is True
    assert payload["total_rows"] == 10
    assert len(payload["rows"]) == 3


@pytest.mark.asyncio
async def test_missing_connection_name_is_explained(manager):
    out = text(await server.execute_sql(sql="SELECT 1", connection=None, max_rows=500))
    assert "várias conexões" in out

    out = text(await server.execute_sql(sql="SELECT 1", connection="saels", max_rows=500))
    assert "sales_prod" in out  # suggestion


@pytest.mark.asyncio
async def test_use_connection_sets_active(manager):
    await server.use_connection(connection="Sales")
    assert manager.active_name == "sales_prod"
    out = text(await server.execute_sql(sql="SELECT 1", connection=None, max_rows=500))
    assert out.startswith("[conexão: sales_prod")


@pytest.mark.asyncio
async def test_list_connections_never_shows_password(manager):
    await server.open_connection(connection="local")
    out = text(await server.list_connections(search=None))
    data = json.loads(out)
    assert "pw" not in out
    assert {c["name"] for c in data["connections"]} == {"sales_prod", "local"}
    local = next(c for c in data["connections"] if c["name"] == "local")
    assert local["status"] == "aberta"


@pytest.mark.asyncio
async def test_close_connection(manager):
    await server.open_connection(connection="local")
    await server.close_connection(connection="local", close_all=False)
    assert not manager.resolve("local").is_open

    await server.open_connection(connection="local")
    await server.open_connection(connection="sales")
    out = json.loads(text(await server.close_connection(connection=None, close_all=True)))
    assert sorted(out["closed"]) == ["local", "sales_prod"]


@pytest.mark.asyncio
async def test_add_connection_warns_on_literal_password(manager):
    out = text(
        await server.add_connection(
            name="nova",
            url="postgresql://u:plain@h/db",
            access_mode="restricted",
            description="",
            aliases=[],
            tags=[],
            persist=True,
            test=True,
            overwrite=False,
            engine="postgres",
        )
    )
    payload = json.loads(out)
    assert payload["warnings"]
    assert "plain" not in json.dumps(payload["added"])
    assert "nova" in json.loads(manager.config_path.read_text(encoding="utf-8"))["connections"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "overwrite"), [("quebrada", False), ("local", True)])
async def test_add_connection_not_registered_when_test_fails(manager, name, overwrite):
    original_url = manager.resolve("local").config.url
    with patch("pg_multi_mcp.server.probe_connection", AsyncMock(side_effect=ValueError("connection refused"))):
        out = text(
            await server.add_connection(
                name=name,
                url="postgresql://u@unreachable/db",
                access_mode="restricted",
                description="",
                aliases=[],
                tags=[],
                persist=False,
                test=True,
                overwrite=overwrite,
                engine="postgres",
            )
        )
    assert out.startswith("Error: Conexão não adicionada")
    assert "quebrada" not in manager.names
    assert manager.resolve("local").config.url == original_url  # failed overwrite keeps previous definition


@pytest.mark.asyncio
async def test_reload_connections_tool(manager):
    manager.config_path.write_text(json.dumps({"connections": {"inventory": {"url": "postgresql://u@h/inventory"}}}), encoding="utf-8")
    out = json.loads(text(await server.reload_connections()))
    assert out["added"] == ["inventory"]
    assert "inventory" in manager.names
    assert "sales_prod" in manager.names  # session connections preserved


@pytest.mark.asyncio
async def test_reload_connections_tool_reports_invalid_file(manager):
    manager.config_path.write_text("{ invalido", encoding="utf-8")
    out = text(await server.reload_connections())
    assert out.startswith("Error: Arquivo não recarregado")
    listed = json.loads(text(await server.list_connections(search=None)))
    assert "config_error" in listed



async def call_tool(fn, **kwargs):
    """Call a tool function directly, resolving the pydantic Field defaults of omitted arguments."""
    params = inspect.signature(fn).parameters
    defaults = {n: p.default.default for n, p in params.items() if n not in kwargs and isinstance(p.default, FieldInfo)}
    return await fn(**defaults, **kwargs)


@pytest.mark.asyncio
async def test_pg_only_tools_reject_mongo_connection(manager):
    manager.register(ConnectionConfig(name="docs", url="mongodb://u@h/app", persistent=False))
    for fn in (server.execute_sql, server.explain_query, server.get_top_queries, server.analyze_db_health):
        kwargs = {"sql": "SELECT 1"} if fn in (server.execute_sql, server.explain_query) else {}
        assert "só funciona com postgres" in text(await call_tool(fn, connection="docs", **kwargs))
    assert not manager.resolve("docs").is_open

    out = text(await call_tool(server.execute_mongo, operation="find", collection="c", connection="local"))
    assert "só funciona com mongodb" in out


@pytest.mark.asyncio
async def test_execute_mongo_routes_to_driver(manager):
    manager.register(ConnectionConfig(name="docs", url="mongodb://u@h/app", persistent=False))
    seen = {}

    async def fake_execute(self, operation, **kwargs):
        seen.update(operation=operation, readonly=self.readonly, **kwargs)
        return [{"ok": 1}]

    with patch("pg_multi_mcp.connections.MongoConnPool", FakePool), patch.object(MongoDriver, "execute", fake_execute):
        out = text(await call_tool(server.execute_mongo, operation="count", collection="orders", filter={"status": "open"}, connection="docs"))
    assert out.startswith("[conexão: docs | modo: restricted]")
    assert seen["operation"] == "count" and seen["readonly"] is True and seen["filter"] == {"status": "open"}
