"""Gerenciamento de múltiplas conexões de banco nomeadas (PostgreSQL e MongoDB).

Cada conexão é declarada no arquivo de configuração (JSON) ou adicionada em tempo de execução.
O pool só é aberto no primeiro uso, é reaproveitado entre chamadas, fechado após inatividade
(idle timeout) e despejado por LRU quando o limite de pools abertos é atingido.
"""

import asyncio
import contextlib
import difflib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from typing import Mapping
from typing import Optional
from typing import Union
from urllib.parse import quote

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .mongodb import MongoConnPool
from .mongodb import MongoDriver
from .mongodb import is_mongo_url
from .mongodb import mongo_password
from .mongodb import mongo_target
from .mongodb import probe_mongo
from .postgres.sql import DbConnPool
from .postgres.sql import SafeSqlDriver
from .postgres.sql import SqlDriver
from .postgres.sql import obfuscate_password

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".pg-multi-mcp" / "connections.json"
DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_MAX_OPEN = 10
CONNECT_TIMEOUT = 10

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class AccessMode(str, Enum):
    """SQL access modes for a connection."""

    UNRESTRICTED = "unrestricted"  # Unrestricted access
    RESTRICTED = "restricted"  # Read-only with safety features


class Engine(str, Enum):
    """Motores de banco suportados. Novos motores entram aqui (driver + ferramentas)."""

    POSTGRES = "postgres"
    MONGODB = "mongodb"


_ENGINE_ALIASES = {"postgresql": "postgres", "pg": "postgres", "mongo": "mongodb"}

# Conexões declaradas por variável de ambiente (ex.: bloco "env" do .mcp.json)
ENV_CONNECTION_PREFIX = "PG_MULTI_MCP_CONN_"
ENV_DEFAULT_CONNECTION = "PG_MULTI_MCP_DEFAULT"


class ConnectionConfigError(Exception):
    """Erro de configuração ou resolução de conexão (mensagem pensada para o agente)."""


def normalize_name(value: str) -> str:
    """Normaliza para comparação: minúsculas, sem acento, só [a-z0-9]."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", without_accents.lower())


def expand_env(value: str, quote_values: bool = False) -> str:
    """Substitui ${VAR} pelo valor da variável de ambiente. Falha se alguma não existir.

    quote_values=True aplica percent-encoding no valor (usuário/senha de URI MongoDB).
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            missing.append(match.group(1))
            return match.group(0)
        return quote(env_value, safe="") if quote_values else env_value

    expanded = _ENV_PATTERN.sub(replace, value)
    if missing:
        raise ConnectionConfigError(f"Variável(is) de ambiente não definida(s): {', '.join(missing)}")
    return expanded


def expand_url(url: str, engine: Engine) -> str:
    """Expande ${VAR} na URL. No MongoDB, valores em usuário/senha são percent-encoded (exigido pela URI)."""
    if engine != Engine.MONGODB or "://" not in url:
        return expand_env(url)
    scheme, rest = url.split("://", 1)
    authority_end = next((i for i, c in enumerate(rest) if c in "/?"), len(rest))
    at = rest.rfind("@", 0, authority_end)
    if at < 0:
        return expand_env(url)
    return f"{scheme}://{expand_env(rest[:at], quote_values=True)}{expand_env(rest[at:])}"


async def probe_connection(url: str, engine: Engine = Engine.POSTGRES, timeout: int = CONNECT_TIMEOUT) -> None:
    """Conexão avulsa de teste: falha rápido com o erro real (auth, DNS, recusa).

    O pool do psycopg re-tenta em segundo plano e só reporta timeout genérico.
    """
    if engine == Engine.MONGODB:
        await probe_mongo(url, timeout)
        return
    conn = await psycopg.AsyncConnection.connect(url, connect_timeout=timeout, application_name="pg-multi-mcp")
    await conn.close()


