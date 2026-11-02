# SPEC

## Objetivo
Instrumentar e executar uma prova operacional contínua no darkfac que compare versão implantada, configuração efetiva e control store, reconcilie USR-449 e conduza um achado real não semeado pela fila canônica até rechecagem persistida.

## Fora de escopo
- Criar outro scheduler, fila ou motor paralelo ao CloudCoordinator, FeederCycle e BacklogFeeder.
- Usar falhas semeadas ou testes sintéticos como evidência principal de aceite.
- Provocar indisponibilidade deliberada em produção; concorrência, replay, retomada e queda de host serão exercitados em ambiente isolado.
- Alterar permissões de outros projetos, o experimento Canaletto ou governança protegida sem autorização assinada.
- Declarar USR-449 inexistente apenas por ausência no checkout ou por indisponibilidade de uma fonte operacional.

## Desenho
Adicionar um serviço headless de prova operacional chamado pelo FeederCycle antes da alimentação normal. Ele executará probes tipados para os domínios deployment, effective_config e control_store, vinculando cada rodada a um SHA candidato e persistindo snapshots, achados, ticket de remediação e rechecagens de forma append-only no ControlStore. Achados reais receberão fingerprint determinístico e serão registrados como UserTicket pela fila canônica já consumida pelo BacklogFeeder; unicidade transacional impedirá duplicação entre coordenadores e replays. ProjectRegistry fornecerá o projeto e endpoints, enquanto configuração versionada controlará domínios, intervalos, limites e baselines. Desabilitar um domínio suspenderá apenas novos probes e remediações dessa origem. Rodadas atrasadas serão consolidadas em uma execução rastreável. Um avaliador fail-closed só concluirá o aceite quando houver achado live não semeado, correção entregue pela linha e rechecagem persistida para o mesmo candidato; caso contrário persistirá pending com causas estruturadas.

## Arquivos a tocar
- core/line/operational_proof.py
- core/line/feeder_cycle.py
- core/workflow/control_contracts.py
- core/workflow/control_store.py
- core/orchestrator/adapters/control_postgres.py
- .factory/config/operational_proof.json
- hub/backend/api.py
- hub/backend/service_line.py
- tests/test_control_store.py
- tests/test_feeder_cycle.py
- docs/manual/fila.md
- docs/FASE_ATUAL.md

## Riscos
- USR-449 não aparece no checkout atual; versão implantada, configuração efetiva e store ainda precisam ser consultados e podem divergir ou estar indisponíveis.
- Consistência eventual entre merge, deploy, configuração e store pode gerar falso positivo; probes precisam de janela de estabilização e vínculo ao SHA imutável.
- Dois coordenadores podem observar o mesmo desvio; fingerprint e gravação transacional devem impedir tickets duplicados.
- Credenciais, cobertura, cota, capacidade, CI ou acesso ao ambiente podem impedir evidência suficiente; o resultado deve permanecer pending com causa explícita.
- O arquivo corrigido pelo achado real é imprevisível e será delimitado pelo ticket canônico de remediação, não por esta especificação.
- Mudanças de schema devem manter paridade entre SQLite, PostgreSQL real e modo mock, sem perder histórico existente.
