# Requisitos obrigatórios do workflow híbrido autônomo

Versão 1.1 — 08/09/2026. Origem: revisão explícita do owner. Complemento normativo do [plano híbrido](HYBRID_WORKFLOW_PLAN_2026-09-08.md) e da [política de handoff](HANDOFF_POLICY.md). Estes requisitos integram a onda 1; não criam IDs concorrentes nem declaram automações já instaladas. Aplicam-se também aos módulos da onda 2.

## 1. Recepção → Grill → Especificação

Grill é uma etapa obrigatória de esclarecimento orientada a decisões, conduzida por alta inteligência. O objetivo é eliminar ambiguidades materiais com o menor esforço humano possível, antes de congelar a especificação.

**Inputs:** demanda original, contexto e preferências persistentes, requisitos já respondidos, restrições, exemplos de resultado e integrações existentes. **Ações:** restituir a intenção em poucas linhas; identificar lacunas e alternativas que mudam resultado, custo ou compromisso; consultar contexto antes de perguntar; testar alternativas com exemplos concretos, cenários ou preview quando útil. Questões técnicas resolvíveis pelo planejador são resolvidas por ele.

Apresentar de uma a três perguntas por rodada, em ordem de impacto, com duas ou três opções distintas, recomendação e consequência curta; aceitar texto livre. Perguntar apenas sobre decisões que dependem da intenção/conhecimento do owner. Meta para demanda comum: uma rodada e até cinco minutos; não transformar essa meta em encerramento forçado. Não repetir respostas conhecidas. Se não há lacunas, registrar Grill satisfeito pelo contexto, sem pergunta artificial. Uma nova descoberta material reabre somente a decisão afetada.

**Output `GrillRecord`:** demanda/versão, intenção sintetizada, fatos conhecidos e origem, decisões com alternativas e resposta, pressupostos reversíveis explícitos, perguntas pendentes, critérios exemplificados, `ready_for_spec` e justificativa. A saída exige nenhuma ambiguidade material pendente. Silêncio não é consentimento. Somente a parte dependente da resposta espera; pesquisa ou outros tickets independentes continuam.

## 2. Dependências operacionais e resolução autônoma

Declarar requisitos já no planejamento/provisionamento e reconciliar obrigatoriamente após implementação, antes do aceite e novamente no destino da release. Cada etapa que introduz I/O externo inclui um `EnvironmentManifest` versionado: ferramentas/versões, sistema/arquitetura, serviços/contas, referências de segredos (nunca valores), permissões/scopes, variáveis obrigatórias, endpoints/DNS/TLS, portas/direção e origem da conexão, firewall/proxy/Tailscale, volumes, quotas, worker/identidade efetiva, instalação, probes, rollback e limpeza. Campos não aplicáveis têm justificativa.

O gate produz `EnvironmentEvidence` por dependência: requisito, ambiente, identidade sanitizada, origem da execução, build/digest, versão de configuração sem segredos, teste realizado, resultado esperado/observado, horário e referência da evidência. Mudança relevante de credencial, rota, build ou configuração invalida a evidência afetada. Um teste no notebook não comprova acesso pelo worker cloud.

Sequência obrigatória: detectar → configurar automaticamente dentro da autorização → testar → procurar alternativa equivalente → testar equivalência → abrir dependência humana apenas se indispensável. A alternativa deve preservar critérios funcionais, qualidade, segurança, integração, custo autorizado e requisitos operacionais; não vale mock no lugar da funcionalidade, credencial mais privilegiada, firewall desativado ou trocar serviço obrigatório por outro sem aprovação da intenção. Se exigir decisão arquitetural, alta inteligência revisa o handoff; o implementador econômico não improvisa a troca.

**`ManualDependency`:** ID, tickets afetados, motivo exclusivo do humano, alternativas tentadas e por que não servem, local exato/URL de configuração, pré-requisitos, passos numerados curtos, resultado esperado em cada passo, campo seguro para segredo, comando/probe final, critério de retomada e rota de ajuda. Preparar tudo que o agente puder antes de notificar. O usuário não precisa editar o workflow. Após a resposta, revalidar automaticamente; “já fiz” não equivale a probe aprovado. Bloquear só a dependência e seus descendentes, agrupar pedidos compatíveis e deduplicar notificações.

