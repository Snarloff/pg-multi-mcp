"""Pool e driver MongoDB (pymongo AsyncMongoClient) com a mesma interface usada pelo ConnectionManager."""

import contextlib
import datetime
import json
import logging
import time
from typing import Any
from typing import Optional
from urllib.parse import unquote
from urllib.parse import urlparse

import pymongo
from bson import Binary
from bson import Decimal128
from bson import Int64
from bson import ObjectId
from bson import json_util
from pymongo import AsyncMongoClient
from pymongo.errors import ConfigurationError

from ..postgres.sql import obfuscate_password
from .safe import OPERATIONS
from .safe import WRITE_OPERATIONS
from .safe import check_readonly

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_SIZE = 100
MAX_SCHEMA_DEPTH = 4
MAX_SCHEMA_FIELDS = 300


def is_mongo_url(url: str) -> bool:
    return url.lower().startswith(("mongodb://", "mongodb+srv://"))


def mongo_target(url: str) -> dict[str, Any]:
    """Destino da URI MongoDB sem a senha. Não resolve DNS (mongodb+srv)."""
    parsed = urlparse(url)
    target: dict[str, Any] = {
        "host": parsed.netloc.rpartition("@")[2],
        "dbname": parsed.path.lstrip("/"),
        "user": unquote(parsed.username) if parsed.username else None,
    }
    if parsed.scheme.lower() == "mongodb+srv":
        target["srv"] = True
    return {k: v for k, v in target.items() if v}


def mongo_password(url: str) -> Optional[str]:
    password = urlparse(url).password
    return unquote(password) if password else None


def to_bson(value: Any) -> Any:
    """JSON vindo do agente -> BSON, interpretando Extended JSON ({"$oid": ...}, {"$date": ...})."""
    return json_util.loads(json.dumps(value)) if value is not None else None


def to_json(value: Any) -> Any:
    """BSON -> JSON simples (Extended JSON relaxed: ObjectId, datas e decimais sem perder o tipo)."""
    return json.loads(json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS))


def _client(url: str, timeout: int, **kwargs: Any) -> AsyncMongoClient:
    ms = timeout * 1000
    return AsyncMongoClient(url, serverSelectionTimeoutMS=ms, connectTimeoutMS=ms, appname="pg-multi-mcp", **kwargs)


async def probe_mongo(url: str, timeout: int = 10) -> None:
    """Conexão avulsa de teste. O client conecta de forma lazy, então o ping força o erro real (auth, DNS, recusa)."""
    client = _client(url, timeout, maxPoolSize=1)
    try:
        await client.admin.command("ping")
    finally:
        await client.close()


