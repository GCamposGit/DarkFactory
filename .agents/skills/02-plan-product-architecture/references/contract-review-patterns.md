# Padrões de contratos verificáveis

Aplicar seletivamente ao especificar ou revisar gates, evidências, persistência e interfaces. Derivado de falhas reproduzidas no ciclo HF; estes padrões não são uma certificação de que os módulos já foram corrigidos.

## 1. Declaração, evidência e autoridade

Um schema pode aceitar `passed`, `current`, `approved`, `high`, `resolved` ou `independent_review`; isso não prova o fato. Declare quem produz cada informação, como o consumidor autentica sua origem e quais dados compara.

Requisitos obrigatórios vêm da política confiável, não da lista removível pelo candidato. Defina a cardinalidade mínima por estágio e uma dispensa tipada e autorizada quando zero é legítimo. Não promover uma coleção vazia por `all([])` ou ausência de motivos.

Referência de artefato só é evidência após resolução e validação de integridade/produtor/escopo. Comparar SHA com baseline não demonstra PR, merge ou alcançabilidade no remoto. `health=ok` não comprova jornada, configuração ou operação do produto.

## 2. Evidência temporal e causal

Vincular evidência ao sujeito, requisito, plano/política, build/candidate, configuração, ambiente, identidade e rota aplicáveis. Declare a fonte esperada de cada comparação; apenas acrescentar campos sem verificá-los não implementa o gate.

Usar `now` explícito e política de validade/versionamento para calcular freshness. Testar tempo antigo/futuro, timezone ausente, config/credencial/identidade alterada e prova de outro requisito. Não confiar em `current` enviado pelo produtor.

Resposta humana pode acionar probe; não substitui seu resultado. A resolução precisa de receipt do probe correto, posterior à mudança e da identidade/alvo exigidos. Repetição deve ser idempotente e bloquear apenas descendentes afetados.

## 3. Etapas e transições

Declare **quando a prova passa a existir** e **quando é exigida**. Uma evidência operacional não pode ser condição circular para começar a implementação que a produz. Consuma `required_for` ou campo equivalente de verdade; teste o fluxo válido completo e a tentativa de pular estágio.

Uma tabela de transições isolada não protege outra API que avança estado. Faça o consumidor público passar pela mesma validação, distinguindo inspeção do estado atual, proposta de transição e confirmação. Cancelado/failed não recebem prontidão de release por um fallback genérico.

Estados de produto, execução e prontidão têm semânticas distintas; não deduza revisão independente ou operação apenas do nome do estado. Migração entre versões precisa de regra explícita.

## 4. Schemas e fronteira de transporte

`extra='forbid'` fecha campos adicionais, mas não torna tipos estritos, objetos profundamente imutáveis nem fontes confiáveis. Declare coerções permitidas por campo; preserve booleanos/inteiros de controle conforme a política, admitindo enums/datas do JSON pela rota adequada.

Teste constructor, `model_validate`, `model_validate_json` e leitura real pelo processo consumidor quando fazem parte da API. Um validador `before` que transforma string em Path pode contrariar strict JSON; prove o roundtrip do objeto exato e o comando que o lê. Não remover strict globalmente para corrigir um único campo sem examinar a semântica restante.

Defina a fronteira de confiança para instâncias existentes, cópias e mutação de listas/objetos aninhados. Não use `model_construct` ou `model_copy(update=...)` como validação de dados não confiáveis. Revalide ou construa um valor imutável controlado no ponto de consumo.

As regras de coerção variam por entrada e campo; conferir versão instalada e documentação oficial de [strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/) e [models](https://docs.pydantic.dev/latest/concepts/models/). Recomendação local: escrever casos da API realmente usada, sem assumir equivalência entre modos.

## 5. Parsing, replay e eventos

Não completar um identificador já qualificado com outro prefixo. Testar referências intra/intermódulo, duplicadas, desconhecidas e ambíguas. Conflitos e fontes obrigatórias ausentes afetam a prontidão; avisos não podem ser descartados por um leitor de campos incompletos.

Verificação de replay recompõe os resultados derivados a partir das entradas confiáveis ou verifica atestado independente. Recalcular um hash fornecido pelo mesmo arquivo adulterável não autentica sua interpretação. Ciclo, conflito e mudança de readiness/assessment devem ser detectados.

Efeitos e aprovações vinculam operação lógica, workflow e digest; mudar o ID de uma decisão não autoriza aprovar outro digest. Evento/ack antigo ou duplicado não avança candidato novo. Catálogo de cenários é protegido do adaptador; caso unsupported continua unsupported.

## 6. Segredos e diagnósticos

Preferir campos de referência seguros e minimizar texto livre. Se o contrato promete não serializar credenciais, cobrir URLs com query/userinfo e todos os caminhos de exportação/log, inclusive erro de validação. Usar somente sentinelas sintéticas em testes.

Regex de nomes conhecidos não garante detectar toda forma de segredo. Declarar limite e testar variantes plausíveis; mensagens/relatórios devem omitir valores brutos. Não imprimir entradas sensíveis para explicar que foram rejeitadas.

## 7. Oráculos independentes e linguagem da entrega

Para cada garantia crítica escrever um caminho válido e um contraexemplo que atravessem a API pública. Testar falsos positivos e falsos negativos; fixtures não podem escrever o mesmo status final que o sistema deveria verificar.

Separar código integrado, teste unitário, transporte/processo real, integração com alvo e operação. Uma auditoria local pode concluir que existe um bug; não precisa corrigir o código do implementador nem alegar aprovação. Vincular achado → reprodução → ticket de reparo → verificador. Alterações de skill passam por cenário independente; testes de headings/palavras não demonstram eficácia comportamental.
