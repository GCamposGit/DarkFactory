# Protocolo fail-closed de worktrees paralelas

Use este protocolo sempre que duas ou mais frentes puderem escrever no mesmo repositório ou quando uma entrega isolada precisar voltar ao checkout de integração.

## 1. Fechar a baseline antes do fan-out

1. Eleja exatamente um coordenador e um checkout de integração. Nenhuma outra tarefa pode escrever nesse diretório.
2. Confirme o repositório, branch e `HEAD`. Registre o SHA-base.
3. Se houver alterações locais, gere um backup recuperável fora do índice de integração e registre um fingerprint determinístico do estado: `git status --porcelain=v1`, `git diff --binary` e lista de arquivos untracked com hashes.
4. Separe o trabalho validado em commits lógicos. A baseline de fan-out deve estar limpa. Se isso não for possível, pare; não use `startingState: working-tree` para multiplicar um checkout sujo.
5. Verifique o ambiente antes de despachar: caminho e versão do Python, import de `pytest`, coleta da suíte e comandos focais. Em Windows/sandbox, prove também um `--basetemp` novo e gravável. Runtime sem dependências ou diretório temporário inacessível é bloqueio explícito; não descubra isso depois da implementação.

## 2. Manifesto, ownership e leases

Antes de criar worktrees, mantenha um manifesto versionável ou artefato de execução recuperável com: ticket/título, task ID, owner, branch, worktree, SHA-base, escopo permitido, lease com fencing e expiração, testes e ordem topológica.

Rejeite o despacho quando escopos se sobrepõem, quando owners usam o mesmo diretório/branch, quando o título/ID é temporário ou vazio, ou quando já existe escritor ativo no checkout de integração. Um `clientThreadId` de setup não é o task ID final: resolva e persista o ID real, título e `cwd` antes de autorizar escrita. Renovação preserva fencing monotônico; conclusão com lease vencido ou token obsoleto é rejeitada.

## 3. Execução e retry

- Cada frente escreve somente nos caminhos declarados. Expansão exige nova verificação de sobreposição.
- Não inclua no commit arquivos herdados da baseline ou alterações de outro owner. Compare sempre `SHA-base..HEAD` e o estado residual.
- Enquanto houver trabalho, publique heartbeat observável em intervalos de no máximo 60 segundos com etapa, último comando e bloqueio. Dois intervalos sem nova revisão exigem auditoria do task; não presuma progresso apenas porque o status é `active`.
- Falha de ferramenta, setup, quota, dependência ou teste produz RCA antes do retry. O retry reutiliza a mesma identidade quando o resultado anterior é incerto e nunca cria outra frente silenciosamente.
- `setup refresh`, task criado sem ID final, turno sem itens e executor sem `pytest` são falhas de infraestrutura. Preserve o estado, verifique o ambiente e retome a mesma frente. Só crie substituta após provar que a anterior não escreve mais e registrar a substituição no manifesto.
- Não use handoff para checkout sujo ou com escritor ativo. Primeiro encerre o escritor, capture backup e estabeleça checkpoint limpo. Se um handoff parcial falhar, audite stash, branch, worktree e reachability antes de tentar de novo; nunca encadeie handoffs às cegas.

## 4. Contrato de handoff

A frente entrega ticket/owner/task ID/lease; SHA-base e commit seletivo; `git show --name-status`; testes com exit codes, contagem e marcadores; relatório; e `git status --short` residual separando qualquer herança.

Ausência de final textual não invalida um commit verificável, mas exige auditoria direta. Declaração textual sem commit, arquivos e testes verificáveis nunca autoriza integração.

## 5. Integração topológica

1. Confirme backup recuperável e checkout de integração limpo.
2. Integre por dependência usando `cherry-pick` ou merge explícito do commit seletivo. Nunca copie a worktree inteira nem aceite automaticamente um lado completo de conflito.
3. Resolva conflitos por conteúdo, ownership e contrato do ticket. Após cada integração, execute os testes focais afetados e registre o novo SHA.
4. Depois do último ticket, rode os comandos obrigatórios, whitespace, marcadores de conflito, sync de skills e gates de governança.

## 6. Limpeza recuperável

Remova uma worktree somente quando o commit seletivo estiver alcançável pela branch de integração, os gates conjuntos estiverem verdes e o estado residual não contiver delta exclusivo. Resolva e confira o caminho absoluto antes da remoção. Preserve worktree, branch e backup quando qualquer prova estiver ausente; não use remoção forçada para ocultar estado não auditado.
