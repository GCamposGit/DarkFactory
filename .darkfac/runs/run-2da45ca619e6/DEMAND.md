# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Investigar demora nao limitada do preflight de cota e wake

## Problema
Sessao de operacao observou run_ticket.py USR-318 --dry-run --json iniciado 16:55 e retornando aproximadamente 17:55 de 20/10/2026 durante sondagem de cota/wake; tentativa automatica de email do grokbox observada. Nova tentativa completou em38s. Causa ainda nao demonstrada; distinguir hibernacao, wrappers e timeout de rede antes de corrigir. Inspecao de deliver_parallel.py confirma rc7 escreve queue.txt aposentada, runner_grok reenfileira myqueue sem mutex; kind implementation falha em ledger-only USR315; runner_local usa gate local forcado e piso5.5GB contradiz scheduler7.5GB. Scripts operacionais fora do repo em C:/Users/guigc/darkfac-ops.

[Decisao do owner OA-048] Autorizar revisão independente USR-321 pelo Grok -> opcao A (Autorizar Grok), em 2026-10-20T23:41:40Z. Nota do owner: Owner respondeu a pergunta explicita de envio de arquivos/diff privados USR-321 ao Grok: Autorizar revisao pelo Grok. Evidencia recebida pelo coordenador nesta sessao.

## Jornada
Investigar demora nao limitada do preflight de cota e wake

## Fora de escopo
- Nao relaxar piso de cota, Gate G1 ou isolamento de projetos.

## Criterios de aceite
- Medir tempos por provedor e wake com relogio monotonic e reproduzir lentidao ou documentar causa observada.
- Sondagens externas falham em prazo configurado com erro estruturado sem implementar com cota desconhecida.
- Unificar reenqueue sob mutex na fila ativa e classificar corretamente entrega ledger-only.
- Limitar despacho local a capacidade verificada e preservar workers ocupados.
