#!/usr/bin/env python3
"""Minimalny named process connector dla Subactor Shell Bridge.

Wejście: pojedynczy obiekt JSON na stdin.
Wyjście: krótki obiekt JSON na stdout.
Logi diagnostyczne należy pisać na stderr bez sekretów.
"""

from __future__ import annotations

import json
import sys
from typing import Any


ALLOWED_OPERATIONS = {"project.inspect", "project.apply"}


def fail(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def read_request() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError:
        fail("invalid JSON input")
    if not isinstance(value, dict):
        fail("input must be an object")
    return value


def main() -> None:
    request = read_request()
    operation = request.get("operation")
    args = request.get("args")
    if operation not in ALLOWED_OPERATIONS:
        fail("operation is not allowed")
    if not isinstance(args, dict):
        fail("args must be an object")

    project_ref = args.get("project_ref")
    if not isinstance(project_ref, str) or not project_ref:
        fail("project_ref is required")

    if operation == "project.inspect":
        result = {
            "ok": True,
            "project_ref": project_ref,
            "state": "ready",
            "state_version": "example-v1"
        }
    else:
        environment = args.get("environment")
        if environment not in {"dev", "staging", "prod"}:
            fail("environment must be dev, staging or prod")
        # Tu wywołaj istniejącą usługę lub bibliotekę. Nie uruchamiaj tekstu
        # pochodzącego z requestu jako komendy shell.
        result = {
            "ok": True,
            "project_ref": project_ref,
            "environment": environment,
            "changed": 0,
            "state_version": "example-v2"
        }

    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
