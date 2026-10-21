"""Transport real long Unicode read prompts through a temporary file."""
from pathlib import Path
import pytest
from core.line import agent_cli
from core.line.agent_cli import AgentRequest


@pytest.mark.parametrize("failure", [None, "timeout", "os_error"])
def test_long_prompt_is_private_file_and_always_removed(tmp_path, monkeypatch, failure):
    prompt = "revisao \u00e7 \u6f22\u5b57\n" * 6000
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: "grok.exe")
    monkeypatch.setattr(agent_cli, "check_grok_optional_mcp_servers", lambda: ([], []))
    seen: list[Path] = []
    def execute(argv, **kw):
        assert sum(len(a) for a in argv) < 1000
        assert prompt not in argv and "-p" not in argv
        file = Path(argv[argv.index("--prompt-file") + 1])
        seen.append(file)
        assert file.read_text(encoding="utf-8") == prompt
        assert kw["cwd"] == tmp_path
        assert kw["timeout_s"] == 17
        if failure == "os_error":
            raise OSError("simulated launch failure")
        return agent_cli.BoundedProcessResult(returncode=1 if failure else 0, stdout="" if failure else "reviewed", stderr="", duration_s=0.1, timed_out=failure == "timeout")
    monkeypatch.setattr(agent_cli, "_run_bounded", execute)
    req = AgentRequest(prompt=prompt,cwd=tmp_path,mode="read",harness="grok",timeout_s=17)
    res=agent_cli.run_agent(req)
    assert res.ok is (failure is None)
    if failure:
        assert res.error_kind == ("timeout" if failure == "timeout" else "crash")
    assert seen and not seen[0].exists()
