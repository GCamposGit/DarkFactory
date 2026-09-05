---
name: repo-code-scout
description: Minera repositórios open-source e componentes testados de alta qualidade para não reinventar a roda. Avalia compatibilidade de licença (MIT/Apache/BSD), maturidade (estrelas, commits recentes) e presença de suítes de teste. Salva o catálogo de repositórios e insights arquiteturais no Knowledge Ledger (.factory/research/). Use sempre que precisar implementar uma funcionalidade que já foi resolvida com excelência pela comunidade open-source.
---

# Repo Code Scout: Mineração & Reúso de Componentes de Alta Qualidade

Esta skill assegura o princípio fundamental da DarkFac: **"Não reinventar a roda"**. Antes de escrever qualquer componente não-trivial do zero, o agente deve minerar o ecossistema open-source em busca de bibliotecas maduras, componentes testados e padrões de implementação de referência.

## 🚀 Quando Usar Esta Skill
- Ao receber uma tarefa que envolve utilitários de sistema, decodificação de formatos, parsers, algoritmos comuns ou drivers de comunicação.
- Ao buscar componentes que já possuem suíte de testes unitários abrangente e casos de borda resolvidos.
- Para inspecionar como projetos consolidados desenham interfaces e isolam dependências.

---

## 🤖 Modelos Recomendados
- **Primário (Varredura e Avaliação de Repositórios)**: `gemini-3.8-flash` (Antigravity Native) — leitura de árvores de diretórios, análise de licenças e síntese rápida.
- **Auditoria de Licença e Segurança**: `gpt-review:latest` (Ollama Local) ou `claude-3.7-sonnet` — verificação de compatibilidade de licença e ausência de vulnerabilidades.
- **Local Fallback ($0 custo)**: `qwen-code-fast:latest` (Ollama) — extração de trechos de código e regex estruturado.

---

## 🛠️ Procedimento Operacional Passo a Passo

### 1. Detecção da Intenção de Reúso
Confirme se a tarefa solicita um componente ou código já existente:
```bash
python core/research/cli.py classify "Procurar repositório com cliente redis com pool testado"
```

### 2. Mineração Cirúrgica no GitHub (Headless) & Tendências de Comunidade
Execute a busca de repositórios aplicando filtros de qualidade:
```bash
# 1. Buscar repositórios consolidados com filtro de linguagem e estrelas mínimas
python core/research/cli.py scout "faster-whisper audio vad" --language python --min-stars 50 --permissive-only

# 2. (Opcional) Consultar ferramentas emergentes divulgadas por influencers e experts
python core/research/cli.py trends "faster-whisper real time" --limit 3
```


### 3. Matriz de Avaliação de Confiabilidade do Componente
O agente deve validar 4 critérios inegociáveis antes de reutilizar o código:
1. **Licença Permissiva**:
   - ✅ **Aprovado para reuso/cópia direta**: `MIT`, `Apache-2.0`, `BSD-2/3-Clause`, `ISC`.
   - ⚠️ **Apenas referência conceitual (PROIBIDO COPIAR)**: `GPL-2.0/3.0`, `AGPL-3.0` ou sem licença.
2. **Presença de Testes**:
   - O repositório deve ter diretório `tests/` ou configuração de CI demonstrando cobertura real.
3. **Atividade e Manutenção**:
   - Repositório não pode estar arquivado (*archived*) e deve ter atualizações recentes.
4. **Desacoplamento Headless**:
   - A lógica reutilizável deve ser desacoplada de interfaces visuais ou frameworks rígidos.

### 4. Geração Automática do Knowledge Ledger
Gere o registro canônico da pesquisa de repositórios:
```bash
python core/research/cli.py auto "código existente de detecção de voz em python" --permissive-only
```

O arquivo `INSIGHTS.md` gerado documentará:
- Repositórios identificados com quantidade de estrelas e licença.
- Se o componente deve ser importado via gerenciador de pacotes ou adaptado localmente.
- Lições aprendidas e padrões de arquitetura a replicar.

### 5. Marcação de Proveniência no Código
Ao incorporar padrões ou componentes inspirados no repositório pesquisado, adicione o cabeçalho:
```python
# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_voice_activity_detection
# Audit Doc: .factory/research/20260905_voice_activity_detection/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_voice_activity_detection/ledger.json
```

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **RCA em Reúso Quebrado ou Dependências Incompatíveis**:
   - Se um componente open-source integrado quebrar a compilação ou trouxer conflitos de sub-dependências, pare e execute RCA.
   - Identifique a causa raiz (ex.: conflito de versão de Pydantic, wheels binários ausentes no Windows).
   - Registre a restrição no ledger (`python core/learning/cli.py rca`) para que bibliotecas com os mesmos problemas não sejam recomendadas novamente.
2. **Seleção One-Shot de Componentes**:
   - Ao minerar repositórios, verifique previamente as preferências registradas de licença e stack do usuário, recomendando imediatamente a biblioteca perfeita sem exigir que o usuário pergunte "essa licença é permitida?".
3. **Extrapolação de Padrões de Qualidade**:
   - Quando um repositório consolidado apresentar um padrão excelente de testes ou isolamento de estado, extraia essa prática e sugira sua aplicação no repositório inteiro.

