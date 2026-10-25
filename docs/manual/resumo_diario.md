<!-- covers: core/notifications/daily_digest.py -->
# Resumo diario ao owner

Duas mensagens por dia no bot do Telegram (mesmo envio da linha, `default_telegram_sender`):

- Manha (a partir das 08:00 de Sao Paulo, ate 12:00): tickets prontos por bloco (max. 8), em voo, decisoes e acoes do owner abertas por prioridade.
- Noite (a partir das 21:00): entregas das ultimas 24 h com PR, learning pack e paginas do manual alteradas.

Links: fila do owner `/#owner-actions` e demandas `/#demands` no DarkHub (o frontend e por hash; nao ha pagina por ticket), learning pack em `/api/learning-packs/<pack_id>/html`, PR no GitHub. O learning pack so aparece se o indice de packs citar o ticket.

CLI (nao envia com `--dry-run`):

```powershell
python -m core.notifications.daily_digest morning --dry-run
python -m core.notifications.daily_digest evening
```

Agendamento: `python -m core.line.fleet_dispatcher --loop` chama `maybe_send_due` a cada ciclo. O marcador `.factory/daily_digest_state.json` garante no maximo um envio por tipo por dia; so marca se o envio deu certo. Sem token do Telegram, nada e enviado e nada e marcado.