## 3. Integração real e definição de entrega

Toda nova dependência fora do ambiente de desenvolvimento exige teste em produção autorizado ou ambiente de integração realisticamente equivalente, com todos os componentes reais necessários (serviços, banco, rede, identidade do worker, callbacks, processos remotos). O plano declara diferenças entre simulação e alvo; mocks unitários continuam úteis, mas não aprovam prontidão operacional.

Simulação realista valida comportamento; conta, segredo e firewall específicos de produção exigem probe no alvo, executado pela identidade/rota que o produto usará. Usar canário/namespace de teste e dados sintéticos com limpeza; ações com efeitos não autorizados não são disparadas como “teste”. Antes de promover cliente pagante, preservar seu aceite obrigatório. Após promoção, verificar jornada crítica, integração e persistência, além de saúde HTTP.

Estados distintos: `code_integrated`, `validated_in_simulation`, `ready_for_release`, `operationally_verified`, `delivered`. Para funcionalidade destinada à produção, somente `operationally_verified` com critérios aprovados, evidência atual e protocolo GitHub satisfeito permite `delivered`; merge ou simulação isolados não bastam. Tickets de pesquisa/planejamento têm entrega documental explicitamente delimitada, sem alegar operação da funcionalidade futura. Componentes internos devem funcionar em seu ambiente operacional declarado; produção não significa publicar toda biblioteca na internet.

A promessa verificável é ausência de falhas conhecidas nos critérios e integrações exigidos, com monitoração e recuperação. Não existe prova de ausência de toda falha futura. Uma falha descoberta abre incidente/reparo, marca saúde degradada e invalida prontidão pertinente; nunca ocultar um teste que falhou para sustentar “entregue”.

## 4. Alta inteligência, assinaturas e atualização diária

Mapa inicial solicitado pelo owner em 08/09/2026, preservando os nomes que ele utiliza:

| Harness pago | Preferência inicial de alta inteligência | Resolução obrigatória |
| --- | --- | --- |
| Antigravity | 3.8 Flash | Resolver ID e acesso reais no catálogo do harness/conta. |
| Grok Build | grok-4.6 | Resolver ID e acesso reais no catálogo do harness/conta. |
| Claude Code | opus-5.1 | Resolver ID e acesso reais no catálogo do harness/conta. |
| Codex | astra-6 | Nesta sessão, o catálogo expõe `gpt-6-astra`; registrar o ID efetivamente usado. |

Esses são alvos aprovados pelo owner, não certificação de login, cota ou existência de adaptador remoto em todas as contas. A referência oficial consultada confirma o identificador [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra). Não traduzir nomes em IDs por adivinhação.

Selecionar a melhor versão disponível e qualificada no harness pago em uso. Esforço permitido: `high` por padrão; `max` quando a complexidade justificar e for suportado. Nunca `xhigh`/extra-high/Ultra; não supor que esses rótulos sejam equivalentes entre fornecedores. Se nenhum dos dois níveis existir, o adaptador documenta a capacidade e o roteador usa outro candidato compatível, sem enviar parâmetro inválido.

HF-07 e skills 12/14 devem atualizar o catálogo ao menos uma vez a cada 24 horas, com recuperação ao reiniciar se a execução diária foi perdida: releases oficiais, disponibilidade por conta/host, aliases/IDs, esforços aceitos, preços, limites, resultado de smoke e avaliação curta por tipo de tarefa. Promover automaticamente versão nova compatível e qualificada para novos jobs; fixar versão por job/run já em andamento, com rollback do catálogo se houver regressão. Falha na consulta mantém o último catálogo válido com idade visível, sem bloquear jobs executáveis. Não confundir “último release” com capacidade já validada; candidatos reprovados geram reavaliação automática.

Antes de esperar crédito, avaliar outras assinaturas/hosts compatíveis, capacidade local qualificada e APIs autorizadas. Se uma tarefa de alta inteligência for urgente ou bloquear o fluxo e as assinaturas não tiverem capacidade, escolher na fronteira de Pareto por qualidade demonstrada para a tarefa, custo total estimado, latência e orçamento reservado. Aceitar menor qualidade marginal do modelo por economia substancial, mantendo o piso de qualidade da especificação e os mesmos gates. Registrar candidatos, evidência, qualidade relativa apenas quando comparável, custo e motivo. Não inventar percentuais de inteligência entre benchmarks incompatíveis.

