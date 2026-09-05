---
name: topic-deep-research
description: Executa pesquisa aprofundada de conceitos, melhores práticas, arquitetura técnica e papers científicos recentes (arXiv) de alta credibilidade. Salva todas as fontes e insights estruturados no Knowledge Ledger (.factory/research/) para referência perpétua da aplicação. Use ao pesquisar fundamentos teóricos, algoritmos complexos ou novas arquiteturas.
---

# Topic Deep Research: Fundamentação Teórica, Papers & Melhores Práticas

Esta skill orienta agentes autônomos na exploração profunda de conceitos de engenharia, arquiteturas avançadas, RFCs e literatura científica recente, garantindo que as decisões técnicas do DarkFac sejam fundamentadas no estado da arte e auditáveis por qualquer versão futura do software.

## 🚀 Quando Usar Esta Skill
- Ao receber um requisito que envolve algoritmos avançados (ex: consenso distribuído, compressão, criptografia, streaming de áudio, machine learning).
- Ao definir padrões de arquitetura e trade-offs técnicos antes da elaboração de um PRD (`02-plan-product-architecture`).
- Ao avaliar alternativas de mercado e padrões recomendados por papers acadêmicos e blogs de engenharia de elite (Netflix, Meta, Cloudflare, Google DeepMind).

---

## 🤖 Modelos Recomendados
- **Primário (Orquestração & Contexto)**: `gemini-3.8-flash` (Antigravity Native) — ingestão massiva de papers e sintetização de insights com alta precisão.
- **Pesquisa Externa & Documentação Dinâmica**: `grok-4.6` — para consulta de benchmarks recentes e tendências da indústria.
- **Raciocínio Conceitual Profundo**: `claude-3.7-sonnet` (Extended Thinking) ou `deepseek-r1` — para análise crítica de limitações e trade-offs teóricos.
- **Local Fallback ($0 custo)**: `qwen-code-deep:latest` (Ollama) — análise e resumo local estruturado.

---

## 🛠️ Procedimento Operacional Passo a Passo

### 1. Classificação e Refinamento de Intenção
Certifique-se de que a pesquisa é de fato de natureza conceitual/arquitetural utilizando o classificador da DarkFac:
```bash
python core/research/cli.py classify "Sua pergunta ou tema de pesquisa"
```

### 2. Execução da Busca de Papers & Artigos Científicos (Headless)
Consulte a API pública do arXiv para obter preprints recentes com DOIs e citações canônicas:
```bash
# Busca direta de papers científicos
python core/research/cli.py papers "termo técnico em inglês" --limit 5
```

### 3. Radar de Tendências & Experts da Comunidade (Ideação de Features)
Para ampliar o escopo e capturar ideias de features emergentes que estão em alta na comunidade tech e divulgadas por líderes, fundadores e influencers respeitados:
```bash
# Busca de debates de ponta, novidades e opiniões de experts
python core/research/cli.py trends "nome da tecnologia ou ferramenta" --limit 3
```

> ⚠️ **Princípio dos Dois Tiers**:
> - **Tier de Tendências (Experts/Influencers)**: utilizado estritamente para **ampliar escopo, priorizar roadmap e trazer ideias de features** populares.
> - **Tier Canônico de Alta Credibilidade (Papers/RFCs/Docs)**: utilizado para **fundamentação arquitetural, integridade matemática, contratos de dados e segurança**. Nenhuma feature pode ser desenhada tecnicamente apenas com base em opiniões de rede social.

### 4. Síntese e Persistência Integrada no Knowledge & Insight Ledger
Execute a pesquisa automatizada com integração dual (Alta Credibilidade + Tendências):
```bash
python core/research/cli.py auto "Explicação técnica e papers sobre tema X" --limit 5
```

O comando gera e persiste em `.factory/research/<slug>/`:
1. `ledger.json`: dados canônicos estruturados das fontes divididos por `authority_tier`.
2. `INSIGHTS.md`: dossiê auditável contendo:
   - **🏛️ [ALTA CREDIBILIDADE]**: fundamentos teóricos comprovados e garantias matemáticas.
   - **🔥 [TENDÊNCIA / EXPERT]**: ideias de features em alta, com nota explícita de validação necessária.

### 5. Vinculação no Código-Fonte
Toda implementação derivada desta pesquisa **deve** incluir a anotação de proveniência no topo dos arquivos centrais:
```python
# [RESEARCH PROVENANCE & INSIGHTS]
# Ledger ID: 20260905_raft_consensus
# Audit Doc: .factory/research/20260905_raft_consensus/INSIGHTS.md
# Canonical Sources: .factory/research/20260905_raft_consensus/ledger.json
```

---

## 📋 Regras Inegociáveis de Governança
1. **Sem Alucinação de Fontes**: Toda citação deve vir acompanhada de URL canônica válida e identificador auditável (arXiv ID, DOI, RFC number, discussion ID).
2. **Separação Fato vs. Inferência**: Distinguir claramente o que foi comprovado pelo paper versus o que é sinal de mercado/ideação de influencer.
3. **Persistência Obrigatória**: Nenhuma decisão arquitetural de grande porte pode avançar para implementação sem o respectivo registro em `.factory/research/`.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **RCA em Pesquisas Vagas ou Fontes Inacessíveis**:
   - Se uma query do arXiv retornar 0 resultados ou artigos irrelevantes, execute RCA para identificar o descompasso de vocabulário técnico.
   - Refine a taxonomia e registre os termos de busca canônicos no ledger para que futuras pesquisas sobre o tema sejam imediatas (one-shot).
2. **Calibração de Profundidade com Preferências do Usuário**:
   - Aprenda se o usuário prefere sínteses concisas orientadas a trade-offs de implementação ou dissertações teóricas detalhadas. Atualize a preferência no ledger e produza dossiês no nível exato desejado.
3. **Extrapolação Arquitetural**:
   - Transforme as garantias teóricas descobertas em papers (ex.: consistência linear, ordenação causal) em asserções formais de teste na escada do `validation-harness`.


