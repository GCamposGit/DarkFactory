# SPEC

## Objetivo
No DarkHub (hub/frontend/index.html), mover os links/acoes de navegacao do header horizontal para um menu vertical fixo na lateral esquerda, mantendo uma topbar enxuta (logo/badges + busca/Ctrl+K + seletor de projeto + botao Novo). A sidebar e fixa em desktop (>=1024px) e colapsavel via hamburguer em telas menores, destaca o item correspondente a rota/secao atual e nao introduz nenhuma dependencia externa: o CSS continua sendo o stylesheet auto-contido gerado por scripts/generate_hub_styles.py.

## Fora de escopo
- Nao alterar hub/frontend/live.html, live.css nem live.js (a esteira ao vivo tem layout proprio)
- Nao adicionar dependencias externas, CDNs, frameworks de UI ou build de Tailwind (o gerador local continua a unica fonte do CSS)
- Nao alterar modelos de dados, endpoints do FastAPI (hub/backend) ou regras de negocio
- Nao renomear nem reescrever as funcoes de drawer/modal existentes (openDemandsDrawer, openRoadmapDrawer, openCommandPalette, etc.) - apenas reposicionar os gatilhos
- Nao redesenhar o conteudo das secoes do <main> (cards, graficos, tabelas)

## Desenho
1) CSS: scripts/generate_hub_styles.py e o unico gerador de hub/frontend/styles.css e hub/frontend/static/styles.css (as duas copias sao escritas por main()). A maioria das utilitarias necessarias ja existe (.fixed, .inset-y-0, .left-0, .w-64, .translate-x-0, .-translate-x-full, .transition-transform, .overflow-y-auto, .z-40, .flex-col, .h-screen, .lg\:hidden, .lg\:block, .lg\:flex), mas faltam as variantes responsivas de offset/transform usadas pelo padrao sidebar: lg:translate-x-0, lg:ml-64, lg:pl-64, lg:w-64, lg:max-w-none (secao 11 'Responsive Breakpoints', por volta de scripts/generate_hub_styles.py:829). Essas regras sao adicionadas no bloco @media (min-width: 1024px) e o CSS e regenerado rodando o script. 2) Markup: no header atual (index.html linhas ~15-213) os botoes de navegacao ficam todos no container 'Quick Actions / Status Header'; eles sao movidos para um novo <aside id="hub-sidebar"> (nav vertical com <nav aria-label>, agrupado por blocos: Operacao, Conhecimento, Infra/Ferramentas), preservando literalmente os atributos onclick/title/emoji de cada item. A topbar mantem marca, badge de cobertura, link 'Esteira ao vivo', seletor global de projeto, indicadores Ollama/OpenRouter, busca (Ctrl+K), engrenagem e 'Novo', mais um botao hamburguer id="hub-sidebar-toggle" visivel somente abaixo de lg. O <main> recebe o offset lateral (lg:ml-64) e deixa de ser centralizado por max-w-7xl mx-auto quando ha sidebar. 3) Comportamento: a logica de abrir/fechar (classe -translate-x-full <-> translate-x-0, aria-expanded no toggle, overlay clicavel id="hub-sidebar-overlay", fechar com Escape e ao clicar num item no mobile) e o destaque do item ativo (aria-current="page" + classes de realce, derivado de location.pathname e do data-nav-target da secao visivel) vivem em hub/frontend/app.js, para nao criar um novo <script> (test_frontend_foundation exige que TODAS as tags script compartilhem a mesma versao ?v=). 4) Cache busting: ao regenerar o CSS, a versao do link do stylesheet e incrementada em index.html e o assert correspondente em tests/test_frontend_foundation.py (test_stylesheet_link_uses_cache_version_*) e atualizado no mesmo ticket. 5) Teste headless novo em tests/test_transformar_menu_superior_em_m.py, no estilo de tests/test_hub_browser.py (parse com lxml.html) e tests/test_frontend_foundation.py (TestClient de hub.backend.main:app), validando estrutura da sidebar, ausencia dos gatilhos de navegacao no header, presenca das utilitarias no CSS e GET / == 200.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/app.js
- scripts/generate_hub_styles.py
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- tests/test_transformar_menu_superior_em_m.py
- tests/test_frontend_foundation.py

## Riscos
- tests/test_frontend_foundation.py trava a versao exata do link do stylesheet (?v=20260924b) e exige versao unica em todos os <script>: regenerar o CSS ou adicionar um novo arquivo JS quebra esses asserts se o teste nao for atualizado no mesmo ticket
- styles.css tem ~1.1 MB e e gerado; editar o CSS a mao causa divergencia com scripts/generate_hub_styles.py na proxima regeneracao - toda classe nova deve entrar no gerador
- as duas copias (hub/frontend/styles.css e hub/frontend/static/styles.css) devem ficar byte-identicas; o teste de fundacao checa tamanho/HTTP das duas
- index.html tem ~1805 linhas e os onclick apontam para funcoes globais espalhadas em app.js/demands.js/roadmap.js/etc.; mover um gatilho perdendo o onclick ou o id quebra silenciosamente um fluxo (nao ha teste de clique real)
- regressao de layout no <main>: remover max-w-7xl mx-auto sem compensar o offset pode estourar grids largos (DAG, infra) em telas intermediarias
- o gate oficial (core/harness/runner.py --quick, via scripts/line_validate.py) roda a suite inteira - mudancas no header podem quebrar outros testes de HTML (test_hub.py, test_hub_browser.py, test_hub_coverage.py, test_task_dashboard.py) que fazem grep por trechos de markup
