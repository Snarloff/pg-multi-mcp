"""Validação somente leitura para operações MongoDB (modo 'restricted').

O MongoDB não tem transação READ ONLY como o PostgreSQL: esta allowlist é a barreira da aplicação.
A garantia real é conectar com um usuário que tenha apenas a role 'read' no servidor.
"""

from typing import Any
from typing import Optional

READ_OPERATIONS = frozenset({"find", "aggregate", "count", "distinct", "command"})
WRITE_OPERATIONS = frozenset({"insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many"})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS

# Commands allowed through operation='command' in restricted mode (compared in lowercase)
READONLY_COMMANDS = frozenset(
    {
        "buildinfo",
        "collstats",
        "connectionstatus",
        "dbstats",
        "hostinfo",
        "listcollections",
        "listdatabases",
        "listindexes",
        "ping",
        "serverstatus",
    }
)

# Stages that write ($out, $merge) and operators that run JavaScript on the server
FORBIDDEN_OPERATORS = frozenset({"$out", "$merge", "$where", "$function", "$accumulator"})


class MongoReadOnlyError(ValueError):
    """Operação recusada em conexão somente leitura."""


def find_forbidden_operators(value: Any) -> set[str]:
    """Procura operadores proibidos em qualquer nível (inclui $lookup.pipeline, $facet, $unionWith)."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_OPERATORS:
                found.add(key)
            found |= find_forbidden_operators(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= find_forbidden_operators(item)
    return found


def check_readonly(operation: str, *parts: Any, command: Optional[dict[str, Any]] = None) -> None:
    """Falha se a operação (ou filtro/pipeline/comando) puder escrever ou executar JavaScript."""
    if operation not in READ_OPERATIONS:
        raise MongoReadOnlyError(
            f"Operação '{operation}' não é permitida em modo restricted (somente leitura). Permitidas: {', '.join(sorted(READ_OPERATIONS))}."
        )
    if operation == "command":
        name = next(iter(command), "") if command else ""
        if not isinstance(name, str) or name.lower() not in READONLY_COMMANDS:
            raise MongoReadOnlyError(f"Comando '{name}' não é permitido em modo restricted. Permitidos: {', '.join(sorted(READONLY_COMMANDS))}.")
    found = set().union(*(find_forbidden_operators(part) for part in (*parts, command)))
    if found:
        raise MongoReadOnlyError(f"Operador(es) não permitido(s) em modo restricted: {', '.join(sorted(found))}.")
