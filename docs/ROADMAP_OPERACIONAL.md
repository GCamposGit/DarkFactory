# Roadmap Operacional do Projeto

Revisão obrigatória do owner (08/09/2026): [requisitos de autonomia do workflow híbrido](HYBRID_AUTONOMY_REQUIREMENTS.md), com Grill, ambiente testado, dependências manuais guiadas, modelos atualizados diariamente, aprendizado/pesquisa e despacho paralelo automático. A matriz por módulo integra esses requisitos à onda 1 e preserva os IDs existentes.

## Procedimento obrigatório: planejar com alta inteligência, executar com modelos econômicos

**Por instrução explícita do owner, toda funcionalidade, correção, script, skill ou configuração deve ser especificada e aprovada por um modelo de alta inteligência antes do handoff para um modelo econômico implementar e testar. Sempre.** A profundidade varia com a tarefa; a separação de papéis e o gate de prontidão não são opcionais. Lacunas de arquitetura, escopo ou critérios retornam ao planejador, sem serem resolvidas silenciosamente pelo implementador.

O contrato completo está em [HANDOFF_POLICY.md](HANDOFF_POLICY.md). Já estão detalhados [HF-01](handoffs/HF-01.md), em seis tickets, e [HF-02](handoffs/HF-02.md), em oito tickets. A revisão da baseline e a decisão final de runtime permanecem com alta inteligência; implementação, coleta e testes são econômicos. Dependências e acesso são conferidos antes de cada despacho.

Até a implementação dos gates em HF-04/HF-06/HF-07, o coordenador deve aplicar esse procedimento no workflow PIV existente. Depois, o orquestrador deve bloquear deterministicamente qualquer despacho sem plano vigente, aprovado e específico para o ticket. Esta atualização estabelece a regra; não declara esse bloqueio automático já implementado.

## Extensão proposta — workflow híbrido (08/09/2026)

O [plano integrado HF](HYBRID_WORKFLOW_PLAN_2026-09-08.md) complementa DF-01–DF-23, RM-01–RM-09 e INFRA-01–INFRA-11 com duas ondas: decisão/ativação do fluxo híbrido e expansão de capacidades. HF-01–HF-15 e HF-20–HF-25 são propostas, sem alterar o status dos tickets existentes. Esta referência integra os documentos; a ingestão de HF/INFRA no painel e a reconciliação das evidências pertencem a HF-01/HF-13 e ainda não foram implementadas. As fontes atualmente suportadas pelo painel continuam descritas abaixo.

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
