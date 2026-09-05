# MISSION.md — Objetivo & Limites de Escopo do Projeto

Este arquivo define formalmente o que está sendo construído e, mais importante, o que **NUNCA** será construído.

> [!IMPORTANT]
> **Arquivo Protegido**: Este arquivo NÃO pode ser alterado por agentes autônomos. Apenas um commit direto de um desenvolvedor humano pode alterar o escopo.

---

## 1. O Problema & A Visão

- **O que estamos resolvendo**: [Descreva sucintamente o problema real]
- **Para quem**: [Usuários-alvo primários]
- **Resultado Esperado**: [O valor observável gerado]

---

## 2. A Jornada Principal (The Core Happy Path)

Passo a passo da ação mais valiosa realizada pelo usuário:
1. O usuário aciona o sistema via [CLI/HTTP/API].
2. O sistema processa [dados de entrada].
3. O resultado observável é [saída esperada e estado persistido].

---

## 3. Escopo em Aberto (In-Scope Atual)

- Funcionalidade A
- Funcionalidade B
- Funcionalidade C

---

## 4. Fora de Escopo Para Sempre (Non-Goals Inegociáveis)

> [!WARNING]
> O agente autônomo deve rejeitar qualquer solicitação, issue ou PR que tente implementar itens desta lista.

- **Non-Goal 1**: [Ex: Interface gráfica nativa ou renderização no browser se for um microserviço headless]
- **Non-Goal 2**: [Ex: Autenticação proprietária complexa antes de validar o MVP]
- **Non-Goal 3**: [Ex: Otimizações prematuras de banco de dados distribuído]
- **Non-Goal 4**: [Ex: Funcionalidades colaterais não relacionadas ao core]
