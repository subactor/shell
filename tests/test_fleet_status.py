from unittest.mock import MagicMock, patch

from rich.console import Console

from subactor_shell.fleet_status import (
    DoctorIssueSummary,
    FleetOverview,
    PullRequestSummary,
    fetch_doctor_issues,
    fetch_fleet_overview,
    fetch_github_prs,
    fetch_planfile_health,
    fetch_systemd_services,
    render_fleet_startup_banner,
)


def test_fetch_github_prs_success():
    mock_json = """[
        {
            "repository": {"nameWithOwner": "subactor/core"},
            "number": 255,
            "title": "ticket-128: converge scheduler",
            "url": "https://github.com/subactor/core/pull/255",
            "updatedAt": "2026-08-30T10:00:00Z"
        }
    ]"""
    with patch("shutil.which", return_value="/usr/bin/gh"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_json)
            prs = fetch_github_prs(limit=5)
            assert len(prs) == 1
            assert prs[0].repository == "subactor/core"
            assert prs[0].number == 255
            assert prs[0].title == "ticket-128: converge scheduler"
            assert prs[0].url == "https://github.com/subactor/core/pull/255"


def test_fetch_github_prs_offline_returns_empty():
    with patch("shutil.which", return_value=None):
        assert fetch_github_prs() == []


def test_fetch_doctor_issues_success():
    mock_json = """[
        {
            "repository": {"nameWithOwner": "subactor/doctor-agent"},
            "number": 301,
            "title": "[Doctor] missing-file",
            "url": "https://github.com/subactor/doctor-agent/issues/301",
            "labels": [{"name": "doctor:detected"}, {"name": "repair:completed"}]
        }
    ]"""
    with patch("shutil.which", return_value="/usr/bin/gh"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_json)
            issues = fetch_doctor_issues(limit=5)
            assert len(issues) == 1
            assert issues[0].repository == "subactor/doctor-agent"
            assert issues[0].number == 301
            assert "repair:completed" in issues[0].labels


def test_fetch_systemd_services():
    mock_stdout = """subactor-coding-agent.service loaded active running Subactor coding ticket executor
subactor-repair-agent.timer loaded active waiting Subactor repair timer
"""
    with patch("shutil.which", return_value="/usr/bin/systemctl"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_stdout)
            services = fetch_systemd_services()
            assert len(services) == 2
            assert services[0][0] == "subactor-coding-agent.service"
            assert services[0][1] == "active"


def test_render_fleet_startup_banner_outputs_rich_content():
    overview = FleetOverview(
        prs=[
            PullRequestSummary(
                repository="if-uri/roadmap",
                number=1,
                title="fix: repair configuration",
                url="https://github.com/if-uri/roadmap/pull/1",
            )
        ],
        doctor_issues=[
            DoctorIssueSummary(
                repository="subactor/doctor-agent",
                number=301,
                title="missing-file in if-uri/roadmap",
                url="https://github.com/subactor/doctor-agent/issues/301",
                labels=["doctor:detected", "repair:completed"],
            )
        ],
        active_services=[("subactor-coding-agent.service", "active", "running")],
        planfile_ok=True,
        planfile_version="0.1.124",
    )
    console = Console(record=True, width=100)
    render_fleet_startup_banner(console, overview, hyperlinks=True)
    text = console.export_text()
    assert "Subactor Autonomous Ecosystem & Fleet Activity" in text
    assert "if-uri/roadmap#1" in text
    assert "fix: repair configuration" in text
    assert "subactor/doctor-agent#301" in text
    assert "Planfile" in text
    assert "OK (v0.1.124)" in text
