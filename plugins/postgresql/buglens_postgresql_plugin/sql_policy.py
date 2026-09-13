"""Parse PostgreSQL SQL before allowing one bounded, read-only SELECT."""

from __future__ import annotations

import re
from typing import Any

from pglast import ast, parse_sql
from pglast.stream import RawStream

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "lower",
        "upper",
        "length",
        "char_length",
        "abs",
        "round",
        "ceil",
        "ceiling",
        "floor",
        "date_trunc",
        "date_part",
        "now",
        "substring",
        "substr",
        "btrim",
        "ltrim",
        "rtrim",
        "replace",
        "concat",
        "concat_ws",
        "row_number",
        "rank",
        "dense_rank",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "bool_and",
        "bool_or",
        "array_agg",
        "string_agg",
        "json_agg",
        "jsonb_agg",
    }
)
SAFE_TYPES = frozenset(
    {
        "int2",
        "int4",
        "int8",
        "integer",
        "bigint",
        "smallint",
        "numeric",
        "float4",
        "float8",
        "text",
        "varchar",
        "bpchar",
        "bool",
        "boolean",
        "date",
        "timestamp",
        "timestamptz",
        "interval",
        "uuid",
        "json",
        "jsonb",
    }
)


def bind_sql(sql: str, parameters: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Convert :name / %(name)s outside literals/comments to native $n binds."""
    result: list[str] = []
    names: list[str] = []
    index = 0
    while index < len(sql):
        start = index
        char = sql[index]
        if char in "'\"":
            index += 1
            while index < len(sql):
                if sql[index] == "\\" and char == "'":
                    index += 2
                elif sql[index] == char:
                    index += 1
                    if index < len(sql) and sql[index] == char:
                        index += 1
                    else:
                        break
                else:
                    index += 1
            result.append(sql[start:index])
            continue
        if sql.startswith("--", index):
            index = sql.find("\n", index)
            if index < 0:
                index = len(sql)
            result.append(sql[start:index])
            continue
        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(sql) and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            result.append(sql[start:index])
            continue
        dollar = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
        if dollar:
            end = sql.find(dollar[0], index + len(dollar[0]))
            if end < 0:
                raise ValueError("invalid_sql")
            index = end + len(dollar[0])
            result.append(sql[start:index])
            continue
        named = re.match(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s", sql[index:])
        colon = (
            re.match(r":([A-Za-z_][A-Za-z0-9_]*)", sql[index:])
            if index == 0 or sql[index - 1] != ":"
            else None
        )
        match = named or colon
        if match:
            name = match[1]
            if name not in parameters:
                raise ValueError("missing_parameter")
            if name not in names:
                names.append(name)
            result.append(f"${names.index(name) + 1}")
            index += len(match[0])
        else:
            if re.match(r"\$\d+", sql[index:]):
                raise ValueError("named_parameters_required")
            result.append(char)
            index += 1
    return "".join(result), tuple(parameters[name] for name in names)


def guarded_query(
    sql: str, parameters: dict[str, Any], config: dict[str, Any]
) -> tuple[str, tuple[Any, ...]]:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 16_000:
        raise ValueError("invalid_sql")
    normalized, values = bind_sql(sql, parameters)
    try:
        parsed = parse_sql(normalized)
    except Exception as exc:
        raise ValueError("invalid_sql") from exc
    if len(parsed) != 1 or not isinstance(parsed[0].stmt, ast.SelectStmt):
        raise ValueError("read_only_select_required")
    tree = parsed[0].stmt()
    schema = config.get("schema", "public")
    allowed = set(config.get("allowed_tables", []))
    denied = set(config.get("denied_columns", []))
    relation_names: set[str] = set()

    def collect(value):
        if isinstance(value, dict):
            if value.get("@") == "RangeVar":
                relation_names.add(value["relname"])
                if value.get("alias"):
                    relation_names.add(value["alias"]["aliasname"])
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(tree)

    def check(value, scope: frozenset[str] = frozenset()):
        if isinstance(value, (list, tuple)):
            for child in value:
                check(child, scope)
            return
        if not isinstance(value, dict):
            return
        tag = value.get("@", "")
        if tag.endswith("Stmt") and tag != "SelectStmt":
            raise ValueError("read_only_select_required")
        if tag == "SelectStmt":
            if value.get("intoClause") or value.get("lockingClause"):
                raise ValueError("read_only_select_required")
            ctes = (value.get("withClause") or {}).get("ctes", ())
            scope = scope | frozenset(item["ctename"] for item in ctes)
        if tag == "RangeVar":
            name = value["relname"]
            if value.get("catalogname"):
                raise ValueError("table_not_allowed")
            if value.get("schemaname") is None and name in scope:
                pass
            else:
                if value.get("schemaname") not in (None, schema) or (
                    allowed
                    and name not in allowed
                    and f"{schema}.{name}" not in allowed
                ):
                    raise ValueError("table_not_allowed")
                value["schemaname"] = schema
        if tag in {"RangeFunction", "RangeTableFunc", "TableFunc"}:
            raise ValueError("function_not_allowed")
        if tag == "FuncCall":
            name = [item["sval"] for item in value["funcname"]]
            if name[-1] not in SAFE_FUNCTIONS or (
                len(name) > 1 and name[:-1] != ["pg_catalog"]
            ):
                raise ValueError("function_not_allowed")
            value["funcname"] = (
                {"@": "String", "sval": "pg_catalog"},
                {"@": "String", "sval": name[-1]},
            )
        if tag == "TypeName":
            names = [item["sval"] for item in value.get("names", ())]
            if (
                not names
                or names[-1] not in SAFE_TYPES
                or (len(names) > 1 and names[:-1] != ["pg_catalog"])
            ):
                raise ValueError("type_not_allowed")
        if tag == "A_Expr":
            names = value.get("name", ())
            if len(names) > 1 and [item["sval"] for item in names[:-1]] != [
                "pg_catalog"
            ]:
                raise ValueError("operator_not_allowed")
        if denied and tag == "ColumnRef":
            fields = value.get("fields", ())
            if (
                any(item.get("@") == "A_Star" for item in fields)
                or (fields and fields[-1].get("sval") in denied)
                or (len(fields) == 1 and fields[0].get("sval") in relation_names)
            ):
                raise ValueError("column_not_allowed")
        for child in value.values():
            check(child, scope)

    check(tree)
    return RawStream()(ast.SelectStmt(tree)), values
