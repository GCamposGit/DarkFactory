---
name: topic-deep-research
description: Executa pesquisa aprofundada de conceitos, melhores práticas, arquitetura técnica e papers científicos recentes (arXiv) de alta credibilidade. Salva todas as fontes e insights estruturados no Knowledge Ledger (.factory/research/) para referência perpétua da aplicação. Use ao pesquisar fundamentos teóricos, algoritmos complexos ou novas arquiteturas.
---

# 10 - Topic Deep Research: Fundamentação Teórica & Papers

Esta skill orienta a investigação estruturada de conceitos de engenharia, arquiteturas avançadas, RFCs e literatura científica recente, assegurando que as decisões técnicas do DarkFac sejam fundamentadas no estado da arte e auditáveis por versões futuras.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Tema da Investigação**: Consulta ou requisito arquitetural complexo (algoritmos distribuídos, criptografia, concorrência, etc.).
- **Classificação de Intenção**: Identificação se a demanda é conceitual, algorítmica ou de tooling.
- **Tiers de Autoridade Exigidos**: Nível canônico (papers, RFCs, documentações oficiais) ou tendências de comunidade.

### Ações e Procedimento Executável
1. **Classificação e Refinamento de Intenção**:
   ```bash
   python core/research/cli.py classify "Pergunta ou tema de pesquisa"
   ```
2. **Busca Headless de Papers Científicos (arXiv)**:
   ```bash
   python core/research/cli.py papers "termo técnico em inglês" --limit 5
   ```
3. **Radar de Tendências da Comunidade**:
   ```bash
   python core/research/cli.py trends "tecnologia ou ferramenta" --limit 3
   ```
4. **Síntese Automatizada no Knowledge Ledger**:
   ```bash
   python core/research/cli.py auto "Explicação técnica e papers sobre tema X" --limit 5
   ```
5. **Anotação de Proveniência no Código**:
   Ao implementar código derivado da pesquisa, inclua o cabeçalho auditável:
   ```python
   # [RESEARCH PROVENANCE & INSIGHTS]
   # Ledger ID: <slug>
   # Audit Doc: .factory/research/<slug>/INSIGHTS.md
   # Canonical Sources: .factory/research/<slug>/ledger.json
   ```

### Outputs Estruturados
- **Dossiê Auditável**: `.factory/research/<slug>/INSIGHTS.md` contendo garantias matemáticas, trade-offs e limitações.
- **Knowledge Ledger Estruturado**: `.factory/research/<slug>/ledger.json` com metadados canônicos (DOIs, URLs canônicas, autores, datas).
- **Insumos para Planejamento**: Fatos e restrições incorporáveis ao `GrillRecord.known_facts` e `WorkflowHandoff.resource_requirements`.

### Portões, Política e Validação
- **Princípio dos Dois Tiers**:
  - *Tier de Tendências (Experts/Comunidade)*: Usado exclusivamente para ideação de features e priorização.
  - *Tier Canônico de Alta Credibilidade (Papers/RFCs)*: Obrigatório para fundamentação arquitetural, integridade de dados e segurança. Nenhuma feature pode ter arquitetura baseada em posts informais.
- **Proibição Estrita de Alucinação**: Toda referência bibliográfica deve conter URL canônica e identificador real verificável.
- **Persistência Obrigatória**: Nenhuma decisão arquitetural crítica pode ser implementada sem o respectivo registro persistido no Knowledge Ledger.

---

## 2. Continuous Self-Improvement & RCA de Pesquisa

- **RCA em Falhas de Descoberta**: Se uma busca no arXiv retornar 0 resultados úteis, execute RCA para identificar lacunas de terminologia e registrar os termos canônicos no ledger.
