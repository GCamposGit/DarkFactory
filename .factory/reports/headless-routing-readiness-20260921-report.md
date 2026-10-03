# Headless routing readiness — 2026-09-21

## Escopo

Teste de prontidão do relay VPS → workers locais, com demanda mock não mutante e verificação do fallback Desktop → notebook para o harness Claude Code.

## Resultado executivo

O caminho Codex está operacional. O coordenador VPS encaminhou `planning` e `development` para `provider:remote_codex` e concluiu o run completo.

O caminho Claude não pôde ser certificado: o worker Desktop e o worker notebook não encontram o executável Claude Code. Além disso, a entrada cloud atual não expõe uma preferência de harness para forçar Claude; o worker usa a cascata especializada padrão.

## Evidência remota

- Coordenador: `http://100.83.176.60:8001/healthz` respondeu `200`, com banco `ready`, DBOS `active` e dois slots.
- Worker Desktop: `/health` apresentou HTTP 500 na última sondagem; `/harness/execute` para Claude respondeu falha estruturada: `Claude Code executable not installed on this host`.
- Worker notebook: `/health` respondeu `200`, com `available_harnesses` contendo somente `codex`, `grok` e `antigravity`.
- Probe Codex real no notebook: respondeu `CODEX_HEADLESS_PROBE`, modelo `gpt-5.6-luna`, duração aproximada de 7,7 s.
- Probe Claude real no notebook: falhou imediatamente por executável ausente.
- Busca somente leitura no notebook não encontrou `claude`, `claude-code`, `claude.cmd` ou `claude.exe` em PATH, npm global, WSL ou caminhos padrão conhecidos.

## Demanda mock real

- `run_id`: `run-8325467a2f25`
- `demand_id`: `dem-331d68d8836f`
- Resultado: `completed`
- `planning`: `provider:remote_codex`
- `development`: `provider:remote_codex`
- `grill`, `memory_observation`, `learning_eval`, `validation`, `independent_review`, `integration`, `build_deploy`, `target_journey`: `provider:deterministic_mock`
- O dispatcher expirou aos 120 s enquanto `development` estava executando, mas o workflow continuou e terminou; há um gap de observabilidade/timeout do cliente.

## Ajuste implementado

Em `core/execution/providers.py`, uma falha estruturada de um harness agora usa `continue` em vez de `break`. Assim, se Desktop responder que Claude não está instalado, o mesmo harness é tentado no notebook antes de trocar de harness.

Foi adicionado `tests/test_remote_provider_failover.py::test_structured_harness_failure_fails_over_to_next_node`.

O patch foi validado no teste focal: `5 passed`.

## Validação oficial

Checkout limpo temporário vinculado ao commit `36bf7e57ce0f126a97b594a3622a38797ab19c35`:

- sintaxe: passou;
- suíte: `1326` testes descobertos, `1318 passed`, `2 skipped`, `6 failed`;
- harness: `[HARNESS_FAIL]`.

As falhas foram ambientais/preexistentes ao patch:

- cinco testes falham porque `telegram_gateway_auth` não possui usuários autorizados;
- um conflito de porta local `8001` ocorreu durante a suíte completa e passou na reprodução isolada seguinte;
- a suíte completa também emitiu aviso de thread Uvicorn por tentativa de bind na porta ocupada.

A primeira execução no workspace principal falhou antes dos checks porque o runner detectou worktree suja, conforme contrato.

## Próximas correções necessárias

1. Instalar/autenticar o CLI Claude Code no notebook e reiniciar o worker; o health deve listar `claude`.
2. Corrigir ou diagnosticar o HTTP 500 do `/health` do Desktop.
3. Expor uma preferência de harness no intake/cloud worker para que uma demanda possa realmente forçar `claude`; hoje a pipeline cloud usa a cascata especializada e não transporta essa preferência.
4. Promover o patch de failover para a imagem/branch implantada na VPS antes de repetir o teste Claude.
5. Resolver o preflight de Telegram ou ajustar os testes de ambiente para distinguirem sandbox sem autenticação de prontidão geral.

O patch não foi instalado na VPS nem foram alteradas credenciais, serviços ou arquivos dos workers.
