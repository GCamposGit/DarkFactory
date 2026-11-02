# SPEC

## Objetivo
Qualificar, em ambiente isolado, a robustez do ciclo integrado nos domínios segurança, qualidade de código e operação da fábrica, persistindo evidência sintética e operacional separada, custo mensurado ou desconhecido e motivos distintos de adiamento.

## Fora de escopo
- Criar produto, interface ou scheduler paralelo.
- Alterar o piso de 15%, limites de concorrência, leases ou políticas de retry existentes.
- Provocar indisponibilidade deliberada em produção.
- Tratar simulação ou teste automatizado como prova operacional real.
- Descartar histórico ou tickets já admitidos ao desativar um domínio.
- Presumir a causa ou a inexistência do USR-449 sem reconciliar versão implantada, configuração e control store.

## Desenho
Adicionar um único serviço headless de qualificação que orquestre CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store reais, usando namespace, persistência e candidatos isolados. ProjectRegistry expõe a ativação dos três domínios sem conflitar com o domínio HTTP do projeto. Cada execução produz uma matriz Pydantic com cenário, domínio, candidate_sha, run_id, janelas abrangidas, esperado, observado, ValidationMode, referências de evidência, custo nullable com origem e motivo de adiamento tipado. FeederCycle consolida janelas vencidas em uma execução e mantém watermark durável. Cenários sintéticos usam relógio e probes injetados; a prova operacional usa componentes implantados e store isolado, vinculando versão, configuração e hashes. O aceite só fecha após um achado operacional real ser corrigido e rechecado.

## Arquivos a tocar
- core/line/cycle_qualification.py
- core/line/feeder_cycle.py
- core/line/backlog_feeder.py
- core/orchestrator/cloud_coordinator.py
- core/projects/models.py
- core/projects/registry.py
- scripts/qualify_integrated_cycle.py
- tests/test_cycle_qualification.py
- tests/test_feeder_cycle.py
- tests/test_line_fleet_loop.py
- docs/manual/fila.md
- docs/manual/dicionario_dados.md

## Riscos
- Confundir o campo público ProjectDescriptor.domain com os domínios da qualificação.
- Uma falha de isolamento atingir tickets, leases ou hosts produtivos.
- Relógio real tornar cenários concorrentes ou de lease instáveis; a suíte deve usar relógio injetado.
- Custo de assinatura ou API não estar atribuível; a matriz deve registrar unknown, nunca zero presumido.
- Watermark inconsistente duplicar ou perder janelas após restart.
- A divergência USR-449 depender de versão, configuração ou store diferentes do candidato observado.
- Não surgir achado operacional real; nesse caso o aceite permanece pendente, sem fabricar falha.
