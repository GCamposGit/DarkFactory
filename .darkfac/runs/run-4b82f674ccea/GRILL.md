# GRILL

## Premissas
- A mudança é pontual no frontend existente: HTML, JavaScript e CSS local gerado, sem novas dependências ou alterações de domínio, API ou modelos de dados.
- Todos os atalhos de navegação atuais serão movidos para um único aside#hub-sidebar, preservando ordem, rótulos, ícones, IDs e handlers, inclusive os que abrem drawers, modais ou rolam até seções.
- A topbar preservará marca, busca e atalho Ctrl+K, #global-project-select, badges e botão Novo; o link Esteira ao vivo será tratado como navegação e movido para a sidebar.
- Em telas a partir de 1024px, a sidebar permanecerá visível com largura de 256px e o main terá lg:ml-64. Abaixo desse limite, iniciará fechada e abrirá pelo hambúrguer, com overlay, Escape, aria-expanded e fechamento após selecionar um item.
- O item ativo acompanhará o drawer, modal ou seção selecionada, com apenas um aria-current="page" por vez; os handlers existentes continuarão responsáveis por abrir cada destino.
- O visual escuro atual será preservado. A sidebar terá rolagem própria quando necessário, e o fechamento móvel devolverá o foco ao botão de abertura.
- Os scripts compartilharão uma única versão ?v=. Os dois styles.css serão gerados por scripts/generate_hub_styles.py e permanecerão idênticos.
- Esta rodada é exclusivamente de leitura e Grill: não implementa, escreve arquivos ou submete o piloto. A execução posterior depende de USR-84, USR-85 e USR-86 concluídos e CI verde, seguindo a linha até PR, validação, merge, deploy e smoke.
- Na implementação, o teste tests/test_transformar_menu_superior_em_m.py deverá cobrir os critérios específicos, e scripts/line_validate.py deverá concluir com exit 0 usando o portão oficial.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
