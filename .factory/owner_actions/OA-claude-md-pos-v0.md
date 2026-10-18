# Proposta: secao 0 do CLAUDE.md pos-V0

Substituir o bloco atual "## 0. Congelamento V0 (vigente desde 11/10/2026)" (titulo e 4 itens) pelo texto abaixo. O conteudo espelha a secao "Pos-V0" do `AGENTS.md` (commit 0a55547).

## Texto novo

## 0. Pós-V0 (conclusão declarada pelo Owner em 17/10/2026)

- O congelamento V0 foi encerrado (regra em `AGENTS.md`, seção "Pós-V0"). As tags `v0-critico` e `congelado-v0` não restringem a fila ativa; em tickets concluídos ficam como histórico.
- A fila segue o workflow atual, respeitando Gate G1, dependências, estado do ticket e capacidade. A frota pode executar até 3 ciclos simultâneos, em hosts distintos na prioridade Grokbox, Notebook e Desktop, quando saudáveis e compatíveis.
- Não há limite geral de critérios de aceite nem fatiamento obrigatório por quantidade de critérios. Gate G1 continua obrigatório para ambiguidades materiais.
- Defeito encontrado continua virando correção ou ticket (seção 3), com evidência e critérios de aceite, sem triagem ou tags do V0.
