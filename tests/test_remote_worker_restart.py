"""Restarting the HTTP test worker must not launch the production queue worker."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from core.harness import remote_worker


def test_restart_endpoint_preserves_listener_settings(tmp_path: Path) -> None:
    app = remote_worker.create_worker_app(tmp_path, node_id="desktop-x", host="127.0.0.1", port=8181)
    with patch.object(remote_worker, "trigger_daemon_restart") as restart:
        response = TestClient(app).post("/system/restart")
    assert response.status_code == 200
    restart.assert_called_once_with(tmp_path, "127.0.0.1", 8181, "desktop-x")


def test_trigger_starts_detached_helper_before_exit(tmp_path: Path) -> None:
    with patch.object(remote_worker.threading, "Thread") as thread, \
         patch.object(remote_worker.subprocess, "Popen") as popen, \
         patch.object(remote_worker.os, "_exit") as exit_process, \
         patch.object(remote_worker.time, "sleep"):
        remote_worker.trigger_daemon_restart(tmp_path, "0.0.0.0", 8181, "desktop-x")
        thread.return_value.start.assert_called_once()
        thread.call_args.kwargs["target"]()
    command = popen.call_args.args[0]
    assert command[:3] == [sys.executable, str(Path(remote_worker.__file__).resolve()), "--restart-wait"]
    assert command[command.index("--host") + 1] == "0.0.0.0"
    assert command[command.index("--port") + 1] == "8181"
    assert command[command.index("--node-id") + 1] == "desktop-x"
    assert "start_onprem_worker.ps1" not in " ".join(command)
    exit_process.assert_called_once_with(0)


def test_restart_helper_direct_launch_after_port_release(tmp_path: Path) -> None:
    with patch.object(remote_worker, "_wait_for_released_port", return_value=True) as wait, \
         patch.object(remote_worker.sys, "platform", "linux"), \
         patch.object(remote_worker.subprocess, "Popen") as popen:
        remote_worker._restart_after_exit(tmp_path, "127.0.0.1", 8181, "desktop-x")
    wait.assert_called_once_with("127.0.0.1", 8181)
    command = popen.call_args.args[0]
    assert command == [sys.executable, str(Path(remote_worker.__file__).resolve()),
                       "--host", "127.0.0.1", "--port", "8181", "--node-id", "desktop-x",
                       "--project-root", str(tmp_path)]
    assert "start_onprem_worker.ps1" not in " ".join(command)


def test_restart_helper_prefers_windows_scheduled_task(tmp_path: Path) -> None:
    with patch.object(remote_worker, "_wait_for_released_port", return_value=True), \
         patch.object(remote_worker.sys, "platform", "win32"), \
         patch.object(remote_worker.subprocess, "run", return_value=MagicMock(returncode=0)) as run, \
         patch.object(remote_worker.subprocess, "Popen") as popen:
        remote_worker._restart_after_exit(tmp_path, "0.0.0.0", 8080, "desktop-x")
    assert run.call_count == 2
    assert "Get-ScheduledTask" in run.call_args_list[0].args[0][-1]
    assert "Start-ScheduledTask" in run.call_args_list[1].args[0][-1]
    popen.assert_not_called()


def test_restart_helper_falls_back_when_windows_task_is_absent(tmp_path: Path) -> None:
    with patch.object(remote_worker, "_wait_for_released_port", return_value=True), \
         patch.object(remote_worker.sys, "platform", "win32"), \
         patch.object(remote_worker.subprocess, "run", return_value=MagicMock(returncode=1)) as run, \
         patch.object(remote_worker.subprocess, "Popen") as popen:
        remote_worker._restart_after_exit(tmp_path, "0.0.0.0", 8080, "desktop-x")
    run.assert_called_once()
    assert popen.call_args.args[0][2:8] == ["--host", "0.0.0.0", "--port", "8080", "--node-id", "desktop-x"]


def test_restart_helper_avoids_duplicate_when_port_stays_occupied(tmp_path: Path) -> None:
    with patch.object(remote_worker, "_wait_for_released_port", return_value=False), \
         patch.object(remote_worker.subprocess, "run") as run, \
         patch.object(remote_worker.subprocess, "Popen") as popen:
        remote_worker._restart_after_exit(tmp_path, "0.0.0.0", 8080, "desktop-x")
    run.assert_not_called()
    popen.assert_not_called()
