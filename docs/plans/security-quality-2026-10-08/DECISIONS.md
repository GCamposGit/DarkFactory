# Decisões e limites — segurança e qualidade

**Projeto:** DarkFac (Dark Factory)
**Estado:** Onda 0 ativa; a autorização é delimitada abaixo.
**Atualizado:** 2026-10-09 21:08 UTC / 23:08 Europe/Budapest
**Fonte de baseline:** `origin/main` em `2476f6ddb295e1f90cbdb4b13878f9a14d4a7e77`.

Este arquivo substitui, para decisões atuais, as linhas de planejamento que diziam que nenhuma implementação estava autorizada. A atualização em 09/10 registra o início autorizado da execução da Onda 0 para DarkFac. Ela não libera produtos externos, produção, campanhas ativas, scanners agendados ou efeitos sensíveis sem perfil e autorização aplicáveis.

## Decisões confirmadas

| ID | Decisão | Fonte e estado | Consequência |
|---|---|---|---|
| D-01 | Escopo inicial: somente DarkFac. | Resposta explícita do Owner em 2026-10-09; confirmado. | Os tickets desta etapa não alteram produtos externos nem promovem regras universais sem piloto e nova autorização. |
| D-02 | Classes de dados: público, interno, confidencial e restrito. | Aprovação explícita do Owner em 2026-10-09; confirmado. | SQ-02 deve definir critérios e exemplos para DarkFac; classificação desconhecida não é rebaixada por conveniência. |
| D-03 | Perfil ausente bloqueia efeitos sensíveis. | Aprovação explícita do Owner em 2026-10-09; confirmado. | O caminho de execução deve falhar fechado antes de rede, credenciais, escrita, publicação, deploy ou outro efeito sensível; leitura/documentação local não ganha privilégios implícitos. |
| D-04 | Testes priorizam dados sintéticos; dados reais são minimizados; evidências de segurança ficam sanitizadas e restritas, sem segredos. | Resposta anterior explícita do Owner registrada no roadmap; confirmado para esta etapa. | Testes e evidências devem comprovar a regra sem introduzir dados reais. Obrigações adicionais são resolvidas por projeto no Grill. |
| D-05 | Red team somente em ambientes isolados; produção requer autorização explícita por alvo. | Resposta anterior explícita do Owner registrada no roadmap; confirmado. | Nenhuma campanha ou probe de produção é iniciado por este plano. Cada campanha futura precisa de alvo e limites aprovados. |
| D-06 | Radar: alertas diariamente, pesquisa mensal e revisão extraordinária em incidente crítico. | Resposta anterior explícita do Owner registrada no roadmap; confirmado como requisito futuro. | A frequência está definida; ativar agendadores permanece desligado até ticket e operação próprios. |
| D-07 | Estratégia de dados por módulo; dicionário por função; manual por função, módulo e projeto; termos consistentes no portfólio; atualização em cada rodada/PR/push; versão, integridade e ACL verificadas a cada consulta humana ou de IA. | Pedido explícito do Owner registrado no complemento; confirmado. | SQ-31..38 preservam esse aceite; conteúdo/índice de versão anterior ou ACL divergente bloqueia a consulta. |
| D-08 | A primeira etapa de implementação é Onda 0; nenhuma ativação automática de scanners, agendadores, linha de produção ou campanhas. | Instrução atual do Owner, combinada com o limite do ticket USR-191; confirmado para o escopo deste ciclo. | Fazer tickets canônicos pequenos, em ordem de dependência; efeitos fora do escopo continuam bloqueados. |

## Pendências — não inferir

| ID | Decisão ainda necessária | Estado e condição de retomada |
|---|---|---|
| P-01 | Budget, duração, requisições, concorrência, custo, tamanho/destino/TTL de evidências e exclusões de cada campanha. | Pendente por campanha. Bloqueia qualquer campanha até existir contrato específico; não impede modelar perfis ou documentação. |
| P-02 | Obrigações legais e contratuais específicas de cada projeto piloto. | Pendente por projeto. Resolver no Grill do projeto antes de processar dados que dependam delas; não inferir jurisdição pelo fuso do operador. |
| P-03 | Limites numéricos de qualidade e performance. | Pendente até medir baseline do produto e propor SLOs próprios; nenhum percentual universal será criado. |
| P-04 | Inventário final de ativos, ameaças, fronteiras e consumidores por módulo DarkFac. | SQ-02 deve levantar evidência e marcar desconhecidos; ausência de evidência não significa ausência de risco. |
| P-05 | Proposta final para alterar arquivos protegidos de governança. | SQ-03 deve documentar o percurso permitido e os owners/verificadores existentes antes de tocar esses arquivos. Nenhuma regra protegida é relaxada nesta etapa. |

## Sequência aprovada

1. **SQ-01 / USR-191 — concluído nesta entrega:** decisões, baseline e propriedade das frentes reconciliadas.
2. **SQ-02 — próximo:** perfis de segurança e threat model do DarkFac, usando D-01..D-03; mudanças de perfil ausente bloqueiam efeitos sensíveis.
3. **SQ-03 — em seguida/conforme dependência:** documentar evolução legítima da governança sem editar arquivos protegidos até a rota estar estabelecida.
4. **SQ-31 e ondas seguintes:** somente após predecessores e aceites do roadmap; preservar os tickets existentes USR-170..174, USR-176, USR-162 e demais dependências, sem criar cópias.

Toda execução continua sujeita ao ticket canônico, preflight de cota, isolamento, revisão independente da família implementadora e portão oficial. A autorização deste arquivo não equivale a PASS de segurança, autorização de produção ou liberação de automação.
