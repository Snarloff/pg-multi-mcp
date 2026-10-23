# ruff: noqa: B008
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from typing import List
from typing import Optional

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic.fields import FieldInfo

from .connections import DEFAULT_CONFIG_PATH
from .connections import AccessMode
from .connections import ConnectionConfig
from .connections import ConnectionLease
from .connections import ConnectionManager
from .connections import Engine
from .connections import expand_env
from .connections import has_literal_password
from .connections import probe_connection
from .postgres.artifacts import ErrorResult
from .postgres.artifacts import ExplainPlanArtifact
from .postgres.database_health import DatabaseHealthTool
from .postgres.database_health import HealthType
from .postgres.explain import ExplainPlanTool
from .postgres.index.dta_calc import DatabaseTuningAdvisor
from .postgres.index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .postgres.index.presentation import TextPresentation
from .postgres.sql import SafeSqlDriver
from .postgres.sql import check_hypopg_installation_status
from .postgres.sql import get_postgres_version
from .postgres.sql import obfuscate_password
from .postgres.top_queries import TopQueriesCalc

INSTRUCTIONS = """\
Servidor MCP com MÚLTIPLAS conexões PostgreSQL nomeadas.

Fluxo recomendado:
1. Quando o usuário citar um banco, sistema, projeto ou ambiente, chame list_connections(search="<termo>")
   para descobrir o nome exato da conexão (a busca considera nome, aliases, tags e descrição, sem acento).
2. Passe SEMPRE o parâmetro `connection` explicitamente nas ferramentas de banco quando houver mais de uma conexão.
   Alternativamente, use_connection define a conexão ativa da sessão.
3. Toda resposta começa com [conexão: X | modo: Y]; confirme ao usuário em qual banco a operação rodou.
4. Pools são reaproveitados automaticamente e fechados por inatividade. Use close_connection quando terminar um
   trabalho longo ou se o usuário pedir.
   O arquivo de conexões é recarregado sozinho quando editado; reload_connections força a releitura e mostra o que mudou.
5. Conexões em modo 'restricted' são somente leitura. Nunca tente contornar essa restrição.
6. Não peça nem repita senhas no chat. Para novas conexões prefira placeholders ${VAR} de variável de ambiente.
"""

# Initialize FastMCP with default settings
mcp = FastMCP("pg-multi-mcp", instructions=INSTRUCTIONS)

# Constants
PG_STAT_STATEMENTS = "pg_stat_statements"
HYPOPG_EXTENSION = "hypopg"
DEFAULT_MAX_ROWS = 500

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]

logger = logging.getLogger(__name__)

# Global connection registry (replaced in main() with the configured one)
connections = ConnectionManager()


def connection_field() -> FieldInfo:
    return Field(
        description="Nome ou alias da conexão (veja list_connections). Se omitido, usa a conexão ativa ou a padrão.",
        default=None,
    )


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=1, default=str)


def format_text_response(text: Any, lease: Optional[ConnectionLease] = None) -> ResponseType:
    """Format a text response, prefixed with the connection it ran on."""
    body = _to_text(text)
    if lease is not None:
        body = f"[conexão: {lease.name} | modo: {lease.access_mode.value}]\n{body}"
    return [types.TextContent(type="text", text=body)]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {obfuscate_password(error)}")


# ---------------------------------------------------------------------------
# Gerenciamento de conexões
# ---------------------------------------------------------------------------