def has_literal_password(url: str) -> bool:
    """True se a URL/DSN contém senha em texto puro (não um placeholder ${VAR})."""
    try:
        password = mongo_password(url) if is_mongo_url(url) else conninfo_to_dict(url).get("password")
    except Exception:
        return False
    return bool(password) and not _ENV_PATTERN.fullmatch(str(password))


@dataclass
class ConnectionConfig:
    """Definição de uma conexão nomeada."""

    name: str
    url: str
    engine: Optional[Engine] = None  # None = inferido da URL (mongodb:// -> mongodb, senão postgres)
    access_mode: AccessMode = AccessMode.RESTRICTED
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    max_pool_size: int = 3
    query_timeout: int = 30
    persistent: bool = True  # False = existe só nesta sessão (não é gravada no arquivo)
    source: str = "file"  # file | env | runtime

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            raise ConnectionConfigError(f"Nome de conexão inválido: '{self.name}'. Use letras, números, '_', '-' ou '.' (máx. 63).")
        if not self.url:
            raise ConnectionConfigError(f"Conexão '{self.name}' sem 'url'.")
        self.access_mode = AccessMode(self.access_mode)
        if not self.persistent and self.source == "file":
            self.source = "runtime"  # only file entries are owned (and reloaded) by the config file
        if not self.engine:
            self.engine = Engine.MONGODB if is_mongo_url(self.url) else Engine.POSTGRES
        engine = self.engine.value if isinstance(self.engine, Engine) else str(self.engine).lower()
        try:
            self.engine = Engine(_ENGINE_ALIASES.get(engine, engine))
        except ValueError:
            supported = ", ".join(e.value for e in Engine)
            raise ConnectionConfigError(f"Engine '{engine}' não suportada em '{self.name}'. Suportadas: {supported}.") from None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any], source: str = "file") -> "ConnectionConfig":
        return cls(
            name=name,
            url=data.get("url", ""),
            engine=data.get("engine"),
            access_mode=AccessMode(data.get("access_mode", AccessMode.RESTRICTED.value)),
            description=data.get("description", ""),
            aliases=list(data.get("aliases", [])),
            tags=list(data.get("tags", [])),
            max_pool_size=int(data.get("max_pool_size", 3)),
            query_timeout=int(data.get("query_timeout", 30)),
            persistent=source == "file",
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine.value,
            "url": self.url,
            "access_mode": self.access_mode.value,
            "description": self.description,
            "aliases": self.aliases,
            "tags": self.tags,
            "max_pool_size": self.max_pool_size,
            "query_timeout": self.query_timeout,
        }

    def resolved_url(self) -> str:
        """URL com os placeholders ${VAR} expandidos (só no momento de conectar)."""
        return expand_url(self.url, self.engine)

    def safe_target(self) -> dict[str, Any]:
        """Destino da conexão sem a senha (para exibir ao agente/usuário)."""
        try:
            if self.engine == Engine.MONGODB:
                return mongo_target(self.url)
            info = conninfo_to_dict(self.url)
            return {k: info[k] for k in ("host", "port", "dbname", "user") if info.get(k)}
        except Exception:
            return {"url": obfuscate_password(self.url)}

    def search_keys(self) -> set[str]:
        return {normalize_name(self.name), *(normalize_name(a) for a in self.aliases)}


@dataclass
class ManagedConnection:
    """Estado em tempo de execução de uma conexão."""

    config: ConnectionConfig
    pool: Optional[Union[DbConnPool, MongoConnPool]] = None
    opened_at: Optional[float] = None
    last_used: Optional[float] = None
    last_error: Optional[str] = None
    active_leases: int = 0
    query_count: int = 0

    @property
    def is_open(self) -> bool:
        return self.pool is not None and self.pool.pool is not None


@dataclass
class ConnectionLease:
    """Conexão emprestada para uma chamada de ferramenta."""

    name: str
    access_mode: AccessMode
    driver: Union[SqlDriver, SafeSqlDriver, MongoDriver]
    engine: Engine = Engine.POSTGRES