**Fable-5.1 não é fallback padrão.** Qualquer candidato caro deve demonstrar vantagem suficiente para justificar o custo e caber no envelope; ausência de preço não equivale a preço zero. A alternativa Pareto ocupa o papel de planejador qualificado de contingência, sem transferir decisões ao implementador. Se não houver rota qualificada dentro do saldo/orçamento autorizado, `waiting_budget` com contas consultadas, próximo reset/reavaliação e motivo. Não comprar créditos automaticamente. A disponibilidade desta sessão interativa não prova execução sem notebook.

## 5. Continuidade e paralelismo em todos os módulos

Cada módulo declara `trigger_events`, entradas/dependências, outputs, verificadores, chaves de conflito, requisitos de recursos, limites de conta, timeout, retry/backoff, evento sucessor, retomada, fallback e condições exclusivas de interação humana. Inclui Grill, planejamento, pesquisa, código, testes, revisão, deploy, documentação e aprendizado. Nenhum sucesso fica aguardando um novo prompt para acionar o próximo estágio.

HF-05/HF-03 devem instalar gatilhos duráveis no controle cloud: transição confirmada grava evento de despacho na mesma transação (outbox ou equivalente); consumidor idempotente reserva job/lease; recuperação periódica reconcilia eventos perdidos e leases expirados. Meta inicial: despacho em até 30 segundos com capacidade livre; reconciliação em até 60 segundos, parâmetros medidos no HF-15. Hook é caminho rápido; timer/cron durável é recuperação, sem criar duas autoridades. Registrar ativação e testar reboot com notebook desligado. Atualização diária de modelos usa esse serviço, não cron dependente da conversa do owner.

Despachar todos os jobs liberados e sem conflito até a capacidade disponível, simultaneamente entre projetos e entre etapas. Desenvolvimento, testes, pesquisa e revisão têm pools/limites independentes onde os recursos físicos permitirem; nenhuma barreira global “terminar todos os desenvolvimentos antes de testar”. Mesmo projeto admite worktrees isoladas e ownership de arquivos/contratos; migrações, merge e promoção disputam locks específicos. Reservar CPU/RAM/GPU, cotas e orçamento atomicamente; limitar por capacidade medida, sem hardcode de um job para toda a fábrica. Justiça por projeto e envelhecimento de prioridade evitam starvation. Fila de merge pode serializar a integração sem serializar os demais trabalhos.

Caso de referência: quatro tickets de desenvolvimento de três projetos, dois do mesmo projeto sem conflito, e cinco jobs de teste independentes devem ficar simultaneamente em execução quando houver nove slots compatíveis e recursos suficientes. Com menos slots, maximizar ocupação útil e expor a restrição concreta. Falha ou espera humana de um projeto não congela os demais.

Esperas de negócio legítimas: `waiting_human` para decisão/acesso insubstituível; `waiting_budget` somente quando nenhuma rota qualificada cabe no saldo/orçamento autorizado. Esperas técnicas reais (`waiting_dependency`, `waiting_capacity`, `retry_scheduled`, recuperação de incidente) não desaparecem por convenção: devem ter causa, próximo evento/tentativa e recuperação automática, nunca pausa administrativa indefinida. Falha de gate gera correção/replanejamento, não aprovação forçada. Esgotar tentativas de uma estratégia dispara análise e nova estratégia dentro do orçamento; evitar loop infinito sem progresso. Cancelamento explícito do owner permanece válido.

O estado ideal do painel é projetos entregues ou decisões humanas concretas; no mundo real, mostrar runs ativos, capacidade ocupada, créditos e recuperação. Job pronto parado apesar de recursos livres é defeito do scheduler, detectado por watchdog, não estado normal. Após indisponibilidade externa, revalidar automaticamente e continuar. Nenhum modelo pode substituir consentimento ou autorização obrigatórios.

