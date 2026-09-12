---
name: repo-code-scout
description: Minera repositórios open-source e componentes testados de alta qualidade para não reinventar a roda. Avalia compatibilidade de licença (MIT/Apache/BSD), maturidade (estrelas, commits recentes) e presença de suítes de teste. Salva o catálogo de repositórios e insights arquiteturais no Knowledge Ledger (.factory/research/). Use sempre que precisar implementar uma funcionalidade que já foi resolvida com excelência pela comunidade open-source.
---

# 11 - Repo Code Scout: Mineração e Reúso de Componentes

Esta skill assegura o princípio de eficiência da DarkFac: **"Não reinventar a roda"**. Antes de implementar um componente utilitário, parser ou driver do zero, o agente deve minerar o ecossistema open-source em busca de bibliotecas maduras, desacopladas e exaustivamente testadas.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Requisito Técnico**: Descrição da funcionalidade ou utilitário procurado (ex: pool de conexão, cliente redis, parser tipado).
- **Restrições de Licença**: `--permissive-only` (MIT, Apache-2.0, BSD-2/3, ISC).
- **Filtros de Maturidade**: Linguagem (`--language python`), estrelas mínimas (`--min-stars 50`).

### Ações e Procedimento Executável
1. **Detecção e Classificação da Intenção de Reúso**:
   ```bash
   python core/research/cli.py classify "Procurar componente com pool testado"
   ```
2. **Mineração Cirúrgica no GitHub (Headless)**:
   ```bash
   python core/research/cli.py scout "nome da funcionalidade" --language python --min-stars 50 --permissive-only
   ```
3. **Avaliação dos 4 Critérios Inegociáveis**:
   - **Licença Permissiva**: Verifique que a licença autoriza incorporação sem contaminação viral.
   - **Presença de Testes**: Confirme a existência de pasta `tests/` ou pipeline de CI funcional.
   - **Atividade e Manutenção**: Descarte projetos arquivados ou sem commits recentes.
   - **Desacoplamento Headless**: Assegure que a lógica não está acoplada a interfaces gráficas.
4. **Geração Automática do Registro de Proveniência**:
   ```bash
   python core/research/cli.py auto "código existente de detecção de voz" --permissive-only
   ```

### Outputs Estruturados
- **Dossiê no Knowledge Ledger**: `.factory/research/<slug>/INSIGHTS.md` detalhando repositórios avaliados, licenças, métricas e recomendações de adaptação.
- **Catálogo de Fontes**: `.factory/research/<slug>/ledger.json` com URLs de repositórios e hashes de releases.
- **Anotação de Proveniência**: Cabeçalho incluído no código que adapta a lógica descoberta:
   ```python
   # [RESEARCH PROVENANCE & INSIGHTS]
   # Ledger ID: <slug>
   # Audit Doc: .factory/research/<slug>/INSIGHTS.md
   # Canonical Sources: .factory/research/<slug>/ledger.json
   ```

### Portões, Política e Validação
- **Compatibilidade Rigorosa de Licenças**:
  - Aprovado para reúso/cópia: `MIT`, `Apache-2.0`, `BSD-2/3-Clause`, `ISC`.
  - Estritamente proibido copiar: `GPL-2.0/3.0`, `AGPL-3.0` ou código sem licença declarada.
- **Obrigatoriedade de Testes**: O componente só pode ser recomendado se sua confiabilidade for demonstrada por suíte de testes existente.
- **Isolamento de Dependências**: Avalie impactos transitivos no `pyproject.toml` para evitar conflitos de versão.

---

## 2. Continuous Self-Improvement & RCA de Reúso

- **RCA em Conflito de Dependências**: Se uma biblioteca minerada introduzir conflito de dependências ou quebrar o harness, execute RCA e registre a incompatibilidade via `python -m core.learning.cli rca`.
