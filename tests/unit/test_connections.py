import json
import os
import time
from typing import ClassVar
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from pg_multi_mcp.connections import AccessMode
from pg_multi_mcp.connections import ConnectionConfig
from pg_multi_mcp.connections import ConnectionConfigError
from pg_multi_mcp.connections import ConnectionManager
from pg_multi_mcp.connections import Engine
from pg_multi_mcp.connections import expand_env
from pg_multi_mcp.connections import has_literal_password
from pg_multi_mcp.connections import normalize_name
from pg_multi_mcp.postgres.sql import SafeSqlDriver
from pg_multi_mcp.postgres.sql import SqlDriver


class FakePool:
    """Stand-in for DbConnPool that never touches the network."""

    instances: ClassVar[list["FakePool"]] = []
    fail_urls: ClassVar[set[str]] = set()

    def __init__(self, connection_url=None, min_size=1, max_size=5, **kwargs):
        self.connection_url = connection_url
        self.max_size = max_size
        self.pool = None
        self.server_version = None
        self.closed = False
        FakePool.instances.append(self)

    async def pool_connect(self, connection_url=None):
        if self.connection_url in FakePool.fail_urls:
            raise ValueError(f"Connection attempt failed: could not connect to {self.connection_url}")
        self.pool = object()
        return self.pool

    async def close(self):
        self.pool = None
        self.closed = True

    @property
    def is_valid(self):
        return self.pool is not None


@pytest.fixture(autouse=True)
def fake_pool():
    FakePool.instances = []
    FakePool.fail_urls = set()
    with patch("pg_multi_mcp.connections.DbConnPool", FakePool), patch("pg_multi_mcp.connections.probe_connection", AsyncMock()):
        yield FakePool


def make_manager(*configs: ConnectionConfig, **kwargs) -> ConnectionManager:
    manager = ConnectionManager(**kwargs)
    for config in configs:
        manager.register(config)
    return manager


def cfg(name: str, **kwargs) -> ConnectionConfig:
    kwargs.setdefault("url", f"postgresql://user@host/{name}")
    kwargs.setdefault("persistent", False)
    return ConnectionConfig(name=name, **kwargs)


# ---------------------------------------------------------------- helpers


def test_normalize_name_ignores_case_accents_and_separators():
    assert normalize_name("Relatórios Produção") == "relatoriosproducao"
    assert normalize_name("relatorios_produ-cao.") == "relatoriosproducao"


def test_expand_env(monkeypatch):
    monkeypatch.setenv("PG_TEST_PASS", "s3cr3t")
    assert expand_env("postgresql://u:${PG_TEST_PASS}@h/db") == "postgresql://u:s3cr3t@h/db"


def test_expand_env_missing_variable(monkeypatch):
    monkeypatch.delenv("PG_MISSING", raising=False)
    with pytest.raises(ConnectionConfigError, match="PG_MISSING"):
        expand_env("postgresql://u:${PG_MISSING}@h/db")


def test_has_literal_password():
    assert has_literal_password("postgresql://u:plain@h/db")
    assert has_literal_password("host=h dbname=db user=u password=plain")
    assert not has_literal_password("postgresql://u:${PG_PASS}@h/db")
    assert not has_literal_password("postgresql://u@h/db")


def test_safe_target_hides_password():
    target = cfg("a", url="postgresql://u:plain@db.local:5433/app").safe_target()
    assert target == {"host": "db.local", "port": "5433", "dbname": "app", "user": "u"}


def test_invalid_name_rejected():
    with pytest.raises(ConnectionConfigError):
        cfg("nome com espaço")


# ---------------------------------------------------------------- resolução


def test_resolve_by_name_alias_case_and_accents():
    manager = make_manager(cfg("reports_prod", aliases=["Relatórios Produção"]), cfg("local"))
    assert manager.resolve("reports_prod").config.name == "reports_prod"
    assert manager.resolve("REPORTS-PROD").config.name == "reports_prod"
    assert manager.resolve("relatorios producao").config.name == "reports_prod"


