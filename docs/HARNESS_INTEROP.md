# Interoperabilidade entre harnesses

O repositório é deliberadamente independente do editor ou do agente. O contrato comum está em `MISSION.md`, `FACTORY_RULES.md` e `AGENTS.md`.

## Clone em outro PC

```bash
git clone <URL_DO_REPOSITORIO> DarkFac
cd DarkFac
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python core/harness/runner.py --quick
python -m pytest tests -v --ignore=tests/test_canaletto.py
```

O stack de GPU/transcrição é opcional:

```bash
python -m pip install -r requirements-audio.txt
```

## Antigravity

Abra a raiz clonada como workspace. O catálogo `.agents/skills/` é a fonte canônica das 17 skills do projeto.

## Grok

Abra a raiz clonada como workspace e injete `AGENTS.md` como instrução de projeto quando o harness oferecer essa opção. Mesmo sem carregamento automático de skills, o agente pode seguir `FACTORY_RULES.md` e executar o harness determinístico.

## DarkHub

```bash
python run_hub.py
```

O serviço fica em `http://127.0.0.1:8888` por padrão. O launcher do Canaletto não faz parte do clone compartilhado e continua disponível apenas localmente.

## Skills em ambientes compatíveis

- Antigravity e Codex: `.agents/skills/`.
- Claude Code: `.claude/skills/`, sincronizado pelo script `python scripts/sync_skills.py`.
- Grok e outros harnesses: use o contrato raiz e, se houver suporte a skills, aponte-o para `.agents/skills/`.
