# GRILL

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-51c62b19dad9); o owner nao foi consultado de novo.

> Decisoes adotadas da tentativa anterior do mesmo ticket (run run-83bd3712924d); o owner nao foi consultado de novo.

## Premissas
- O DarkHub é uma aplicação web com front-end já existente e o menu superior atual é composto por links de navegação.
- A mudança é puramente de apresentação/UI, sem alterar modelos de dados ou lógica de negócio.
- O CI do darkfac precisa estar verde (USR-84/85/86) antes de submeter esta demanda à linha, conforme nota do plano HF-28.
- O teste headless será executado via `python -m pytest tests/test_transformar_menu_superior_em_m.py -v` e deve passar sem erros sintáticos ou de tipos.
- Não há premissas anteriores registradas no projeto para esta demanda.
- (technical) A sidebar deve ser fixa (sempre visível) ou colapsável/recolhível em telas menores? -> **Fixa em desktop, colapsável em mobile** (Mantém usabilidade em telas pequenas sem introduzir complexidade desnecessária; alinhado ao escopo de não adicionar dependências pesadas.)
- (technical) Qual o comportamento esperado do estado ativo/seleção do item de menu na sidebar (ex.: destacar rota atual)? -> **Destacar item correspondente à rota atual** (Feedback visual de navegação é esperado em menus laterais e não requer lógica de negócio acoplada à apresentação.)
- (technical) O teste headless `tests/test_transformar_menu_superior_em_m.py` já existe no repositório ou precisa ser criado junto com a mudança? -> **Precisa ser criado nesta demanda** (O critério de aceite cita o arquivo explicitamente, mas o nome sugere geração automática pela fábrica; se não existir, criar um teste mínimo que valide a presença/estrutura do menu lateral.)
- (technical) Há alguma restrição de stack/UI (framework, biblioteca de componentes) que deva ser respeitada para a sidebar? -> **Usar apenas o que já está no projeto (sem novas deps)** (O fora de escopo proíbe dependências externas pesadas; reutilizar o stack atual reduz risco e mantém o CI verde.)

## Decisoes do owner
- [intent] O menu vertical lateral esquerdo deve substituir completamente o menu superior ou coexistir com ele (ex.: topbar com logo/busca + sidebar de navegação)? -> **Manter topbar enxuta (logo/busca) e mover apenas os links de navegação para a sidebar** (respondida pelo owner)