## 6. Memória, pesquisa e Learning Pack preservados

Preservar skills 00/08 (autoaprendizado), 10/11 (pesquisa/reúso), 12/14 (atualização/avaliação) e 13 (aprendizagem do owner), juntamente com DF-05/06/17–19. A modularização conecta essas capacidades aos eventos do runtime; não as elimina nem as adia para a onda 2.

Resultado, correção, incidente, rejeição e marco arquitetural geram evidência persistente e candidatos de aprendizado. Avaliar em casos independentes, promover dentro da política, versionar, monitorar regressão e reverter. Preferências explícitas mantêm origem e escopo; hipótese não vira regra global. Recuperação de contexto é seletiva por projeto e tarefa, com proveniência e isolamento entre clientes. Alterar o verificador do próprio run continua proibido.

Toda pesquisa externa produz `.factory/research/<research-id>/ledger.json` e `INSIGHTS.md` (ou adaptador canônico equivalente): questão, link canônico de cada fonte realmente consultada, título/autor ou organização, data de consulta/publicação quando disponível, versão, trecho/localizador suportando a conclusão, resumo, insights, aprendizados, limitações, decisões/tickets relacionados e validade/revisão. Normalizar/deduplicar URLs; manter link original se o canônico não puder ser confirmado. Nunca inventar fonte consultada ou copiar material integral sem autorização. Reúso cita a pesquisa anterior e revalida fatos voláteis. Sem rede, distinguir memória recuperada de pesquisa nova.

A cada entrega/marco relevante, gerar Learning Pack do owner: o que foi construído, por que, aprendizados práticos, tradeoffs, evidências/fontes e dois ou três tópicos de discussão opcionais. Entregar no Hub/canal configurado, com aprofundamento sob demanda. Confirmação de leitura não bloqueia produção nem o próximo ticket. Falha na geração fica em job de documentação com retry e referência visível, sem perder o evento. HF-15 exige prova de geração/consulta do pack e retomada da memória após reinício.

## 7. Rastreabilidade por módulo e aceitação

Todos os módulos abaixo herdam integralmente as seções 1–6 e devem repeti-las como campos concretos no seu handoff, sem criar implementação nesta revisão.

| Módulo | Complemento obrigatório e evidência |
| --- | --- |
| HF-01 | Inventariar dependências e capacidade de automação; distinguir declaração, simulação e operação; preservar pesquisas/aprendizado; registrar Alemanha como decisão oficial. |
| HF-02 | Laboratório real com PostgreSQL/processos e fronteiras explícitas; decisão mede viabilidade de gatilhos, paralelismo e adaptação; transferência de requisitos do ambiente ao HF-03. |
| HF-03 | Controle cloud, startup, rede, segredos, worker e timers testados pela identidade real; manual dependency somente após alternativa equivalente descartada. |
| HF-04 | Schemas GrillRecord, EnvironmentManifest/Evidence, ManualDependency, readiness e estados; rejeitar entregue sem evidência exigida. |
| HF-05 | Scheduler/outbox, reconciliação, recursos/locks, justiça e paralelismo por estágio; toda transição elegível dispara sucessor. |
| HF-06 | Skills 00–17 e espelhos com contratos/gatilhos; remover nomes de modelos antigos como defaults e qualquer espera por prompt conflitante. |
| HF-07 | Catálogo diário por harness, high/max, Pareto sem Fable default, reserva de orçamento e adaptação real sem notebook. |
| HF-08 | Grill eficiente antes da especificação; decisões persistentes e bootstrap autônomo com dependências explícitas. |
| HF-09 | Testes de integração externa realista, manifesto reconciliado pós-implementação; pools de desenvolvimento/teste/revisão concorrentes. |
| HF-10 | Memória persistente, avaliação/promoção autônoma, fontes canônicas e Learning Pack com tópicos opcionais; recuperar após restart. |
| HF-11 | Push/PR/merge autônomos com identidade real e reconciliação; merge não é prova de operação. |
| HF-12 | Validação de contas/rede/configuração no destino, canário, smoke de jornada, rollback e delivered condicionado à operação. |
| HF-13 | Exibir jobs ativos, espera causal, uso de slots, dependências humanas com passos, evidência de ambiente, pesquisas e Learning Packs. |
| HF-14 | Telegram/n8n com autenticação, callback/polling real e retomada; setup manual guiado só quando inevitável. |
| HF-15 | Executar G1–G8 abaixo além dos cenários já previstos, com evidência e sem falso sucesso. |
| HF-20 | Templates já incluem Grill, manifesto, gates e gatilhos. |
| HF-21 | CRM/SEO/Ads e conteúdo exigem credenciais/rotas reais e canários autorizados, sem compra de anúncios implícita. |
| HF-22 | Fontes canônicas, ingestão e memória isolada testadas no destino. |
| HF-23 | Otimizar concorrência já operacional na onda 1; não adiar o scheduler básico. |
| HF-24 | Incorporar restrições pagantes aos mesmos gates e mecanismos de acesso. |
| HF-25 | Evolução autônoma já iniciada em HF-10; expandir catálogo/reúso sem autoaprovar alteração de autoridade. |

