# USR-219 - Migracao da pagina principal do DarkHub

Limpeza pedida pelo owner. Nenhum endpoint nem backend mudou.

## 1. Secoes recolhidas (criterio 1)

| Secao | Antes | Depois |
|---|---|---|
| Conteineres e cargas de trabalho ativas (`infra.js`, bloco de telemetria) | sempre aberta | recolhida por padrao, botao Abrir/Fechar; estado em `localStorage` chave `darkhub.collapse.infra-containers` |
| Catalogo entre projetos e autoevolucao (`catalog.js`) | sempre aberta | recolhida por padrao, botao Abrir/Fechar; chave `darkhub.collapse.cross-catalog`. Os botoes Nova Proposta e Atualizar continuam no cabecalho |
| Plano de autonomia (DAG, restante do antigo painel de tarefas) | visivel via alternador | recolhido por padrao; chave `darkhub.collapse.tasks-dag`; so busca `/api/autonomy/plan` ao abrir |

Auxiliar compartilhado: `hub/frontend/collapse.js` (todo acesso ao storage em try/catch; sem storage, fica recolhido).

## 2. Estudio de conteudo e ateliê visual (criterio 2)

Saiu do `<main>`. Agora e um painel (`#studio-drawer`) aberto pelo botao **Estudio** do menu lateral (`openStudioDrawer()`), no mesmo padrao dos paineis Portfolio/Roadmap. A secao continua com o id `content-studio-section` (usado por `hub/coverage.json`), montada dentro de `#studio-drawer-body`. A secao Governanca (`enterprise.js`) deixou de usar o Estudio como ancora de posicao.

## 3. Painel de tarefas de `tasks.js` removido (criterio 3)

| Informacao do painel antigo | Para onde foi |
|---|---|
| Cartao "Aguardando decisao do owner (WAITING_HUMAN) - somente leitura" (titulo, ID, run, motivo, ultima atividade, instrucao de grill) | Fila prioritaria do topo (`interventions.js`): item WAITING_HUMAN com botao "Ver Tarefa", que abre o modal do USR-210 com causa, instrucao e acoes (responder Grill, retomar, cancelar run) |
| Cartoes da fila (posicao, etapa, run, passo, custo da tarefa, ultima atividade, evidencias) | Esteira ao vivo (`/live`): trilha de etapas, custo por run e por etapa, iteracoes, evidencias por etapa |
| Resumo "Em execucao" (runs ativos) | Esteira ao vivo (KPIs e faixas de execucao) |
| Resumo "Custo" | Esteira ao vivo: KPI de custo 24 h e custo total por run |
| Resumo "Na fila" / "Excecoes" | Fila prioritaria do topo (contagem de bloqueios) e Esteira ao vivo (estado/causa por run) |
| Barra de aviso de fontes (`sources`/`warnings`) | Nao migrada: era diagnostico do proprio painel; as fontes continuam em `/api/tasks/dashboard` |
| Alternador Fila de Jobs / DAG & Autonomia | Fila removida; o DAG permaneceu na secao `tasks-dashboard-section` (recolhida) |

Mantido em `tasks.js`: `loadLineStatus`, `renderLineCriterionHint` (USR-215), polling de 30 s so com a aba visivel, e o DAG da autonomia (`loadAutonomyPlan`, `renderAutonomyPlan`, modal de unidade).

Menu lateral: o botao "Tarefas" virou "Autonomia" (aponta para o plano de autonomia).

## 4. Pendencia registrada

`GET /api/tasks/dashboard` (e aliases `/api/tasks`, `/tasks/dashboard`) ficou sem consumidor no frontend. Nao foi removido (fora do escopo); fica mapeado em `hub/coverage.json` para a superficie `tasks-dashboard-section`.
