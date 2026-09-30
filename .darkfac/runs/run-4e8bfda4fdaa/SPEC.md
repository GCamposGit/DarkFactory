# SPEC

## Objetivo
Transformar o menu superior do DarkHub (linha flex com ~15 gatilhos que estoura para a direita em hub/frontend/index.html:41-209) numa barra lateral vertical fixa à esquerda, colapsável (ícones+rótulo <-> só ícones) com persistência em localStorage e, em telas pequenas, oculta atrás de um botão hambúrguer que a abre como overlay; a topbar permanece fina no topo apenas com marca DarkHub, badge de cobertura, seletor global de projeto, indicadores Ollama/OpenRouter e busca Ctrl+K.

## Fora de escopo
- Qualquer alteração em hub/backend/* (rotas, modelos Pydantic, serviços)
- Adicionar dependências externas, Tailwind CDN, build step de CSS ou biblioteca de UI
- Alterar a lógica das funções openBenchmarksModal/openLearningDrawer/openPortfolioDrawer/openRoadmapDrawer/openDemandsDrawer/openTelemetryDrawer/openPlaygroundDrawer/openPromptVaultDrawer/openBackupModal/openServiceModal/openCoverageDrawer/openCommandPalette e dos scrollIntoView
- Mexer nas pills de 'category-tabs' e na seção 'Quick Launch Dock', que são conteúdo e não menu
- Mudar o cache-busting ?v=20260924b ou adicionar novos arquivos .js ao index.html
- Acoplar lógica de negócio/estado de dados à sidebar (segue tudo em app.js e módulos atuais)

## Desenho
Camada de apresentação apenas. (1) styles.css passa a conter as utilitárias e componentes hand-authored necessários ao layout vertical (posicionamento fixo, altura de viewport, coluna flex, larguras 14rem/4rem, scroll interno, offset do main, backdrop de overlay), no mesmo estilo manual já usado no arquivo, e hub/frontend/static/styles.css é regravado byte-idêntico. (2) index.html mantém o <header> como topbar fina (marca + v1.0 + #hub-coverage-badge + #global-project-select + #ollama-status-badge + #openrouter-status-badge + botão Ctrl+K) e ganha um botão hambúrguer visível só em telas pequenas; o bloco horizontal de gatilhos é movido para um <aside id='darkhub-sidebar'> fixo à esquerda, com <nav> em coluna, cada item preservando onclick, title e id originais, e o rótulo textual envolvido em um <span class='darkhub-sidebar-label'> para poder sumir no estado colapsado; <main> recebe a classe de offset lateral. (3) app.js ganha funções curtas toggleSidebar()/openSidebarMobile()/closeSidebarMobile() que alternam a classe de colapso no <aside>, gravam/leem a chave localStorage 'darkhub_sidebar_collapsed' (mesmo padrão de 'darkhub_active_project' em app.js:168-188), atualizam aria-expanded e fecham o overlay com Esc ou clique no backdrop; os title= existentes servem de tooltip no modo ícone. (4) Testes seguem o padrão de tests/test_frontend_foundation.py: read_text() + regex sobre HTML/CSS/JS, sem navegador nem lxml.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- hub/frontend/app.js
- tests/test_transformar_menu_superior_em_m.py

## Riscos
- styles.css e static/styles.css podem divergir se o espelho não for regravado; toda alteração de CSS exige copiar o arquivo e validar igualdade byte a byte
- O CSS é hand-authored e sem cascata gerada: redefinir uma classe já existente mais adiante no arquivo pode sobrescrever regras usadas por outras telas — preferir nomes novos com prefixo darkhub-sidebar-
- app.js e outros módulos referenciam ids do header (hub-coverage-badge, hub-coverage-badge-text, portfolio-trigger-btn, ollama-status-badge, openrouter-status-badge, global-project-select); mover markup sem preservar esses ids quebra health.js/coverage.js/portfolio.js em runtime, sem que os testes de texto percebam
- O overlay mobile pode colidir em z-index com o header sticky (z-30) e com os drawers existentes, deixando a sidebar por baixo ou capturando cliques
- O <main> usa max-w-7xl mx-auto: aplicar offset lateral de forma ingênua pode descentralizar ou gerar scroll horizontal em telas médias
- Os testes só leem texto: uma regressão puramente visual (sidebar sobrepondo conteúdo) passa no portão — revisar renderização manualmente após o deploy
