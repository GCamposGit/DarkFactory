# SPEC

## Objetivo
Transformar os 14 botões de navegação do topo do DarkHub em um menu vertical fixo na lateral esquerda, mantendo uma barra superior fina com logo, seletor de projeto e badges de status, sem alterar handlers nem a ordem dos itens.

## Fora de escopo
- Não adicionar dependências externas pesadas ou nova lib de UI.
- Não modificar modelos de dados ou arquivos fora do escopo desta demanda.
- Não acoplar lógica de negócios diretamente à apresentação gráfica.
- Não introduzir autenticação, gating ou controle de acesso por papel.
- Não reordenar nem reagrupar os 14 itens de navegação.
- Não migrar logo, seletor de projeto ou badges de status para o menu lateral.

## Desenho
O HTML passa a ter uma topbar fina com logo/seletor/badges e um <aside> lateral contendo os mesmos 14 botões, na mesma ordem e com os mesmos onclick. No desktop, o aside vira coluna fixa à esquerda com largura fixa e o conteúdo principal recebe offset; no mobile, vira off-canvas acionado por hamburger, reaproveitando o padrão de drawers existente. O CSS mantém a paleta slate/indigo, ajusta z-index para não conflitar com drawers z-30+ e preserva cache-busting ?v= em styles.css e scripts. O teste headless segue o padrão de tests/test_frontend_foundation.py, usando pytest + TestClient + regex/lxml.html sobre o HTML servido.

## Arquivos a tocar
- hub/frontend/index.html
- hub/frontend/styles.css
- hub/frontend/app.js
- tests/test_transformar_menu_superior_em_m.py

## Riscos
- Quebra de handlers se IDs, classes ou ordem dos botões forem alterados.
- Conflito de z-index ou sticky entre sidebar, topbar e drawers existentes.
- Comportamento mobile incorreto se o off-canvas não reutilizar o padrão de drawer atual.
- Cache-busting ?v= esquecido em styles.css ou scripts, servindo assets antigos.
- Teste headless não capturar regressões visuais de layout, apenas estrutura e presença de classes.