def test_resolve_unknown_suggests_close_names():
    manager = make_manager(cfg("billing_prod"), cfg("billing_hml"), cfg("local"))
    with pytest.raises(ConnectionConfigError) as exc:
        manager.resolve("billing")
    assert "billing_hml" in str(exc.value)
    assert "billing_prod" in str(exc.value)


def test_resolve_without_name_priority():
    manager = make_manager(cfg("a"), cfg("b"))
    with pytest.raises(ConnectionConfigError, match="várias conexões"):
        manager.resolve(None)

    manager.default_name = "a"
    assert manager.resolve(None).config.name == "a"

    manager.set_active("b")
    assert manager.resolve(None).config.name == "b"


def test_resolve_without_name_single_connection():
    manager = make_manager(cfg("only"))
    assert manager.resolve(None).config.name == "only"


def test_register_rejects_alias_clash():
    manager = make_manager(cfg("a", aliases=["centro"]))
    with pytest.raises(ConnectionConfigError, match="conflita"):
        manager.register(cfg("b", aliases=["Centro"]))


def test_search_matches_tags_and_description():
    manager = make_manager(
        cfg("a", tags=["producao"], description="Relatórios de vendas"),
        cfg("b", tags=["homologacao"]),
    )
    assert [mc.config.name for mc in manager.search("produção")] == ["a"]
    assert [mc.config.name for mc in manager.search("vendas")] == ["a"]
    assert len(manager.search(None)) == 2


# ---------------------------------------------------------------- ciclo de vida


@pytest.mark.asyncio
async def test_lease_opens_lazily_and_reuses_pool():
    manager = make_manager(cfg("a"))
    assert not manager.resolve("a").is_open

    async with manager.lease("a") as lease:
        assert lease.name == "a"
    async with manager.lease("a"):
        pass

    assert len(FakePool.instances) == 1
    mc = manager.resolve("a")
    assert mc.is_open
    assert mc.query_count == 2


@pytest.mark.asyncio
async def test_lease_driver_respects_access_mode():
    manager = make_manager(cfg("ro"), cfg("rw", access_mode=AccessMode.UNRESTRICTED))

    async with manager.lease("ro") as lease:
        assert isinstance(lease.driver, SafeSqlDriver)
        assert lease.access_mode == AccessMode.RESTRICTED
    async with manager.lease("rw") as lease:
        assert type(lease.driver) is SqlDriver
        assert lease.access_mode == AccessMode.UNRESTRICTED


@pytest.mark.asyncio
async def test_access_cap_forces_restricted():
    manager = make_manager(cfg("rw", access_mode=AccessMode.UNRESTRICTED), access_cap=AccessMode.RESTRICTED)
    async with manager.lease("rw") as lease:
        assert isinstance(lease.driver, SafeSqlDriver)
    assert manager.describe(manager.resolve("rw"))["access_mode"] == "restricted"


@pytest.mark.asyncio
async def test_env_placeholder_resolved_only_at_connect(monkeypatch):
    monkeypatch.setenv("PG_A_PASS", "s3cr3t")
    manager = make_manager(cfg("a", url="postgresql://u:${PG_A_PASS}@h/db"))
    await manager.open("a")
    assert FakePool.instances[0].connection_url == "postgresql://u:s3cr3t@h/db"
    # The stored config keeps the placeholder
    assert manager.resolve("a").config.url == "postgresql://u:${PG_A_PASS}@h/db"


@pytest.mark.asyncio
async def test_open_failure_records_obfuscated_error():
    manager = make_manager(cfg("bad", url="postgresql://u:plain@h/db"))
    FakePool.fail_urls.add("postgresql://u:plain@h/db")

    with pytest.raises(ConnectionConfigError, match="Falha ao conectar em 'bad'"):
        await manager.open("bad")

    info = manager.describe(manager.resolve("bad"))
    assert info["status"] == "erro"
    assert "plain" not in info["last_error"]


