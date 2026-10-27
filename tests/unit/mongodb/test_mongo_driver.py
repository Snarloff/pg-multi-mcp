import datetime
import json

import pytest
from bson import ObjectId

from pg_multi_mcp.mongodb import MongoDriver
from pg_multi_mcp.mongodb import MongoReadOnlyError
from pg_multi_mcp.mongodb import infer_schema
from pg_multi_mcp.mongodb import mongo_password
from pg_multi_mcp.mongodb import mongo_target
from pg_multi_mcp.postgres.sql import obfuscate_password


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.calls: list[tuple] = []

    def find(self, filter, projection=None, **kwargs):
        self.calls.append(("find", filter, kwargs))
        limit = kwargs.get("limit") or len(self.docs)
        return FakeCursor(self.docs[:limit])

    async def aggregate(self, pipeline):
        self.calls.append(("aggregate", pipeline))
        limit = next((s["$limit"] for s in pipeline if "$limit" in s), len(self.docs))
        return FakeCursor(self.docs[:limit])

    async def count_documents(self, filter):
        return len(self.docs)


class FakeDatabase:
    name = "app"

    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        return self.collection


class FakeClient:
    def __init__(self, collection):
        self.db = FakeDatabase(collection)

    def __getitem__(self, name):
        return self.db

    def get_default_database(self):
        return self.db


class FakeMongoPool:
    def __init__(self, docs):
        self.collection = FakeCollection(docs)
        self.pool = FakeClient(self.collection)


def make_driver(docs, readonly=True):
    pool = FakeMongoPool(docs)
    return MongoDriver(conn=pool, readonly=readonly, timeout=5 if readonly else None), pool.collection  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_find_truncates_on_server_and_converts_bson():
    oid = ObjectId()
    docs = [{"_id": oid, "n": i, "at": datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)} for i in range(10)]
    driver, coll = make_driver(docs)
    result = await driver.execute("find", collection="items", filter={"_id": {"$oid": str(oid)}}, max_rows=3)

    _, sent_filter, kwargs = coll.calls[0]
    assert sent_filter == {"_id": oid}  # Extended JSON input became a real ObjectId
    assert kwargs["limit"] == 4  # max_rows + 1, so the server never sends everything
    assert result["truncated"] is True and result["returned_rows"] == 3
    assert result["rows"][0]["_id"] == {"$oid": str(oid)}
    assert result["rows"][0]["at"] == {"$date": "2026-01-01T00:00:00Z"}
    json.dumps(result)


@pytest.mark.asyncio
async def test_aggregate_appends_limit_and_restricted_rejects_out():
    driver, coll = make_driver([{"n": i} for i in range(5)])
    result = await driver.execute("aggregate", collection="items", pipeline=[{"$match": {}}], max_rows=10)
    assert coll.calls[0][1][-1] == {"$limit": 11}
    assert len(result) == 5

    with pytest.raises(MongoReadOnlyError):
        await driver.execute("aggregate", collection="items", pipeline=[{"$out": "copy"}])
    with pytest.raises(MongoReadOnlyError):
        await driver.execute("delete_many", collection="items", filter={})
    js = {"$function": {"body": "function() { return 1 }", "args": [], "lang": "js"}}
    with pytest.raises(MongoReadOnlyError, match=r"\$function"):
        await driver.execute("find", collection="items", projection={"x": js})
    assert len(coll.calls) == 1  # rejected before reaching the server


@pytest.mark.asyncio
async def test_unknown_operation_and_missing_collection():
    driver, _ = make_driver([])
    with pytest.raises(ValueError, match="desconhecida"):
        await driver.execute("drop", collection="items")
    with pytest.raises(ValueError, match="collection"):
        await driver.execute("count")


def test_infer_schema_reports_types_and_frequency_but_no_values():
    docs = [
        {"_id": ObjectId(), "cpf": "123.456.789-00", "address": {"city": "Recife"}, "items": [{"sku": "A1"}]},
        {"_id": ObjectId(), "cpf": None, "address": {"city": "Olinda", "zip": "53000"}},
    ]
    schema = {f["field"]: f for f in infer_schema(docs)}
    assert schema["cpf"] == {"field": "cpf", "types": ["null", "string"], "present_in": 2}
    assert schema["address.city"]["present_in"] == 2
    assert schema["address.zip"]["present_in"] == 1
    assert schema["items.sku"]["types"] == ["string"]
    text = json.dumps(list(schema.values()))
    for value in ("123.456.789-00", "Recife", "Olinda", "53000", "A1"):
        assert value not in text


def test_mongo_target_hides_password_and_supports_multi_host():
    url = "mongodb://app:s3cr3t@db1:27017,db2:27017/sales?replicaSet=rs0"
    assert mongo_target(url) == {"host": "db1:27017,db2:27017", "dbname": "sales", "user": "app"}
    assert mongo_target("mongodb+srv://app:${PW}@cluster0.example.net/")["srv"] is True
    assert mongo_password("mongodb://app:p%40ss@h/db") == "p@ss"


def test_obfuscate_mongo_urls():
    assert "s3cr3t" not in obfuscate_password("mongodb+srv://app:s3cr3t@cluster0.example.net/db")
    assert "s3cr3t" not in obfuscate_password("failed: mongodb://app:s3cr3t@db1:27017,db2:27017/db timed out")