class MongoConnPool:
    """Pool de conexões MongoDB (o AsyncMongoClient mantém o pool internamente)."""

    def __init__(
        self,
        connection_url: Optional[str] = None,
        min_size: int = 1,
        max_size: int = 5,
        connect_timeout: int = 10,
        application_name: str = "pg-multi-mcp",
    ):
        self.connection_url = connection_url
        self.pool: Optional[AsyncMongoClient] = None
        self.min_size = min_size
        self.max_size = max_size
        self.connect_timeout = connect_timeout
        self.application_name = application_name
        self.server_version: Optional[str] = None
        self._last_error: Optional[str] = None

    async def pool_connect(self, connection_url: Optional[str] = None) -> AsyncMongoClient:
        if self.pool is not None:
            return self.pool
        url = connection_url or self.connection_url
        self.connection_url = url
        if not url:
            self._last_error = "Database connection URL not provided"
            raise ValueError(self._last_error)

        client: Optional[AsyncMongoClient] = None
        try:
            client = _client(url, self.connect_timeout, maxPoolSize=self.max_size)
            await client.admin.command("ping")
            info = await client.admin.command("buildInfo")
        except Exception as e:
            self._last_error = str(e)
            if client is not None:
                await client.close()
            raise ValueError(f"Connection attempt failed: {obfuscate_password(str(e))}") from e

        self.pool = client
        self.server_version = info.get("version")
        self._last_error = None
        return client

    async def close(self) -> None:
        if self.pool is not None:
            try:
                await self.pool.close()
            except Exception as e:
                logger.warning(f"Error closing MongoDB client: {e}")
            finally:
                self.pool = None
                self.server_version = None

    @property
    def is_valid(self) -> bool:
        return self.pool is not None

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Int64):
        return "long"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, datetime.datetime):
        return "date"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, (bytes, Binary)):
        return "binData"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def infer_schema(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Campos, tipos e frequência nos documentos amostrados — nunca os valores (LGPD)."""
    stats: dict[str, dict[str, Any]] = {}

    def visit(value: Any, path: str, depth: int, seen: set[str]) -> None:
        if path not in stats:
            if len(stats) >= MAX_SCHEMA_FIELDS:
                return
            stats[path] = {"types": set(), "count": 0}
        stats[path]["types"].add(_type_name(value))
        if path not in seen:
            seen.add(path)
            stats[path]["count"] += 1
        if depth >= MAX_SCHEMA_DEPTH:
            return
        # Arrays are transparent in MongoDB dot notation: items.sku covers [{"sku": ...}]
        children = [value] if isinstance(value, dict) else [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
        for child in children:
            for key, item in child.items():
                visit(item, f"{path}.{key}", depth + 1, seen)

    for doc in docs:
        seen: set[str] = set()
        for key, value in doc.items():
            visit(value, key, 1, seen)

    return [{"field": path, "types": sorted(s["types"]), "present_in": s["count"]} for path, s in stats.items()]


class MongoDriver:
    """Executa operações em uma conexão MongoDB; em modo somente leitura valida antes de enviar ao servidor."""

    def __init__(self, conn: MongoConnPool, readonly: bool = True, timeout: Optional[int] = None):
        self.conn = conn
        self.readonly = readonly
        self.timeout = timeout

    @property
    def client(self) -> AsyncMongoClient:
        if self.conn.pool is None:
            raise ValueError("Conexão MongoDB não está aberta")
        return self.conn.pool

    def database(self, name: Optional[str] = None):
        if name:
            return self.client[name]
        try:
            return self.client.get_default_database()
        except ConfigurationError:
            raise ValueError("Informe o banco ('database' / 'schema_name'): a URL da conexão não define um banco padrão.") from None

    def _deadline(self):
        """Timeout client-side para todas as operações do bloco (só em restricted, como no PostgreSQL)."""
        return pymongo.timeout(self.timeout) if self.timeout else contextlib.nullcontext()

    # ------------------------------------------------------------------ metadados

    async def server_info(self) -> dict[str, Any]:
        started = time.perf_counter()
        with self._deadline():
            await self.client.admin.command("ping")
            build = await self.client.admin.command("buildInfo")
            status = await self.client.admin.command("connectionStatus")
        users = status.get("authInfo", {}).get("authenticatedUsers", [])
        try:
            database = self.database().name
        except ValueError:
            database = None
        return {
            "database": database,
            "user": ", ".join(f"{u.get('user')}@{u.get('db')}" for u in users) or None,
            "version": f"MongoDB {build.get('version')}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    async def list_databases(self) -> list[dict[str, Any]]:
        with self._deadline():
            result = await self.client.admin.command("listDatabases", authorizedDatabases=True)
        return [
            {"database": d["name"], "size_on_disk": d.get("sizeOnDisk"), "empty": d.get("empty")}
            for d in sorted(result.get("databases", []), key=lambda d: d["name"])
        ]

    async def list_collections(self, database: Optional[str], object_type: str = "collection") -> list[dict[str, Any]]:
        kinds = {"table": {"collection", "timeseries"}, "collection": {"collection", "timeseries"}, "view": {"view"}}.get(object_type)
        if kinds is None:
            raise ValueError(f"Unsupported object type for MongoDB: {object_type}. Use 'collection' or 'view'.")
        db = self.database(database)
        with self._deadline():
            infos = await (await db.list_collections()).to_list(None)
        return sorted(
            ({"database": db.name, "name": i["name"], "type": i.get("type", "collection")} for i in infos if i.get("type", "collection") in kinds),
            key=lambda o: o["name"],
        )

    async def collection_details(self, database: Optional[str], name: str, sample_size: int = DEFAULT_SAMPLE_SIZE) -> dict[str, Any]:
        db = self.database(database)
        with self._deadline():
            infos = await (await db.list_collections(filter={"name": name})).to_list(None)
            if not infos:
                return {}
            info = infos[0]
            kind = info.get("type", "collection")
            coll = db[name]
            result: dict[str, Any] = {"basic": {"database": db.name, "name": name, "type": kind}}
            if kind == "view":
                result["basic"]["view_on"] = info.get("options", {}).get("viewOn")
            else:
                result["estimated_count"] = await coll.estimated_document_count()
                indexes = await (await coll.list_indexes()).to_list(None)
                result["indexes"] = [to_json({k: v for k, v in idx.items() if k not in ("v", "ns")}) for idx in indexes]
            docs = await (await coll.aggregate([{"$sample": {"size": sample_size}}])).to_list(None)
        result["fields"] = infer_schema(docs)
        result["sampled_documents"] = len(docs)
        return result

    # ------------------------------------------------------------------ operações

    async def execute(
        self,
        operation: str,
        collection: Optional[str] = None,
        database: Optional[str] = None,
        filter: Optional[dict[str, Any]] = None,
        projection: Optional[dict[str, Any]] = None,
        sort: Optional[dict[str, Any]] = None,
        skip: int = 0,
        limit: int = 0,
        pipeline: Optional[list[dict[str, Any]]] = None,
        field: Optional[str] = None,
        documents: Optional[list[dict[str, Any]]] = None,
        update: Optional[dict[str, Any] | list[dict[str, Any]]] = None,
        upsert: bool = False,
        command: Optional[dict[str, Any]] = None,
        explain: bool = False,
        max_rows: int = 500,
    ) -> Any:
        if operation not in OPERATIONS:
            raise ValueError(f"Operação '{operation}' desconhecida. Use: {', '.join(sorted(OPERATIONS))}.")
        filter, projection, pipeline, update, command, documents = (to_bson(v) for v in (filter, projection, pipeline, update, command, documents))
        if self.readonly:
            # projection accepts aggregation expressions since MongoDB 4.4, so it is checked too
            check_readonly(operation, filter, projection, sort, pipeline, command=command)
        if explain and operation not in ("find", "aggregate"):
            raise ValueError("explain só é suportado em 'find' e 'aggregate'.")
        if operation != "command" and not collection:
            raise ValueError(f"Informe 'collection' para a operação '{operation}'.")

        db = self.database(database)
        with self._deadline():
            if operation == "command":
                if not command:
                    raise ValueError("Informe 'command', ex.: {\"dbStats\": 1}.")
                return to_json(await db.command(command))

            coll = db[collection]
            if operation == "find":
                cap = max_rows + 1
                cursor = coll.find(
                    filter or {},
                    projection,
                    sort=list(sort.items()) if sort else None,
                    skip=skip,
                    limit=min(limit, cap) if limit else cap,
                )
                if explain:
                    return to_json(await cursor.explain())
                return self._rows(await cursor.to_list(None), max_rows)

            if operation == "aggregate":
                stages = list(pipeline or [])
                if explain:
                    explain_cmd = {"aggregate": collection, "pipeline": stages, "cursor": {}}
                    return to_json(await db.command("explain", explain_cmd, verbosity="queryPlanner"))
                writes = bool(stages) and next(iter(stages[-1]), "") in ("$out", "$merge")
                if not writes:
                    stages.append({"$limit": max_rows + 1})  # truncate on the server, not after fetching everything
                docs = await (await coll.aggregate(stages)).to_list(None)
                return {"ok": True, "message": "Pipeline com $out/$merge executado"} if writes else self._rows(docs, max_rows)

            if operation == "count":
                return {"count": await coll.count_documents(filter or {})}

            if operation == "distinct":
                if not field:
                    raise ValueError("Informe 'field' para distinct.")
                values = await coll.distinct(field, filter or {})
                return self._rows(values, max_rows)

            assert operation in WRITE_OPERATIONS
            return await self._write(coll, operation, filter or {}, documents or [], update, upsert)

    async def _write(self, coll, operation: str, filter: dict[str, Any], documents: list[Any], update: Any, upsert: bool) -> dict[str, Any]:
        if operation == "insert_one":
            if len(documents) != 1:
                raise ValueError("insert_one espera exatamente um item em 'documents'.")
            result = await coll.insert_one(documents[0])
            return to_json({"inserted_id": result.inserted_id})
        if operation == "insert_many":
            if not documents:
                raise ValueError("Informe 'documents' para insert_many.")
            result = await coll.insert_many(documents)
            return to_json({"inserted_ids": result.inserted_ids})
        if operation in ("delete_one", "delete_many"):
            result = await (coll.delete_one(filter) if operation == "delete_one" else coll.delete_many(filter))
            return {"deleted_count": result.deleted_count}

        if not update:
            raise ValueError(f"Informe 'update' para {operation}.")
        if operation == "replace_one":
            result = await coll.replace_one(filter, update, upsert=upsert)
        elif operation == "update_one":
            result = await coll.update_one(filter, update, upsert=upsert)
        else:
            result = await coll.update_many(filter, update, upsert=upsert)
        return to_json({"matched_count": result.matched_count, "modified_count": result.modified_count, "upserted_id": result.upserted_id})

    @staticmethod
    def _rows(items: list[Any], max_rows: int) -> Any:
        rows = to_json(items[:max_rows])
        if len(items) > max_rows:
            return {"rows": rows, "truncated": True, "returned_rows": max_rows}
        return rows
