# Portão elástico

O ticket recebe feedback rápido no host que o executa: perfil PR com afetados,
contratos e fumaça. A suíte completa continua sendo a prova no CI de main.

A seleção percorre imports transitivos e referências desde o merge-base com
origin/main; por isso inclui todos os commits da branch. Configuração global,
grafo inválido, base ausente e cobertura desconhecida tornam essa inferência
insuficiente: a execução registra affected_fallback e testa fumaça fixa.
Uma aprovação local não afirma cobertura completa.

Na memória, processo único economiza o controlador/gateway do xdist. PR aceita
1,5 GiB livres; full preserva reserva de 2 GiB mais 2 GiB por worker. Queda de
worker é infraestrutura: uma tentativa serial no mesmo prazo distingue crash de
falha real. A validação continua ligada ao candidato imutável e ao marcador PASS.

Perguntas de revisão:
- Por que não usar HEAD~1? Perderia alterações de commits anteriores do ticket.
- Por que -n 0 com um worker? Evita criar worker e gateway sem paralelismo útil.
- O que prova o fallback? Fumaça e contratos; a suite completa depende do CI.
- O que faz required? Só impede fallback local no escopo full explícito.