@mcp.tool(
    description="Lista as conexões configuradas (nome, aliases, tags, descrição, destino sem senha, modo de acesso, "
    "status do pool, conexão ativa/padrão). Use 'search' para filtrar por nome, alias, tag ou descrição.",
    annotations=ToolAnnotations(title="List Connections", readOnlyHint=True),
)
async def list_connections(
    search: Optional[str] = Field(description="Termo de busca opcional (ignora maiúsculas e acentos)", default=None),
) -> ResponseType:
    try:
        await connections.refresh_if_changed()
        items = [connections.describe(mc) for mc in connections.search(search)]
        result: dict[str, Any] = {
            "active": connections.active_name,
            "default": connections.default_name,
            "open_pools": sum(1 for mc in connections.search(None) if mc.is_open),
            "max_open": connections.max_open,
            "idle_timeout_seconds": connections.idle_timeout,
            "connections": items,
        }
        if connections.last_reload_error:
            result["config_error"] = f"Última edição do arquivo não aplicada (usando a versão anterior): {connections.last_reload_error}"
        if search and not items:
            result["hint"] = f"Nenhuma conexão corresponde a '{search}'. Disponíveis: {', '.join(connections.names)}"
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error listing connections: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Relê o arquivo de conexões agora e informa o que mudou (added/changed/removed/unchanged). "
    "Pools de conexões inalteradas continuam abertos; os de alteradas/removidas são fechados. "
    "Normalmente não é necessário: o arquivo é recarregado sozinho quando muda.",
    annotations=ToolAnnotations(title="Reload Connections", readOnlyHint=True),
)
async def reload_connections() -> ResponseType:
    try:
        report = await connections.reload()
        return format_text_response({"config": str(connections.config_path), **report, "total": len(connections.names)})
    except Exception as e:
        connections.last_reload_error = str(e)
        logger.error(f"Error reloading connections: {e}")
        return format_error_response(f"Arquivo não recarregado (configuração anterior mantida): {e}")


