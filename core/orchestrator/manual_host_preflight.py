"""Preflight do contrato Python usado pelo host manual.

Este módulo deve ser executado pelo host antes de iniciar PLAN. Ele valida tanto
a versão do interpretador efetivamente selecionado quanto as dependências
necessárias para executar os portões da fábrica.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple


MINIMUM_PYTHON = (3, 12)


@dataclass(frozen=True)
class GateDependency:
    module: str
    distribution: str


REQUIRED_GATE_DEPENDENCIES = (
    GateDependency(module="importlinter", distribution="import-linter"),
)


@dataclass(frozen=True)
class ContractViolation:
    cause: str
    correction_command: str

    def render(self) -> str:
        return "{} Correcao: {}".format(self.cause, self.correction_command)


def _version_tuple(version_info: object) -> Tuple[int, int, int]:
    major = int(getattr(version_info, "major", version_info[0]))
    minor = int(getattr(version_info, "minor", version_info[1]))

    try:
        micro = int(getattr(version_info, "micro", version_info[2]))
    except (IndexError, TypeError):
        micro = 0

    return major, minor, micro


def _python_install_command(platform_name: str) -> str:
    if platform_name == "nt":
        return (
            "winget install --id Python.Python.3.12 -e && "
            "py -3.12 -m pip install -r requirements-dev.txt"
        )

    return (
        "instale Python 3.12+ pelo gerenciador do sistema e execute "
        "python3.12 -m pip install -r requirements-dev.txt"
    )


def _dependency_install_command(
    python_executable: str,
    dependency: GateDependency,
) -> str:
    return '"{}" -m pip install {}'.format(
        python_executable,
        dependency.distribution,
    )


def check_manual_host_contract(
    version_info: object = None,
    module_available: Optional[Callable[[str], bool]] = None,
    python_executable: Optional[str] = None,
    platform_name: Optional[str] = None,
    dependencies: Sequence[GateDependency] = REQUIRED_GATE_DEPENDENCIES,
) -> Tuple[ContractViolation, ...]:
    """Retorna todas as violações que devem impedir o início de PLAN."""

    selected_version = version_info if version_info is not None else sys.version_info
    selected_executable = python_executable or sys.executable
    selected_platform = platform_name or os.name

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
                    "selecionado em {!r}; o contrato exige Python 3.12+."
                ).format(
                    version[0],
                    version[1],
                    version[2],
                    selected_executable,
                ),
                correction_command=_python_install_command(selected_platform),
            )
        )

    for dependency in dependencies:
        if not module_available(dependency.module):
            violations.append(
                ContractViolation(
                    cause=(
                        "host_preflight_failed:gate_dependency_missing: "
                        "modulo {!r} ausente no Python selecionado em {!r}."
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
) -> bool:
    """Imprime diagnóstico acionável e informa se PLAN pode ser iniciado."""

    output = stderr if stderr is not None else sys.stderr
    violations = check_manual_host_contract(
        version_info=version_info,
        module_available=module_available,
        python_executable=python_executable,
        platform_name=platform_name,
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
