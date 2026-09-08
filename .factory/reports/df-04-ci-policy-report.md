# DF-04 — Trusted CI policy

## Resultado

A validação de pull requests agora separa política confiável de execução candidata.

- `pull_request_target` executa somente o guard carregado da base confiável. Esse job não instala dependências e não executa código do candidato.
- `pull_request` executa os testes sem contexto privilegiado, depois de validar a base explícita e materializar `core/harness/` e `harness.config.json` a partir da base.
- Checkouts de base e candidato usam SHAs explícitos e `persist-credentials: false`.
- `AGENTS.md`, workflows, configuração do harness, implementação do verificador e seus testes contratuais são caminhos protegidos.
- Os passos do harness declaram `kind` explicitamente; a suíte possui timeout de 600 segundos.

A proteção da branch deve exigir os checks `trusted-pr-policy` e `pr-validation`. Essa configuração vive no provedor GitHub e não pode ser garantida apenas pelo arquivo versionado.

## Arquivos

- `.github/workflows/ci.yml`
- `core/orchestrator/guard.py`
- `harness.config.json`
- `tests/test_ci_policy.py`
- `tests/test_learning_pack.py` (isolamento de estado descoberto pela validação)
- `.factory/learning/learning_ledger.json` (RCA `rca_66fc5404`, ainda `proposed`)

## Reprodução e RCA

Os três testes iniciais falharam no estado anterior: controles incompletos, ausência de verificador base-owned e tipos implícitos no harness.

A primeira execução completa também detectou uma flake preexistente: o teste REST de Learning Packs consultava o storage real compartilhado e assumia que `latest` não mudaria entre requests. O teste agora redireciona o storage para `tmp_path`; a regra preventiva foi registrada como candidata, sem promoção automática.

## Validação

- `python -m pytest tests/test_ci_policy.py tests/test_governance_guard.py tests/test_harness_contract.py -v`: 18 passed.
- `python core/harness/runner.py --quick`: 170 passed, `[HARNESS_PASS]`.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: 170 passed.
- `python core/harness/runner.py | python core/harness/markers.py`: `Validation Result: PASS`.
- Workflow YAML carregado com sucesso.