@pytest.mark.asyncio
async def test_lru_eviction_respects_max_open():
    manager = make_manager(cfg("a"), cfg("b"), cfg("c"), max_open=2)
    await manager.open("a")
    await manager.open("b")
    manager.resolve("a").last_used = time.monotonic() - 100  # "a" is the least recently used

    await manager.open("c")

    assert not manager.resolve("a").is_open
    assert manager.resolve("b").is_open
    assert manager.resolve("c").is_open


@pytest.mark.asyncio
async def test_lru_does_not_evict_connection_in_use():
    manager = make_manager(cfg("a"), cfg("b"), max_open=1)
    async with manager.lease("a"):
        await manager.open("b")
        assert manager.resolve("a").is_open  # still in use, kept open over the limit
    assert manager.resolve("b").is_open


@pytest.mark.asyncio
async def test_close_idle():
    manager = make_manager(cfg("a"), cfg("b"), idle_timeout=60)
    await manager.open("a")
    await manager.open("b")
    manager.resolve("a").last_used = time.monotonic() - 120

    closed = await manager.close_idle()

    assert closed == ["a"]
    assert manager.resolve("b").is_open


@pytest.mark.asyncio
async def test_close_and_close_all():
    manager = make_manager(cfg("a"), cfg("b"))
    await manager.open("a")
    await manager.open("b")

    await manager.close("a")
    assert not manager.resolve("a").is_open

    assert await manager.close_all() == ["b"]
    # Closed connections reopen transparently on next use
    async with manager.lease("a"):
        assert manager.resolve("a").is_open


# ---------------------------------------------------------------- persistência


def write_config(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_config(tmp_path):
    path = tmp_path / "connections.json"
    write_config(
        path,
        {
            "default": "Local Dev",
            "settings": {"idle_timeout_seconds": 5, "max_open_connections": 3},
            "connections": {
                "local": {"url": "postgresql://u@localhost/db", "aliases": ["local dev"], "access_mode": "unrestricted"},
                "prod": {"url": "postgresql://u@prod/db"},
            },
        },
    )
    manager = ConnectionManager(config_path=path)
    manager.load()

    assert manager.names == ["local", "prod"]
    assert manager.default_name == "local"
    assert manager.idle_timeout == 5
    assert manager.max_open == 3
    assert manager.resolve("prod").config.access_mode == AccessMode.RESTRICTED


def test_cli_overrides_file_settings(tmp_path):
    path = tmp_path / "connections.json"
    write_config(path, {"settings": {"idle_timeout_seconds": 5}, "connections": {}})
    manager = ConnectionManager(config_path=path, idle_timeout=0)
    manager.load()
    assert manager.idle_timeout == 0


@pytest.mark.asyncio
async def test_add_and_remove_persist(tmp_path):
    path = tmp_path / "sub" / "connections.json"
    manager = ConnectionManager(config_path=path)

    await manager.add(ConnectionConfig(name="a", url="postgresql://u:${P}@h/a", aliases=["alpha"]))
    await manager.add(ConnectionConfig(name="tmp", url="postgresql://u@h/tmp", persistent=False))

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved["connections"]) == ["a"]
    assert saved["connections"]["a"]["url"] == "postgresql://u:${P}@h/a"

    reloaded = ConnectionManager(config_path=path)
    reloaded.load()
    assert reloaded.resolve("alpha").config.name == "a"

    manager.set_active("a")
    await manager.remove("a")
    assert manager.active_name is None
    assert json.loads(path.read_text(encoding="utf-8"))["connections"] == {}


@pytest.mark.asyncio
async def test_add_existing_requires_overwrite(tmp_path):
    manager = ConnectionManager(config_path=tmp_path / "c.json")
    await manager.add(ConnectionConfig(name="a", url="postgresql://u@h/a"))
    with pytest.raises(ConnectionConfigError, match="overwrite"):
        await manager.add(ConnectionConfig(name="a", url="postgresql://u@h/other"))

    await manager.open("a")
    await manager.add(ConnectionConfig(name="a", url="postgresql://u@h/other"), overwrite=True)
    assert manager.resolve("a").config.url == "postgresql://u@h/other"
    assert not manager.resolve("a").is_open  # old pool closed on overwrite


