# Entrega remota verificável

Use este protocolo para todo ticket que altera código, documentação, configuração,
testes ou skills compartilhados.

## Pré-publicação

1. Confirme o repositório e o remoto por URL, branch-base e identidade do owner.
2. Separe arquivos do ticket de logs, caches, credenciais, artefatos regeneráveis e
   experimentos locais fora do escopo. Não use `git add -A` sem revisar o conjunto.
3. Registre `git status --short`, `git diff --check`, SHA-base, SHA final e o resumo
   de `git show --name-status`.
4. Execute os gates do repositório e preserve os logs/contagens. Um gate vermelho ou
   uma coleta vazia interrompe a publicação.

## Publicação e PR

1. Faça commit apenas do conjunto revisado e publique a branch com upstream explícito.
2. Confirme por leitura remota que o SHA da branch publicada é o SHA final local.
3. Crie uma única PR para a branch-base correta. A descrição deve apontar para o
   relatório e listar testes, limitações e mudanças de escopo.
4. Confirme que a PR compara exatamente a branch publicada e não um ref temporário,
   fork inesperado ou SHA antigo.

## Merge e pós-verificação

1. Aguarde checks obrigatórios, aprovações e políticas de branch. Não force push, não
   ignore checks e não use um merge local como substituto do GitHub.
2. Faça o merge pela interface/API autorizada do GitHub apenas quando a política do
   repositório permitir e a solicitação do usuário cobrir a entrega externa.
3. Leia novamente a PR e confirme `state=closed`, `merged=true`, `mergedAt` presente,
   `baseRefName` correto e o SHA do commit de merge.
4. Leia o `main` remoto e confirme que ele alcança o commit entregue. Quando Git estiver
   disponível, `git fetch --prune` e `git merge-base --is-ancestor <sha> origin/main`
   devem passar; caso contrário, use a evidência equivalente da API/UI.
5. Só então declare o ticket concluído. Se a branch foi publicada mas a PR não foi
   mergeada, o estado é `published_pending_merge`; se o remoto não puder ser lido, é
   `blocked_remote_verification`.

## Evidência mínima no relatório

- URL e número da PR;
- branch-base, branch publicada e SHA-base;
- SHA final da branch e SHA do merge;
- checks/reviews relevantes e horário da verificação;
- SHA observado no `main` remoto;
- `git status --short` residual, com cada item explicado.
