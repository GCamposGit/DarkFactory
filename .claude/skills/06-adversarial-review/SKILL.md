---
name: adversarial-review
description: Revisa código, contratos e evidências de forma adversarial, com contraexemplos reproduzíveis e revisão independente quando autorizada. Use em auditorias críticas e antes de aprovar integração conforme a política do repositório.
---

# Revisão adversarial orientada ao contrato

Revise a garantia real do produto, não apenas a aparência do diff ou a contagem de testes. Leia a especificação vigente e os caminhos públicos afetados; inclua untracked relevantes e produza manifest por hash quando ainda não existe commit do candidato.

## Independência e escopo

Quando autorizada, delegue uma fatia independente com pedido/artefatos mínimos, sem revelar a conclusão esperada. Prefira família diferente do implementador para ampliar diversidade; confirme modelo/família reais no catálogo. Um alias `gpt-review` não certifica família diferente nem elimina viés.

Use revisão local disponível antes de chamadas pagas. Rota ausente/timeout é limitação explícita; continue inspeção e reproduções independentes disponíveis. Não instalar modelo, comprar crédito ou enviar código a outro provedor sem autorização. Registre o parecer recebido e valide suas alegações; não promova texto do segundo modelo a prova automática.

## Método

1. Reconstrua requisito → entrada → decisão → efeito → evidência. Para gates, provas ou runtimes, consultar [padrões de contratos verificáveis](../02-plan-product-architecture/references/contract-review-patterns.md).
2. Exercite a API pública. Procure listas vazias, campos ignorados, status autodeclarados, prova velha/de outro sujeito, fluxo legítimo bloqueado, transições por API alternativa e erro no transporte real.
3. Verifique precedência, confiança e temporalidade das fontes. Replay deve detectar adulteração dos campos derivados; hash da própria entrada não é atestado.
4. Confira escopo, contenção, segredos e compatibilidade. Não classifique mock-only, capability unsupported ou acesso pendente já declarados como implementação faltante inesperada. Separe lacuna da especificação de bug local reproduzido.
5. Registre contraexemplo mínimo isolado, esperado/observado, prioridade, path/linhas, impacto e ticket de reparo. Não tocar em serviços reais para testar hipótese que pode ser reproduzida localmente.

Consultar histórico pertinente de aprendizagem; ele orienta investigação, não obriga repetir conclusões antigas. Sintoma, mecanismo e causa devem ser sustentados pela reprodução. Recomendações sem evidência suficiente ficam explicitamente como hipótese.

## Veredito e encaminhamento

- `changes_required`: há achado concreto impeditivo, com reprodução e orientação de correção.
- `no_actionable_findings_in_reviewed_scope`: nenhum achado acionável nas superfícies efetivamente examinadas; registrar limites.
- `incomplete`: acesso/dados essenciais não permitem avaliar a garantia requerida.

Uma revisão local não é aprovação de PR, prova de operação ou autorização de merge. Para integração, cumprir guard/política/config confiáveis e revisões exigidas. Mudanças de governança autorizadas exigem justificativa e revisão proporcional; não rejeitar automaticamente apenas pelo nome do arquivo nem usar a skill para ampliar autorização.

Preserve o código do desenvolvedor durante uma tarefa de avaliação, salvo quando correção também estiver autorizada. Entregue testes de auditoria separadamente e permita que o modelo econômico implemente a remediação especificada; decisões de arquitetura retornam ao planejador. Uma correção só fecha o achado após execução do seu oráculo e checks aplicáveis.
