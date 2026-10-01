# SPEC

## Objetivo
Substituir o menu superior de navegação do DarkHub por uma sidebar vertical fixa à esquerda em desktop e colapsável em mobile, mantendo uma topbar enxuta com logo/busca e destacando o item ativo conforme a rota atual, sem novas dependências.

## Fora de escopo
- Adicionar dependências externas pesadas ou novas bibliotecas de UI
- Alterar modelos de dados, schemas ou lógica de negócio
- Modificar arquivos fora do escopo de apresentação do menu
- Acoplar lógica de negócio diretamente à camada de apresentação
- Redesenhar outras áreas do DarkHub além do menu/topbar

## Desenho
Manter a topbar existente reduzida a logo e busca; extrair os links de navegação do menu superior para um componente de sidebar renderizado à esquerda. Em desktop a sidebar é fixa (sempre visível) com largura constante; em mobile vira colapsável via toggle, usando apenas CSS/JS já presentes no projeto. O item ativo é determinado pela rota atual (comparação com o path) e recebe classe de destaque. O layout principal passa a ser um grid/flex de duas colunas (sidebar + conteúdo) com a topbar acima. Nenhuma lógica de negócio é movida para a view; apenas apresentação e estado de UI.

## Arquivos a tocar
- templates/base.html (ou layout principal equivalente)
- templates/partials/sidebar.html (novo)
- templates/partials/topbar.html (ajuste para logo/busca)
- static/css/layout.css (ou stylesheet principal)
- static/js/sidebar.js (toggle mobile, se necessário)
- tests/test_transformar_menu_superior_em_m.py (novo)

## Riscos
- Quebra de layout responsivo em telas pequenas se o toggle não for testado
- Regressão de acessibilidade (foco/teclado) ao mover links para a sidebar
- Teste headless citado no aceite pode não existir e precisar ser criado do zero
- CI do darkfac precisa estar verde (USR-84/85/86) antes de submeter à linha
- Seleção de item ativo pode falhar em rotas aninhadas se a comparação for ingênua
