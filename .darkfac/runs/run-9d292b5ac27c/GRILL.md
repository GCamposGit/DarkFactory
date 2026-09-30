# GRILL

## Premissas
- O frontend do DarkHub é hub/frontend/index.html (HTML+JS vanilla, sem framework SPA) com Tailwind CSS precompilado localmente (styles.css, versionado via query string ?v=), servido por um backend FastAPI; a mudança será feita nesse arquivo e nos assets associados (styles.css, app.js e demais *.js de cada drawer).
- Os 14 botões de navegação e seus handlers onclick existentes (open*Drawer()/open*Modal() em app.js, portfolio.js, roadmap.js, demands.js, telemetry.js, learning.js, benchmarks.js etc.) permanecem funcionalmente idênticos; apenas o container/orientação/CSS muda de horizontal para vertical.
- Não há hoje autenticação ou controle de acesso por papel nos itens de menu; nenhum novo gating será introduzido nesta mudança.
- O teste headless de aceite (tests/test_transformar_menu_superior_em_m.py) será criado seguindo o padrão já usado em tests/test_frontend_foundation.py (pytest + TestClient + regex/lxml.html sobre o HTML servido), validando a presença/estrutura do menu lateral, a ausência do padrão antigo de nav horizontal, e reforçando a convenção existente de cache-busting ?v= em tags <script>/<link>.
- A paleta visual dark (slate/indigo) e o estilo dos drawers/header atuais serão reaproveitados no menu lateral, sem introduzir um novo design system nem dependências externas pesadas (ex.: nenhuma lib de UI adicional).
- z-index e o comportamento sticky do layout serão ajustados para não conflitar com os drawers existentes (que já usam z-30 e mais).
- (technical) Qual o estado padrão do menu lateral no desktop: sempre expandido (ícone+rótulo) ou colapsável (rail de ícones que expande)? -> **Sempre expandido (ícone+rótulo), largura fixa** (Menor complexidade de estado e sem necessidade de armazenar/persistir toggle em JS, evitando dependências desnecessárias; pode evoluir depois se necessário.)
- (technical) Como o menu deve se comportar em telas estreitas (mobile/tablet), já que hoje não existe hamburger e os botões apenas escondem o rótulo de texto? -> **Off-canvas: vira um drawer ocultável acionado por botão hamburger, reaproveitando os padrões de drawer já existentes no código (#portfolio-drawer, #roadmap-drawer etc.)** (Reaproveita CSS/JS de drawers já existentes na base (index.html), evitando nova dependência e mantendo consistência visual, com melhor uso de espaço em telas pequenas.)
- (technical) O menu lateral deve reservar uma coluna fixa à esquerda (empurrando o conteúdo principal) ou sobrepor o conteúdo como overlay fixo? -> **Coluna fixa reservada à esquerda; o conteúdo principal se desloca — padrão comum em dashboards admin** (Mais previsível e acessível, evita sobreposição do conteúdo existente e é consistente com o header sticky atual.)

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] O escopo da mudança é apenas os botões de navegação (viram menu vertical), ou o header inteiro (logo, seletor de projeto, badges de status Ollama/OpenRouter) também deve migrar para o menu lateral, eliminando a barra superior? (id=q1)
- [intent] A ordem e o conjunto dos 14 itens atuais (Benchmarks, Aprendizado, Portfólio, Roadmap, Demandas, Telemetria, Estúdio, Testes, Infra, Tarefas, Playground, Prompts, Backup/Settings, Novo) devem ser mantidos idênticos, só mudando a orientação, ou é permitido reagrupar por categoria? (id=q4)
