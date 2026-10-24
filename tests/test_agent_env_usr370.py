import os
import sys

from core.line import agent_cli
from core.line.stage_build import TicketSpec, run_shell_commands

SECRETS = {"DOKPLOY_TOKEN": "a", "BACKUP_S3_KEY": "b", "DATABASE_URL": "c", "GH_TOKEN": "d",
           "POSTGRES_PASSWORD": "f", "AWS_SECRET_ACCESS_KEY": "h"}
PROBE = "import os;print(os.environ.get('GH_TOKEN'),os.environ.get('DOKPLOY_TOKEN'))"


def _cmd(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_shell_verify_child_cannot_see_secrets(monkeypatch, tmp_path):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    code = f"import os;print(sorted(k for k in os.environ if k in {sorted(SECRETS)!r}))"
    res = run_shell_commands([_cmd(code)], tmp_path, timeout_s=30)
    assert res.ok and "[]" in res.combined_output


def test_shell_capability_and_explicit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DOKPLOY_TOKEN", "d")
    res = run_shell_commands([_cmd(PROBE)], tmp_path, timeout_s=30, capabilities=["GH_TOKEN"])
    assert "t None" in res.combined_output
    explicit = run_shell_commands([_cmd(PROBE)], tmp_path, timeout_s=30,
                                  env={**os.environ, "DOKPLOY_TOKEN": "x"})
    assert "x" in explicit.combined_output  # product env is passed through untouched


def test_ticket_capabilities_default_and_reach_child(monkeypatch):
    assert TicketSpec(id="T", title="t").capabilities == []
    spec = TicketSpec(id="T", title="t", capabilities=["GH_TOKEN"])
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("DOKPLOY_TOKEN", "d")
    req = agent_cli.AgentRequest(prompt="p", cwd=".", mode="write", harness="claude",
                                 capabilities=spec.capabilities)
    res = agent_cli._run_bounded([sys.executable, "-c", PROBE], timeout_s=30,
                                 capabilities=tuple(req.capabilities))
    assert "tok None" in res.stdout
