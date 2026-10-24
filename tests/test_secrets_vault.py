import json
import os
import re
import sys

from core.infra import secrets_vault as v


def _fake_sops(tmp_path, monkeypatch, payload):
    script = tmp_path / "sops_fake.py"
    script.write_text(f"print({json.dumps(json.dumps(payload))})\n", encoding="utf-8")
    if sys.platform == "win32":
        exe = tmp_path / "sops.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = tmp_path / "sops"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


def test_noop_without_sops_or_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert v.decrypt(tmp_path / "x.enc.yaml") == {}
    assert v.load_into_env(tmp_path / "x.enc.yaml") == []


def test_load_into_env_and_check_with_fake_sops(tmp_path, monkeypatch):
    from scripts import bootstrap_secrets as b

    _fake_sops(tmp_path, monkeypatch, {"FAKE_VAULT_KEY": "abc", "GH_APP_ID": "1"})
    f = tmp_path / "f.enc.yaml"
    f.write_text("x", encoding="utf-8")
    monkeypatch.delenv("FAKE_VAULT_KEY", raising=False)
    try:
        assert "FAKE_VAULT_KEY" in v.load_into_env(f)
        assert os.environ["FAKE_VAULT_KEY"] == "abc"
    finally:
        os.environ.pop("FAKE_VAULT_KEY", None)
        os.environ.pop("GH_APP_ID", None)
    miss = b.missing_keys(f)
    assert "GH_APP_ID" not in miss and "DOKPLOY_TOKEN" in miss


def test_encrypt_uses_temp_file_and_cleans_up(tmp_path, monkeypatch):
    from scripts import bootstrap_secrets as b

    # fake sops: "encripta" ecoando o arquivo de entrada (ultimo argumento)
    script = tmp_path / "sops_fake.py"
    script.write_text("import sys\nprint(open(sys.argv[-1], encoding='utf-8').read())\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if sys.platform == "win32":
        (bindir / "sops.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        exe = bindir / "sops"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    target = tmp_path / "secrets" / "factory.enc.yaml"
    b._encrypt({"K": "v"}, target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"K": "v"}
    assert [p.name for p in target.parent.iterdir()] == ["factory.enc.yaml"]


def test_agent_policy_denies_sops():
    pol = json.loads((v.REPO_ROOT / ".factory/config/agent_env_policy.json").read_text(encoding="utf-8"))
    assert any(re.search(p, "SOPS_AGE_KEY_FILE") for p in pol["deny_patterns"])
