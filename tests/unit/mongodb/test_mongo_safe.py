import pytest

from pg_multi_mcp.mongodb import MongoReadOnlyError
from pg_multi_mcp.mongodb import check_readonly
from pg_multi_mcp.mongodb.safe import find_forbidden_operators


@pytest.mark.parametrize("operation", ["find", "aggregate", "count", "distinct"])
def test_read_operations_allowed(operation):
    check_readonly(operation, {"status": "open"}, [{"$match": {"a": 1}}, {"$group": {"_id": "$a", "n": {"$sum": 1}}}])


@pytest.mark.parametrize("operation", ["insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many"])
def test_write_operations_rejected(operation):
    with pytest.raises(MongoReadOnlyError, match=operation):
        check_readonly(operation, {})


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$match": {}}, {"$out": "copy"}],
        [{"$merge": {"into": "copy"}}],
        [{"$lookup": {"from": "b", "pipeline": [{"$merge": "x"}], "as": "j"}}],
        [{"$facet": {"a": [{"$match": {}}], "b": [{"$out": "x"}]}}],
        [{"$unionWith": {"coll": "b", "pipeline": [{"$out": "x"}]}}],
        [{"$group": {"_id": None, "v": {"$accumulator": {"init": "function() {}"}}}}],
        [{"$addFields": {"v": {"$function": {"body": "function() {}", "args": [], "lang": "js"}}}}],
    ],
)
def test_aggregate_rejects_writes_and_js_at_any_depth(pipeline):
    with pytest.raises(MongoReadOnlyError):
        check_readonly("aggregate", None, pipeline)


def test_filter_rejects_where():
    with pytest.raises(MongoReadOnlyError, match=r"\$where"):
        check_readonly("find", {"$or": [{"a": 1}, {"$where": "sleep(1000)"}]})


def test_forbidden_operator_match_ignores_case():
    assert find_forbidden_operators([{"$OUT": "x"}]) == {"$OUT"}


def test_forbidden_name_as_value_is_fine():
    check_readonly("find", {"stage": "$out", "tags": ["$merge"]})


def test_readonly_commands():
    check_readonly("command", command={"dbStats": 1})
    check_readonly("command", command={"listCollections": 1, "nameOnly": True})
    for command in ({"dropDatabase": 1}, {"insert": "x", "documents": [{}]}, {"aggregate": "x", "pipeline": []}, {"eval": "1"}):
        with pytest.raises(MongoReadOnlyError, match="Comando"):
            check_readonly("command", command=command)
    with pytest.raises(MongoReadOnlyError):
        check_readonly("command", command=None)
