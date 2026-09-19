---
name: second-brain-knowledge
description: Conector headless e cliente MCP para consulta e ingestão na base de conhecimento perpétua do Segundo Cérebro (C:\dev\SegundoCerebro). Recupera trechos e citações verificáveis (arquivo, seção, página/slide, hash SHA-256) com garantia estrita anti-alucinação (Fail-Closed) e isolamento multi-projeto (darkfac, atrium, jarvis, shared). Suporta ingestão de documentos Office (.docx, .xlsx, .pptx), PDFs, notas Markdown e transcrições de áudio.
---

# 18 - Segundo Cérebro & Conector MCP de Conhecimento Perpétuo

A skill **second-brain-knowledge** capacita qualquer agente da Dark Factory (e do Jarvis) a consultar o acervo maduro do **Segundo Cérebro** (`C:\dev\SegundoCerebro`) e a ingerir novos documentos sob demanda.

O motor opera com **Custo Marginal $0.00** (busca híbrida BM25 + embeddings locais FastEmbed / E5-large) e aplica uma política inegociável de **Proveniência Estrita e Fail-Closed**:
- Nenhuma alucinação é permitida: se o limiar de relevância não for satisfeito, a resposta reporta explicitamente `INSUFFICIENT_EVIDENCE`.
- Todo trecho retornado traz arquivo de origem, localização precisa e hash criptográfico de integridade.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Consulta (`query`)**: Pergunta em linguagem natural ou termos de busca.
- **Escopo do Projeto (`project_id`)**: `darkfac`, `atrium`, `jarvis` ou `shared`.
- **Limiar Mínimo (`min_score`)**: Default `0.55`. Corta qualquer passagem fraca.
- **Documento para Ingestão**: Caminho para arquivo Office (`.docx`, `.xlsx`, `.pptx`), PDF (`.pdf`), Markdown (`.md`) ou áudio transcrito.

### Ações e Procedimento Executável

1. **Checagem de Saúde e Conectividade do Servidor MCP**:
   ```powershell
   python C:\dev\DarkFac\core\knowledge\cli.py status
   ```

2. **Busca Semântica & Lexical (com Segregação por Projeto)**:
   ```powershell
   python C:\dev\DarkFac\core\knowledge\cli.py search "pergunta sobre regra de negócio" --project jarvis --min-score 0.60
   ```
   *Saída JSON estruturada*:
   ```powershell
   python C:\dev\DarkFac\core\knowledge\cli.py search "pergunta" --project darkfac --json
   ```

3. **Leitura Detalhada de Contexto por Chunk ID**:
   ```powershell
   python C:\dev\DarkFac\core\knowledge\cli.py read <chunk_id> --window 2
   ```

4. **Ingestão Sob Demanda de Arquivos (Office / PDF / Markdown / Áudio)**:
   ```powershell
   python C:\dev\DarkFac\core\knowledge\cli.py ingest C:\caminho\arquivo.docx --project jarvis
   python C:\dev\DarkFac\core\knowledge\cli.py ingest C:\caminho\planilha.xlsx --project atrium
   python C:\dev\DarkFac\core\knowledge\cli.py ingest C:\caminho\apresentacao.pptx --project darkfac
   ```

5. **Uso Programático em Python (DarkFac ou Jarvis)**:
   ```python
   from core.knowledge import KnowledgeQuery, SegundoCerebroClient

   client = SegundoCerebroClient()
   result = client.search(
       KnowledgeQuery(
           query="Quais as regras de timeout do supervisor?",
           project_id="darkfac",
           min_score=0.60,
       )
   )

   if result.status == "FOUND":
       for citation in result.citations:
           print(f"Fonte: {citation.file_path} ({citation.locator})")
           print(f"Conteúdo: {citation.content}")
           print(f"Hash: {citation.provenance_hash}")
   else:
       print("Nenhuma evidência verificada encontrada no acervo. Operação interrompida para evitar alucinação.")
   ```

---

## 2. Regras de Isolamento Multi-Projeto

- Cada documento pertence a uma partição de corpus (`<project_id>/`).
- Documentos na pasta `shared/` ou sem prefixo de projeto são visíveis por todos os workspaces.
- Documentos dentro de `jarvis/`, `darkfac/` ou `atrium/` são estritamente isolados: um projeto não tem visibilidade sobre arquivos confidenciais de outro cliente.

---

## 3. Critérios de Aceitação e Verificação

1. **Anti-Hallucination Gate**: Agentes nunca devem responder com suposições quando consultarem esta skill. Se `status == "INSUFFICIENT_EVIDENCE"`, declarar expressamente a falta de evidências comprovadas.
2. **Citação Obrigatória**: Todo PRD, especificação ou resposta ao usuário baseada nesta skill deve apontar o link/caminho do arquivo e a seção exata citada.
