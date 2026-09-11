"""Coordinator's independent acceptance checks for CR-09 and CR-01 only."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.workflow.contracts import WorkflowState as S
from core.workflow.readiness import ReadinessError
from core.workflow.runtime import WorkflowRuntime
from spikes.runtime_choice.contracts import LabConfig, RuntimeKind, WorkflowVersion
from spikes.runtime_choice.effect_server import EffectServer


def database_rows(path: Path) -> tuple[list, list]:
    with sqlite3.connect(path) as connection:
        return (connection.execute('SELECT * FROM runs ORDER BY run_id').fetchall(),
                connection.execute('SELECT * FROM outbox ORDER BY event_id').fetchall())


def local_runtime(path: Path) -> WorkflowRuntime:
    return WorkflowRuntime(path, clock=lambda: datetime(2026, 9, 9, tzinfo=UTC))


def test_delivery_rejections_preserve_run_and_outbox(tmp_path: Path) -> None:
    database = tmp_path / 'workflow.sqlite3'
    runtime = local_runtime(database)
    try:
        before = database_rows(database)
        with pytest.raises(ReadinessError, match='LOCAL_RUNTIME_DELIVERY_FORBIDDEN'):
            runtime.register_run('forbidden', 'review', initial_state=S.DELIVERED)
        assert database_rows(database) == before
        runtime.register_run('valid', 'review')
        for state in (S.IMPLEMENTING_ECONOMY, S.VALIDATING, S.INDEPENDENT_REVIEW):
            runtime.transition_run('valid', state)
        before = database_rows(database)
        with pytest.raises(ReadinessError, match='LOCAL_RUNTIME_DELIVERY_FORBIDDEN'):
            runtime.transition_run('valid', S.DELIVERED, idempotency_key='forbidden-delivery')
        assert database_rows(database) == before
        runtime.cancel_run('valid')
        assert runtime.get_run('valid').state is S.CANCELLED
    finally:
        runtime.close()


def test_legacy_delivery_cannot_be_returned_or_silently_rewritten(tmp_path: Path) -> None:
    database = tmp_path / 'legacy.sqlite3'
    runtime = local_runtime(database)
    runtime.register_run('legacy', 'review')
    runtime.close()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE runs SET state='delivered' WHERE run_id='legacy'")
    before = database_rows(database)
    reopened = None
    try:
        with pytest.raises(ReadinessError, match='LOCAL_RUNTIME_DELIVERY_FORBIDDEN'):
            reopened = local_runtime(database)
            reopened.get_run('legacy')
    finally:
        if reopened is not None:
            reopened.close()
    assert database_rows(database) == before


def config_for(path: Path, url: str = 'http://127.0.0.1:18402') -> LabConfig:
    return LabConfig(lab_id='independent-wave1', root_dir=path,
        runtime=RuntimeKind.NATIVE_SQLITE, runtime_version='native-core',
        workflow_version=WorkflowVersion.V1, database_alias='darkfac_hf02_review',
        effect_base_url=url)


def test_config_roundtrip_preserves_python_string_compatibility(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for path_input in (tmp_path, str(tmp_path)):
        parsed = LabConfig.model_validate({**config.model_dump(), 'root_dir': path_input})
        assert parsed.root_dir == tmp_path.resolve()
        assert LabConfig.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize('field,value', [
    ('sample_interval_ms', True), ('lease_seconds', True), ('root_dir', False),
    ('root_dir', 19), ('root_dir', None), ('root_dir', []), ('root_dir', {}),
    ('runtime', 'unlisted-runtime'), ('extra_control', 'synthetic-value'),
])
def test_invalid_serialized_controls_stay_invalid(tmp_path: Path, field: str, value: object) -> None:
    data = json.loads(config_for(tmp_path).model_dump_json())
    data[field] = value
    with pytest.raises(ValidationError):
        LabConfig.model_validate_json(json.dumps(data))


@pytest.mark.parametrize('invalid_root', [False, {'synthetic': 'PRIVATE_TEST_SENTINEL'}])
def test_driver_rejects_bad_config_without_exposing_input(tmp_path: Path, invalid_root: object) -> None:
    data = json.loads(config_for(tmp_path).model_dump_json())
    data['root_dir'] = invalid_root
    config_path = tmp_path / 'invalid.json'
    config_path.write_text(json.dumps(data), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', '-m', 'spikes.runtime_choice.driver',
        '--config', str(config_path)], cwd=ROOT, input='', capture_output=True,
        text=True, encoding='utf-8', timeout=10,
        env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr.strip() == 'CONFIG_INVALID'


def test_driver_process_completes_before_shutdown_and_persists_effect(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        config_path = tmp_path / 'config.json'
        config_path.write_text(config_for(tmp_path, server.base_url).model_dump_json(), encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-B', '-m', 'spikes.runtime_choice.driver',
            '--config', str(config_path)], cwd=ROOT, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        lines: queue.Queue[str | None] = queue.Queue()
        def consume() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        observed = []
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps({'command_id': 'start-review', 'action': 'start',
                'workflow_id': 'wave1-independent', 'scenario_id': 'R01', 'workflow_version': 'v1',
                'payload': {'approval_pre_resolved': True, 'value': 29, 'release_digest': 'release-review'}}) + '\n')
            process.stdin.flush()
            deadline = time.monotonic() + 10
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, 'No terminal event within deadline'
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    pytest.fail('No terminal event within deadline')
                assert line is not None, 'Driver exited before terminal event'
                event = json.loads(line)
                observed.append(event)
                assert event['kind'] != 'error', event.get('code')
                if event['kind'] == 'completed':
                    assert event['workflow_id'] == 'wave1-independent'
                    assert event['runtime_status'] == 'succeeded'
                    break
            assert any(event['kind'] == 'started' for event in observed)
            assert [event['step_id'] for event in observed if event['kind'] == 'step_observed'] == ['S0', 'S1', 'S3', 'S4']
            assert server.store.effect_count(workflow_id='wave1-independent') == 1
            with sqlite3.connect(tmp_path / 'native/orchestrator.sqlite3') as connection:
                row = connection.execute('SELECT status FROM runs WHERE task_id=?', ('wave1-independent',)).fetchone()
                assert row is not None and row[0] == 'SUCCEEDED'
            process.stdin.write(json.dumps({'command_id': 'stop-review', 'action': 'shutdown'}) + '\n')
            process.stdin.flush()
            process.stdin.close()
            assert process.wait(timeout=10) == 0
            assert process.stderr is not None
            assert process.stderr.read() == ''
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
