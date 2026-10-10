# Dark Factory — roadmap de segurança e qualidade do código

Data de referência: **08/10/2026, Europe/Budapest**. A atualização do Owner em 09/10 autoriza execução delimitada do plano em DarkFac, começando pela Onda 0. Isso não autoriza outros projetos, produção, scanners/agendadores ativos ou campanhas; decisões e limites atuais estão em [DECISIONS.md](plans/security-quality-2026-10-08/DECISIONS.md) e a fotografia reconciliada em [BASELINE.md](plans/security-quality-2026-10-08/BASELINE.md).

## 1. Resultado pretendido e limites

Fazer a DarkFac produzir e operar software com controles de segurança demonstráveis, código sustentável e evidências que uma IA ou um auditor consiga conferir. O programa cobre **a fábrica e cada produto desenvolvido por ela**, desde a adoção do projeto até a operação, manutenção e desativação.

As entregas são: desenvolvimento seguro e proteção de dados; atualização contínua das práticas; campanhas de red team em marcos de módulos e conclusão de sistemas; inventário e refatoração obrigatória; estratégia/dicionário de dados consistente no portfólio e manuais atualizados por função, módulo e projeto. A etapa de refatoração é obrigatória; uma transformação específica só é executada quando existe melhoria demonstrável. Um parecer fundamentado de que não há refatoração necessária também é uma saída válida.

Não existe prova de ausência absoluta de vulnerabilidades. O critério de conclusão será cobertura dos controles aplicáveis, testes negativos, avaliação independente, ausência de achados impeditivos sem tratamento, rastreabilidade ao candidato e risco residual documentado. Desempenho será medido contra objetivos do produto; tamanho de função ou quantidade de PRs não prova ineficiência por si só.

Não estão incluídos neste turno: escrever módulos, instalar scanners, executar ataques em sistemas reais, alterar skills operacionais, ativar agendadores, despachar tickets ou modificar regras de produção. Persistir este plano e registrar lacunas é trabalho de planejamento.

## 2. Baseline, andamento e método

A inspeção de código ancorada em `0d71e59487cbed74184d9cb47593e8d0ca7e9005` e a worktree documental de `72c40a401d36cc7c4d4365388a1edf36602f17fb` são fotografias históricas de 08/10. A fotografia atual é `2476f6ddb295e1f90cbdb4b13878f9a14d4a7e77`; ver `plans/security-quality-2026-10-08/BASELINE.md`. Nenhum desses estados certifica segurança ou produção.

Foram examinados contratos, manifestos, CI, guardrail, execução de agentes, revisão, sandbox, proteção de segredos, Enterprise, catálogo, inventário de infraestrutura, backlog e PRs abertos. Duas verificações negativas ocorreram apenas em arquivos sintéticos locais e com dependências simuladas. Não houve pentest, acesso a dados de clientes nem validação de topologia de produção.

Levantamento por AST dos arquivos Python rastreados de `core/` e `hub/`: **304 arquivos analisáveis, 3.235 funções incluindo métodos e funções internas, 942 classes incluindo classes internas**. Não inclui scripts, frontend, módulos de produtos externos ou uma análise semântica de todos os símbolos. Exemplos a investigar por responsabilidade/churn: `create_worker_app` (667 linhas, contém handlers internos), `materialize_result` (535) e `ReadinessGate.evaluate` (497). A última pertence ao workflow legado; não deve ganhar prioridade sem consumidor ativo comprovado.

### 2.1 O que reaproveitar