class ConnectionManager:
    """Registro de conexões nomeadas com pools lazy, reaproveitamento, idle timeout e LRU."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        idle_timeout: Optional[int] = None,
        max_open: Optional[int] = None,
        access_cap: Optional[AccessMode] = None,
        lock_config: bool = False,
        auto_reload: bool = True,
    ):
        self.config_path = config_path
        self.auto_reload = auto_reload
        self.last_reload_error: Optional[str] = None
        self._config_mtime: Optional[int] = None
        self._env_default: Optional[str] = None
        self._idle_timeout_override = idle_timeout
        self._max_open_override = max_open
        self.idle_timeout = idle_timeout if idle_timeout is not None else DEFAULT_IDLE_TIMEOUT
        self.max_open = max_open if max_open is not None else DEFAULT_MAX_OPEN
        self.access_cap = access_cap
        self.lock_config = lock_config
        self.default_name: Optional[str] = None
        self.active_name: Optional[str] = None
        self._connections: dict[str, ManagedConnection] = {}
        self._raw: dict[str, Any] = {}
        self._shadowed: dict[str, ConnectionConfig] = {}  # file entries overridden by env in this session
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ config

    def load(self) -> None:
        """Carrega o arquivo de configuração (se existir)."""
        if not self.config_path or not self.config_path.exists():
            logger.info("Arquivo de conexões não encontrado; iniciando sem conexões configuradas")
            return

        raw, configs = self._read_file()
        self._raw = raw
        self._apply_settings(raw)
        for config in configs.values():
            self.register(config)
        self._resolve_default()
        self._config_mtime = self._file_mtime()

        logger.info(f"{len(self._connections)} conexão(ões) carregada(s) de {self.config_path}")

    def _read_file(self) -> tuple[dict[str, Any], dict[str, ConnectionConfig]]:
        """Lê e valida o arquivo inteiro, sem alterar o estado (falha antes de aplicar qualquer coisa)."""
        if not self.config_path:
            raise ConnectionConfigError("Nenhum arquivo de configuração definido.")
        try:
            with self.config_path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raise ConnectionConfigError(f"Arquivo de conexões não encontrado: {self.config_path}") from None
        except json.JSONDecodeError as e:
            # Only position and reason: never echo file content (it may hold passwords)
            raise ConnectionConfigError(f"JSON inválido em {self.config_path} (linha {e.lineno}, coluna {e.colno}): {e.msg}") from None

        configs = {name: ConnectionConfig.from_dict(name, data) for name, data in raw.get("connections", {}).items()}
        self._check_clashes(list(configs.values()))
        return raw, configs

    def _apply_settings(self, raw: dict[str, Any]) -> None:
        settings = raw.get("settings", {})
        if self._idle_timeout_override is None:
            self.idle_timeout = int(settings.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT))
        if self._max_open_override is None:
            self.max_open = int(settings.get("max_open_connections", DEFAULT_MAX_OPEN))

    def _resolve_default(self) -> None:
        """Padrão = PG_MULTI_MCP_DEFAULT (sessão) ou 'default' do arquivo; some se a conexão não existir mais."""
        name = self._env_default or self._raw.get("default")
        self.default_name = None
        if name:
            try:
                self.default_name = self.resolve(name).config.name
            except ConnectionConfigError:
                logger.warning(f"Conexão padrão '{name}' não existe; nenhuma conexão padrão definida")

    def _file_mtime(self) -> Optional[int]:
        try:
            return self.config_path.stat().st_mtime_ns if self.config_path else None
        except FileNotFoundError:
            return None

    @staticmethod
    def _check_clashes(configs: list[ConnectionConfig]) -> None:
        seen: dict[str, str] = {}
        for config in configs:
            for key in config.search_keys():
                other = seen.get(key)
                if other and other != config.name:
                    raise ConnectionConfigError(f"Nome/alias '{key}' usado por '{other}' e '{config.name}'.")
                seen[key] = config.name

    async def reload(self) -> dict[str, list[str]]:
        """Relê o arquivo e aplica a diferença.

        Conexões inalteradas mantêm o pool aberto; alteradas e removidas têm o pool fechado.
        Conexões da env / de sessão são preservadas (e continuam sobrepondo as do arquivo).
        """
        raw, file_configs = self._read_file()

        async with self._lock:
            current_file = {name: mc for name, mc in self._connections.items() if mc.config.source == "file"}
            old_defs = {name: mc.config for name, mc in current_file.items()} | self._shadowed

            report: dict[str, list[str]] = {"added": [], "changed": [], "removed": [], "unchanged": []}
            for name in sorted(old_defs.keys() - file_configs.keys()):
                report["removed"].append(name)
            for name, config in sorted(file_configs.items()):
                if name not in old_defs:
                    report["added"].append(name)
                elif config.to_dict() != old_defs[name].to_dict():
                    report["changed"].append(name)
                else:
                    report["unchanged"].append(name)

            new_connections = {name: mc for name, mc in self._connections.items() if mc.config.source != "file"}
            new_shadowed: dict[str, ConnectionConfig] = {}
            for name, config in file_configs.items():
                if name in new_connections:
                    new_shadowed[name] = config  # still overridden by env/session
                elif name in report["unchanged"] and name in current_file:
                    new_connections[name] = current_file[name]  # keep pool and stats
                else:
                    new_connections[name] = ManagedConnection(config=config)
            self._check_clashes([mc.config for mc in new_connections.values()])

            for name, mc in current_file.items():
                if new_connections.get(name) is not mc:
                    await self._close_unlocked(mc)

            self._connections = new_connections
            self._shadowed = new_shadowed
            self._raw = raw
            self._apply_settings(raw)
            if self.active_name not in self._connections:
                self.active_name = None
            self._resolve_default()
            self._config_mtime = self._file_mtime()
            self.last_reload_error = None

        logger.info(f"Conexões recarregadas de {self.config_path}: {report}")
        return report

    async def refresh_if_changed(self) -> Optional[dict[str, list[str]]]:
        """Recarrega automaticamente se o arquivo mudou desde a última leitura/gravação."""
        if not self.auto_reload or not self.config_path:
            return None
        mtime = self._file_mtime()
        if mtime == self._config_mtime:
            return None
        try:
            return await self.reload()
        except Exception as e:
            # Keep the previous (valid) registry; retry only when the file changes again
            self._config_mtime = mtime
            self.last_reload_error = str(e)
            logger.error(f"Falha ao recarregar conexões; mantendo configuração anterior: {e}")
            return None

    def load_env(self, environ: Optional[Mapping[str, str]] = None) -> None:
        """Carrega conexões de variáveis PG_MULTI_MCP_CONN_<NOME> (ex.: bloco "env" do .mcp.json).

        O valor é uma URL/DSN ou um objeto JSON com os mesmos campos do arquivo. Essas conexões existem
        só na sessão; se o nome já existir no arquivo, a da env prevalece (a do arquivo é preservada no disco).
        PG_MULTI_MCP_DEFAULT define a conexão padrão da sessão.
        """
        environ = os.environ if environ is None else environ
        for key, value in sorted(environ.items()):
            if not key.upper().startswith(ENV_CONNECTION_PREFIX) or not value.strip():
                continue
            name = key[len(ENV_CONNECTION_PREFIX) :].lower()
            try:
                data = json.loads(value) if value.lstrip().startswith("{") else {"url": value}
            except json.JSONDecodeError as e:
                raise ConnectionConfigError(f"{key}: JSON inválido ({e.msg})") from None
            config = ConnectionConfig.from_dict(name, data, source="env")

            existing = self._connections.get(name)
            if existing and existing.config.persistent:
                logger.warning(f"Conexão '{name}' do arquivo sobreposta por {key} nesta sessão")
                self._shadowed[name] = existing.config
            self.register(config, replace=existing is not None)

        default = environ.get(ENV_DEFAULT_CONNECTION)
        if default:
            self.default_name = self.resolve(default).config.name
            self._env_default = default

    def save(self) -> None:
        """Grava as conexões persistentes no arquivo (escrita atômica)."""
        if not self.config_path:
            raise ConnectionConfigError("Nenhum arquivo de configuração definido; não é possível persistir.")

        data = dict(self._raw)
        persisted = {name: config.to_dict() for name, config in self._shadowed.items() if name in self._connections}
        persisted.update({mc.config.name: mc.config.to_dict() for mc in self._connections.values() if mc.config.persistent})
        data["connections"] = dict(sorted(persisted.items()))
        # The default stays as written in the file (an env default is session-only), unless it was removed
        if data.get("default") not in data["connections"]:
            data["default"] = None

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.config_path)
        self._raw = data
        self._config_mtime = self._file_mtime()

    def register(self, config: ConnectionConfig, replace: bool = False) -> None:
        for mc in self._connections.values():
            if replace and mc.config.name == config.name:
                continue
            clash = config.search_keys() & mc.config.search_keys()
            if clash:
                raise ConnectionConfigError(f"Nome/alias de '{config.name}' conflita com a conexão existente '{mc.config.name}'.")
        self._connections[config.name] = ManagedConnection(config=config)

    # ------------------------------------------------------------------ resolução

    @property
    def names(self) -> list[str]:
        return sorted(self._connections)

    def effective_access_mode(self, mc: ManagedConnection) -> AccessMode:
        if self.access_cap == AccessMode.RESTRICTED:
            return AccessMode.RESTRICTED
        return mc.config.access_mode

    def resolve(self, name: Optional[str]) -> ManagedConnection:
        """Resolve nome/alias (sem diferenciar maiúsculas/acentos). Sem nome: ativa > padrão > única."""
        if not name:
            name = self.active_name or self.default_name
            if not name:
                if len(self._connections) == 1:
                    return next(iter(self._connections.values()))
                if not self._connections:
                    raise ConnectionConfigError("Nenhuma conexão configurada. Use add_connection ou edite o arquivo de conexões.")
                raise ConnectionConfigError(
                    "Há várias conexões e nenhuma está ativa. Informe o parâmetro 'connection' ou chame use_connection. "
                    f"Disponíveis: {', '.join(self.names)}"
                )

        if name in self._connections:
            return self._connections[name]

        key = normalize_name(name)
        matches = [mc for mc in self._connections.values() if key in mc.config.search_keys()]
        if len(matches) == 1:
            return matches[0]

        lookup = {k: mc.config.name for mc in self._connections.values() for k in mc.config.search_keys()}
        close = difflib.get_close_matches(key, list(lookup), n=3, cutoff=0.6)
        partial = [mc.config.name for mc in self._connections.values() if key and any(key in k for k in mc.config.search_keys())]
        suggestions = sorted({lookup[k] for k in close} | set(partial))
        hint = f" Você quis dizer: {', '.join(suggestions)}?" if suggestions else ""
        raise ConnectionConfigError(f"Conexão '{name}' não encontrada.{hint} Use list_connections para ver todas.")

    def search(self, term: Optional[str]) -> list[ManagedConnection]:
        """Filtra conexões por termo em nome, alias, tag ou descrição."""
        items = sorted(self._connections.values(), key=lambda mc: mc.config.name)
        if not term:
            return items
        key = normalize_name(term)
        result = []
        for mc in items:
            haystack = mc.config.search_keys() | {normalize_name(t) for t in mc.config.tags} | {normalize_name(mc.config.description)}
            if any(key in h for h in haystack):
                result.append(mc)
        return result

    def describe(self, mc: ManagedConnection) -> dict[str, Any]:
        now = time.monotonic()
        status = "aberta" if mc.is_open else ("erro" if mc.last_error else "fechada")
        info: dict[str, Any] = {
            "name": mc.config.name,
            "active": mc.config.name == self.active_name,
            "default": mc.config.name == self.default_name,
            "engine": mc.config.engine.value,
            "description": mc.config.description,
            "aliases": mc.config.aliases,
            "tags": mc.config.tags,
            "access_mode": self.effective_access_mode(mc).value,
            "target": mc.config.safe_target(),
            "status": status,
            "source": mc.config.source,
        }
        if mc.is_open:
            info["open_for_seconds"] = int(now - mc.opened_at) if mc.opened_at else None
            info["idle_seconds"] = int(now - mc.last_used) if mc.last_used else None
            info["in_use"] = mc.active_leases
            if mc.pool and mc.pool.server_version:
                info["server_version"] = mc.pool.server_version
        if mc.query_count:
            info["calls"] = mc.query_count
        if mc.last_error and not mc.is_open:
            info["last_error"] = mc.last_error
        return info

    # ------------------------------------------------------------------ ciclo de vida

    @contextlib.asynccontextmanager
    async def lease(self, name: Optional[str], require: Optional[Engine] = None) -> AsyncIterator[ConnectionLease]:
        """Empresta o driver da conexão (abrindo o pool se preciso) durante a chamada.

        require: motor exigido pela ferramenta; checado antes de abrir o pool.
        """
        await self.refresh_if_changed()
        mc = self.resolve(name)
        if require and mc.config.engine != require:
            raise ConnectionConfigError(f"A conexão '{mc.config.name}' é {mc.config.engine.value}; esta ferramenta só funciona com {require.value}.")
        await self._ensure_open(mc)
        assert mc.pool is not None

        mc.active_leases += 1
        mc.last_used = time.monotonic()
        try:
            access_mode = self.effective_access_mode(mc)
            restricted = access_mode == AccessMode.RESTRICTED
            driver: Union[SqlDriver, SafeSqlDriver, MongoDriver]
            if mc.config.engine == Engine.MONGODB:
                driver = MongoDriver(conn=mc.pool, readonly=restricted, timeout=mc.config.query_timeout if restricted else None)  # type: ignore[arg-type]
            else:
                driver = SqlDriver(conn=mc.pool)
                if restricted:
                    driver = SafeSqlDriver(sql_driver=driver, timeout=mc.config.query_timeout)
            yield ConnectionLease(name=mc.config.name, access_mode=access_mode, driver=driver, engine=mc.config.engine)
        finally:
            mc.active_leases -= 1
            mc.query_count += 1
            mc.last_used = time.monotonic()

    async def open(self, name: Optional[str]) -> ManagedConnection:
        await self.refresh_if_changed()
        mc = self.resolve(name)
        await self._ensure_open(mc)
        return mc

    async def _ensure_open(self, mc: ManagedConnection) -> None:
        async with self._lock:
            if mc.is_open:
                return
            await self._evict_lru_unlocked(exclude=mc)

            url = mc.config.resolved_url()
            pool_cls = MongoConnPool if mc.config.engine == Engine.MONGODB else DbConnPool
            pool = pool_cls(url, min_size=1, max_size=mc.config.max_pool_size, connect_timeout=CONNECT_TIMEOUT)
            try:
                await probe_connection(url, mc.config.engine)
                await pool.pool_connect()
            except Exception as e:
                mc.last_error = obfuscate_password(str(e))
                raise ConnectionConfigError(f"Falha ao conectar em '{mc.config.name}': {mc.last_error}") from e

            mc.pool = pool
            mc.last_error = None
            mc.opened_at = mc.last_used = time.monotonic()
            logger.info(f"Pool aberto para '{mc.config.name}'")

    async def _evict_lru_unlocked(self, exclude: ManagedConnection) -> None:
        open_conns = [m for m in self._connections.values() if m is not exclude and m.is_open]
        while len(open_conns) >= self.max_open:
            idle = [m for m in open_conns if m.active_leases == 0]
            if not idle:
                logger.warning("Limite de conexões abertas atingido, mas todas estão em uso; abrindo além do limite")
                return
            lru = min(idle, key=lambda m: m.last_used or 0)
            logger.info(f"Fechando '{lru.config.name}' (LRU) para respeitar max_open={self.max_open}")
            await self._close_unlocked(lru)
            open_conns.remove(lru)

    async def _close_unlocked(self, mc: ManagedConnection) -> bool:
        if mc.pool is None:
            return False
        was_open = mc.is_open
        try:
            await mc.pool.close()
        finally:
            mc.pool = None
            mc.opened_at = None
        return was_open

    async def close(self, name: Optional[str]) -> ManagedConnection:
        mc = self.resolve(name)
        async with self._lock:
            await self._close_unlocked(mc)
        return mc

    async def close_all(self) -> list[str]:
        closed = []
        async with self._lock:
            for mc in self._connections.values():
                if await self._close_unlocked(mc):
                    closed.append(mc.config.name)
        return closed

    async def close_idle(self) -> list[str]:
        """Fecha pools sem uso há mais de idle_timeout segundos."""
        if self.idle_timeout <= 0:
            return []
        now = time.monotonic()
        closed = []
        async with self._lock:
            for mc in self._connections.values():
                if mc.is_open and mc.active_leases == 0 and mc.last_used and now - mc.last_used > self.idle_timeout:
                    await self._close_unlocked(mc)
                    closed.append(mc.config.name)
        for name in closed:
            logger.info(f"Pool de '{name}' fechado por inatividade")
        return closed

    async def run_idle_reaper(self, interval: int = 30) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.close_idle()
            except Exception as e:
                logger.error(f"Erro ao fechar conexões ociosas: {e}")

    # ------------------------------------------------------------------ edição

    async def _check_editable(self) -> None:
        if self.lock_config:
            raise ConnectionConfigError("Configuração travada (--lock-config): não é permitido adicionar/remover conexões.")
        # Pick up manual edits first, so saving from memory never overwrites them
        await self.refresh_if_changed()
        if self.last_reload_error:
            raise ConnectionConfigError(
                f"O arquivo de conexões tem erro e não foi recarregado; corrija-o antes de adicionar/remover. {self.last_reload_error}"
            )

    async def add(self, config: ConnectionConfig, overwrite: bool = False) -> ManagedConnection:
        await self._check_editable()
        if config.persistent and not self.config_path:
            raise ConnectionConfigError("Nenhum arquivo de configuração definido; use persist=false ou inicie com --config.")
        if config.name in self._connections:
            if not overwrite:
                raise ConnectionConfigError(f"Já existe a conexão '{config.name}'. Use overwrite=true para substituir.")
            await self.close(config.name)
        if not config.persistent:
            config.source = "runtime"
        self.register(config, replace=overwrite)
        if config.persistent:
            self._shadowed.pop(config.name, None)
            self.save()
        return self._connections[config.name]

    async def remove(self, name: str) -> ConnectionConfig:
        await self._check_editable()
        mc = await self.close(name)
        del self._connections[mc.config.name]
        if self.active_name == mc.config.name:
            self.active_name = None
        if self.default_name == mc.config.name:
            self.default_name = None
        shadowed = self._shadowed.pop(mc.config.name, None)
        if shadowed is not None:
            # Removing an env override brings back the file definition instead of deleting it
            self._connections[shadowed.name] = ManagedConnection(config=shadowed)
        elif mc.config.persistent:
            self.save()
        return mc.config

    def set_active(self, name: str) -> ManagedConnection:
        mc = self.resolve(name)
        self.active_name = mc.config.name
        return mc
