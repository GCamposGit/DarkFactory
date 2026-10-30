# Proposta governada: Antigravity como interface (USR-433)

`AGENTS.md` e protegido. Ele nao foi alterado nesta worktree. O diff proposto esta em `docs/proposals/USR-433-antigravity-operation.patch`; altera somente a regra final de harness para explicitar que Antigravity e somente leitura no headless e pode escrever quando atua como interface interativa.

## Registro de autorizacao

Foi preparado o registro sem assinatura em `.factory/governance/authorizations/AUTH-USR-433-AGENTS.json`:

- Ticket: `USR-433`.
- Paths exatos: `AGENTS.md`.
- Tipo: `tree`.
- SHA da arvore candidata (base `HEAD` 4e69c19a0310cea0f2bddde3f84ed656f8d21b92 com apenas o patch protegido aplicado): `8ad183bd7dabbf4ad32a3d25e118dace05d34536`.
- Validade: ate 30/10/2026 23:59 UTC.

O registro e um rascunho para este SHA. Se a base ou qualquer conteudo do candidato mudar, gere novamente o SHA e atualize o registro antes de assinar; assinatura de candidato diferente nao autoriza a mudanca. A chave privada nunca deve ser copiada para o repositorio.

## Aplicacao pelo owner

No PowerShell, na revisao correspondente ao SHA acima, aplique o patch e assine o JSON:

```powershell
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-433-antigravity-operation.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-433-antigravity-operation.patch
python -m core.governance.keys sign C:\dev\DarkFac\.factory\governance\authorizations\AUTH-USR-433-AGENTS.json
```

A assinatura deve ocorrer depois de confirmar que `git rev-parse HEAD^{tree}` corresponde ao SHA `candidate` no registro e que o diff contem somente a mudanca autorizada. Apos a assinatura, o fluxo da fabrica publica o registro na base confiavel e verifica o guard. Nao assine se a arvore divergir.
