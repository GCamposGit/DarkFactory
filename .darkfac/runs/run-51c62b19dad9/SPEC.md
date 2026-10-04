# SPEC

## Objetivo
No DarkHub (hub/frontend), manter uma topbar enxuta (logo/badge de cobertura/seletor de projeto/busca Ctrl+K/status/Novo) e mover os ~12 botoes de navegacao que hoje se acumulam a direita do header para um menu vertical fixo na lateral esquerda: fixo em desktop (>=1024px), colapsavel via hamburguer em telas menores, com destaque (aria-current="page") do item correspondente a rota/secao atual. Mudanca puramente de apresentacao: os handlers existentes (openRoadmapDrawer, openDemandsDrawer, openBenchmarksModal, scrollIntoView das secoes, etc.) sao reaproveitados sem alteracao.

## Fora de escopo
- Nao adicionar dependencias externas nem CDNs (o stylesheet e local e auto-contido por DH-13/USR-49)
- Nao alterar modelos de dados, rotas da API (hub/backend) ou logica de negocio
- Nao redesenhar os drawers/modais, secoes internas ou o conteudo de main
- Nao criar core/transformar_menu_superior_em_m/service.py (o files_hint autogerado da demanda nao se aplica: nao ha regra de negocio nova)
- Nao editar hub/frontend/styles.css a mao (arquivo gerado por scripts/generate_hub_styles.py)

## Desenho
1) CSS: adicionar um bloco de componentes semanticos na secao 12 ("Preserved DarkHub Custom Styling") de scripts/generate_hub_styles.py: .dh-shell, .dh-sidebar, .dh-sidebar-nav, .dh-sidebar-link, .dh-sidebar-link[aria-current="page"], .dh-sidebar-toggle, .dh-sidebar-backdrop e .dh-content (margin-left: 16rem a partir de 1024px; sidebar com transform: translateX(-100%) e .is-open para abrir abaixo de 1024px). Classes semanticas evitam criar variantes utilitarias inexistentes (lg:translate-x-0, lg:ml-64 nao existem no gerador). Regenerar via `python scripts/generate_hub_styles.py`, que escreve hub/frontend/styles.css e hub/frontend/static/styles.css identicos. 2) HTML: em hub/frontend/index.html (header nas linhas 16-211) manter no header apenas marca, badge de cobertura, seletor global de projeto, badges Ollama/OpenRouter, busca, engrenagem e "Novo", mais um botao hamburguer (.dh-sidebar-toggle, visivel so no mobile, aria-controls/aria-expanded). Criar <aside id="dh-sidebar"> com <nav aria-label="Navegacao principal"> contendo os itens movidos (Benchmarks, Aprendizado, Portfolio, Roadmap, Demandas, Telemetria, Estudio, Testes, Infra, Tarefas, Playground, Prompts), cada um com data-nav-target (id da secao ou nome do drawer) e os onclick atuais preservados; envolver header+main em .dh-content. 3) JS: novo hub/frontend/nav.js (script tag com a mesma versao ?v=20261003a exigida por tests/test_frontend_foundation.py) com toggleSidebar/closeSidebar (backdrop, Esc, clique em item no mobile) e marcacao do item ativo por location.hash + IntersectionObserver sobre as secoes. 4) Cache-busting: o stylesheet muda, entao subir ?v= do link em index.html e atualizar a assercao de versao em tests/test_frontend_foundation.py.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/nav.js
- scripts/generate_hub_styles.py
- hub/frontend/styles.css
- hub/frontend/static/styles.css
- tests/test_frontend_foundation.py
- tests/test_transformar_menu_superior_em_m.py

## Riscos
- tests/test_frontend_foundation.py fixa a string exata do link do stylesheet (?v=20260924b) e exige versao unica em todos os <script>: esquecer de atualizar/alinhar quebra o CI
- hub/frontend/styles.css e hub/frontend/static/styles.css precisam continuar byte-a-byte identicos e >10 KB; editar a mao em vez de regenerar causa drift
- Usar classes utilitarias inexistentes no gerador (ex.: lg:ml-64) nao quebra teste mas deixa o layout silenciosamente errado em producao
- Mover botoes pode romper o fluxo de teclado/foco e a acessibilidade se o aside nao vier com nav/aria-label e aria-current
- Sidebar fixa pode sobrepor o conteudo se .dh-content nao receber o offset, ou vazar scroll horizontal no mobile
- O guard de tree-hygiene (tests/_tree_hygiene.py) falha se algum teste novo escrever no checkout: o teste deve ser somente leitura (lxml/regex) ou usar tmp_path