@mcp.tool(
    description="Define a conexão ativa da sessão (usada quando 'connection' é omitido) e valida que ela abre.",
    annotations=ToolAnnotations(title="Use Connection", readOnlyHint=True),
)
async def use_connection(
    connection: str = Field(description="Nome ou alias da conexão"),
) -> ResponseType:
    try:
        mc = await connections.open(connection)
        connections.set_active(mc.config.name)
        return format_text_response({"message": f"Conexão ativa: {mc.config.name}", **connections.describe(mc)})
    except Exception as e:
        logger.error(f"Error selecting connection: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Abre (ou reaproveita) o pool da conexão e testa: retorna banco, usuário, versão e latência.",
    annotations=ToolAnnotations(title="Open Connection", readOnlyHint=True),
)
async def open_connection(
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    try:
        started = time.perf_counter()
        async with connections.lease(connection) as lease:
            rows = await lease.driver.execute_query(
                "SELECT current_database() AS database, current_user AS user, version() AS version"
            )
            await get_postgres_version(lease.driver)
            info = rows[0].cells if rows else {}
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return format_text_response(info, lease)
    except Exception as e:
        logger.error(f"Error opening connection: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Fecha o pool de uma conexão (libera as conexões no servidor). Use close_all=true para fechar todas. "
    "A configuração não é removida; a conexão reabre automaticamente no próximo uso.",
    annotations=ToolAnnotations(title="Close Connection", readOnlyHint=True),
)
async def close_connection(
    connection: Optional[str] = Field(description="Nome ou alias da conexão a fechar", default=None),
    close_all: bool = Field(description="Fecha todos os pools abertos", default=False),
) -> ResponseType:
    try:
        if close_all:
            closed = await connections.close_all()
            return format_text_response({"closed": closed})
        if not connection:
            return format_error_response("Informe 'connection' ou use close_all=true.")
        mc = await connections.close(connection)
        return format_text_response({"closed": mc.config.name, "status": connections.describe(mc)["status"]})
    except Exception as e:
        logger.error(f"Error closing connection: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Adiciona uma conexão nomeada. A 'url' aceita URI (postgresql://...) ou DSN (host=... dbname=...) e "
    "placeholders ${VAR} resolvidos de variáveis de ambiente — prefira-os a senhas literais. "
    "Por padrão testa a conexão antes de salvar e grava no arquivo de configuração (persist=true).",
    annotations=ToolAnnotations(title="Add Connection", destructiveHint=False, idempotentHint=False),
)
async def add_connection(
    name: str = Field(description="Nome único (letras, números, '_', '-', '.')"),
    url: str = Field(description="URI/DSN da conexão; use ${VAR} para segredos"),
    access_mode: str = Field(description="'restricted' (somente leitura, padrão) ou 'unrestricted'", default="restricted"),
    description: str = Field(description="Descrição livre (sistema, ambiente, finalidade)", default=""),
    aliases: list[str] = Field(description="Apelidos usados para encontrar a conexão", default=[]),
    tags: list[str] = Field(description="Tags para busca/agrupamento (ex.: 'producao', 'homologacao')", default=[]),
    persist: bool = Field(description="Grava no arquivo de configuração", default=True),
    test: bool = Field(description="Testa a conexão antes de manter", default=True),
    overwrite: bool = Field(description="Substitui conexão existente com o mesmo nome", default=False),
    engine: str = Field(description=f"Motor do banco: {', '.join(e.value for e in Engine)}", default=Engine.POSTGRES.value),
) -> ResponseType:
    try:
        config = ConnectionConfig(
            name=name,
            url=url,
            engine=engine,
            access_mode=AccessMode(access_mode),
            description=description,
            aliases=aliases,
            tags=tags,
            persistent=persist,
        )
        # Test before registering, so a failed overwrite keeps the previous definition
        if test:
            try:
                await probe_connection(expand_env(url))
            except Exception as e:
                return format_error_response(f"Conexão não adicionada: {e}")
        mc = await connections.add(config, overwrite=overwrite)
        warnings = []
        if persist and has_literal_password(url):
            warnings.append(f"A senha foi gravada em texto puro em {connections.config_path}. Prefira ${{VAR}} ou pgpass.")
        return format_text_response({"added": connections.describe(mc), "warnings": warnings})
    except Exception as e:
        logger.error(f"Error adding connection: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Remove uma conexão do registro (fecha o pool e, se veio do arquivo, apaga do arquivo de configuração). "
    "Conexões definidas por variável de ambiente (.mcp.json) só saem desta sessão.",
    annotations=ToolAnnotations(title="Remove Connection", destructiveHint=True),
)
async def remove_connection(
    connection: str = Field(description="Nome ou alias da conexão a remover"),
) -> ResponseType:
    try:
        config = await connections.remove(connection)
        return format_text_response({"removed": config.name, "source": config.source})
    except Exception as e:
        logger.error(f"Error removing connection: {e}")
        return format_error_response(str(e))


# ---------------------------------------------------------------------------
# Ferramentas de banco
# ---------------------------------------------------------------------------


@mcp.tool(
    description="List all schemas in the database",
    annotations=ToolAnnotations(
        title="List Schemas",
        readOnlyHint=True,
    ),
)
async def list_schemas(
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """List all schemas in the database."""
    try:
        async with connections.lease(connection) as lease:
            rows = await lease.driver.execute_query(
                """
                SELECT
                    schema_name,
                    schema_owner,
                    CASE
                        WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                        WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                        ELSE 'User Schema'
                    END as schema_type
                FROM information_schema.schemata
                ORDER BY schema_type, schema_name
                """
            )
            schemas = [row.cells for row in rows] if rows else []
            return format_text_response(schemas, lease)
    except Exception as e:
        logger.error(f"Error listing schemas: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List objects in a schema",
    annotations=ToolAnnotations(
        title="List Objects",
        readOnlyHint=True,
    ),
)
async def list_objects(
    schema_name: str = Field(description="Schema name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """List objects of a given type in a schema."""
    try:
        async with connections.lease(connection) as lease:
            sql_driver = lease.driver

            if object_type in ("table", "view"):
                table_type = "BASE TABLE" if object_type == "table" else "VIEW"
                rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT table_schema, table_name, table_type
                    FROM information_schema.tables
                    WHERE table_schema = {} AND table_type = {}
                    ORDER BY table_name
                    """,
                    [schema_name, table_type],
                )
                objects = (
                    [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                    if rows
                    else []
                )

            elif object_type == "sequence":
                rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT sequence_schema, sequence_name, data_type
                    FROM information_schema.sequences
                    WHERE sequence_schema = {}
                    ORDER BY sequence_name
                    """,
                    [schema_name],
                )
                objects = (
                    [
                        {"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]}
                        for row in rows
                    ]
                    if rows
                    else []
                )

            elif object_type == "extension":
                # Extensions are not schema-specific
                rows = await sql_driver.execute_query(
                    """
                    SELECT extname, extversion, extrelocatable
                    FROM pg_extension
                    ORDER BY extname
                    """
                )
                objects = (
                    [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
                    if rows
                    else []
                )

            else:
                return format_error_response(f"Unsupported object type: {object_type}")

            return format_text_response(objects, lease)
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Show detailed information about a database object",
    annotations=ToolAnnotations(
        title="Get Object Details",
        readOnlyHint=True,
    ),
)
async def get_object_details(
    schema_name: str = Field(description="Schema name"),
    object_name: str = Field(description="Object name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """Get detailed information about a database object."""
    try:
        async with connections.lease(connection) as lease:
            sql_driver = lease.driver

            if object_type in ("table", "view"):
                # Get columns
                col_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = {} AND table_name = {}
                    ORDER BY ordinal_position
                    """,
                    [schema_name, object_name],
                )
                columns = (
                    [
                        {
                            "column": r.cells["column_name"],
                            "data_type": r.cells["data_type"],
                            "is_nullable": r.cells["is_nullable"],
                            "default": r.cells["column_default"],
                        }
                        for r in col_rows
                    ]
                    if col_rows
                    else []
                )

                # Get constraints
                con_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                    FROM information_schema.table_constraints AS tc
                    LEFT JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema = {} AND tc.table_name = {}
                    """,
                    [schema_name, object_name],
                )

                constraints = {}
                if con_rows:
                    for row in con_rows:
                        cname = row.cells["constraint_name"]
                        ctype = row.cells["constraint_type"]
                        col = row.cells["column_name"]

                        if cname not in constraints:
                            constraints[cname] = {"type": ctype, "columns": []}
                        if col:
                            constraints[cname]["columns"].append(col)

                constraints_list = [{"name": name, **data} for name, data in constraints.items()]

                # Get indexes
                idx_rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = {} AND tablename = {}
                    """,
                    [schema_name, object_name],
                )

                indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

                result = {
                    "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                    "columns": columns,
                    "constraints": constraints_list,
                    "indexes": indexes,
                }

            elif object_type == "sequence":
                rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT sequence_schema, sequence_name, data_type, start_value, increment
                    FROM information_schema.sequences
                    WHERE sequence_schema = {} AND sequence_name = {}
                    """,
                    [schema_name, object_name],
                )

                if rows and rows[0]:
                    row = rows[0]
                    result = {
                        "schema": row.cells["sequence_schema"],
                        "name": row.cells["sequence_name"],
                        "data_type": row.cells["data_type"],
                        "start_value": row.cells["start_value"],
                        "increment": row.cells["increment"],
                    }
                else:
                    result = {}

            elif object_type == "extension":
                rows = await SafeSqlDriver.execute_param_query(
                    sql_driver,
                    """
                    SELECT extname, extversion, extrelocatable
                    FROM pg_extension
                    WHERE extname = {}
                    """,
                    [object_name],
                )

                if rows and rows[0]:
                    row = rows[0]
                    result = {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
                else:
                    result = {}

            else:
                return format_error_response(f"Unsupported object type: {object_type}")

            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error getting object details: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates.",
    annotations=ToolAnnotations(
        title="Explain Query",
        readOnlyHint=True,
    ),
)
async def explain_query(
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. "
        "Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] = Field(
        description="""A list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]
If there is no hypothetical index, you can pass an empty list.""",
        default=[],
    ),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """
    Explains the execution plan for a SQL query.

    Args:
        sql: The SQL query to explain
        analyze: When True, actually runs the query for real statistics
        hypothetical_indexes: Optional list of indexes to simulate
        connection: Connection name or alias
    """
    try:
        async with connections.lease(connection) as lease:
            sql_driver = lease.driver
            explain_tool = ExplainPlanTool(sql_driver=sql_driver)
            result: ExplainPlanArtifact | ErrorResult | None = None

            # If hypothetical indexes are specified, check for HypoPG extension
            if hypothetical_indexes and len(hypothetical_indexes) > 0:
                if analyze:
                    return format_error_response("Cannot use analyze and hypothetical indexes together")

                # Use the common utility function to check if hypopg is installed
                (
                    is_hypopg_installed,
                    hypopg_message,
                ) = await check_hypopg_installation_status(sql_driver)

                # If hypopg is not installed, return the message
                if not is_hypopg_installed:
                    return format_text_response(hypopg_message, lease)

                # HypoPG is installed, proceed with explaining with hypothetical indexes
                result = await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
            elif analyze:
                # Use EXPLAIN ANALYZE
                result = await explain_tool.explain_analyze(sql)
            else:
                # Use basic EXPLAIN
                result = await explain_tool.explain(sql)

            if result and isinstance(result, ExplainPlanArtifact):
                return format_text_response(result.to_text(), lease)
            else:
                error_message = "Error processing explain plan"
                if isinstance(result, ErrorResult):
                    error_message = result.to_text()
                return format_error_response(error_message)
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


# Query function declaration without the decorator - we'll add it dynamically based on access mode
async def execute_sql(
    sql: str = Field(description="SQL to run"),
    connection: Optional[str] = connection_field(),
    max_rows: int = Field(description="Máximo de linhas retornadas (o excedente é truncado)", default=DEFAULT_MAX_ROWS),
) -> ResponseType:
    """Executes a SQL query against the database."""
    try:
        async with connections.lease(connection) as lease:
            rows = await lease.driver.execute_query(sql)  # type: ignore
            if rows is None:
                return format_text_response("No results", lease)
            result: Any = [r.cells for r in rows[:max_rows]]
            if len(rows) > max_rows:
                result = {"rows": result, "truncated": True, "total_rows": len(rows), "returned_rows": max_rows}
            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze frequently executed queries in the database and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Workload Indexes",
        readOnlyHint=True,
    ),
)
async def analyze_workload_indexes(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        async with connections.lease(connection) as lease:
            index_tuning = DatabaseTuningAdvisor(lease.driver)
            dta_tool = TextPresentation(lease.driver, index_tuning)
            result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Query Indexes",
        readOnlyHint=True,
    ),
)
async def analyze_query_indexes(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    if len(queries) == 0:
        return format_error_response("Please provide a non-empty list of queries to analyze.")
    if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
        return format_error_response(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")

    try:
        async with connections.lease(connection) as lease:
            index_tuning = DatabaseTuningAdvisor(lease.driver)
            dta_tool = TextPresentation(lease.driver, index_tuning)
            result = await dta_tool.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)
            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyzes database health. Here are the available health checks:\n"
    "- index - checks for invalid, duplicate, and bloated indexes\n"
    "- connection - checks the number of connection and their utilization\n"
    "- vacuum - checks vacuum health for transaction id wraparound\n"
    "- sequence - checks sequences at risk of exceeding their maximum value\n"
    "- replication - checks replication health including lag and slots\n"
    "- buffer - checks for buffer cache hit rates for indexes and tables\n"
    "- constraint - checks for invalid constraints\n"
    "- all - runs all checks\n"
    "You can optionally specify a single health check or a comma-separated list of health checks. The default is 'all' checks.",
    annotations=ToolAnnotations(
        title="Analyze Database Health",
        readOnlyHint=True,
    ),
)
async def analyze_db_health(
    health_type: str = Field(
        description=f"Optional. Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
        default="all",
    ),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    """Analyze database health for specified components."""
    try:
        async with connections.lease(connection) as lease:
            health_tool = DatabaseHealthTool(lease.driver)
            result = await health_tool.health(health_type=health_type)
            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error analyzing database health: {e}")
        return format_error_response(str(e))


@mcp.tool(
    name="get_top_queries",
    description=f"Reports the slowest or most resource-intensive queries using data from the '{PG_STAT_STATEMENTS}' extension.",
    annotations=ToolAnnotations(
        title="Get Top Queries",
        readOnlyHint=True,
    ),
)
async def get_top_queries(
    sort_by: str = Field(
        description="Ranking criteria: 'total_time' for total execution time or 'mean_time' for mean execution time per call, or 'resources' "
        "for resource-intensive queries",
        default="resources",
    ),
    limit: int = Field(description="Number of queries to return when ranking based on mean_time or total_time", default=10),
    connection: Optional[str] = connection_field(),
) -> ResponseType:
    try:
        async with connections.lease(connection) as lease:
            top_queries_tool = TopQueriesCalc(sql_driver=lease.driver)

            if sort_by == "resources":
                result = await top_queries_tool.get_top_resource_queries()
            elif sort_by == "mean_time" or sort_by == "total_time":
                # Map the sort_by values to what get_top_queries_by_time expects
                result = await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")
            else:
                return format_error_response("Invalid sort criteria. Please use 'resources' or 'mean_time' or 'total_time'.")
            return format_text_response(result, lease)
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server (multi-conexão)")
    parser.add_argument(
        "--config",
        type=lambda value: Path(value).expanduser(),
        default=Path(os.environ.get("PG_MULTI_MCP_CONFIG", DEFAULT_CONFIG_PATH)).expanduser(),
        help=f"Arquivo JSON de conexões (padrão: $PG_MULTI_MCP_CONFIG ou {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=None,
        help="Teto global: 'restricted' força TODAS as conexões a somente leitura. Omitido = modo de cada conexão.",
    )
    parser.add_argument("--idle-timeout", type=int, default=None, help="Segundos de inatividade até fechar um pool (0 = nunca)")
    parser.add_argument("--max-open", type=int, default=None, help="Máximo de pools abertos simultaneamente (LRU)")
    parser.add_argument("--lock-config", action="store_true", help="Impede add/remove de conexões pelas ferramentas")
    parser.add_argument("--no-auto-reload", action="store_true", help="Não recarrega o arquivo de conexões quando ele muda")
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Select MCP transport: stdio (default) or streamable-http",
    )
    parser.add_argument("--host", type=str, default="localhost", help="Host do streamable HTTP (padrão: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Porta do streamable HTTP (padrão: 8000)")
    parser.add_argument("--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def register_execute_sql(access_cap: Optional[AccessMode]) -> None:
    """Add the query tool with a description and annotations appropriate to the access mode."""
    if access_cap == AccessMode.RESTRICTED:
        mcp.add_tool(
            execute_sql,
            description="Execute a read-only SQL query on the chosen connection",
            annotations=ToolAnnotations(title="Execute SQL (Read-Only)", readOnlyHint=True),
        )
    else:
        mcp.add_tool(
            execute_sql,
            description="Execute SQL on the chosen connection. Connections in 'restricted' mode only accept read-only SQL "
            "(validated, READ ONLY transaction, timeout); 'unrestricted' connections accept any SQL (DML/DDL).",
            annotations=ToolAnnotations(title="Execute SQL", destructiveHint=True),
        )


async def main(argv: Optional[list[str]] = None):
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    global connections
    access_cap = AccessMode(args.access_mode) if args.access_mode else None
    connections = ConnectionManager(
        config_path=args.config,
        idle_timeout=args.idle_timeout,
        max_open=args.max_open,
        access_cap=access_cap,
        lock_config=args.lock_config,
        auto_reload=not args.no_auto_reload,
    )
    connections.load()
    # Connections declared in the MCP client config (.mcp.json "env": PG_MULTI_MCP_CONN_<NAME>)
    connections.load_env()

    # Compatibility: DATABASE_URI becomes a session-only connection named "default"
    database_url = os.environ.get("DATABASE_URI")
    if database_url and "default" not in connections.names:
        connections.register(ConnectionConfig(name="default", url=database_url, persistent=False, source="env"))

    register_execute_sql(access_cap)
    logger.info(
        f"Starting pg-multi-mcp with {len(connections.names)} connection(s); access cap: {access_cap.value if access_cap else 'per-connection'}"
    )

    reaper = asyncio.create_task(connections.run_idle_reaper())
    try:
        if args.transport == "stdio":
            await mcp.run_stdio_async()
        else:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            await mcp.run_streamable_http_async()
    finally:
        reaper.cancel()
        closed = await connections.close_all()
        logger.info(f"Closed connection pools: {closed}")