@pytest.mark.asyncio
async def test_lock_config_blocks_edits(tmp_path):
    manager = ConnectionManager(config_path=tmp_path / "c.json", lock_config=True)
    with pytest.raises(ConnectionConfigError, match="travada"):
        await manager.add(ConnectionConfig(name="a", url="postgresql://u@h/a"))


# ---------------------------------------------------------------- engine e env (.mcp.json)


def test_engine_defaults_to_postgres_and_accepts_aliases():
    assert cfg("a").engine == Engine.POSTGRES
    assert cfg("b", engine="postgresql").engine == Engine.POSTGRES
    with pytest.raises(ConnectionConfigError, match="não suportada"):
        cfg("c", engine="oracle")


def test_load_env_plain_url_and_json():
    manager = ConnectionManager()
    manager.load_env(
        {
            "PG_MULTI_MCP_CONN_LOCAL": "postgresql://u@localhost/dev",
            "PG_MULTI_MCP_CONN_SALES_PROD": json.dumps({"url": "postgresql://u@prod/sales", "aliases": ["sales"], "tags": ["producao"]}),
            "PG_MULTI_MCP_DEFAULT": "local",
            "OTHER_VAR": "ignored",
        }
    )
    assert manager.names == ["local", "sales_prod"]
    assert manager.default_name == "local"
    mc = manager.resolve("sales")
    assert mc.config.source == "env"
    assert not mc.config.persistent
    assert mc.config.access_mode == AccessMode.RESTRICTED


def test_load_env_invalid_json():
    with pytest.raises(ConnectionConfigError, match="PG_MULTI_MCP_CONN_BAD"):
        ConnectionManager().load_env({"PG_MULTI_MCP_CONN_BAD": "{not json"})


