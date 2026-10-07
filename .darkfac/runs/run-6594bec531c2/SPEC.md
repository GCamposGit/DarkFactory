# SPEC

## Objetivo
No DarkHub (hub/frontend/index.html), esvaziar o cluster de ~12 botões de navegação que hoje se acumula à direita do header e reconstruí-los como um menu vertical fixo na lateral esquerda: topbar enxuta (marca, badges de status/cobertura, seletor de projeto, busca Ctrl+K, ⚙️ e 'Novo'), sidebar com os links de navegação (Benchmarks, Aprendizado, Portfólio, Roadmap, Demandas, Telemetria, Estúdio, Testes, Infra, Tarefas, Playground, Prompts), fixa em >=1024px e colapsável por botão hambúrguer abaixo disso, com destaque do item ativo. Mudança 100% de apresentação: nenhuma função JS de drawer, id de elemento, rota ou modelo de dados é alterada.

## Fora de escopo
- Não adicionar dependências externas, bundler, framework de componentes ou CSS de CDN (test_frontend_foundation proíbe recursos externos)
- Não alterar hub/backend (rotas, models, service), dados em hub/data nem contratos de API
- Não redesenhar o conteúdo das seções/drawers, nem renomear funções globais como openRoadmapDrawer/openDemandsDrawer/openCommandPalette
- Não mexer em hub/frontend/live.html, live.css e live.js (esteira ao vivo tem layout próprio)
- Não introduzir roteamento SPA / history API novo: 'rota atual' = hash + seção visível na página única

## Desenho
1) CSS: hub/frontend/styles.css (1.1MB) e seu espelho hub/frontend/static/styles.css são GERADOS por scripts/generate_hub_styles.py, cujas variantes responsivas (sm:/md:/lg:) são listadas à mão — utilitários novos como lg:ml-64 NÃO existem. Portanto a sidebar usa uma camada semântica nova na seção de componentes preservados do gerador (.dh-shell, .dh-sidebar, .dh-sidebar-brand, .dh-sidebar-nav, .dh-nav-link, .dh-nav-link[aria-current="page"], .dh-sidebar-footer, .dh-sidebar-toggle, .dh-sidebar-overlay), com @media (max-width:1023.98px) aplicando transform: translateX(-100%) + estado .is-open, e @media (prefers-reduced-motion: reduce) desligando transições — mesmo padrão já usado para .roadmap-*. Depois roda-se `python scripts/generate_hub_styles.py`, que reescreve os dois arquivos de forma idêntica. 2) HTML: body passa a <div class="dh-shell"> contendo <aside id="dh-sidebar" class="dh-sidebar"> com <nav aria-label="Navegação principal"> e os botões movidos VERBATIM (mesmo onclick, mesmo title, ids preservados) + rodapé com ⚙️ e os contêineres #ollama-status-badge/#openrouter-status-badge (preenchidos por id, logo o move é seguro), e <div class="dh-main"> com o <header> enxuto e o <main> existente. Um <button id="dh-sidebar-toggle" aria-controls="dh-sidebar" aria-expanded="false"> aparece só em telas pequenas, mais um <div id="dh-sidebar-overlay">. 3) JS: novo hub/frontend/sidebar.js (carregado com o MESMO cache token dos demais scripts, ?v=20261003a, exigência de test_frontend_foundation) com toggle/overlay/Esc/foco e marcação do item ativo por location.hash e IntersectionObserver sobre as seções do <main>, tolerando as seções injetadas em runtime por infra.js/studio.js/harness.js (ids content-studio-section, harness-validation-section, infrastructure-cards-section só existem após init). 4) Cache busting: o link do CSS é verificado por asserção literal em tests/test_frontend_foundation.py::test_stylesheet_link_uses_cache_version_20260924b; como o CSS muda, o token é bumpado para ?v=20261007a e esse teste é atualizado no mesmo ticket para manter a suíte verde. 5) Testes estruturais em tests/test_transformar_menu_superior_em_m.py usando pathlib + lxml.html + fastapi.testclient (padrão de tests/test_hub_browser.py e tests/test_frontend_foundation.py).

## Arquivos a tocar
- scripts/generate_hub_styles.py
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- hub/frontend/index.html
- hub/frontend/sidebar.js
- tests/test_transformar_menu_superior_em_m.py
- tests/test_frontend_foundation.py

## Riscos
- styles.css é gerado: editar o CSS à mão cria drift com scripts/generate_hub_styles.py e com o espelho static/styles.css — toda mudança deve passar pelo gerador e ambos os arquivos precisam ficar idênticos
- Utilitários Tailwind novos (lg:ml-64, w-64, lg:translate-x-0) não existem no stylesheet local; usar classe utilitária inexistente resulta em layout quebrado sem erro visível
- tests/test_frontend_foundation.py fixa literalmente o token do stylesheet e exige token único em todos os <script>; esquecer o bump/atualização ou usar outro token no sidebar.js quebra a suíte
- Os botões movidos dependem de funções globais (openRoadmapDrawer, openPortfolioDrawer, openCommandPalette, switchTaskViewMode…) e de ids de seção injetados em runtime; qualquer reescrita do onclick ou perda de id derruba navegação em produção sem falhar teste estático
- index.html tem 1805 linhas e o header concentra ids consumidos por vários JS (#hub-coverage-badge, #global-project-select, #ollama-status-badge, #openrouter-status-badge, #line-live-link): mover markup sem preservar ids causa regressões silenciosas em app.js/health.js/coverage.js
- Sidebar fixa reduz a largura útil do <main max-w-7xl>; grids de 4-5 colunas podem apertar em telas de 1024-1280px
