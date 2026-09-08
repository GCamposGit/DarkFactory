---
name: build-dark-factory
description: Adota ou inicia qualquer repositório greenfield ou brownfield pela Project Adoption Gateway transacional da Dark Factory. Instala runtime namespaced com proveniência verificável, preserva contratos do produto e prepara worktrees de demanda sem misturar roadmaps. Use quando o usuário quiser começar um projeto ou colocar a fábrica para desenvolver um projeto existente.
---

# Build Dark Factory: adoção nativa de projetos

Esta skill conecta um produto à Dark Factory compartilhada. **Nunca copie diretórios manualmente nem importe um checkout irmão.** Use `core.adoption.cli`; ele isola a operação numa worktree, instala a fábrica em `.factory/runtime`, preserva o ownership do produto e fixa proveniência em `.factory/darkfac.lock.json`.

## O Dial de Autonomia (The Autonomy Dial)

| Nível | O que é Automático | O que o Humano Faz |
| :--- | :--- | :--- |
| **0** | Workflows e scripts existem | Executa tudo manualmente |
| **1** | Issue rotulada -> PR abre | Revisa diff e faz merge manual |
| **2** | Validador roda e emite veredito | Faz merge manual |
| **3 (Padrão)** | **Auto-merge quando todos os portões e revisões forem verdes** | Escreve issues/PRD e faz releases |
| **4** | Sistema faz triagem e gera seus próprios testes de estresse | Escreve issues de alto nível |
| **5** | Sistema cria as próprias issues a partir da MISSION | Apenas monitora resultados |

> O nível é uma permissão do produto, não uma promessa da instalação. O padrão seguro do gateway é nível 2. Auto-merge, deploy, agendamento, credenciais e chamadas pagas permanecem fora de escopo até autorização explícita.

## Fluxo obrigatório

```text
inspect -> plan -> worktree isolada -> apply -> verify -> commit -> prepare-task
```

## Brownfield

```powershell
python -m core.adoption.cli inspect C:\dev\Produto
python -m core.adoption.cli plan C:\dev\Produto
python -m core.adoption.cli adopt C:\dev\Produto --branch codex/darkfac-adoption
```

Mesmo que o checkout principal esteja sujo, `adopt` usa apenas o commit solicitado. Para migrar arquivos de governança já preparados, forneça explicitamente `--mission-file`, `--rules-file` e `--harness-file`. Nenhum outro overlay é aceito.

## Greenfield

```powershell
python -m core.adoption.cli init C:\dev\NovoProduto --name NovoProduto
```

O destino precisa estar vazio. Sem stack/harness determinístico, a instalação não declara readiness. Revise os TODOs de governança antes de permitir trabalho autônomo.

## Portões antes da primeira demanda

1. `python -m core.adoption.cli verify <worktree>` precisa retornar `ready: true`.
2. Execute `python .factory/darkfac.py harness --quick` dentro do produto e exija `[HARNESS_PASS]` com contagem positiva.
3. Revise e faça commit da adoção.
4. Crie a demanda com `python -m core.adoption.cli prepare-task ...`, declarando owner, caminhos e validações.
5. Só então rode Prime-Plan-Implement-Validate e revisão adversarial.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Instalação do Loop Mestre de Aprendizado (`00-continuous-self-improvement`)**:
   - O runtime namespaced inclui `core/learning/`; sua saída é gravada no `.factory/` do produto por meio da raiz portátil.
   - Skills copiadas têm comandos reescritos para `.factory/darkfac.py`, sem colisão com pacotes `core` do produto.
2. **RCA de Falhas de Implantação e Transição de Estados**:
   - Se uma issue travar no estado `NEEDS_FIX` por mais de 2 voltas, o sistema dispara RCA automático para diagnosticar a causa sistêmica (especificação vaga, dependência quebrada ou teste frágil).
   - O patch é aplicado diretamente na camada de orientação ou no harness antes de retomar a execução autônoma.

Contrato completo: `docs/PROJECT_ADOPTION.md`.
