# Roadmap Operacional do Projeto

Este arquivo registra as fontes humanas do roadmap aprovado para o painel do DarkHub. A projeção read-only combina o manifesto versionado em `.factory/roadmap/darkfac.json` (tickets RM) com a tabela executável de `docs/DEVELOPMENT_PLAN_2026-09-05.md` (tickets DF); o painel não possui estado de planejamento próprio.

## Fontes canônicas combinadas

- `approved-roadmap`: manifesto JSON dos tickets RM-01–RM-09.
- `development-plan`: tabela de backlog DF-01–DF-23 do plano de desenvolvimento.
- Relatórios `df-*-*-report.md` e `rm-*-*-report.md` existentes são evidências de conclusão; o relatório operacional também cobre RM-01–RM-07. Sem evidência vinculada, o ticket permanece planejado.

## Política de fidelidade

- Cada item aponta para a fonte que o produziu.
- Datas ausentes continuam ausentes; horizonte não é data.
- Conflitos, ciclos causais, órfãos e conclusões sem evidência são avisos do snapshot.
- O projeto `darkfac` é o núcleo compartilhado. O experimento local Canaletto não é uma fonte nem um projeto do painel.
- O MVP termina em RM-06. RM-07 completa a experiência visual; RM-08 trata atualização incremental, telemetria e escala; RM-09 é evolução.
- O histórico RM-09 é uma retenção local, limitada e somente leitura durante a vida do serviço; persistência durável e sincronização remota permanecem fora deste incremento.

## Tickets aprovados

| ID | Entrega | Depende de | Horizonte |
| --- | --- | --- | --- |
| RM-01 | Inventário formal das fontes canônicas e regras de precedência | — | Agora |
| RM-02 | Contratos Pydantic e fixtures do grafo | RM-01 | Agora |
| RM-03 | Compilador, normalização e resolução de identidade | RM-02 | Agora |
| RM-04 | Validador de ciclos, conflitos, órfãos e evidências | RM-03 | Agora |
| RM-05 | Snapshot store, cache e API somente leitura | RM-03, RM-04 | Agora |
| RM-06 | Painel do Hub com visão geral, filtros e drawer | RM-05 | Agora |
| RM-07 | Linha do tempo, grafo de dependências e tabela acessível | RM-06 | Próximo |
| RM-08 | Atualização incremental, telemetria e testes de escala | RM-05, RM-07 | Mais adiante |
| RM-09 | Histórico de snapshots e comparação entre versões | RM-08 | Exploratório |
