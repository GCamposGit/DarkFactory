import sys

from core.line import agent_cli
from core.line.agent_env import build_agent_env, load_policy

SECRETS = {"DOKPLOY_TOKEN": "a", "BACKUP_S3_KEY": "b", "DATABASE_URL": "c", "GH_TOKEN": "d",
           "GH_ADMIN_TOKEN": "e", "POSTGRES_PASSWORD": "f", "TELEGRAM_BOT_TOKEN": "g", "AWS_SECRET_ACCESS_KEY": "h"}


def test_denies_other_roles_and_keeps_harness_auth():
    base = {**SECRETS, "PATH": "/bin", "ANTHROPIC_API_KEY": "sk-keep", "OPENAI_API_KEY": "x"}
    out = build_agent_env(base)
    assert not set(SECRETS) & set(out)
    assert out["PATH"] == "/bin" and out["ANTHROPIC_API_KEY"] == "sk-keep" and "OPENAI_API_KEY" in out


def test_capability_grants_one_secret():
    out = build_agent_env({"GH_TOKEN": "t", "DOKPLOY_TOKEN": "d"}, capabilities=["gh_token"])
    assert out == {"GH_TOKEN": "t"}


def test_fallback_policy_when_file_missing(tmp_path):
    assert "^DOKPLOY" in load_policy(tmp_path / "nope.json")["deny_patterns"]


def test_probe_subprocess_cannot_see_secrets(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    code = "import os;print(sorted(k for k in os.environ if k in %r))" % (sorted(SECRETS),)
    res = agent_cli._run_bounded([sys.executable, "-c", code], timeout_s=30)
    assert res.stdout.strip() == "[]"
    res2 = agent_cli._run_bounded([sys.executable, "-c", code], timeout_s=30, scrub_env=False)
    assert "DOKPLOY_TOKEN" in res2.stdout
