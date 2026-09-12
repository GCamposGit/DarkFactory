# Agent Contract

## Antes de começar

- Leia `MISSION.md` e `FACTORY_RULES.md`.
- Preserve o escopo: o núcleo DarkFac é compartilhado.
- Use `.agents/skills/` como catálogo de skills. Grok e outros harnesses devem seguir este contrato mesmo que não carreguem skills automaticamente.

## Convenções de código

- Python 3.12+, type hints em APIs públicas e nomes `snake_case`.
- UTF-8 na entrada/saída de CLI; não imprimir emojis sem fallback seguro no Windows.
- `pathlib.Path` para caminhos e `logging` para diagnóstico.
- Pydantic v2 para contratos de dados; FastAPI apenas na camada HTTP.
- Mantenha domínio e I/O separados para permitir testes `library`, `cli` e `http`.
- Trate falhas externas (Ollama, OpenRouter, GitHub, arXiv) com fallback ou erro estruturado, sem vazar credenciais.

## Instruções ao Usuário e Configurações Manuais

- Sempre que um passo envolver configuração manual pelo usuário (dashboards, portais, integrações, arquivos de ambiente, etc.), forneça instruções passo a passo, tela por tela na versão atual da interface da plataforma.
- Forneça sugestões de conteúdo para absolutamente todos os campos que precisam ser preenchidos e seletores.
- Nunca assuma que o usuário tem experiência na configuração ou sabe o que está fazendo; o guia deve ser à prova de falhas e retrabalho.

## Validação obrigatória

```powershell
python core/harness/runner.py --quick
python -m pytest tests -v --ignore=tests/test_canaletto.py
```

## Compatibilidade entre harnesses

- Antigravity: carrega `.agents/skills/`.
- Grok: use a raiz clonada como workspace, leia `AGENTS.md` e `FACTORY_RULES.md` e execute os comandos acima.
- Outros agentes: `AGENTS.md` é o contrato mínimo; `docs/HARNESS_INTEROP.md` contém o fluxo de bootstrap.
