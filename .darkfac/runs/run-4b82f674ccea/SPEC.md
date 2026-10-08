# SPEC

## Objetivo
Mover toda a navegacao do header do DarkHub para uma sidebar vertical fixa a esquerda (aside#hub-sidebar), mantendo na topbar apenas marca, busca Ctrl+K, #global-project-select, badges de status e botao Novo; a sidebar fica visivel a partir de 1024px (256px, main com lg:ml-64) e colapsavel abaixo disso (hamburguer, overlay, Escape, aria-expanded, retorno de foco), com aria-current="page" unico no item ativo, preservando ordem, rotulos, icones, IDs e handlers originais.

## Fora de escopo
- Backend, rotas HTTP, modelos Pydantic ou contratos de API (hub/backend/*)
- Novas dependencias de frontend, build step, Tailwind CDN ou framework de UI
- Redesenho visual/tema (o visual escuro atual e preservado) e mudancas no conteudo das sections do <main>
- Alterar hub/frontend/live.html / live.css (apenas o link 'Esteira ao vivo' migra para a sidebar)
- Submeter o piloto a linha (depende de USR-84/85/86 e CI verde) ou alterar o pipeline da fabrica

## Desenho
hub/frontend/index.html hoje tem um <header> (linhas 16-215) com ~15 gatilhos de navegacao (botoes com onclick openBenchmarksModal/openLearningDrawer/openPortfolioDrawer/openRoadmapDrawer/openDemandsDrawer/openTelemetryDrawer/openPlaygroundDrawer/openPromptVaultDrawer/openBackupModal, scrollIntoView para content-studio-section/harness-validation-section/infrastructure-cards-section/tasks-dashboard-section, e o link #line-live-link para /live) misturados aos controles de estado. A mudanca e so de apresentacao: (1) os gatilhos de navegacao sao recortados para <aside id="hub-sidebar"> + <nav aria-label="Navegacao principal"> em coluna (fixed inset-y-0 left-0 w-64, overflow-y-auto, mesmo visual slate/indigo), com os MESMOS onclick/id/title/emoji e ordem; (2) a topbar vira marca + badge de cobertura + badges Ollama/OpenRouter + busca Ctrl+K + #global-project-select + botao Novo, mais um <button id="hub-sidebar-toggle" aria-controls="hub-sidebar" aria-expanded="false" class="lg:hidden"> e um <div id="hub-sidebar-overlay" class="hidden lg:hidden">; (3) <main> ganha lg:ml-64. O CSS e local e gerado: scripts/generate_hub_styles.py (build_stylesheet, secao 11 de breakpoints) escreve o MESMO conteudo em hub/frontend/styles.css e hub/frontend/static/styles.css — as utilities novas (lg:ml-64, lg:translate-x-0, lg:static, fixed/inset-y-0/left-0, z-40/z-50, overflow-y-auto) entram la e os dois arquivos sao regerados. O comportamento vai em hub/frontend/app.js, reaproveitando setupEventListeners (handler global de keydown, ja com o ramo Escape que chama closeAllModals) e o padrao hidden/classList dos drawers: openHubSidebar/closeHubSidebar/toggleHubSidebar alternam -translate-x-full/translate-x-0 e o overlay, sincronizam aria-expanded, fecham no clique em item (<1024px) e devolvem o foco ao toggle; setHubSidebarActive(id) garante um unico aria-current="page" e e chamada pelos gatilhos. Os <script> de index.html ja compartilham ?v=20261007a (coberto por tests/test_frontend_foundation.py::test_all_script_tags_share_one_cache_version); ao mudar os assets todas as tags sao bumpadas juntas para uma unica nova versao. Atencao: tests/test_frontend_foundation.py::test_stylesheet_link_uses_cache_version_20260924b fixa a versao do CSS no HTML e precisa acompanhar o bump do stylesheet. Testes do repo inspecionam o HTML/JS estaticamente (pathlib + lxml.html, ver tests/test_hub_browser.py), sem navegador: o novo tests/test_transformar_menu_superior_em_m.py segue esse padrao.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/app.js
- scripts/generate_hub_styles.py
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- tests/test_transformar_menu_superior_em_m.py
- tests/test_frontend_foundation.py

## Riscos
- tests/test_frontend_foundation.py::test_stylesheet_link_uses_cache_version_20260924b trava a versao do CSS no index.html: bumpar o ?v= do stylesheet sem atualizar esse teste deixa o portao vermelho
- Editar styles.css a mao quebra a paridade com static/styles.css e a reprodutibilidade; tudo deve vir de scripts/generate_hub_styles.py (que escreve os dois arquivos)
- Perder um gatilho no recorte do header (ex.: #line-live-link, o botao ⚙️ openBackupModal ou os scrollIntoView de sections) deixa a funcionalidade inacessivel sem erro visivel
- Outros testes dependem de IDs do index.html (tests/test_benchmarks_routing_consultation.py, tests/test_dh_autonomy_and_health_ops_api.py, tests/test_adicionar_cards_de_infrae.py, tests/test_cross_project_catalog.py): IDs devem ser preservados, nunca renomeados
- main com lg:ml-64 somado ao max-w-7xl mx-auto pode deslocar/encolher o conteudo em telas medias-grandes; o header sticky z-30 convive com sidebar z-40 e overlay, exigindo cuidado de z-index
- Reaproveitar o ramo Escape de closeAllModals pode fechar a sidebar junto com drawers legitimos (ou nao fechar nada) se a ordem de precedencia nao for explicita
