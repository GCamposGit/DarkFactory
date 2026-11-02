# SPEC

## Objetivo
Diagnosticar e corrigir, no menor consumidor responsavel, a falha em que o portao oficial (python core/harness/runner.py --quick) cai para execucao local sem capacidade apta (fallback AI_Notebook, 1 worker xdist, 5.6 GiB livres), queima os 3000s do passo unit_and_integration_tests_parallel e publica HARNESS_FAIL com count=0 e exit 124 — veredito que nao pertence ao candidato. A entrega deve: (a) tornar distinguivel por evidencia estruturada se a causa foi capacidade, teste lento/travado ou transporte; (b) impedir que o fallback local sem capacidade inicie o passo paralelo, produzindo estado estruturado e retomavel em vez de falso HARNESS_FAIL; (c) tornar a perda de worker remoto e o teardown pos-cancelamento (OSError 'cannot send already closed', heartbeat perdido) um estado retomavel e limpo; (d) fechar com portao completo em host elegivel, HARNESS_PASS com descoberta positiva vinculada ao candidato, cache e suite_lock preservados, manual e evidencia atualizados.

## Fora de escopo
- Aumentar timeout_sec de harness.config.json ou qualquer limite de passo sem medida que o justifique (arquivo protegido; mudanca so por autorizacao assinada do owner).
- Forcar numero de workers xdist acima do que a RAM disponivel permite ou remover o piso de memoria de tests/_worker_capacity.
- Executar duas suites concorrentes no mesmo host ou contornar o suite_lock.
- Reduzir descoberta, marcar/excluir testes, usar -k/--deselect ou aceitar count=0 como sucesso.
- Usar hosts desabilitados, alterar arquivos de governanca protegida (MISSION.md, AGENTS.md, guard, core/governance/*, .github/workflows/*) ou pedir rotina tecnica ao owner.
- Atribuir a falha a um teste especifico ou ao candidato 2d3a56ca sem medida reproduzivel; reescrever testes de produto para 'ficarem rapidos'.
- Criar motor novo de capacidade/retomada: reutilizar core/line/gate_ram.py, tests/_worker_capacity.py, core/harness/remote_dispatch.py e core/harness/suite_lock.py.

## Desenho
Reconciliacao: a precondicao de capacidade JA existe, mas apenas no pre-gate do run_ticket (run_ticket.py:1163-1180 chama core.line.gate_ram.wait_for_gate_ram com MIN_GATE_WORKERS=2 e causa gate_insufficient_ram). O runner.py nao tem essa precondicao no caminho de fallback local acionado DEPOIS da perda do worker remoto (core/harness/runner.py:~822-1000). Foi exatamente esse caminho que rodou com 1 worker no Notebook e estourou 3001.1s. Logo, o menor consumidor responsavel e o fallback local do runner, nao o timeout nem a suite.

1) Evidencia antes da correcao (T1): enriquecer _record_step_timeout (core/harness/runner.py:285) e o registro [RUN_TIMING]/RunTimings (runner.py:675-735) com os dados que hoje faltam para classificar a causa: leitura de capacidade no momento do passo (gate_ram.measure_workers(): available_gib + workers), workers xdist efetivamente usados (linha '[WORKERS] allowed' / header 'N workers [M items]' ja parseado em runner.py:190-212), percentual de progresso e ultimos node ids em voo extraidos do tail do output, e o motivo do caminho local (remote lost vs local_only vs sem worker configurado). Classificar em um campo unico timeout_cause com valores fechados: insufficient_capacity | slow_or_hung_test | transport_loss | unknown. Nada de inferencia: 'unknown' quando a evidencia nao sustenta.

2) Precondicao no fallback local (T2): antes de iniciar qualquer step kind=test localmente, medir capacidade com gate_ram (reuso direto, sem duplicar a conta) e, se workers < MIN_GATE_WORKERS, nao iniciar o passo: imprimir marcador/estado estruturado de 'sem capacidade' (cause gate_insufficient_ram, available_gib, workers, candidate_sha, tree_hash, host) e sair com codigo distinto de falha-de-candidato, sem gravar verdict negativo no cache e liberando o suite_lock. Reaproveitar wait_for_gate_ram para esperar a janela antes de desistir (comportamento identico ao pre-gate), e manter o caminho remoto intacto. Consumidores (run_ticket, line_validate) tratam esse estado como retomavel, nao como reprovacao do candidato — alinhado a defesa de classificacao ja existente para exit -15 (USR-298 e correlatos).

3) Transporte (T3): perda de heartbeat remoto e teardown pos-cancelamento devem terminar limpos: o OSError 'cannot send already closed' no teardown apos cancelamento deve ser tratado no ponto onde o stream e fechado (core/harness/remote_dispatch.py:~418-425, ~586, ~997-1043; core/harness/remote_worker.py:~1300, ~1411-1451) sem engolir erro real (sem except: pass) e registrando outcome estruturado no remote_attempts, preservando a retomada do mesmo job ja implementada (resume_job_id, remote_dispatch.py:961-1197).

4) Fechamento (T4): portao completo em host elegivel com HARNESS_PASS, descoberta positiva e binding ao candidato; manual (docs/manual/portao.md, docs/manual/testes.md) e docs/FASE_ATUAL.md atualizados com a causa medida e o novo estado 'sem capacidade'; defeitos remanescentes virando ticket formal via run_ticket --create --queue-only em vez de mencao solta.

## Arquivos a tocar
- core/harness/runner.py
- core/line/gate_ram.py
- core/harness/remote_dispatch.py
- core/harness/remote_worker.py
- run_ticket.py
- tests/test_runner_fallback_diagnostics.py
- tests/test_gate_ram_wait.py
- tests/test_remote_dispatch_resilience.py
- tests/test_runner_candidate_binding.py
- docs/manual/portao.md
- docs/manual/testes.md
- docs/FASE_ATUAL.md

## Riscos
- O proprio portao de validacao desta entrega roda no mesmo mecanismo que esta sendo corrigido: uma regressao na precondicao pode travar toda a linha. Mitigar com fallback conservador (na duvida, comportamento atual) e testes de unidade que injetam a leitura de RAM, nunca medindo o host real (padrao ja usado em tests/conftest.py:783).
- harness.config.json e protegido: se a medida mostrar que a suite completa realmente nao cabe em 3000s em host apto, a correcao de limite exige autorizacao assinada do owner. Nesse caso registrar OA-NNN e nao mexer no arquivo.
- Classificar capacidade insuficiente como estado retomavel pode mascarar reprovacao real se o codigo de saida for confundido com sucesso: o estado precisa ser explicitamente nao-PASS e nunca gravar verdict no cache.
- Medir RAM dentro do processo do runner pode divergir do que o pytest mede nos workers; usar a mesma funcao (tests/_worker_capacity) para evitar duas verdades.
- Reproduzir o travamento de 3000s consome host e janela de suite_lock; limitar a reproducao a uma execucao instrumentada e reaproveitar o log ja existente em .factory/test_logs/step-timeouts/.