G1: demanda ambígua recebe opções; demanda já clara passa Grill sem pergunta redundante; resposta retoma só os jobs afetados.

G2: chave ausente com alternativa equivalente aprovada tecnicamente é resolvida/testada automaticamente; chave insubstituível abre passos seguros; resposta incorreta não libera gate.

G3: unitários verdes com firewall/scopes inválidos no worker real bloqueiam prontidão; correção e nova evidência liberam; simulação não certifica segredo de produção.

G4: quatro desenvolvimentos e cinco testes independentes usam nove slots quando disponíveis; conflito real restringe só os jobs envolvidos; capacidade menor gera fila explicável sem starvation.

G5: evento duplicado, evento perdido e reinício recuperam o despacho sem duplicar efeito; job elegível não depende de prompt humano.

G6: assinatura sem cota usa outra rota qualificada; tarefa urgente/bloqueante usa Pareto no orçamento; nenhuma rota válida espera crédito; Fable não é default; atualização diária não troca modelo no meio do job.

G7: fonte pesquisada resolve ao link registrado, insights vinculam decisões, memória sobrevive a restart e Learning Pack apresenta tópicos; leitura humana é opcional.

G8: projeto pagante preserva aceite antes do deploy; entrega só após provas operacionais; falha de produção abre recuperação e não para projetos independentes.

## 8. Topologia documental e confirmação técnica

Alemanha/Falkenstein e Hetzner CX23 são a decisão vigente. O owner esclareceu que **a instalação Ubuntu e a formatação foram abandonadas**, preservando **Windows com ambiente Linux via WSL2** no servidor. [INFRA-03](../.factory/infra/roadmap.md) associa Windows/Docker Desktop/WSL2 ao nó local. O [guia antigo de setup](../.factory/infra/immediate_next_steps.md) fica superado nesse ponto e não comprova Ubuntu instalado na VPS.

O owner confirmou que a VPS usa outra configuração, padrão da Hetzner; WSL2 pertence ao servidor local Windows. A imagem e a versão exatas da VPS não foram declaradas. Identificar seu sistema/versão e runtime de containers por leitura técnica antes de aplicar scripts de instalação; não assumir WSL2 nem Ubuntu somente pela descrição do nó local. Esse preflight não exige nova decisão arquitetural do owner. Reaproveitar acesso delegado existente. Se faltar acesso indispensável à implantação, aplicar o fluxo de dependência guiada.

Verificação opcional na Hetzner: abrir o projeto, selecionar Servers e `darkfac-vps-primary` (#165058444), abrir o ícone da console no canto superior direito e autenticar com a conta existente, conforme [guia oficial](https://docs.hetzner.com/cloud/servers/getting-started/vnc-console/). Em shell Linux, `cat /etc/os-release` identifica a distribuição; em PowerShell do host Windows, `Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version` identifica o host e `wsl --list --verbose` lista distribuições. Um Ubuntu dentro de WSL2 não significa que o host foi formatado para Ubuntu. Não usar Rebuild, Rescue ou reinstalação para essa consulta. Não compartilhar senhas pelo chat; se login não estiver disponível, registrar acesso pendente para o preflight, sem redefinir credenciais só para satisfazer esta dúvida.
