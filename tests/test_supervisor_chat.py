from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from subactor_shell.app import build_parser

import pytest

from subactor_shell.supervisor_chat import (
    SUPERVISOR_CHAT_ACTIONS,
    SUPERVISOR_CHAT_USAGE,
    SupervisorChatError,
    compact_supervisor_view,
    format_supervisor_chat_result,
    parse_supervisor_chat_args,
    resolve_supervisor_invocation,
    run_supervisor_chat_command,
    supervisor_process_env,
)


def test_parse_defaults_to_status_and_rejects_mutate_actions() -> None:
    assert parse_supervisor_chat_args([]) == {"action": "status"}
    assert parse_supervisor_chat_args(["observe"]) == {"action": "observe"}
    assert parse_supervisor_chat_args(["cycle"]) == {"action": "cycle"}
    assert parse_supervisor_chat_args(["help"]) == {"action": "help"}
    assert SUPERVISOR_CHAT_ACTIONS["cycle"][-1] == "--discover"
    with pytest.raises(SupervisorChatError, match="nie jest dostępna z czatu"):
        parse_supervisor_chat_args(["grant"])
    with pytest.raises(SupervisorChatError, match="nie jest dostępna z czatu"):
        parse_supervisor_chat_args(["apply"])
    with pytest.raises(SupervisorChatError, match="dodatkowych argumentów"):
        parse_supervisor_chat_args(["cycle", "--discover"])
    with pytest.raises(SupervisorChatError, match="Nieznana akcja"):
        parse_supervisor_chat_args(["unknown"])
    assert parse_supervisor_chat_args(
        ["answer", "supervisor-question-b8fb52ac-0bcf-4b0f-9597-49777df287a1", "Nie,", "pozostaw", "zablokowany"]
    ) == {
        "action": "answer",
        "question_id": "supervisor-question-b8fb52ac-0bcf-4b0f-9597-49777df287a1",
        "answer": "Nie, pozostaw zablokowany",
    }
    with pytest.raises(SupervisorChatError, match="Użycie: /supervisor answer"):
        parse_supervisor_chat_args(["answer"])
    with pytest.raises(SupervisorChatError, match="Użycie: /supervisor answer"):
        parse_supervisor_chat_args(["answer", "--apply", "tak"])


def test_resolve_requires_absolute_cli(tmp_path: Path) -> None:
    cli = tmp_path / "autonomy.js"
    cli.write_text("// mock\n", encoding="utf-8")
    invocation = resolve_supervisor_invocation(
        {"SUBACTOR_SUPERVISOR_CLI": str(cli)},
        workspace=tmp_path,
        which=lambda _name: None,
    )
    assert invocation["cli_path"] == str(cli.resolve())
    assert invocation["executable"] == "node"
    assert invocation["prefix_args"] == [str(cli.resolve())]

    with pytest.raises(SupervisorChatError, match="bezwzględną ścieżką"):
        resolve_supervisor_invocation(
            {"SUBACTOR_SUPERVISOR_CLI": "autonomy.js"},
            workspace=tmp_path,
            which=lambda _name: None,
        )
    with pytest.raises(SupervisorChatError, match="niedostępny"):
        resolve_supervisor_invocation({}, workspace=tmp_path, which=lambda _name: None)


def test_run_uses_allowlisted_argv_without_shell() -> None:
    calls: list[dict[str, object]] = []

    def fake_run(argv, **options):
        calls.append({"argv": argv, "options": options})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "supervisor": {"paused": False, "cycles": 3}}),
            stderr="",
        )

    result = run_supervisor_chat_command(
        "status",
        env={"PATH": "/usr/bin", "HOME": "/home/tom", "SECRET": "leak"},
        run=fake_run,
        invocation={
            "executable": "node",
            "prefix_args": ["/opt/supervisor/bin/autonomy.js"],
            "root": "/opt/supervisor",
            "cli_path": "/opt/supervisor/bin/autonomy.js",
        },
    )
    assert result["ok"] is True
    assert calls[0]["argv"] == [
        "node",
        "/opt/supervisor/bin/autonomy.js",
        "supervisor",
        "status",
        "--root",
        "/opt/supervisor",
    ]
    assert calls[0]["options"]["cwd"] == "/opt/supervisor"
    assert "SECRET" not in calls[0]["options"]["env"]
    rendered = format_supervisor_chat_result(result)
    assert "akcja: status" in rendered
    assert "wynik: ok" in rendered


def test_cycle_is_fixed_and_help_does_not_spawn() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    cycle = run_supervisor_chat_command(
        "cycle",
        run=fake_run,
        invocation={
            "executable": "node",
            "prefix_args": ["/opt/autonomy.js"],
            "root": "/opt",
            "cli_path": "/opt/autonomy.js",
        },
    )
    assert cycle["argv"] == ["/opt/autonomy.js", "supervisor", "cycle", "--discover", "--root", "/opt"]
    answer = run_supervisor_chat_command(
        {
            "action": "answer",
            "question_id": "supervisor-question-b8fb52ac-0bcf-4b0f-9597-49777df287a1",
            "answer": "Nie, pozostaw system zablokowany",
        },
        run=fake_run,
        invocation={
            "executable": "node",
            "prefix_args": ["/opt/autonomy.js"],
            "root": "/opt",
            "cli_path": "/opt/autonomy.js",
        },
    )
    assert answer["argv"] == [
        "/opt/autonomy.js",
        "supervisor",
        "answer",
        "supervisor-question-b8fb52ac-0bcf-4b0f-9597-49777df287a1",
        "Nie, pozostaw system zablokowany",
        "--root",
        "/opt",
    ]
    help_result = run_supervisor_chat_command("help", run=fake_run)
    assert help_result["ok"] is True
    assert calls == [
        ["node", "/opt/autonomy.js", "supervisor", "cycle", "--discover", "--root", "/opt"],
        [
            "node",
            "/opt/autonomy.js",
            "supervisor",
            "answer",
            "supervisor-question-b8fb52ac-0bcf-4b0f-9597-49777df287a1",
            "Nie, pozostaw system zablokowany",
            "--root",
            "/opt",
        ],
    ]
    assert "Użycie: /supervisor" in help_result["stdout"]
    assert "nie uruchamia grant" in SUPERVISOR_CHAT_USAGE


