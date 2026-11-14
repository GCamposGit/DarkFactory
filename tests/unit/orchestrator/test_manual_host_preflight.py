from io import StringIO
from types import SimpleNamespace

from core.orchestrator.manual_host_preflight import (
    check_manual_host_contract,
    declared_distributions,
    enforce_manual_host_contract,
    gate_requirements_path,
    load_gate_dependencies,
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
    assert "py -3.12 -m pip install -r requirements.txt" in message
    assert "requirements-dev.txt" not in message


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


def test_dependencias_saem_do_requirements_do_portao(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\n".join(
            [
                "# contrato do portao",
                "import-linter>=2.0,<3",
                "pytest-xdist>=3.6",
                "Pillow>=10",
                "PyYAML>=6",
                "uvicorn[standard]>=0.30",
                "",
            ]
        ),
        encoding="utf-8",
    )

    dependencies = load_gate_dependencies(requirements)

    assert [(item.module, item.distribution) for item in dependencies] == [
        ("importlinter", "import-linter"),
        ("xdist", "pytest-xdist"),
        ("PIL", "Pillow"),
        ("yaml", "PyYAML"),
        ("uvicorn", "uvicorn"),
    ]


def test_arquivo_ausente_nao_libera_o_contrato(tmp_path):
    violations = check_manual_host_contract(
        version_info=version(3, 12, 0),
        module_available=lambda _module: True,
        python_executable="python3.12",
        platform_name="posix",
        requirements_path=tmp_path / "requirements.txt",
    )

    assert len(violations) == 1
    message = violations[0].render()
    assert "host_preflight_failed:gate_contract_unreadable" in message
    assert "requirements.txt" in message


def test_recusa_cada_modulo_declarado_no_contrato(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("import-linter>=2\npytest>=8\nmypy==1.13.0\n", encoding="utf-8")
    missing = {"importlinter", "mypy"}

    violations = check_manual_host_contract(
        version_info=version(3, 12, 2),
        module_available=lambda module: module not in missing,
        python_executable="python3.12",
        platform_name="posix",
        requirements_path=requirements,
    )

    rendered = [item.render() for item in violations]
    assert len(violations) == 2
    assert any("modulo 'importlinter'" in line and "pip install import-linter" in line for line in rendered)
    assert any("modulo 'mypy'" in line and "pip install mypy" in line for line in rendered)
    assert all("modulo 'pytest'" not in line for line in rendered)


def test_contrato_padrao_acompanha_o_requirements_txt_do_repositorio():
    path = gate_requirements_path()
    declared = declared_distributions(path.read_text(encoding="utf-8"))
    dependencies = load_gate_dependencies(path)

    assert path.name == "requirements.txt"
    assert [item.distribution for item in dependencies] == list(declared)
    assert any(item.module == "importlinter" and item.distribution == "import-linter" for item in dependencies)
    assert len(dependencies) > 1
