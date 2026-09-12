---
name: build-dark-factory
description: Adota ou inicia qualquer repositório greenfield ou brownfield pela Project Adoption Gateway transacional da Dark Factory. Instala runtime namespaced com proveniência verificável, preserva contratos do produto e prepara worktrees de demanda sem misturar roadmaps. Use quando o usuário quiser começar um projeto ou colocar a fábrica para desenvolver um projeto existente.
---

# 07 - Build Dark Factory: Adoção Nativa de Projetos

Esta skill conecta qualquer produto de software à Dark Factory compartilhada de forma transacional e namespaced, fixando proveniência auditável sem contaminar a árvore de trabalho principal.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Diretório do Produto Alvo**: Caminho do repositório brownfield ou pasta vazia para greenfield.
- **Configurações de Adoção**: Arquivos opcionais de missão (`--mission-file`), regras (`--rules-file`) e harness (`--harness-file`).
- **Branch de Adoção**: Nome da branch dedicada (ex: `codex/darkfac-adoption`).

### Ações e Procedimento Executável
1. **Fluxo Obrigatório Transacional**:
   ```text
   inspect -> plan -> worktree isolada -> apply -> verify -> commit -> prepare-task
   ```
2. **Execução Headless para Brownfield**:
   ```powershell
   python -m core.adoption.cli inspect C:\dev\Produto
   python -m core.adoption.cli plan C:\dev\Produto
   python -m core.adoption.cli adopt C:\dev\Produto --branch codex/darkfac-adoption
   ```
3. **Execução Headless para Greenfield**:
   ```powershell
   python -m core.adoption.cli init C:\dev\NovoProduto --name NovoProduto
   ```
4. **Validação de Portão de Prontidão da Adoção**:
   - Execute a checagem formal:
     ```powershell
     python -m core.adoption.cli verify <worktree>
     ```
   - Execute o harness isolado do produto:
     ```powershell
     python .factory/darkfac.py harness --quick
     ```

### Outputs Estruturados
- **Runtime Namespaced**: Instalação isolada em `.factory/runtime/` no produto.
- **Lockfile de Proveniência**: `.factory/darkfac.lock.json` registrando hashes exatos e contratos preservados.
- **Relatório de Adoção**: Confirmação estruturada com `ready: true` e diagnóstico do harness.

### Portões, Política e Validação
- **Portão Pré-Demanda**: O `ReadinessGate` do produto rejeita qualquer ticket se `core.adoption.cli verify` não retornar `ready: true` ou se o harness não emitir `[HARNESS_PASS]`.
- **Dial de Autonomia Controlado**:
  - Nível 2 (Padrão Seguro): Testes e validações rodam de forma autônoma; o merge é manual.
  - Nível 3: Auto-merge estritamente condicionado a portões e revisões verdes, após autorização explícita do owner.
- **Isolamento em Worktrees**: Proibida qualquer cópia manual de pastas ou operação em branch suja.

---

## 2. Continuous Self-Improvement & RCA de Adoção

- **Instalação do Loop Mestre**: O runtime adotado inclui o pacote `core.learning`, com registros direcionados para o `.factory/` do produto.
- **RCA em Travamento de Implantação**: Se uma adoção falhar por dependências de sistema ou oráculos incompatíveis, execute RCA imediatamente antes de qualquer nova tentativa.