| Capacidade pedida | Evidência existente | Avaliação e reaproveitamento |
|---|---|---|
| Revisão de código independente | `core/line/stage_review.py`, roteamento por outra família, testes da linha | Implementada. É revisão de código; ampliar vínculo da evidência e escopo de segurança. Não equivale a campanha ofensiva. |
| Proteção da própria governança | `core/orchestrator/guard.py`, CI com verificador pertencente à base confiável | Implementada no código/CI. Preservar; não permitir que o candidato reescreva os critérios que o aprovam. |
| Proteção contra segredos versionados | `core/git/secret_scan.py`, `tests/test_no_tracked_secrets.py`, USR-100 concluído | Implementada para padrões cobertos. Complementar por ferramenta madura, artefatos e histórico; não recriar do zero. Rotação histórica consta como concluída, sem reprobe de credenciais nesta sessão. |
| Contenção de execução | `core/execution/sandbox.py`, `core/line/agent_cli.py`, compose do worker | Parcial. Helpers de caminhos/rede/processos e sandbox do harness existem, mas não demonstram isolamento de todo shell arbitrário. O modo cloud depende do contêiner; falta prova de fronteira por tarefa e separação de segredos. |
| Proteção Enterprise | `core/enterprise/{policy,residency,audit_chain,sla_guard}.py`, testes e APIs do Hub; HF-24/USR-59 | Implementada como componente. Consumo obrigatório nos caminhos reais de inferência/entrega não foi demonstrado na busca. Estender este núcleo; proteção mínima não pode depender de perfil pago. |
| Backup e recuperação | `core/infra/backup_service.py`, `backup_cron.py`, deploy hooks | Existentes. Reusar drills, criptografia e retenção de backup; validar restauração e controle de acesso, sem duplicar o daemon. Retenção de evidências de segurança é uma política distinta. |
| Inventário | `core/infra/inventory.py`, `core/catalog/models.py` | Inventário de infraestrutura e catálogo de componentes reutilizáveis existentes. Nenhum deles é um inventário completo de símbolos e relações do código. Estender por referências, sem transformar catálogo em cópia de todo arquivo. |
| Estratégia/dicionário e manuais | Contratos Pydantic, docs de arquitetura/runbooks e docstrings; 1.736 das 3.235 funções examinadas têm docstring | Reaproveitar schemas e documentação existentes. Presença de docstring não comprova manual útil; 1.499 funções não têm docstring, mas podem ter referência externa. Glossário comum, vínculo de dados por função e validação obrigatória da versão do manual não foram encontrados na busca de `core/`, `hub/`, `docs/` e `tests/`. Não é avaliação semântica completa de todos os manuais. |
| Tipos e qualidade estática | `harness.config.json` usa `compileall`; dependências atuais não incluem checker/linter de qualidade | Sintaxe implementada; análise de tipos não demonstrada. Precisa de checker explícito, lint e contratos arquiteturais. |
| Pesquisa/aprendizado | `core/research`, `core/learning`, `core/evolution`, skills 00/08/10/11 | Auxiliares existentes. Não foi encontrada rotina transversal de atualização de segurança conectada a controles executáveis. Reusar ledger e consumidores reais. |
| Red team por marco | Revisão adversarial e testes existentes | Campanha dedicada com autorização de alvo, laboratório isolado e reteste não encontrada no escopo inspecionado. Criar orquestração própria, reutilizando scanners existentes. |
| Refatoração obrigatória | PIV, testes, revisão e histórico de correções | Base de validação existente. Etapa explícita de consolidação, inventário de símbolos e prova de equivalência não encontrada. |

### 2.2 Frentes existentes: respeitar a propriedade e conferir antes de executar

