from io import StringIO
from types import SimpleNamespace

from core.orchestrator.manual_host_preflight import (
    check_manual_host_contract,
    enforce_manual_host_contract,
)


def version(major, minor, micro=0):
    return SimpleNamespace(major=major, minor=minor, micro=micro)


def test_recusa_python_antigo_com_causa_e_comando_de_correcao():
    violations = check_manual_host_contract(
        version_info=version(3, 11, 9),
        module_available=lambda _module: True,
        python_executable=r"C:\Program Files\Python311\python.exe",
        platform_name="nt",
    )

    assert len(violations) == 1

    message = violations[0].render()
    assert "host_preflight_failed:python_version" in message
    assert "Python 3.11.9" in message
    assert r"C:\Program Files\Python311\python.exe" in message
    assert "Python 3.12+" in message
    assert "winget install --id Python.Python.3.12" in message
    assert "py -3.12 -m pip install -r requirements-dev.txt" in message


def test_recusa_modulo_do_portao_ausente_com_comando_de_correcao():
    violations = check_manual_host_contract(
        version_info=version(3, 12, 2),
        module_available=lambda module: module != "importlinter",
        python_executable=r"C:\darkfac\.venv\Scripts\python.exe",
        platform_name="nt",
    )

    assert len(violations) == 1

    message = violations[0].render()
    assert "host_preflight_failed:gate_dependency_missing" in message
    assert "importlinter" in message
    assert (
        r'"C:\darkfac\.venv\Scripts\python.exe" -m pip install import-linter'
        in message
    )


def test_exibe_todas_as_violacoes_antes_de_plan():
    stderr = StringIO()

    accepted = enforce_manual_host_contract(
        version_info=version(3, 11, 0),
        module_available=lambda _module: False,
        python_executable="python",
        platform_name="nt",
        stderr=stderr,
    )

    output = stderr.getvalue()
    assert accepted is False
    assert "recusou a execucao antes de PLAN" in output
    assert "host_preflight_failed:python_version" in output
    assert "host_preflight_failed:gate_dependency_missing" in output
    assert "Correcao:" in output


def test_aceita_python_e_dependencias_conformes():
    violations = check_manual_host_contract(
        version_info=version(3, 12, 0),
        module_available=lambda _module: True,
        python_executable="python3.12",
        platform_name="posix",
    )

    assert violations == ()
