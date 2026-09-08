# DF-24 — Project Adoption Gateway

## Resultado

A DarkFac agora possui uma fronteira headless para iniciar greenfields, adotar brownfields, atualizar seu runtime e preparar demandas em worktrees isoladas. O runtime do consumidor fica em `.factory/runtime`, sem colisão com `core/` ou `src/` do produto, e é fixado por commit de origem e hashes SHA-256.

## Garantias implementadas

- inspeção sem escrita e plano idempotente;
- source e target sujos falham fechados;
- worktree dedicada criada a partir de commit, sem copiar alterações locais;
- ownership explícito: arquivos da fábrica são gerenciados, governança/harness do produto são preservados;
- seeds opcionais e restritos a `MISSION.md`, `FACTORY_RULES.md` e `harness.config.json`;
- escritas atômicas, rollback e lock gravado por último;
- updates só alteram ou removem arquivos gerenciados intactos;
- verificação de drift e preparação tipada de tickets com caminhos/validações;
- raiz portátil para todos os módulos que persistem estado no `.factory/` do produto;
- comandos `python` do harness vinculados ao `sys.executable` que iniciou o runner, inclusive no Windows;
- roteador tolerante a modelos descobertos antes da publicação de métricas, preservando `unknown != 0`.

## Evidência

- Suíte focal de adoção: `8 passed`.
- Regressão crítica de harness/benchmarks: `42 passed`.
- Suíte completa final: `212 passed, 1 skipped`.
- Harness formal final: `[HARNESS_PASS]`, `count=213`, `passed_count=212`, `skipped_count=1`.

O ensaio ignorado é o teste live GPU de áudio, deliberadamente opt-in.

## Pesquisa

Decisões e fontes primárias: `.factory/research/20260907_project_adoption_gateway/`.
