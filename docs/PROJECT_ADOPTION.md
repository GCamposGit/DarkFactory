# Project Adoption Gateway

O gateway é a fronteira nativa entre a Dark Factory compartilhada e cada produto. Ele permite iniciar um repositório vazio, assumir um brownfield, atualizar a versão da fábrica e preparar tarefas isoladas sem misturar roadmaps ou namespaces.

## Contrato

- O produto possui seu roadmap, código, `MISSION.md`, `FACTORY_RULES.md` e `harness.config.json`.
- A fábrica possui o runtime em `.factory/runtime`, o wrapper `.factory/darkfac.py`, as skills copiadas e o bloco delimitado em `AGENTS.md`.
- `.factory/darkfac.lock.json` fixa commit/repositório de origem e SHA-256 de cada arquivo gerenciado.
- Fonte e destino precisam representar estado Git comprometido. Estado sujo, drift ou conflito bloqueia antes de qualquer escrita.
- A aplicação usa escritas atômicas, rollback dos arquivos tocados e grava o lock por último.

## Arquitetura

```text
DarkFac commit limpo
        |
        v
inspect -> plan -> worktree isolada -> apply transacional -> verify
                      |                                      |
                      +-- branch codex/*                     +-- prepare-task

Produto:  MISSION / RULES / harness / roadmap
Fábrica:  .factory/runtime / skills / lock / bloco AGENTS
```

As ações do plano são `create`, `adopt`, `update`, `remove`, `unchanged`, `preserve` e `conflict`. Uma atualização só substitui ou remove um arquivo quando o hash atual ainda é o hash gerenciado anterior. Alteração local de um arquivo gerenciado vira conflito explícito.

## Brownfield: fluxo recomendado

Primeiro veja fatos e plano, sem escrever:

```powershell
python -m core.adoption.cli inspect C:\dev\MeuProduto
python -m core.adoption.cli plan C:\dev\MeuProduto
```

Depois crie uma worktree limpa e aplique:

```powershell
python -m core.adoption.cli adopt C:\dev\MeuProduto `
  --branch codex/darkfac-adoption `
  --destination C:\dev\MeuProduto_worktrees\darkfac-adoption
```

Para migrar contratos já preparados em outra árvore, selecione apenas os três arquivos de produto permitidos:

```powershell
python -m core.adoption.cli adopt C:\dev\MeuProduto `
  --mission-file C:\dev\MeuProduto\MISSION.md `
  --rules-file C:\dev\MeuProduto\FACTORY_RULES.md `
  --harness-file C:\dev\MeuProduto\harness.config.json
```

Esses seeds têm seus hashes registrados como proveniência inicial, mas continuam sendo arquivos editáveis e pertencentes ao produto.

## Greenfield

O destino deve estar ausente ou vazio. `init` cria um repositório Git, um commit baseline e aplica a fábrica:

```powershell
python -m core.adoption.cli init C:\dev\NovoProduto --name NovoProduto
```

Sem um harness explícito ou uma stack reconhecível com testes, a adoção falha fechada. Governança padrão contém TODOs e exige revisão humana antes de trabalho autônomo.

## Preparar uma demanda

Após revisar, validar e commitar a adoção:

```powershell
python -m core.adoption.cli verify C:\dev\MeuProduto_worktrees\darkfac-adoption
python -m core.adoption.cli prepare-task C:\dev\MeuProduto_worktrees\darkfac-adoption `
  --ticket FND-04 `
  --title "Cursor opaco e consistente" `
  --owner factory `
  --path src/produto/acesso/manifesto.py `
  --path tests/test_leitura.py `
  --validate "python -m pytest tests/test_leitura.py"
```

O manifesto em `.factory/tasks/<ticket>.json` fixa base SHA, branch, worktree, owner, caminhos permitidos e comandos de validação.

## Atualização da fábrica

Execute `plan` e `apply` a partir de um novo commit limpo da DarkFac. Arquivos gerenciados intactos são atualizados; arquivos do produto são preservados; qualquer drift interrompe a operação. Não há dependência de caminho absoluto para o checkout fonte.

## Evidência de projeto

As decisões foram fundamentadas na documentação oficial de [Git worktree](https://git-scm.com/docs/git-worktree.html), [Copier updates](https://copier.readthedocs.io/en/stable/updating/), [SLSA artifact verification](https://slsa.dev/spec/v1.2/verifying-artifacts), [GitHub reusable workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations) e [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model). O dossiê persistente está em `.factory/research/20260907_project_adoption_gateway/`.