def test_parse_ignores_log_lines_and_scalar_json_fragments() -> None:
    def fake_run(argv, **_options):
        return SimpleNamespace(
            returncode=0,
            stdout='INFO gate passed {"checks":90}\n          "verify"\n{\n  "source": "running-service",\n  "supervisor": {"paused": false, "cycles": 4}\n}\n',
            stderr="",
        )

    result = run_supervisor_chat_command(
        "status",
        run=fake_run,
        invocation={
            "executable": "node",
            "prefix_args": ["/opt/autonomy.js"],
            "root": "/opt",
            "cli_path": "/opt/autonomy.js",
        },
    )
    assert result["data"]["source"] == "running-service"
    assert result["data"]["supervisor"]["cycles"] == 4
    compact = format_supervisor_chat_result(result, verbose=False)
    assert "akcja: status" in compact
    assert '"source": "running-service"' not in compact
    verbose = format_supervisor_chat_result(result, verbose=True)
    assert '"source": "running-service"' in verbose
    assert "q-1: Czy przywrócić Subactor" in compact_supervisor_view(
        [{"id": "q-1", "question": "Czy przywrócić Subactor?"}]
    )
    cycle_view = compact_supervisor_view(
        {
            "ok": True,
            "cycleId": "supervisor-cycle-1",
            "analyzed": True,
            "observation": {
                "schemaVersion": "subactor.observation/v1",
                "healthy": True,
                "degraded": True,
                "failed": ["subactor.status"],
            },
            "assessment": {
                "decision": "observe_more",
                "systemState": "degraded",
                "summary": "Model niedostępny",
            },
        }
    )
    assert "cykl: supervisor-cycle-1" in cycle_view
    assert "ocena GLM: tak" in cycle_view
    assert "nieudane komendy: subactor.status" in cycle_view
    assert "ostatnia decyzja: observe_more" in cycle_view
    auth_fail = compact_supervisor_view(
        {
            "schemaVersion": "subactor.observation/v1",
            "healthy": True,
            "degraded": True,
            "failed": ["subactor.status"],
            "commands": {
                "status": {
                    "ok": False,
                    "stderr": "error: brak aktywnej sesji. Uruchom `subactor login <email>`",
                }
            },
        }
    )
    assert "status: error: brak aktywnej sesji" in auth_fail


def test_questions_payload_parses_log_prefixed_array() -> None:
    def fake_run(argv, **_options):
        return SimpleNamespace(
            returncode=0,
            stdout='INFO gate {"checks":90}\n[\n  {"id":"q-9","question":"Czy przywrócić Subactor?","status":"pending"}\n]\n',
            stderr="",
        )

    result = run_supervisor_chat_command(
        "questions",
        run=fake_run,
        invocation={
            "executable": "node",
            "prefix_args": ["/opt/autonomy.js"],
            "root": "/opt",
            "cli_path": "/opt/autonomy.js",
        },
    )
    assert result["data"][0]["id"] == "q-9"
    rendered = format_supervisor_chat_result(result, verbose=False)
    assert "q-9: Czy przywrócić Subactor" in rendered
    assert '"status": "pending"' not in rendered


def test_live_fake_cli_and_parser(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["supervisor", "cycle"])
    assert args.tokens == ["cycle"]
    help_args = parser.parse_args(["supervisor"])
    assert args.tokens == ["cycle"]
    assert help_args.tokens == []

    cli = tmp_path / "autonomy.js"
    cli.write_text(
        'process.stdout.write(JSON.stringify({ok:true, argv: process.argv.slice(2)}) + "\\n");\n',
        encoding="utf-8",
    )
    result = run_supervisor_chat_command(
        "status",
        env={
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "SUBACTOR_SUPERVISOR_CLI": str(cli),
            "SUBACTOR_SUPERVISOR_ROOT": str(tmp_path),
        },
        workspace=tmp_path,
    )
    assert result["ok"] is True
    assert result["data"]["argv"] == ["supervisor", "status", "--root", str(tmp_path)]


def test_live_supervisor_status_questions_and_report() -> None:
    try:
        status = run_supervisor_chat_command("status")
    except SupervisorChatError as exc:
        pytest.skip(str(exc))
    if not status.get("ok"):
        pytest.skip(status.get("stderr") or "supervisor status failed")
    text = format_supervisor_chat_result(status, verbose=False)
    assert "akcja: status" in text
    assert "wynik: ok" in text
    questions = run_supervisor_chat_command("questions")
    assert questions.get("ok") is True
    report = run_supervisor_chat_command("report")
    assert report.get("ok") is True


def test_process_env_drops_unrelated_secrets() -> None:
    env = supervisor_process_env(
        {
            "PATH": "/usr/bin",
            "SUBACTOR_ADMIN_TOKEN": "token",
            "OPENAI_API_KEY": "sk-secret",
            "VAULT_TOKEN": "vault",
        }
    )
    assert env["PATH"] == "/usr/bin"
    assert env["SUBACTOR_ADMIN_TOKEN"] == "token"
    assert "OPENAI_API_KEY" not in env
    assert "VAULT_TOKEN" not in env
    assert "SUBACTOR_ADMIN_TOKEN" in env["SUBACTOR_PASS_ENV"]
