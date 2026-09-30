# SPEC

## Objetivo
Transformar o menu superior de ~15 botoes de acao do DarkHub (hoje numa unica linha flex em hub/frontend/index.html que estoura para a direita) em um menu vertical fixo na lateral esquerda, colapsavel com persistencia em localStorage e oculto atras de hambúrguer/overlay no mobile, mantendo marca, seletor de projeto, indicadores de status (Ollama/OpenRouter) e busca Ctrl+K numa topbar fina, sem alterar nenhum handler onclick nem backend.

## Fora de escopo
- Qualquer mudanca em hub/backend/* (rotas FastAPI, modelos Pydantic, servicos)
- Adicionar dependencias externas (Tailwind CDN, framework CSS/JS, lxml, navegador headless)
- Alterar a logica de negocio ou os handlers existentes (openBenchmarksModal, openLearningDrawer, openPortfolioDrawer, openRoadmapDrawer, openDemandsDrawer, openTelemetryDrawer, openPlaygroundDrawer, openPromptVaultDrawer, openBackupModal, openServiceModal, openCommandPalette, openCoverageDrawer e os scrollIntoView)
- Reorganizar as 'category-tabs' (pills de categoria) e a secao 'Quick Launch Dock', que sao conteudo e permanecem como estao
- Redesenhar o conteudo das secoes/drawers do main ou os modais
- Mover marca/seletor de projeto/status/busca para dentro da sidebar (decisao do owner: permanecem na topbar)

## Desenho
Apresentacao pura, CSS hand-authored self-contained (DH-13/USR-49), sem build step. (1) index.html: o <header> atual deixa de acumular os 15 gatilhos e vira uma topbar fina (h-14/h-16) com marca DarkHub + badge de cobertura, seletor global de projeto, badges Ollama/OpenRouter, trigger de busca Ctrl+K e, no mobile, um botao hambúrguer (id=sidebar-mobile-toggle). (2) Novo <aside id="darkhub-sidebar"> irmao do header, position fixed left-0 top-0 h-screen w-56 flex flex-col, com <nav> contendo os mesmos <button> movidos verbatim (onclick/title/id preservados), cada um convertido para linha 'icone + rotulo' (icone em span fixo w-5, rotulo em span.sidebar-label) e um rodape com o botao de colapso (id=sidebar-collapse-toggle). O container root ganha uma classe de offset (md:pl-56 / body.sidebar-collapsed -> md:pl-16) aplicada ao header e ao <main> para nao sobrepor conteudo. (3) styles.css recebe manualmente as utilitarias faltantes (fixed, left-0, top-0, h-screen, w-56, w-16, flex-col, overflow-y-auto, translate-x, transition-transform, z-40/z-50, md:pl-56, md:pl-16 etc.) no mesmo estilo do arquivo existente; hub/frontend/static/styles.css deve ser reescrito como copia byte-identica (checagem existente em tests/test_frontend_foundation.py). (4) app.js ganha um bloco pequeno e isolado: initSidebar() lendo localStorage['darkhub.sidebar.collapsed'] no boot, toggleSidebarCollapsed() alternando a classe no <body>/<aside> e gravando o estado, e toggleSidebarMobile() abrindo/fechando a sidebar como overlay (translate-x + backdrop clicavel + Escape), reaproveitando o padrao visual dos drawers ja existentes. Os atributos title ja presentes nos botoes servem de tooltip no modo colapsado; aria-label/aria-expanded/aria-controls nos toggles. (5) Teste novo tests/test_transformar_menu_superior_em_m.py no mesmo padrao de tests/test_frontend_foundation.py: read_text() + regex sobre index.html/styles.css mais FastAPI TestClient para as rotas, sem dependencia nova.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- hub/frontend/app.js
- tests/test_transformar_menu_superior_em_m.py

## Riscos
- Divergencia entre hub/frontend/styles.css e hub/frontend/static/styles.css quebra test_frontend_foundation.py: sempre copiar um sobre o outro e conferir hash apos editar
- Mover os botoes pode perder silenciosamente um onclick/id (ex.: portfolio-trigger-btn, hub-coverage-badge) e quebrar drawers; mitigado por asserts explicitos de cada handler no teste novo
- Sidebar fixed sem offset correspondente no header/main sobrepoe o conteudo do cockpit em telas medias
- Falta de utilitaria CSS hand-authored (ex.: w-56, h-screen, translate-x-full) faz o layout degradar silenciosamente, sem erro: cada classe nova usada no HTML precisa de assert de existencia em styles.css
- Overlay mobile sem z-index/backdrop consistente pode ficar atras dos drawers existentes ou travar o scroll do body
- Estado colapsado lido de localStorage antes do paint pode causar flash de layout (aplicar a classe o mais cedo possivel no boot)
