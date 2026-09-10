"""Generate isolated two-environment data and configuration for E2E checks.

The generator deliberately creates fresh SQLite databases and JSONL logs under
one caller-selected directory.  It never touches ``backend/data`` and writes a
small scenario manifest containing the absolute time window used by log
queries.  The resulting catalog can be consumed by both the deterministic E2E
test and an optional real-model CLI smoke run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def _seed_database(
    path: Path,
    *,
    orders: list[tuple[str, str, int, str, str | None]],
    events: list[tuple[str, str, str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payment_token TEXT
            );
            CREATE TABLE order_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO orders(order_id, status, amount_cents, created_at, payment_token) "
            "VALUES (?, ?, ?, ?, ?)",
            orders,
        )
        connection.executemany(
            "INSERT INTO order_events(order_id, event_type, observed_at, detail) "
            "VALUES (?, ?, ?, ?)",
            events,
        )
        connection.commit()
    finally:
        connection.close()


def _profile() -> dict[str, Any]:
    return {
        "profiles": {
            "default": {
                "config_version": "two-environment-e2e-v1",
                "prompt_config_version": "tenant-prompts-v1",
                "models": {"default": {"model": "gpt-4.1-mini"}},
                "graph": {
                    "max_investigation_attempts": 2,
                    "max_clarification_rounds": 2,
                },
                "evaluation": {
                    "passing_score": 75,
                    "min_evidence_traceability": 15,
                    "min_verification_executability": 15,
                    "rubric_version": "rubric-v1",
                },
                "nodes": {
                    "analyze": {
                        "model": "default",
                        "max_turns": 6,
                        "prompt_version": "analyzer-v1",
                    },
                    "investigate": {
                        "model": "default",
                        "max_turns": 6,
                        "prompt_version": "investigator-v1",
                    },
                    "evaluate": {
                        "model": "default",
                        "max_turns": 6,
                        "prompt_version": "evaluator-v1",
                    },
                    "summarize": {
                        "model": "default",
                        "max_turns": 6,
                        "prompt_version": "summary-v1",
                    },
                },
                "tools": {
                    "enabled": True,
                    "allowed_nodes": ["investigate"],
                    "allowed_profiles": ["default"],
                    "max_results": 20,
                    "max_result_rows": 200,
                    "timeout_seconds": 30,
                    "max_result_bytes": 65536,
                    "max_calls_per_run": 20,
                    "max_concurrent_calls": 2,
                },
                "retry": {
                    "max_retries": 0,
                    "initial_delay_seconds": 0.1,
                    "max_delay_seconds": 1,
                    "multiplier": 2,
                    "jitter": False,
                },
                "sessions": {"history_item_limit": 100},
                "lease_seconds": 30,
                "lease_renewal_seconds": 10,
            }
        }
    }


def _catalog(output: Path) -> dict[str, Any]:
    def path_for(environment: str, kind: str) -> str:
        return (output / environment / kind).resolve().as_posix()

    return {
        "config_version": "two-environment-e2e-v1",
        "plugin_instances": {
            "staging-sqlite": {
                "plugin_id": "sqlite",
                "enabled": True,
                "config": {
                    "root_path": path_for("staging", "db"),
                    "health_path": "orders.sqlite",
                },
                "default_limits": {
                    "max_results": 200,
                    "timeout_seconds": 10,
                    "max_bytes": 65536,
                },
            },
            "staging-file-logs": {
                "plugin_id": "file_logs",
                "enabled": True,
                "config": {
                    "root_path": path_for("staging", "logs"),
                    "encodings": ["utf-8", "utf-8-sig", "latin-1"],
                },
                "default_limits": {
                    "max_results": 200,
                    "timeout_seconds": 10,
                    "max_bytes": 65536,
                    "max_scan_files": 100,
                    "max_scan_bytes": 16777216,
                },
            },
            "production-sqlite": {
                "plugin_id": "sqlite",
                "enabled": True,
                "config": {
                    "root_path": path_for("production", "db"),
                    "health_path": "orders.sqlite",
                },
                "default_limits": {
                    "max_results": 200,
                    "timeout_seconds": 10,
                    "max_bytes": 65536,
                },
            },
            "production-file-logs": {
                "plugin_id": "file_logs",
                "enabled": True,
                "config": {
                    "root_path": path_for("production", "logs"),
                    "encodings": ["utf-8", "utf-8-sig", "latin-1"],
                },
                "default_limits": {
                    "max_results": 200,
                    "timeout_seconds": 10,
                    "max_bytes": 65536,
                    "max_scan_files": 100,
                    "max_scan_bytes": 16777216,
                },
            },
        },
        "environments": {
            "staging": {
                "display_name": "E2E Staging",
                "aliases": ["stage", "e2e-stage"],
                "level": "non-production",
                "region": "cn-east-1",
                "timezone": "Asia/Shanghai",
                "tags": {"deployment.environment.name": "staging"},
            },
            "production": {
                "display_name": "E2E Production",
                "aliases": ["prod", "e2e-prod"],
                "level": "production",
                "region": "cn-east-1",
                "timezone": "Asia/Shanghai",
                "tags": {"deployment.environment.name": "production"},
            },
        },
        "services": {
            "order-api-staging": {
                "name": "order-api",
                "aliases": ["orders-staging"],
                "version": "e2e-staging-1",
                "environment_id": "staging",
            },
            "order-api-production": {
                "name": "order-api",
                "aliases": ["orders-production"],
                "version": "e2e-production-1",
                "environment_id": "production",
            },
        },
        "nodes": {
            "order-staging-1": {
                "hostname": "e2e-staging-order-1",
                "instance_id": "order-api-staging-1",
                "environment_id": "staging",
            },
            "order-production-1": {
                "hostname": "e2e-production-order-1",
                "instance_id": "order-api-production-1",
                "environment_id": "production",
            },
        },
        "sources": {
            "staging-orders-db": {
                "kind": "database",
                "capabilities": ["database.describe.v1", "database.query.v1"],
                "plugin_instance_id": "staging-sqlite",
                "environment_id": "staging",
                "service_ids": ["order-api-staging"],
                "config": {
                    "path": "orders.sqlite",
                    "allowed_tables": ["orders", "order_events"],
                    "denied_columns": ["payment_token"],
                },
            },
            "staging-order-logs": {
                "kind": "logs",
                "capabilities": ["logs.search.v1"],
                "plugin_instance_id": "staging-file-logs",
                "environment_id": "staging",
                "service_ids": ["order-api-staging"],
                "node_ids": ["order-staging-1"],
                "config": {"paths": ["order-api.jsonl"]},
            },
            "production-orders-db": {
                "kind": "database",
                "capabilities": ["database.describe.v1", "database.query.v1"],
                "plugin_instance_id": "production-sqlite",
                "environment_id": "production",
                "service_ids": ["order-api-production"],
                "config": {
                    "path": "orders.sqlite",
                    "allowed_tables": ["orders", "order_events"],
                    "denied_columns": ["payment_token"],
                },
            },
            "production-order-logs": {
                "kind": "logs",
                "capabilities": ["logs.search.v1"],
                "plugin_instance_id": "production-file-logs",
                "environment_id": "production",
                "service_ids": ["order-api-production"],
                "node_ids": ["order-production-1"],
                "config": {"paths": ["order-api.jsonl"]},
            },
        },
    }


def prepare(output: Path, *, force: bool = False) -> dict[str, Any]:
    """Create a fresh fixture set and return the scenario manifest."""

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    generated_files = [
        output / "catalog.yaml",
        output / "profile.yaml",
        output / "scenario.json",
        output / "staging" / "db" / "orders.sqlite",
        output / "staging" / "logs" / "order-api.jsonl",
        output / "production" / "db" / "orders.sqlite",
        output / "production" / "logs" / "order-api.jsonl",
    ]
    existing = [path for path in generated_files if path.exists()]
    if existing and not force:
        joined = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            f"fixture files already exist ({joined}); use --force or a new output directory"
        )
    if force:
        for path in existing:
            path.unlink()

    base = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=15)
    window_start = base - timedelta(minutes=1)
    window_end = base + timedelta(minutes=11)

    staging_orders = [
        ("ord_stg_1001", "paid", 12800, _iso(base + timedelta(minutes=1)), None),
        ("ord_stg_1002", "pending", 8600, _iso(base + timedelta(minutes=5)), None),
        ("ord_stg_1003", "paid", 25900, _iso(base + timedelta(minutes=7)), None),
    ]
    staging_events = [
        (
            "ord_stg_1001",
            "checkout_received",
            _iso(base + timedelta(minutes=1)),
            "checkout received status=paid",
        ),
        (
            "ord_stg_1002",
            "checkout_received",
            _iso(base + timedelta(minutes=5)),
            "checkout received status=pending",
        ),
        (
            "ord_stg_1002",
            "payment_requested",
            _iso(base + timedelta(minutes=6)),
            "payment requested provider=sandbox-pay",
        ),
        (
            "ord_stg_1002",
            "payment_callback_timeout",
            _iso(base + timedelta(minutes=10)),
            "payment callback deadline exceeded marker=STAGING_ONLY_PAYMENT_CALLBACK",
        ),
        (
            "ord_stg_1003",
            "checkout_received",
            _iso(base + timedelta(minutes=7)),
            "checkout received status=paid",
        ),
    ]
    _seed_database(
        output / "staging" / "db" / "orders.sqlite",
        orders=staging_orders,
        events=staging_events,
    )
    _write_jsonl(
        output / "staging" / "logs" / "order-api.jsonl",
        [
            {
                "timestamp": _iso(base + timedelta(minutes=5)),
                "level": "INFO",
                "service_id": "order-api-staging",
                "node_id": "order-staging-1",
                "correlation_id": "stg-trace-1002",
                "message": "checkout accepted order_id=ord_stg_1002",
            },
            {
                "timestamp": _iso(base + timedelta(minutes=6)),
                "level": "INFO",
                "service_id": "order-api-staging",
                "node_id": "order-staging-1",
                "correlation_id": "stg-trace-1002",
                "message": "payment requested provider=sandbox-pay order_id=ord_stg_1002",
            },
            {
                "timestamp": _iso(base + timedelta(minutes=10)),
                "level": "ERROR",
                "service_id": "order-api-staging",
                "node_id": "order-staging-1",
                "correlation_id": "stg-trace-1002",
                "message": "payment callback deadline exceeded provider=sandbox-pay order_id=ord_stg_1002 marker=STAGING_ONLY_PAYMENT_CALLBACK",
            },
        ],
    )

    production_orders = [
        ("ord_prod_7801", "paid", 12800, _iso(base + timedelta(minutes=2)), None),
        ("ord_prod_7802", "timeout", 8600, _iso(base + timedelta(minutes=4)), None),
        ("ord_prod_7803", "timeout", 25900, _iso(base + timedelta(minutes=5)), None),
        ("ord_prod_7804", "paid", 9900, _iso(base + timedelta(minutes=8)), None),
    ]
    production_events = [
        (
            "ord_prod_7801",
            "checkout_received",
            _iso(base + timedelta(minutes=2)),
            "checkout received status=paid",
        ),
        (
            "ord_prod_7802",
            "checkout_received",
            _iso(base + timedelta(minutes=4)),
            "checkout received",
        ),
        (
            "ord_prod_7802",
            "checkout_timeout",
            _iso(base + timedelta(minutes=6)),
            "db pool acquire timeout active=20 idle=0 waiters=37 marker=PRODUCTION_ONLY_DB_POOL_EXHAUSTED",
        ),
        (
            "ord_prod_7803",
            "checkout_received",
            _iso(base + timedelta(minutes=5)),
            "checkout received",
        ),
        (
            "ord_prod_7803",
            "checkout_timeout",
            _iso(base + timedelta(minutes=7)),
            "db pool acquire timeout active=20 idle=0 waiters=37 marker=PRODUCTION_ONLY_DB_POOL_EXHAUSTED",
        ),
        (
            "ord_prod_7804",
            "checkout_received",
            _iso(base + timedelta(minutes=8)),
            "checkout received status=paid",
        ),
    ]
    _seed_database(
        output / "production" / "db" / "orders.sqlite",
        orders=production_orders,
        events=production_events,
    )
    _write_jsonl(
        output / "production" / "logs" / "order-api.jsonl",
        [
            {
                "timestamp": _iso(base + timedelta(minutes=4)),
                "level": "INFO",
                "service_id": "order-api-production",
                "node_id": "order-production-1",
                "correlation_id": "prod-trace-7802",
                "message": "checkout accepted order_id=ord_prod_7802",
            },
            {
                "timestamp": _iso(base + timedelta(minutes=6)),
                "level": "ERROR",
                "service_id": "order-api-production",
                "node_id": "order-production-1",
                "correlation_id": "prod-trace-7802",
                "message": "db pool acquire timeout active=20 idle=0 waiters=37 order_id=ord_prod_7802 marker=PRODUCTION_ONLY_DB_POOL_EXHAUSTED",
            },
            {
                "timestamp": _iso(base + timedelta(minutes=7)),
                "level": "ERROR",
                "service_id": "order-api-production",
                "node_id": "order-production-1",
                "correlation_id": "prod-trace-7803",
                "message": "db pool acquire timeout active=20 idle=0 waiters=37 order_id=ord_prod_7803 marker=PRODUCTION_ONLY_DB_POOL_EXHAUSTED",
            },
        ],
    )

    catalog = _catalog(output)
    (output / "catalog.yaml").write_text(
        yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (output / "profile.yaml").write_text(
        yaml.safe_dump(_profile(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    scenario = {
        "schema_version": "two-environment-e2e-v1",
        "generated_at": _iso(datetime.now(UTC)),
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "catalog": str(output / "catalog.yaml"),
        "profile": str(output / "profile.yaml"),
        "environments": {
            "staging": {
                "service_id": "order-api-staging",
                "node_id": "order-staging-1",
                "log_source_id": "staging-order-logs",
                "database_source_id": "staging-orders-db",
                "order_id": "ord_stg_1002",
                "correlation_id": "stg-trace-1002",
                "marker": "STAGING_ONLY_PAYMENT_CALLBACK",
                "question": (
                    "staging 的订单 ord_stg_1002 支付后超过 5 分钟仍为 pending，"
                    f"请结合时间窗 [{_iso(window_start)}, {_iso(window_end)}] 内的日志和数据库定位原因。"
                ),
                "expected_keywords": ["payment", "callback", "pending"],
                "forbidden_keywords": ["connection pool", "连接池耗尽"],
            },
            "production": {
                "service_id": "order-api-production",
                "node_id": "order-production-1",
                "log_source_id": "production-order-logs",
                "database_source_id": "production-orders-db",
                "order_ids": ["ord_prod_7802", "ord_prod_7803"],
                "correlation_ids": ["prod-trace-7802", "prod-trace-7803"],
                "marker": "PRODUCTION_ONLY_DB_POOL_EXHAUSTED",
                "question": (
                    "production 的订单接口在时间窗 "
                    f"[{_iso(window_start)}, {_iso(window_end)}] 内出现批量超时，"
                    "请结合日志和数据库定位原因。"
                ),
                "expected_keywords": ["database", "pool", "timeout"],
                "forbidden_keywords": ["payment callback", "支付回调"],
            },
        },
    }
    (output / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return scenario


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate isolated BugLens two-environment E2E fixtures."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".e2e"),
        help="fixture directory (default: .e2e)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace only files previously generated by this script",
    )
    args = parser.parse_args()
    scenario = prepare(args.output, force=args.force)
    print(json.dumps(scenario, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