@pytest.mark.asyncio
async def test_env_override_keeps_file_entry_on_disk(tmp_path):
    path = tmp_path / "connections.json"
    write_config(path, {"default": "local", "connections": {"local": {"url": "postgresql://u@file/db"}}})
    manager = ConnectionManager(config_path=path)
    manager.load()
    manager.load_env({"PG_MULTI_MCP_CONN_LOCAL": "postgresql://u@env/db", "PG_MULTI_MCP_DEFAULT": "local"})

    assert manager.resolve("local").config.url == "postgresql://u@env/db"

    # Saving for another reason must not drop the file definition nor persist the env default
    await manager.add(ConnectionConfig(name="other", url="postgresql://u@h/other"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["connections"]["local"]["url"] == "postgresql://u@file/db"
    assert saved["default"] == "local"

    # Removing the env override brings the file definition back
    await manager.remove("local")
    assert manager.resolve("local").config.url == "postgresql://u@file/db"


# ---------------------------------------------------------------- reload do arquivo


def touch_later(path):
    """Guarantee a new mtime even when the filesystem clock is coarse."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def loaded_manager(path, data, **kwargs) -> ConnectionManager:
    write_config(path, data)
    manager = ConnectionManager(config_path=path, **kwargs)
    manager.load()
    return manager


@pytest.mark.asyncio
async def test_reload_reports_diff_and_keeps_unchanged_pools(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(
        path,
        {
            "connections": {
                "keep": {"url": "postgresql://u@h/keep"},
                "change": {"url": "postgresql://u@h/old"},
                "drop": {"url": "postgresql://u@h/drop"},
            }
        },
    )
    for name in ("keep", "change", "drop"):
        await manager.open(name)
    keep_pool = manager.resolve("keep").pool

    write_config(
        path,
        {
            "connections": {
                "keep": {"url": "postgresql://u@h/keep"},
                "change": {"url": "postgresql://u@h/new"},
                "new": {"url": "postgresql://u@h/new"},
            }
        },
    )
    report = await manager.reload()

    assert report == {"added": ["new"], "changed": ["change"], "removed": ["drop"], "unchanged": ["keep"]}
    assert manager.names == ["change", "keep", "new"]
    assert manager.resolve("keep").pool is keep_pool and manager.resolve("keep").is_open
    assert not manager.resolve("change").is_open
    assert manager.resolve("change").config.url == "postgresql://u@h/new"
    assert sum(p.closed for p in FakePool.instances) == 2  # change + drop


@pytest.mark.asyncio
async def test_auto_reload_on_next_use(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"connections": {"a": {"url": "postgresql://u@h/a"}}})

    write_config(path, {"connections": {"a": {"url": "postgresql://u@h/a"}, "inventory": {"url": "postgresql://u@h/inventory"}}})
    touch_later(path)

    async with manager.lease("inventory") as lease:
        assert lease.name == "inventory"


@pytest.mark.asyncio
async def test_auto_reload_disabled(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"connections": {"a": {"url": "postgresql://u@h/a"}}}, auto_reload=False)
    write_config(path, {"connections": {"b": {"url": "postgresql://u@h/b"}}})
    touch_later(path)
    assert await manager.refresh_if_changed() is None
    assert manager.names == ["a"]


@pytest.mark.asyncio
async def test_invalid_edit_keeps_previous_config_and_blocks_saving(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"connections": {"a": {"url": "postgresql://u:segredo@h/a"}}})

    path.write_text('{"connections": {"a": {"url": "postgresql://u:segredo@h/a"},}}', encoding="utf-8")  # trailing comma
    touch_later(path)

    async with manager.lease("a"):
        pass  # previous registry still works
    assert "JSON inválido" in manager.last_reload_error
    assert "segredo" not in manager.last_reload_error

    with pytest.raises(ConnectionConfigError, match="corrija"):
        await manager.add(ConnectionConfig(name="b", url="postgresql://u@h/b"))
    assert "segredo" in path.read_text(encoding="utf-8")  # broken file not overwritten

    write_config(path, {"connections": {"a": {"url": "postgresql://u@h/a"}}})
    touch_later(path)
    await manager.refresh_if_changed()
    assert manager.last_reload_error is None


@pytest.mark.asyncio
async def test_add_after_manual_edit_does_not_overwrite_it(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"connections": {"a": {"url": "postgresql://u@h/a"}}})

    write_config(path, {"connections": {"a": {"url": "postgresql://u@h/a"}, "manual": {"url": "postgresql://u@h/manual"}}})
    touch_later(path)
    await manager.add(ConnectionConfig(name="via_tool", url="postgresql://u@h/tool"))

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(saved["connections"]) == ["a", "manual", "via_tool"]


@pytest.mark.asyncio
async def test_reload_keeps_env_override_and_active(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"default": "a", "connections": {"a": {"url": "postgresql://u@file/a"}, "b": {"url": "postgresql://u@h/b"}}})
    manager.load_env({"PG_MULTI_MCP_CONN_A": "postgresql://u@env/a"})
    manager.set_active("b")

    write_config(path, {"connections": {"a": {"url": "postgresql://u@file/a2"}, "b": {"url": "postgresql://u@h/b"}}})
    await manager.reload()

    assert manager.resolve("a").config.url == "postgresql://u@env/a"  # env still wins
    assert manager.active_name == "b"
    assert manager.default_name is None  # removed from the file

    await manager.remove("a")  # dropping the override reveals the reloaded file entry
    assert manager.resolve("a").config.url == "postgresql://u@file/a2"


@pytest.mark.asyncio
async def test_reload_rejects_alias_clash_without_changing_state(tmp_path):
    path = tmp_path / "connections.json"
    manager = loaded_manager(path, {"connections": {"a": {"url": "postgresql://u@h/a"}}})
    write_config(path, {"connections": {"a": {"url": "postgresql://u@h/a"}, "b": {"url": "postgresql://u@h/b", "aliases": ["A"]}}})
    with pytest.raises(ConnectionConfigError, match="usado por"):
        await manager.reload()
    assert manager.names == ["a"]