| Frente | Estado observado | Relação com este plano |
|---|---|---|
| USR-134 | `implementing` local; outra conversa acompanha o ticket | Reusar preservação de contexto/rollback. Não modificar sua implementação neste planejamento. |
| USR-136, [PR #167](https://github.com/GCamposGit/DarkFactory/pull/167) | Aberto e draft; revisão anterior registra risco de migração/perda de atualizações | Dependência para filas/evidências duráveis entre Hub e workers. Não considerar entregue pelo status textual do ledger. |
| USR-163 | `planned` | Isolamento de testes de Telegram/backup de credenciais externas. Pré-requisito para laboratório/testes seguros; não abrir substituto. |
| USR-165, [PR #217](https://github.com/GCamposGit/DarkFactory/pull/217) | Integrado durante a sessão, em `542103c` | Rebase após espera por base vermelha; conferir CI da nova base antes de medir regressões/refatorações. A baseline deste plano continua fixada. |
| USR-166, [PR #218](https://github.com/GCamposGit/DarkFactory/pull/218) | `planned` na baseline; PR aberto na consulta posterior | Cancelamento do launcher. Usar no interruptor de campanha; não assumir merge pelo estado da worktree. |
| USR-167, [PR #216](https://github.com/GCamposGit/DarkFactory/pull/216) | Integrado durante a sessão, em `3d54e2c` | Testes contra Postgres real; conferir a implementação integrada antes de produzir evidência de armazenamento/replay de campanhas. |
| USR-169, [PR #215](https://github.com/GCamposGit/DarkFactory/pull/215) | Registro em PR aberto, fora do ledger da baseline documental | Limpeza de contexto de recuperação; coordenar retenção sem apagar evidências necessárias. |
| USR-157, USR-162 | Planejados; falhas de spike/worker | Estabilidade da infraestrutura de evidência. Não contar timeout, falta de resposta ou diagnóstico inconclusivo como PASS. |
| USR-164 | Planejado | Progresso de execuções locais no Hub; reusar o canal de telemetria e suas permissões mínimas. |
| USR-168 | Planejado, registro integrado em #214 | Roteamento de subagentes; não muda independência de revisão ou controles de segurança. |
| HF-03-08, [PR #20](https://github.com/GCamposGit/DarkFactory/pull/20) | Draft aberto | Ativação fail-closed. Conferir consumidor antes de criar portão paralelo. |

PRs antigos/worktrees presentes não provam trabalho ativo. Antes de cada onda: reler `origin/main`, ledger, PRs, claims e estado real dos runs; comparar alterações e proprietários de arquivos; atualizar esta tabela. O estado é uma fotografia, não uma previsão.

## 3. Grill e autorização

| Decisão | Estado | Fonte e consequência |
|---|---|---|
| Escopo desta etapa | Confirmado | Owner em 09/10: somente DarkFac; executar Onda 0 por tickets canônicos e conservar bloqueios de produção/efeitos sensíveis. |
| Alvos de red team | Confirmado | Resposta explícita do Owner: ambientes isolados; produção exige autorização por alvo. |
| Frequência do radar | Confirmado | Resposta explícita do Owner: monitoramento diário de alertas, pesquisa mensal e revisão extraordinária em incidentes críticos. A ativação pertence à implementação futura. |
| Dados e obrigações por projeto | Confirmado | Resposta explícita do Owner: dados sintéticos, minimização de dados reais e evidências restritas sem segredos; nenhum requisito adicional conhecido. Mapear obrigações legais/contratuais no Grill de cada projeto, sem inferir jurisdição pelo fuso do operador. |
| Dicionários e manuais | Confirmado | Pedido adicional do Owner: estratégia/dicionário por módulo; dicionário e manual por função; consistência no portfólio; atualização em cada rodada e PR; versão verificada a cada consulta. "Push request" é tratado como pull request, incluindo seus pushes sucessivos. |
| Retenção, orçamento e limites de campanha | Pendentes por projeto/campanha | Registrar duração, requisições, concorrência, custo, tamanho de evidência, destino, TTL e exclusões no contrato antes de qualquer execução. Campos ausentes bloqueiam a campanha. |
| Critérios numéricos de qualidade e performance | A calibrar na baseline | Coletar medidas primeiro, propor limites por produto e registrar decisão material no Grill. Não impor percentuais universais de cobertura ou ganho de velocidade. |

O Grill de planejamento está **concluído** para as decisões registradas em `plans/security-quality-2026-10-08/DECISIONS.md`. O plano entrou em **`SCOPED_WAVE_0_ACTIVE`** para DarkFac. Retenção, budgets de campanha, SLOs e obrigações por projeto continuam pendentes e devem ser resolvidos nos tickets correspondentes antes de qualquer execução que dependa deles, conforme Gate G1 de `AGENTS.md`. A autorização para planejar não autoriza executar o roadmap. Não há `WorkflowHandoff` aprovado ou `ReadinessGate` aprovado nesta sessão: a esteira HF-27 usa SPEC/tickets diretamente; não reintroduzir o workflow legado por burocracia.

## 4. Arquitetura proposta

### 4.1 Segurança como política executável

Criar um pacote headless de controles e evidências, com adapters de scanners separados do domínio. O nome proposto `core/security/` fica sujeito à reconciliação da onda 0. Ampliar `core/enterprise` para referências de residência, SLA e trilha; não copiar suas regras. A mesma política é consumida por `run_ticket`, estágios da linha, adoção, catálogo/skills e operação. Prompts orientam o agente; o executor externo ao agente decide permissões e verifica o efeito.

Separar quatro identidades: planejador/leitor, agente de build, verificador/revisor e executor de efeitos Git/deploy. O build pode produzir um candidato em sua worktree; tokens de produção, backup e aprovação ficam no executor autorizado. Autonomia interna de commit/PR/merge/deploy continua, depois dos portões; não vira privilégio irrestrito do agente. Modelos, subagentes, skills, MCPs e arquivos externos são componentes de uma cadeia de confiança.

### 4.2 Contratos previstos, ainda não implementados

| Contrato | Campos mínimos e invariantes |
|---|---|
| `ProjectSecurityProfile` | Projeto, stack, serviços, dados/classificação, atores, exposição, residência, controles aplicáveis, versão de política, dono funcional, SLOs; justificativa de cada N/A. |
| `ThreatModel` | Ativos, fronteiras de confiança, fluxos de dados, adversários, casos de abuso, controle responsável, testes associados e risco residual. |
| `ExecutionCapability` | Identidade, run/ticket/projeto, operações e alvos permitidos, caminhos resolvidos, rede/egress, prazo, orçamento e versão de política. Validada pelo executor; não emitida pelo próprio agente. |
| `SkillTrustManifest` | Origem e commit/digest, licença, arquivos/scripts transitivos, dependências, capabilities, rede, identidade de revisão, testes e compatibilidade; sem instalação/upgrade por referência flutuante. |
| `SecurityEvidence` | Candidato/tree e artefato digests, projeto/run/módulo, política e regras, versões/digests das ferramentas, ambiente, configuração, horário UTC, controles esperados/executados/omitidos, achados e identidade do produtor. |
| `Finding` | ID estável, escopo, severidade/confiança separadas, impacto, origem, evidência restrita, ticket de correção, estado e prova de reteste. Modelo não pode declarar corrigido sozinho. |
| `RedTeamCampaign` | Alvos/candidato autorizados, ambiente, prova de posse, exclusões, janela, budgets, ferramentas e corpus fixados, rede restrita, credenciais sintéticas, interruptor, rollback e política de evidência. |
| `CodeInventory` | Manifesto de arquivos e símbolos, relações verificadas e desconhecidas, component IDs, APIs, rotas/jobs/CLI, testes, decisões e provenance; ligado à árvore de origem. |
| `RefactoringAssessment` | Alvo, motivo/evidência, responsabilidades, comportamento preservado, testes de caracterização, comparação antes/depois, compatibilidade e resultado independente. |
| `DataContract` / `SemanticTerm` | ID estável, namespace, versão, significado, nome canônico, aliases, schemas/restrições, unidade, fonte, consumidores, classificação e ciclo de vida. Referenciar contratos tipados existentes; perfil ODCS para datasets quando aplicável. |
| `FunctionManual` / `ManualManifest` | Símbolo estável, finalidade, assinatura, dicionário, efeitos, exemplos/testes e referências; digests do código/documentos/índice e versão consultada, ACL e estado de validação. Publicação do manifesto após commit evita referência circular ao próprio hash. |

Usar Pydantic v2 para contratos; Python 3.12+, `pathlib`, logging e I/O UTF-8. Identificadores de segredos são referências, nunca valores ou URLs com credenciais. JSON externo, resultados de scanners e conteúdo de skills são dados não confiáveis. Esquema inválido, resultado ausente, scanner não executado ou corpus vazio produzem `incomplete/blocked`, não PASS.

### 4.3 Evidência resistente à manipulação

Hash de arquivo identifica conteúdo; não prova autoria, imutabilidade ou autorização. Revisor e verificador devem estar fora da área gravável pelo build. Evidências precisam de armazenamento com ACL separada, gravação transacional, vínculo ao candidato e checkpoints autenticados fora do agente. Apenas encadear SHA-256 no mesmo diretório gravável não fornece não repúdio. Definir retenção e exclusão por classe, sem replicar segredos em backups, issues ou logs públicos.

Resultados privados de segurança ficam fora do repositório público; o backlog pode conter um resumo sanitizado e a referência restrita. Telemetria/Telegram informa ID, severidade e próxima ação, sem payload ofensivo, segredo ou dados de cliente. Esse projeto não solicita envio de mensagens agora.

## 5. Controles ao longo do desenvolvimento

| Momento | Segurança e dados | Qualidade e evidência |
|---|---|---|
| Intake/adoção | Classificação de dados, exposição, autorização, requisitos por setor/cliente; inventário do software existente | Reconciliação de backlog, árvore base e contratos públicos; não duplicar componentes |
| Planejamento do módulo | Threat model, controle aplicável, superfície nova, abuso de agentes/skills e migração de dados | Limites de responsabilidade, API/estado, requisitos de performance, testes de caracterização e plano de refatoração |
| Antes de executar agente/skill | Origem/digest, capabilities mínimas, isolamento e segredos inacessíveis; validação de ferramentas | Baseline identificada; mudanças limitadas aos caminhos do ticket |
| Desenvolvimento/PR | Scan de segredos, SAST, dependências, imagem/IaC quando aplicável; testes negativos de autorização, entradas e isolamento | Lint/formatter, tipos explícitos, contratos de imports, duplicação/churn e avaliação obrigatória de refatoração |
| Módulo estável | DAST autenticado no laboratório, matriz de controles do módulo e campanha de red team | Consolidação arquitetural, inventário reconciliado, equivalência e benchmark conforme SLO |
| Sistema antes de conclusão | Campanha integrada; fronteiras entre módulos, isolamento de clientes e fluxos de dados; reteste dos achados | Checklist completo de sistema, documentação, inventário, testes E2E e risco residual |
| Release e operação | Evidência válida do candidato/artefato, config segura, migração reversível, rotação, monitoramento e vulnerabilidades novas | CI, merge autônomo, deploy e smoke reais; baseline de performance e observação de regressões |

Checks rápidos rodam por mudança. A **campanha ofensiva completa não roda em cada PR**. É exigida quando um módulo declarado estável termina, antes de um sistema ser considerado concluído e quando uma mudança relevante invalida evidência anterior de segurança. A execução por marco fica fora da suíte unitária; o portão oficial confere que a evidência exigida existe e corresponde ao candidato.

Se a árvore muda depois da campanha, seu PASS não vale automaticamente para o candidato novo. Um verificador confiável avalia o diff e a cobertura dos controles: exige reteste dos cenários impactados ou campanha integral se houve nova fronteira de confiança. Mudança puramente documental pode preservar o digest do artefato; a justificativa fica registrada. Nenhum LLM autoriza sozinho essa dispensa.

Para cada módulo, verificar: autenticação/autorização e isolamento de cliente; validação e limites de entrada; comandos, SQL e desserialização; SSRF/egress; armazenamento/criptografia/gestão de chaves; uploads e arquivos; logging e dados pessoais; dependências/build; erros, limites e custos; testes negativos. Controles N/A precisam de razão verificável, não apenas uma lista marcada.

Novos segredos reais, malware confirmado, bypass de autorização e vulnerabilidades confirmadas de impacto alto/crítico devem impedir a entrega até correção/mitigação validada. Severidade depende de exploração, exposição e impacto, não só da nota do scanner. Outros riscos recebem responsável, prazo e monitoramento. Exceções têm escopo, versão e vencimento; não podem relaxar silenciosamente proteção de dados ou reclassificar falha como PASS. A política final de exceções deve ser resolvida na onda 0.

## 6. Segurança de agentes, skills e cadeia de suprimento

1. Tratar README, páginas web, issues, MCP results, memória e skills recebidas como entrada não confiável. A confiança no texto não concede capabilities.
2. Verificar pacote completo da skill: scripts, includes, binários, hooks, dependências e alvos de rede. Usar origem conhecida, digest fixado, licença, revisão independente e testes de abuso. Atualizações retornam à quarentena; sync de espelhos preserva o digest aprovado.
3. Impedir que skills/plugins instalem ferramentas, ampliem acessos ou modifiquem portões como efeito incidental. Homologação deve testar mudança inesperada de ferramentas, caminhos e permissões.
4. Isolar por run/projeto em processo e ambiente apropriados. Worktree organiza Git; não é barreira de segurança. Testar caminhos, symlinks/junctions, arquivos montados, variáveis de ambiente, memória, rede e processos descendentes. No Windows, validar uma fronteira efetiva adequada ao executor, sem presumir que políticas Linux existem.
5. Egress por allowlist fora do agente: registrar destinos autorizados, negar rede inesperada, metadados cloud e desvio por redirect/DNS. Transferir dados apenas pelo broker, respeitando perfil do projeto e residência.
6. Tokens curtos e restritos por papel/projeto; autenticidade de chamadas; proteção de replay e revogação. Integração/deploy/backup em identidades separadas. Instalação de pacotes/build hooks e execução de testes também são execução de código não confiável.
7. Produzir SBOM e provenance do artefato, fixar dependências e Actions/imagens por digest, verificar assinaturas/proveniência quando disponíveis, scanner de vulnerabilidade e licença. Advisory conhecido não é detector suficiente de pacote malicioso sem CVE.
8. Reusar revisão de outra família, mas não tratá-la como limite de confiança: modelos diferentes podem compartilhar a mesma vulnerabilidade a injection. Checks determinísticos e contenção independente permanecem obrigatórios.

Base técnica: [OWASP AI Agent Security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html), [Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/), [OWASP Agentic Skills Top 10](https://owasp.org/projects/agentic-skills-top-10), [MCP Top 10](https://owasp.org/projects/mcp-top-10) e [SLSA 1.2](https://slsa.dev/spec/v1.2/). O roadmap combina essas referências com decisões próprias; não é declaração de certificação.

## 7. Campanhas de red team

### 7.1 Execução futura, passo a passo

1. Receber evento de conclusão de módulo/sistema, candidato imutável e perfil de segurança. Resolver ownership, escopo e budgets; sem autorização completa, bloquear.
2. Criar réplica descartável a partir do artefato e configuração saneada. Usar tenants, usuários, segredos e dados sintéticos. Provar que nenhuma rota alcança produção ou terceiros.
3. Validar isolamento, permissões mínimas, observabilidade, interrupção e restauração antes dos ataques. Se essas provas falharem, abortar e abrir correção da infraestrutura.
4. Descobrir a superfície **dentro da allowlist**: rotas, APIs, contas/roles, jobs, skills e interfaces do agente. Comparar com o inventário esperado.
5. Executar cenários WSTG/ASVS aplicáveis, DAST com autenticação, limites e casos de lógica de negócio. Testar acessos entre tenants, autorização de objetos, injeções, entrada/saída de arquivos e configuração.
6. Em features de IA, executar corpus de injection direta/indireta, envenenamento de memória/skills, abuso de tool calls, escalada de capabilities, exfiltração sintética para um sink local e consumo indevido de recursos. PyRIT/garak complementam o teste do aplicativo; não substituem testes de autorização do produto.
7. Confirmar achados com evidência de impacto em dados sintéticos e repetição controlada. Separar erro de ferramenta, suspeita, vulnerabilidade confirmada e negativo; taxa de ataque bem-sucedido usa denominador e corpus explícitos.
8. Criar ticket de correção, impedir a conclusão do marco quando necessário, corrigir no ciclo canônico futuro e repetir o cenário original mais regressões correlatas. Exigir verificador independente do autor da correção.
9. Emitir relatório privado ligado ao artefato e controles testados. Exportar ao Hub apenas resumo sanitizado. Destruir a réplica e conferir limpeza/retenção.

### 7.2 Regras de engajamento

Sem alvos de terceiros, dados reais de cliente, persistência fora da réplica, divulgação pública de reproduções, ações destrutivas irreversíveis ou tráfego para sinks externos. Testes de destruição, indisponibilidade e persistência, quando necessários, usam cópia descartável e autorização explícita no contrato da campanha. Todos os redirects e novos alvos são revalidados; a autorização de um hostname não autoriza sua rede inteira.

Produção não está autorizada por este plano. Uma futura campanha em produção exige autorização específica do Owner para alvo, técnicas, janela, exclusões e limites, com controle de acesso independente. O red team não pode editar seu escopo ou aprovar seu próprio relatório.

### 7.3 Oráculos

PASS exige cenários aplicáveis executados, cobertura de roles/tenants, ambiente equivalente nos aspectos de segurança, negativos de autorização, evidência íntegra e achados impeditivos corrigidos e retestados. Scanner sem achados ou agente que afirma não ter conseguido invadir não demonstra essa cobertura. A campanha também precisa encontrar vulnerabilidades intencionalmente plantadas **somente em fixtures/laboratório**; remover o controle esperado deve produzir reprovação.

Base: [OWASP WSTG](https://wstg.owasp.org/stable/2-Introduction/), [ASVS](https://owasp.github.io/www-project-application-security-verification-standard/), [MITRE ATLAS](https://atlas.mitre.org/) e ferramentas detalhadas no dossiê de pesquisa. ATT&CK/ATLAS são taxonomias para mapear cenários, não licença para executar técnicas indiscriminadamente.

## 8. Inventário e refatoração

### 8.1 Inventariar tudo sem executar imports não confiáveis

Gerar um manifesto completo de arquivos elegíveis e extrair funções, métodos, classes, módulos, APIs, CLI, rotas HTTP, jobs, eventos, contratos de estado e componentes. Incluir Python, frontend, scripts de operação e o stack de cada produto por adapters; evitar um parser próprio quando AST/Griffe e ferramentas da linguagem resolvem o problema.

Cada símbolo terá identificador estável, nome qualificado, assinatura, caminho/linha, hash, tipo de API, responsabilidade e componente. Relacionar imports/chamadas estáticas, I/O, capabilities, dados tocados, testes e decisões. Relações dinâmicas desconhecidas ficam explicitamente desconhecidas; inferência de LLM é anotação pendente de verificação. Não executar imports de código candidato para construir documentação. Código gerado/vendor deve constar no manifesto com origem e exclusão justificada da documentação detalhada.

O índice técnico pertence ao artefato/candidato e é gerado automaticamente; documentação humana explica responsabilidades, invariantes, efeitos e exemplos. Evitar manter manualmente milhares de listas que se tornam obsoletas. Reusar IDs do catálogo e do inventário de infraestrutura e relacionar componentes aos serviços, sem duplicar suas fontes de verdade.

### 8.2 Rotina obrigatória

1. Antes da feature: conferir responsabilidades, API e testes; fazer refatoração preparatória se ela reduzir risco ou tornar a alteração simples.
2. Depois de fazer funcionar: executar uma avaliação estruturada de duplicação, acoplamento, complexidade, handlers monolíticos, patches repetidos, código morto, compatibilidade e custo de manutenção. Usar churn e incidentes para ordenar hotspots.
3. Ao consolidar módulo: remover caminhos provisórios, unificar a regra de negócio em uma fonte, corrigir limites de responsabilidade, documentar interfaces e compatibilidade, atualizar inventário e testes. Fatiar alterações em passos pequenos com comportamento preservado.
4. Ao concluir sistema: revisão independente das fronteiras entre módulos e da dívida residual. Nenhum atalho de segurança permanece como dívida aceita apenas por conveniência.
5. Medir antes/depois. Refatoração preserva comportamento; otimização exige perfil/benchmark e mantém resultados, consumo de memória, throughput e latência dentro do SLO acordado. Mudança de comportamento/migração é ticket próprio.

Cada avaliação emite `required_and_completed`, `no_change_needed` fundamentado ou `blocked`. Não basta escrever "refatorado" no PR. O agente implementador não pode apagar testes de caracterização, relaxar thresholds ou mudar fixtures para esconder regressões.

Princípios de prática: [Martin Fowler — refatoração](https://www.martinfowler.com/books/refactoring.html) e [refatoração oportunista](https://martinfowler.com/bliki/OpportunisticRefactoring.html). Escolha de hotspots e limites é decisão da fábrica, não reprodução de percentuais comerciais.

### 8.3 Estratégia e dicionário de dados obrigatórios

Cada módulo define e valida sua estratégia de dados durante o desenvolvimento: finalidade, fontes, consumidores, contratos, transformações, armazenamento, classificação, acesso, qualidade, retenção, residência e recuperação. Cada função terá dicionário de parâmetros, retornos, campos e efeitos, ligado aos termos do domínio e às outras funções pelos contratos produtor/consumidor. Até uma função sem persistência possui entradas/saídas e uma declaração explícita dos efeitos aplicáveis.

O vocabulário do portfólio usa IDs estáveis, namespaces por domínio/projeto, nomes canônicos e aliases versionados. Mesmo significado compartilha termo; homônimos com significados distintos ficam identificados separadamente. Python mantém `snake_case`; representações de outras linguagens possuem mapeamento explícito. Unidade, moeda, fuso, nullability, limites e classificação fazem parte do significado. Compartilhar vocabulário/metadados autorizados não concede acesso a dados de outros clientes.

Antes/depois de refatorar: reconciliar dicionário, schemas, nomes, relações e consumidores; preservar IDs e referências; testar compatibilidade e migração quando necessária. Mudança de unidade/significado pode quebrar contrato mesmo que o tipo permaneça igual. Reusar Pydantic e schemas existentes; avaliar [ODCS](https://bitol-io.github.io/open-data-contract-standard/latest/schema/) para datasets, sem manter definições técnicas concorrentes.

### 8.4 Manual de cada função, módulo e projeto

Cada função terá sua própria entrada navegável de manual, inclusive funções internas/privadas: finalidade, quando usar, assinatura, dados, regras, erros, efeitos, segurança, exemplos sintéticos verificados e referências. Módulos acrescentam fluxos e integração; projetos organizam referência, tutoriais, tarefas e explicações, conforme [Diátaxis](https://www.diataxis.fr/start-here/). O manual completo deve existir durante o desenvolvimento e no aceite final.

A cada rodada de desenvolvimento e em cada PR/push, atualizar os dicionários e manuais afetados direta ou indiretamente. O portão oficial verificará a correspondência entre código, comportamento, documentos e exemplos; assinatura intacta não dispensa avaliar mudança de comportamento. Timestamp, texto vazio ou geração automática sem validação não comprova atualização.

Toda consulta humana ou de IA resolve primeiro projeto e versão: código em desenvolvimento, artefato implantado ou versão histórica. O manifesto do manual e o índice de busca devem corresponder a esse alvo. Ausência/divergência bloqueia resposta apresentada como atual e abre recuperação rastreável. Publicação de código/manual e atualização do alias de versão são coordenadas; instruções antigas não substituem o manual requerido. Detalhamento, formatos e cenários: [estratégia de dados e manuais](plans/security-quality-2026-10-08/DATA_STRATEGY_DICTIONARIES_MANUALS.md).

## 9. Rotina de atualização de segurança

Cadência confirmada pelo Owner: acompanhar advisories/KEV diariamente, pesquisar práticas e incidentes mensalmente e abrir revisão extraordinária em incidentes críticos que afetem a fábrica ou um produto. Reusar mecanismo de jobs duráveis existente; não acrescentar um cron paralelo por skill. Nenhum agendamento é criado nesta etapa de planejamento.

O ciclo será: buscar fontes oficiais → verificar data/versão/status → mapear aos ativos/SBOM e controles existentes → deduplicar → registrar insight com URL e hash → propor atualização → gerar casos positivos e negativos → revisão independente → portão oficial/CI → merge/deploy autônomos → monitorar/rollback. Conteúdo externo não altera skills ou permissões diretamente.

Atualizar primeiro a regra/política executável e o corpus, depois prompts e skill canônica e seu espelho. Manter uma skill de segurança vinculada aos módulos reais, sem duplicar limites em texto. Fontes inacessíveis, advisory não confirmado ou versão draft produzem estado de pesquisa incompleta, sem aprovação inventada. Alertar apenas novidade acionável, falha ou prazo vencido; não enviar resumos repetidos quando nada muda.

Usar SSDF final como baseline e monitorar revisão draft separadamente. [NIST SSDF 1.2](https://csrc.nist.gov/pubs/sp/800/218/r1/ipd) aparece como Initial Public Draft na consulta; [SP 800-218A](https://www.nist.gov/publications/secure-software-development-practices-generative-ai-and-dual-use-foundation-models-ssdf) é perfil para desenvolvimento de IA, com aplicabilidade parcial a uma fábrica que utiliza modelos. Não alegar conformidade automática de todo produto. [CISA KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) é uma entrada de priorização, não substitui análise dos ativos.

## 10. Ondas e critérios de passagem

Os IDs `SQ-*` são **itens de planejamento**, não tickets despachados. O detalhamento executável e dependências estão em `docs/plans/security-quality-2026-10-08/roadmap.json`. Cada item tem até quatro arquivos principais propostos; testes/docs auxiliares são indicados à parte. Nomes novos são desenho, não arquivos já existentes.

| Onda | Itens | Resultado e passagem |
|---|---|---|
| 0 — Contrato e reconciliação | SQ-01 a SQ-03; SQ-31 | Grill resolvido; backlog/PRs rechecados; perfil, threat model e vocabulário/IDs piloto definidos; percurso legítimo para evolução de governança documentado. Sem limiares inventados. |
| 1 — Evidências e verificações básicas | SQ-04 a SQ-08 | Evidência ligada ao candidato; segredos/SAST/SCA/IaC executáveis; tipos/lint reais; erros e scanner ausente bloqueiam. Tratar USR-170/171/173. |
| 2 — Contenção e confiança | SQ-09 a SQ-13 | Isolamento por run provado; credenciais separadas; skills/MCP homologados; proteção de dados aplicada aos caminhos reais. Tratar USR-172/174, reusar USR-163. |
| 3 — Inventário, dados, manuais e qualidade | SQ-14 a SQ-18; SQ-32 a SQ-36 | Inventário completo; estratégia/dicionário por módulo e função; manuais por função/módulo/projeto; atualização obrigatória em cada PR; refatoração com equivalência e melhoria mensurada. |
| 4 — Red team controlado | SQ-19 a SQ-23 | Contratos e laboratório; DAST autenticado e cenários de agentes; achado plantado detectado; remediação/reteste e prova de interrupção. |
| 5 — Atualização contínua | SQ-24 a SQ-26 | Radar conforme decisão; proposals deduplicadas; atualização de política/skill/corpus com validação e rollback; sem atualização direta a partir da web. |
| 6 — Adoção universal e observabilidade | SQ-27 a SQ-29; SQ-37 e SQ-38 | Perfis aplicados a projetos novos/existentes; consulta de manual por versão/ACL; migração de dicionários e referências; Hub evidencia bloqueios; piloto DarkFac + produto isolado completo. |
| 7 — Aceite integrado | SQ-30 | Campanha antes da conclusão do sistema, inventário/dicionários/manuais reconciliados, consolidação final, CI e efeitos reais da implementação futura demonstrados. |

Sequência crítica: 0 → 1 → 2 → 4 → 6 → 7. Inventário/refatoração (3) começa após contratos/evidências e converge antes do piloto. Radar (5) pode avançar após política e evidências. Não abrir frentes simultâneas com propriedade sobre `agent_cli`, estágios da linha, ledger ou compose. A onda não autoriza execução automática nesta sessão.

Não estimar semanas pela quantidade de tickets. Na onda 0, medir velocidade e cota dos harnesses, duração real dos portões, capacidade do laboratório, custos de campanhas e disponibilidade dos projetos piloto. Estimar esforço por lote a partir dessas medidas. Se o ambiente não provar contenção, adiar ataques ativos e priorizar a barreira de execução.

## 11. Validação e critérios de conclusão

Toda implementação futura segue Skill 19: preflight `core.line.routing.pick('development')`, piso de cota, worktree isolada, testes focados, revisão independente, portão único `python core/harness/runner.py --quick`, CI, commit/PR/merge autônomos, deploy e smoke. Nunca usar uma segunda suíte integral como outro portão. Checks de scanners são incorporados ao mecanismo oficial por evolução autorizada de governança; não modificar `core/harness/*` ou `.github/workflows/*` incidentalmente.

Para produtos externos, descobrir o gate do projeto na adoção e manter evidência de segurança compatível; o comando da fábrica valida seu núcleo, não substitui o teste do produto.

Uma etapa só encerra quando: controles esperados foram executados; falhas de ferramenta estão resolvidas ou bloqueadas; artefato e política são os examinados; cenários negativos realmente falham; autorização é respeitada; achados têm correção e reteste; dados/segredos não foram exportados; inventário acompanha o código; refatoração tem equivalência; performance cumpre SLO; rollback/interrupção funcionam; o auditor consegue reconstruir a decisão. Cobertura de linhas isolada e ausência de CVEs não provam segurança.

Indicadores propostos: controles aplicáveis com evidência atual; módulos com modelo de ameaças; símbolos inventariados/arquivos cobertos e relações desconhecidas; funções/módulos com dicionário e manual validado; divergências semânticas; PRs com impacto documental resolvido; consultas com código/manual/índice correspondentes; skills homologadas; achados por severidade e idade; remediação/reteste; bypasses e violações de isolamento; custo/duração de checks; reabertura de bugs em hotspots; latência/throughput/memória versus SLO. Cobertura de dicionários e manuais deve reconciliar todo o manifesto do escopo no aceite final; demais limiares materiais dependem de baseline/Grill.

## 12. Lacunas registradas e tratamento nesta sessão

| Ticket formal | Tratamento planejado | Ligação |
|---|---|---|
| USR-170 | Fortalecer validade/rastreabilidade das evidências de auditoria | SQ-04; evidências sintéticas restritas locais |
| USR-171 | Vincular retomada de revisão independente ao candidato atual | SQ-05; evidências sintéticas restritas locais |
| USR-172 | Separar privilégios/credenciais dos agentes de desenvolvimento | SQ-09/SQ-10; revisão documental de topologia, sem exploração |
| USR-173 | Tornar análise de tipos explícita no portão | SQ-08/SQ-03; constatado pelo comando configurado |
| USR-174 | Provar consumo real da política de dados/residência | SQ-13; lacuna de integração a investigar, não exploração confirmada |
| USR-176 | Investigar timeout local da suíte oficial após fallback | Baseline/portão; nova ocorrência, sem causa raiz inferida; reusar USR-162 para a perda remota |

Todos foram registrados como `planned`, horizonte `unscheduled`, sem `line-ok`, com tags de planejamento e sem autorização de execução. Não são entregas concluídas. O detalhe de reprodução fica fora do repositório público. USR-163 e outras falhas já cadastradas foram reutilizadas, sem duplicação.

Conferências locais do plano: JSON, dependências, referências de fontes, limite de arquivos principais, ausência de ativação e de mudanças em código/arquivos protegidos; scanner existente de segredos. O portão oficial iniciado no candidato `85dd870` perdeu o worker remoto e caiu para execução local, que expirou em 1.505,8 segundos no passo paralelo. A atualização documental durante a suíte também invalidou a limpeza do candidato; resultado **`HARNESS_FAIL`**, sem evidência válida de PASS. A versão final documental recebe conferência estrutural própria; não é apresentada como feature implementada ou baseline homologada. A revisão por outro harness não ocorreu: aprovação automática rejeitou exportar documentos internos para destino externo não especificado; foi usada revisão local.

## 13. Próxima entrada da fábrica

A execução delimitada foi autorizada para DarkFac. SQ-01/USR-191 reconcilia decisões e baseline; o próximo lote é SQ-02 e SQ-03, em tickets canônicos pequenos, preservando os IDs reservados e dependências documentadas. Campanhas, produção, scanners/agendadores ativos e alterações em arquivos protegidos sem percurso validado continuam bloqueados. Avançar onda a onda, mantendo pendências de budget, retenção, SLOs e obrigações explícitas.

Fontes e avaliação de ferramentas: [dossiê de pesquisa](plans/security-quality-2026-10-08/RESEARCH.md). Registro estruturado: [roadmap](plans/security-quality-2026-10-08/roadmap.json). Não há dependência manual de dashboard criada neste turno. Se uma implementação futura precisar de conta, token ou ação humana inevitável, o ticket deverá incluir guia atualizado tela por tela, todos os campos/selectors e probe final, conforme AGENTS.md.
