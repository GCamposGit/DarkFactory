# Baseline reconciliada — segurança e qualidade

**Fotografia:** `origin/main` em `85795d8` (inclui PRs #267–#279 e o ajuste do portão USR-135).
**Coleta:** 2026-10-10 20:07 UTC / 2026-10-10 22:07 Europe/Budapest.
**Escopo:** apenas DarkFac.
**Complemento desta candidata:** incorpora os quatro documentos da branch `codex/security-quality-plan-20261008` (HEAD `5e454e2`) e recupera os seis registros reservados USR-170..174 e USR-176; a base atual tem 178 demandas e a candidata passa a ter 184, sem IDs duplicados. USR-175 já existe na base como concluído.

> Esta fotografia substitui os status da seção 2.2 do roadmap de 08/10. Essa seção é preservada como histórico. A aprovação dos checks do PR #244 validou o registro de USR-191, não as features de segurança.

## Artefatos e evidência

- O roadmap contém **38 itens em oito ondas (0–7)**; os itens `SQ-*` são planejamento, e não prova de entrega. O complemento exige estratégia de dados por módulo, dicionário e manual por função, manuais de módulo/projeto, consistência de termos e validação de versão/integridade/ACL por consulta.
- O pedido de origem informa **34 fontes e 17 repositórios candidatos**. A lista de candidatos permite verificar 17 repositórios GitHub distintos (18 URLs GitHub, pois uma URL é a licença do Pyright). O índice atual não normaliza fontes: `RESEARCH.md` tem 15 linhas de fontes técnicas e 9 de mercado/especialistas; o complemento cita mais 9 URLs únicas. A contagem por linhas dá 33 menções, enquanto a contagem de URLs dá outro total. A diferença até 34 fica registrada como discrepância de inventário, sem inventar a fonte ausente. Homologação, versões/licenças e seleção de ferramentas permanecem pendentes; pesquisa não equivale a instalação ou aprovação.
- A fotografia de código anterior do roadmap era `0d71e59487cbed74184d9cb47593e8d0ca7e9005`; a worktree documental antiga tinha base `72c40a401d36cc7c4d4365388a1edf36602f17fb`. Ambas são históricas, não a base atual.
- Os dados AST reportados no documento anterior (304 arquivos, 3.235 funções e 942 classes de `core/`/`hub/`) são da fotografia anterior e não foram recalculados neste ticket.
- A candidata atual não instala scanners, inicia ataques, ativa agendadores nem altera configuração de produção.

## Backlog e frentes já existentes

| Frente | Estado observado na base | Dono confirmado | Condição objetiva de retomada / vínculo |
|---|---|---|---|
| USR-134 — rollback preserva contexto | `completed` | Executor original não reatribuído | Reusar somente quando SQ-23 provar remediação/reteste no candidato atual. |
| USR-136 — backlog durável Hub/linha | PR #167 draft, aberto; validação de produto não executada | Sem claim verificável | Reavaliar quando houver migração/consumo real e evidência de isolamento; não contar como integrado. |
| USR-162 — perda de resposta do worker remoto | `completed`; PR #230 merged em `2026-10-09T21:37:33Z` | Entrega integrada | Revalidar a correção da perda remota no SHA atual antes de fechar a dependência. USR-176 permanece dependente dele para o timeout local. |
| USR-163 — testes externos sem credenciais reais | `completed` | Concluído | Reusar o isolamento já integrado nos testes e laboratórios. |
| USR-164 — progresso ao vivo | `completed` | Concluído | Reusar o canal existente; a URL de escrita remota desta execução estava ausente e o launcher registrou progresso somente localmente. |
| USR-165 — espera por base vermelha | `completed` | Concluído | Reusar a sincronização entregue; conferir SHA antes de medir regressões. |
| USR-166 — cancelamento de run_ticket | `completed` | Concluído | Reusar no interruptor de futuras campanhas, sem ativá-las neste ticket. |
| USR-167 — Postgres real para evidências | `completed` | Concluído | Reusar a evidência integrada; não presumir que o backend cloud esteja conectado neste host. |
| USR-168 — roteamento de subagentes | `completed` | Concluído | Reusar política; não altera revisão independente. |
| USR-169 — limpeza de referências de contexto | `planned` | Sem claim verificável | Reconciliar retenção com as evidências necessárias antes de executar. |
| USR-170 — validade/rastreabilidade de auditoria | `planned` (ID reservado recuperado nesta candidata) | Não atribuído no backlog | Retomar em SQ-04, depois de SQ-02/SQ-03; cobrir identidade, integridade, ausência e vínculo ao candidato. |
| USR-171 — retomada de revisão ligada ao candidato | `planned` (ID reservado recuperado) | Não atribuído | Retomar em SQ-05 após evidência de SQ-04; testar retomada contra SHA divergente. |
| USR-172 — separar privilégios/credenciais | `planned` (ID reservado recuperado) | Não atribuído | Retomar em SQ-09/SQ-10 após threat model e fronteira por tarefa; sem exploração de produção. |
| USR-173 — checker de tipos no portão | `planned` (ID reservado recuperado) | Não atribuído | Retomar em SQ-03/SQ-08 depois de baseline compatível e checker/licença fixados. |
| USR-174 — consumo real da política de dados/residência | `planned` (ID reservado recuperado) | Não atribuído | Retomar em SQ-13 após perfil DarkFac e identidade/efeitos definidos; provar consumidor real, não apenas componente. |
| USR-175 — candidato imutável do portão | `completed` | Concluído | Reusar a validação isolada por SHA. |
| USR-176 — timeout local do portão | `planned` (ID reservado recuperado), depende de USR-162 | Não atribuído | Separar timeout local da perda remota; só fechar após evidência da causa/resultado. Não abrir outro ticket para perda de worker. |
| USR-187 — PID do holder no Windows | `planned`; diagnóstico integrado pelo PR #259 | Sem claim verificável | O diagnóstico aponta divergência entre `Popen.pid` e `os.getpid()` no launcher do Windows; corrigir o marcador do holder e validar no worker Desktop antes de contar o portão remoto como utilizável. |
| USR-188 — helper Codex headless | `planned`; diagnóstico integrado pelo PR #255 | Sem claim verificável | A causa é o refresh de ACL sobre `node_repl.exe` mantido aberto pelo app. A branch `ticket/usr-188` contém correção candidata com override `mcp_servers.node_repl.enabled=false`; teste focal e chamada headless read-only passaram. O gate oficial no SHA `6f59bb59190170418632c15ec184d0e64c1a1a39` falhou: worker remoto indisponível, fallback local excedeu 3.007,1 s, `count=0`; sem integração ou PASS. |
| USR-198 — dependência opcional do psycopg | `planned`; registro integrado pelo PR #256 | Sem claim verificável | Implementar a declaração opcional e aviso de instalação exigidos; o PR #256 registrou a demanda, não entregou a feature. |
| USR-200 — diagnóstico de timeout/lock do portão | `planned`; registro integrado pelo PR #258 | Sem claim verificável | A evidência mais recente é o candidato `6f59bb59190170418632c15ec184d0e64c1a1a39`: worker remoto ficou indisponível e `unit_and_integration_tests_parallel` excedeu 3.007,1 s (código 124; `count=0`). Investigar fallback/worker/lock sem ampliar timeout novamente nem aceitar contagem zero. Dependência do USR-162 satisfeita. |
| USR-179 — isolamento de pytest focal e `SUITE_LOCK` | `planned`; PR #225 aberto, mergeability `UNKNOWN` | Sem claim verificável | O gate oficial reproduziu falha em teste de `suite_lock` dentro da suíte completa; reprodução focal local passa. Investigar contaminação de estado no contexto xdist/worker. |
| USR-190 — ações do Owner no Hub | `completed`; PR #247 merged em `1811757` | Entrega integrada | Confirmar a latência publicada pelo runbook ao usar a fila em produção; não confundir a entrega do Hub com o escopo deste plano. |
| USR-191 — reconciliação | `planned` na base desta fotografia | Este ticket | Fechar somente após documentos reconciliados, portão oficial e integração. |
| USR-193 — allocator de IDs usa base local sem fetch | `planned`, registro integrado pelo PR #246 em `2476f6d` | Sem claim verificável | Corrigir leitura/refresh e colisões via ticket próprio; PR #246 valida apenas o registro. |
| USR-197 — estado/verificação de tokens Telegram | `completed`; PR #251 merged em `60bdbaa`, 2026-10-09T22:51:26Z | PR #250 entrega implementação; PR #251 reconcilia status e evidência após merge | Sem dependência do roadmap. |

USR-170..174 e USR-176 vieram de registros já reservados na branch documental e não estavam em `origin/main`; continuam preservados nesta candidata, mantendo IDs e critérios. USR-175 já estava concluído. A base de 2026-10-10 20:02 UTC inclui USR-214 e USR-223 como `planned`, além dos registros USR-206..220; os seis IDs reservados continuam ausentes da base até esta candidata ser integrada. USR-192 existe como registro proposto no PR #245, ainda aberto e sem mergeability confirmada; não é contado como ticket ativo na base. A perda remota de worker já foi integrada em USR-162/PR #230; não foi criado item duplicado. Nenhum status de implementação é inferido a partir dos PRs de backlog.

## Pull requests observados

| PR | Estado em 2026-10-10 20:07 UTC | Checks/nota | Relação |
|---|---|---|---|
| [#244](https://github.com/GCamposGit/DarkFactory/pull/244) | Merged em `b59d0ad` | Ubuntu e Windows passaram; registro de USR-191. | Só valida backlog do ticket, não a feature. |
| [#246](https://github.com/GCamposGit/DarkFactory/pull/246) | Merged em `2476f6d` | Ubuntu e Windows passaram. | Registro do USR-193. |
| [#247](https://github.com/GCamposGit/DarkFactory/pull/247) | Merged em `1811757`. | Entrega USR-190 integrada. | DarkHub owner actions. |
| [#249](https://github.com/GCamposGit/DarkFactory/pull/249) | Merged em `6cfb157`. | USR-162/181 concluídos e demanda/ação do Owner atualizadas. | Registros do backlog após entregas integradas. |
| [#250](https://github.com/GCamposGit/DarkFactory/pull/250) | Merged em `a95ea32`. | Implementação USR-197 integrada. | `/status` do Telegram e verificação de tokens por papel/fonte. |
| [#251](https://github.com/GCamposGit/DarkFactory/pull/251) | Merged em `60bdbaa`. | USR-197 concluído; OA-008 fechado após convergência do Desktop e deploy código 0. | Reconciliação de backlog/ação do Owner. |
| [#254](https://github.com/GCamposGit/DarkFactory/pull/254) | Merged em `3d94481`. | OA-003 feito. | Configuração do papel `darkfac_live_writer`; fora da entrega de segurança. |
| [#255](https://github.com/GCamposGit/DarkFactory/pull/255) | Merged em `ebb4c22`. | Diagnóstico/registro do USR-188. | Não é a implementação da correção do helper. |
| [#256](https://github.com/GCamposGit/DarkFactory/pull/256) | Merged em `46a4ba4`. | Registro do USR-198. | Não entrega a dependência opcional de psycopg. |
| [#258](https://github.com/GCamposGit/DarkFactory/pull/258) | Merged em `d85cd71`. | Registro do USR-200; checks Ubuntu/Windows concluídos. | Abre a execução do diagnóstico do portão/lock. |
| [#259](https://github.com/GCamposGit/DarkFactory/pull/259) | Merged em `11a2ebb`. | Diagnóstico do USR-187. | Aponta divergência de PID do launcher no worker Windows. |
| [#267](https://github.com/GCamposGit/DarkFactory/pull/267) | Merged em `d7d4447`. | Registros USR-206..213 e OA-010. | Demandas concorrentes preservadas na candidata. |
| [#268](https://github.com/GCamposGit/DarkFactory/pull/268) | Merged em `1a99e4a`. | Registro USR-214. | Evidência de telemetria Codex obsoleta; ainda planejado. |
| [#269](https://github.com/GCamposGit/DarkFactory/pull/269) | Merged em `7e383ab`. | Registros USR-215..220. | Demandas concorrentes preservadas na candidata. |
| [#273](https://github.com/GCamposGit/DarkFactory/pull/273) | Merged em `6e30ce8`. | OA-010 aplicado; sync de cotas voltou. | Contexto do roteamento recuperado durante esta sessão. |
| [#277](https://github.com/GCamposGit/DarkFactory/pull/277) | Merged em `c0f67ba`. | Corrigido allocator sem binário Git no Hub. | Integração concorrente preservada. |
| [#278](https://github.com/GCamposGit/DarkFactory/pull/278) | Merged em `b3661f7`. | Registro USR-223 após CI vermelho no main; CI voltou a verde em SHA posterior. | Diagnóstico de CI, não correção de produto. |
| [#279](https://github.com/GCamposGit/DarkFactory/pull/279) | Merged em `85795d8`. | Conclusão da ação OA-011 sobre limite do portão. | Ajuste operacional; não altera os registros de demanda. |
| [#275](https://github.com/GCamposGit/DarkFactory/pull/275) | Aberto em 20:02 UTC. | Registro USR-221; checks ainda em execução. | Não contado na base. |
| [#276](https://github.com/GCamposGit/DarkFactory/pull/276) | Aberto em 20:02 UTC. | Registro USR-222; checks ainda em execução. | Não contado na base. |
| [#245](https://github.com/GCamposGit/DarkFactory/pull/245) | Aberto; mergeability `UNKNOWN` na consulta atual. | Checks Ubuntu/Windows passaram na última execução disponível. | Registro proposto de USR-192 (`[Errno 2] git` no Telegram). |
| [#230](https://github.com/GCamposGit/DarkFactory/pull/230) | Merged em `2026-10-09T21:37:33Z`. | Entrega USR-162 integrada. | Perda de resposta do worker remoto; sem duplicar. |
| [#225](https://github.com/GCamposGit/DarkFactory/pull/225) | Aberto; mergeability `UNKNOWN` na consulta atual. | Checks Ubuntu/Windows passaram na execução registrada. | USR-179; investigar o bloqueio de `suite_lock` nesta execução. |
| [#167](https://github.com/GCamposGit/DarkFactory/pull/167) | Draft aberto. | Apenas política confiável passou; validação de produto foi pulada. | USR-136, dependência de backlog compartilhado. |
| [#22](https://github.com/GCamposGit/DarkFactory/pull/22) | Aberto. | Ubuntu/Windows falharam na última execução observada. | Teste de idle loop Windows; sem claim atual confirmado. |
| [#20](https://github.com/GCamposGit/DarkFactory/pull/20) | Draft aberto. | Ubuntu/Windows falharam na última execução observada. | Ativação fail-closed; verificar consumidor antes de depender. |

Estados são fotografia, não prova de propriedade. PR aberto, worktree presente ou run recente não são claim válido. Atualizar a consulta antes de cada onda e comparar os diffs/owners antes de abrir implementação.

## Claims, leases e disponibilidade dos dados

- Não há snapshot autoritativo de claims/leases remotos disponível nesta execução: `DARKFAC_HF02_DATABASE_URL` e `DARKHUB_LINE_DATABASE_URL` não estavam configuradas. O próprio `run_ticket` avisou que o progresso desta execução ficaria só no `control.db` local.
- O checkout contém apenas bancos SQLite de experimentos isolados HF-02, não um banco ativo da linha; eles não servem como prova de claim.
- Portanto, nenhum owner/harness é inferido a partir de PR, worktree ou silêncio do banco. O único ownership diretamente confirmado é o Owner das decisões D-01..D-08; tickets sem assignee permanecem “não atribuído”.

## Execução e validação até a fotografia

- A cota do Codex estava acima do piso (57% semanal na última verificação); o helper Codex falhou antes de abrir o terminal ou tocar arquivos (`helper_unknown_error: setup refresh had errors`). O diagnóstico foi registrado no PR #255/USR-188. A branch de implementação candidata desabilita somente o MCP `node_repl` por `-c`; mantém `workspace-write`/`read-only`, não altera config global e não desliga o sandbox. O teste focal passou (24 casos) e uma chamada Codex headless read-only respondeu como esperado.
- O launcher bloqueou Claude ao chegar a 15,0% restante; nenhum `--force` foi usado. O ticket voltou ao roteamento Codex.
- A criação inicial da worktree exigiu permissão Git para atualizar `FETCH_HEAD`; resolvido sem alterar conteúdo compartilhado. A branch `ticket/usr-191` foi rebaseada sobre `origin/main` `eb37442fbe4e0fe3580c3ff1cd4920abadb450a2`; a fotografia agora inclui também #279. Foram preservadas as integrações concorrentes e recuperados os seis registros reservados sem IDs duplicados. A versão desta fotografia tem 178 demandas na base e 184 na candidata.
- O PR #244 passou seu próprio CI, mas isso não é `HARNESS_PASS` das features. O gate oficial do candidato `d5770d61cf40cd28558d564bb4a310696081f293` terminou `[HARNESS_FAIL]`: 3.767 passaram, 22 foram ignorados, 1 falhou (3.790 descobertos), em `tests/test_suite_lock_stalled.py::test_idle_live_holder_triggers_a_stalled_warning_but_is_not_killed`. O holder esperado era PID 7836; o waiter reportou PID 11512 no diretório global do worker. A reprodução focal local passou com `--basetemp` dentro do worktree. O diagnóstico posterior do USR-187 identifica divergência entre `Popen.pid` e o PID real do holder (`os.getpid()`) no launcher Windows do worker Desktop; ainda depende de correção e portão remoto verde. O problema não é duplicado.
- O gate do candidato `1991d679c0409bd70d1146a3cecd9a9af2a0ab09` terminou `[HARNESS_FAIL]`: sintaxe/tipos passaram, mas `unit_and_integration_tests_parallel` excedeu 1.504,4s (código 124; 0 testes reportados). No candidato documental `adfd5fb9cc931960bcffe2810603a35ba30801ac`, rebaseado sobre `a236473`, o mesmo estágio excedeu 1.502,8s (código 124; `count=0`). USR-200 foi registrado pelo PR #258 para executar a investigação planejada em USR-176, sem duplicar a perda remota já coberta por USR-162.
- No candidato USR-188 `07d1b7768035e98fa9fdfa84ffb21690cd238704`, syntax/types passou, mas a tentativa oficial subsequente ficou aguardando o lock global. O sidecar aponta PID 18676 desde 12:41; `psutil.pid_exists(18676)` retornou falso, enquanto o runner atual aguarda o arquivo lock. Nenhum teste começou e não há PASS. Não removemos o lock nem desativamos o mecanismo; a situação está vinculada ao USR-200 e ao diagnóstico de PID USR-187.
- A revisão automatizada por harness externo foi recusada anteriormente porque exportaria documentos internos a destino externo não especificado; nada foi exportado. Esta reconciliação permanece local até o PR autorizado.

## Próximas etapas e critérios de retomada

| Próxima frente | Responsável técnico | Retomar quando | Não fazer antes |
|---|---|---|---|
| SQ-02 — perfil e threat model DarkFac | Harness eleito pelo ticket canônico; Owner fornece decisões de negócio quando solicitado | USR-191 integrado; D-01..D-03 permanecem confirmadas; levantar ativos/fronteiras do repositório e marcar incógnitas. | Não autorizar outro projeto, produção ou efeito sensível sem perfil explícito. |
| SQ-03 — rota de evolução da governança | Harness eleito; Owner é autoridade para exceções em arquivos protegidos | USR-191 integrado; inventariar guard/CI/arquivos protegidos e propor fluxo mínimo com aprovador e evidência. | Não editar/afrouxar `AGENTS.md`, `FACTORY_RULES.md` ou `MISSION.md` sem a rota validada. |
| SQ-31 — vocabulário/nomenclatura | Harness eleito após SQ-02 | Perfil e limites de compartilhamento definidos; comparar termos reais do DarkFac. | Não publicar metadados de outros projetos/tenants. |
| SQ-04..SQ-08 | Harnesses/tickets próprios | Predecessores satisfeitos; USR-170, 171 e 173 preservados nos vínculos corretos. | Não instalar/ativar scanners antes de escopo, versão, licença e portão definidos. |
| SQ-09..SQ-13 | Harnesses/tickets próprios | SQ-02/SQ-04; USR-163; completar pendências de privilégios e dados. | Não provar fronteira por ataque em produção. |
| SQ-14..SQ-18, SQ-31..SQ-38 | Harnesses/tickets próprios | Evidências e controles precedentes; adotar os manuais por versão, dicionários e contratos. | Não impor limite universal nem declarar cobertura sem medir. |
| SQ-19..SQ-30 | Harnesses/tickets próprios | Laboratório isolado, autorização por alvo, budgets/exclusões/retention aprovados, cancelamento e evidência prontos. | Não agendar campanhas nem executar ataque real neste ciclo. |

## Reuso; não criar duplicatas

Reusar `core/enterprise` (HF-24/USR-59), `core.git.secret_scan`/USR-100, `core.catalog`, `core.infra.inventory`, `core/line/stage_review`, `core.research`, `core.infra.backup_service` e o gate oficial. Reusar USR-162 para perda de resposta remota, USR-163 para isolamento das dependências de teste, USR-166 para cancelamento e USR-175 para candidato imutável. Verificar integração real antes de contar qualquer componente como consumidor obrigatório.
