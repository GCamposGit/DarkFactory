# Project Adoption Gateway — pesquisa e decisões

## Conclusão

A Dark Factory não deve ser copiada manualmente para dentro de cada produto nem importar um checkout irmão. O mecanismo definitivo é um gateway transacional: inspeciona o projeto, cria uma worktree a partir de um commit, instala um runtime namespaced, preserva os contratos que pertencem ao produto e grava um lock verificável com commit de origem e SHA-256 de cada arquivo gerenciado.

## Decisões derivadas das fontes

1. **Isolamento por Git worktree.** A documentação oficial do Git define worktrees como árvores de trabalho associadas ao mesmo repositório; o gateway usa uma branch dedicada e `--lock` para impedir descarte acidental durante a transação: https://git-scm.com/docs/git-worktree.html
2. **Atualização por ownership de três vias.** O fluxo de update do Copier mantém estado da versão anterior e trata conflitos explicitamente. O gateway aplica o mesmo princípio sem depender do Copier: https://copier.readthedocs.io/en/stable/updating/
3. **Proveniência verificável.** SLSA exige vincular o digest do artefato às expectativas de origem e parâmetros. O lock contém commit, repositório canônico e hashes dos arquivos; fonte ou alvo sujos falham fechados: https://slsa.dev/spec/v1.2/verifying-artifacts
4. **CI é extensão, não bootstrap.** Workflows reutilizáveis podem ser centralizados e fixados por SHA, mas não são pré-requisito do runtime local: https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
5. **Instruções de agentes são superfície de controle.** Skills e `AGENTS.md` influenciam a execução; por isso o bloco da fábrica é delimitado e verificável, enquanto o restante do arquivo continua pertencendo ao produto: https://developers.openai.com/api/docs/guides/latest-model

## Non-goals

- Não fazer auto-merge, deploy, agendamento, chamadas pagas ou gestão de segredos durante a adoção.
- Não copiar a árvore de trabalho suja do usuário.
- Não sobrescrever `MISSION.md`, `FACTORY_RULES.md` ou `harness.config.json` existentes.
- Não acoplar a fábrica ao layout `src`, `core` ou stack de um produto específico.
- Não declarar um projeto pronto sem teste determinístico positivo.

## Estado de arquivos

`create` cria ausente; `adopt` passa a gerenciar conteúdo já idêntico; `update` troca somente uma versão gerenciada sem drift; `remove` elimina somente um arquivo aposentado ainda intacto; `unchanged` não escreve; `preserve` reconhece ownership do produto; `conflict` bloqueia toda a transação.
