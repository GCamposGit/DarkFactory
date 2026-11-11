"""Preflight do contrato Python usado pelo host manual.

`run_ticket._stage_plan` chama `enforce_manual_host_contract` antes de iniciar
PLAN. A versao do interpretador e conferida primeiro. As dependencias saem de
`requirements.txt`, o mesmo arquivo que o portao instala (`core/harness/runner.py`
e `scripts/typecheck.py`). O mapa abaixo so traduz distribuicao -> modulo quando
o nome importavel nao e o da distribuicao.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple


MINIMUM_PYTHON = (3, 12)
GATE_REQUIREMENTS_NAME = "requirements.txt"

# Excecoes ao padrao "hifen vira underscore". O conjunto exigido vem do arquivo.
_IMPORT_NAME_BY_DISTRIBUTION = {
    "import-linter": "importlinter",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "pytest-xdist": "xdist",
}

_REQUIREMENT_NAME = re.compile(r"[<>=!~;\[]")


class GateContractError(RuntimeError):
    """O arquivo de contrato do portao nao pode ser lido ou nao declara nada."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        super().__init__("{} ({})".format(path, detail))


@dataclass(frozen=True)
class GateDependency:
    module: str
    distribution: str


@dataclass(frozen=True)
class ContractViolation:
    cause: str
    correction_command: str

    def render(self) -> str:
        return "{} Correcao: {}".format(self.cause, self.correction_command)


def gate_requirements_path() -> Path:
    """`requirements.txt` na raiz do repositorio que contem este modulo."""

    return Path(__file__).resolve().parents[2] / GATE_REQUIREMENTS_NAME


def import_name_for_distribution(distribution: str) -> str:
    """Nome importavel de uma distribuicao do contrato do portao."""

    mapped = _IMPORT_NAME_BY_DISTRIBUTION.get(distribution.casefold())
    if mapped:
        return mapped
    return distribution.replace("-", "_")


def declared_distributions(text: str) -> Tuple[str, ...]:
    """Nomes de distribuicao em um `requirements.txt`, na ordem do arquivo."""

    names = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = _REQUIREMENT_NAME.split(line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return tuple(names)


def load_gate_dependencies(path: Optional[Path] = None) -> Tuple[GateDependency, ...]:
    """Dependencias declaradas pelo contrato do portao, nao uma lista paralela."""

    target = path if path is not None else gate_requirements_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateContractError(target, "{}: {}".format(type(exc).__name__, exc)) from exc
    names = declared_distributions(text)
    if not names:
        raise GateContractError(target, "nenhuma dependencia declarada")
    return tuple(
        GateDependency(module=import_name_for_distribution(name), distribution=name)
        for name in names
    )


def _version_tuple(version_info: object) -> Tuple[int, int, int]:
    if hasattr(version_info, "major"):
        major = int(getattr(version_info, "major"))
        minor = int(getattr(version_info, "minor"))
        micro = int(getattr(version_info, "micro", 0))
        return major, minor, micro

    sequence = tuple(version_info)  # type: ignore[arg-type]
    micro = int(sequence[2]) if len(sequence) > 2 else 0
    return int(sequence[0]), int(sequence[1]), micro


def _python_install_command(platform_name: str) -> str:
    install = "pip install -r {}".format(GATE_REQUIREMENTS_NAME)
    if platform_name == "nt":
        return "winget install --id Python.Python.3.12 -e && py -3.12 -m {}".format(install)
    return "instale Python 3.12+ pelo gerenciador do sistema e execute python3.12 -m {}".format(install)


def _dependency_install_command(python_executable: str, dependency: GateDependency) -> str:
    return '"{}" -m pip install {}'.format(python_executable, dependency.distribution)


def _contract_dependencies(
    dependencies: Optional[Sequence[GateDependency]],
    requirements_path: Optional[Path],
    python_executable: str,
) -> Tuple[Tuple[GateDependency, ...], Optional[ContractViolation]]:
    if dependencies is not None:
        return tuple(dependencies), None
    try:
        return load_gate_dependencies(requirements_path), None
    except GateContractError as exc:
        return (), ContractViolation(
            cause="host_preflight_failed:gate_contract_unreadable: {}".format(exc),
            correction_command='"{}" -m pip install -r {}'.format(
                python_executable,
                GATE_REQUIREMENTS_NAME,
            ),
        )


def check_manual_host_contract(
    version_info: object = None,
    module_available: Optional[Callable[[str], bool]] = None,
    python_executable: Optional[str] = None,
    platform_name: Optional[str] = None,
    dependencies: Optional[Sequence[GateDependency]] = None,
    requirements_path: Optional[Path] = None,
) -> Tuple[ContractViolation, ...]:
    """Retorna todas as violacoes que devem impedir o inicio de PLAN."""

    selected_version = version_info if version_info is not None else sys.version_info
    selected_executable = python_executable or sys.executable
    selected_platform = platform_name or os.name
    required, unreadable = _contract_dependencies(
        dependencies,
        requirements_path,
        selected_executable,
    )

    if module_available is None:

        def module_available(module: str) -> bool:
            try:
                return importlib.util.find_spec(module) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                return False

    version = _version_tuple(selected_version)
    violations = []

    if version[:2] < MINIMUM_PYTHON:
        violations.append(
            ContractViolation(
                cause=(
                    "host_preflight_failed:python_version: Python {}.{}.{} "
                    "selecionado em {}; o contrato exige Python 3.12+."
                ).format(
                    version[0],
                    version[1],
                    version[2],
                    selected_executable,
                ),
                correction_command=_python_install_command(selected_platform),
            )
        )

    if unreadable is not None:
        violations.append(unreadable)
        return tuple(violations)

    for dependency in required:
        if not module_available(dependency.module):
            violations.append(
                ContractViolation(
                    cause=(
                        "host_preflight_failed:gate_dependency_missing: "
                        "modulo {!r} ausente no Python selecionado em {}."
                    ).format(
                        dependency.module,
                        selected_executable,
                    ),
                    correction_command=_dependency_install_command(
                        selected_executable,
                        dependency,
                    ),
                )
            )

    return tuple(violations)


def enforce_manual_host_contract(
    version_info: object = None,
    module_available: Optional[Callable[[str], bool]] = None,
    python_executable: Optional[str] = None,
    platform_name: Optional[str] = None,
    stderr: object = None,
    dependencies: Optional[Sequence[GateDependency]] = None,
    requirements_path: Optional[Path] = None,
) -> bool:
    """Imprime diagnostico acionavel e informa se PLAN pode ser iniciado."""

    output = stderr if stderr is not None else sys.stderr
    violations = check_manual_host_contract(
        version_info=version_info,
        module_available=module_available,
        python_executable=python_executable,
        platform_name=platform_name,
        dependencies=dependencies,
        requirements_path=requirements_path,
    )

    if violations:
        print(
            "Preflight do host manual recusou a execucao antes de PLAN.",
            file=output,
        )
        for violation in violations:
            print("- {}".format(violation.render()), file=output)
        return False

    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Valida o contrato do host manual antes de PLAN."
    )
    parser.parse_args(argv)

    if not enforce_manual_host_contract():
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
