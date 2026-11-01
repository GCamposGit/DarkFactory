# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Portao completo excede 3000s no fallback Notebook e nao produz verdict utilizavel

## Problema
Defeito observado durante a publicacao documental do PR 821. Candidato 2d3a56ca0cb2f6cd1fe61dad03ed51594b48e938, arvore 652d10cc9772f1a4316bdbfb95131503fb8a7e8f, python core/harness/runner.py --quick. Worker http://100.77.130.5:8080 nao manteve estabilidade; fallback AI_Notebook permitiu apenas 1 worker xdist com RAM disponivel 5.6 GiB. Syntax e tipos passaram, mas unit_and_integration_tests_parallel atingiu 3001.1s e exit 124 com progresso ate 90%, count=0, HARNESS_FAIL. Log .worktrees/codex-quality-roadmap-20261101/.factory/test_logs/step-timeouts/20261101T224503Z-1793573103918757900-unit_and_integration_tests_parallel.log. Tambem houve heartbeat remoto perdido e teardown OSError cannot send already closed apos cancelamento. Causa exata ainda nao confirmada: nao atribuir automaticamente a um teste ou ao candidato. Objetivo: diagnosticar e corrigir a falha de vazao/roteamento/observabilidade para executar o portao completo em recurso apto e produzir verdict vinculado ao candidato. Aceite automatico por evidencia comportamental e portao oficial; a publicacao do PR 821 continua bloqueada enquanto nao houver PASS completo valido. Reutilizar tickets/rotinas existentes se a reconciliacao revelar causa ja coberta.

## Jornada
Defeito observado durante a publicacao documental do PR 821. Candidato 2d3a56ca0cb2f6cd1fe61dad03ed51594b48e938, arvore 652d10cc9772f1a4316bdbfb95131503fb8a7e8f, python core/harness/runner.py --quick. Worker http://100.77.130.5:8080 nao manteve estabilidade; fallback AI_Notebook permitiu apenas 1 worker xdist com RAM disponivel 5.6 GiB. Syntax e tipos passaram, mas unit_and_integration_tests_parallel atingiu 3001.1s e exit 124 com progresso ate 90%, count=0, HARNESS_FAIL. Log .worktrees/codex-quality-roadmap-20261101/.factory/test_logs/step-timeouts/20261101T224503Z-1793573103918757900-unit_and_integration_tests_parallel.log. Tambem houve heartbeat remoto perdido e teardown OSError cannot send already closed apos cancelamento. Causa exata ainda nao confirmada: nao atribuir automaticamente a um teste ou ao candidato. Objetivo: diagnosticar e corrigir a falha de vazao/roteamento/observabilidade para executar o portao completo em recurso apto e produzir verdict vinculado ao candidato. Aceite automatico por evidencia comportamental e portao oficial; a publicacao do PR 821 continua bloqueada enquanto nao houver PASS completo valido. Reutilizar tickets/rotinas existentes se a reconciliacao revelar causa ja coberta.

## Fora de escopo
- Nao aumentar cegamente timeout, forcar workers alem da memoria ou executar duas suites concorrentes no mesmo host.
- Nao reduzir descoberta, excluir testes ou aceitar count zero para fazer passar.
- Nao usar hosts desabilitados, alterar governanca protegida ou pedir rotina tecnica ao owner.

## Criterios de aceite
- Reproduzir e isolar se o problema e capacidade, testes lentos/travados ou transporte; registrar medida e causa sem inferencia inventada.
- Corrigir o menor consumidor responsavel e consolidar testes comportamentais existentes; worker remoto instavel e fallback sem capacidade devem produzir estado estruturado e retomavel.
- Executar portao completo em host elegivel, obter HARNESS_PASS com descoberta e checagens positivas, preservando binding ao candidato, cache e suite_lock; atualizar manual e evidencia.
