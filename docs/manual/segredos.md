# Segredos: cofre SOPS + age (passo a passo, Windows)

1. Instale as ferramentas no PowerShell: `winget install FiloSottile.age` e `winget install Mozilla.SOPS`. Feche e reabra o PowerShell.
2. Gere sua chave: `New-Item -ItemType Directory -Force $env:APPDATA\sops\age; age-keygen -o $env:APPDATA\sops\age\keys.txt`. O comando imprime `Public key: age1...`; copie esse valor.
3. Aponte o SOPS para a chave (uma vez por maquina): `setx SOPS_AGE_KEY_FILE "$env:APPDATA\sops\age\keys.txt"` e reabra o PowerShell.
4. Abra `C:\dev\DarkFac\.sops.yaml` e coloque a chave publica em `age: "age1..."` (varias maquinas: separe por virgula).
5. Cadastre os segredos: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py`. Ele pergunta, sem eco, so as chaves que faltam (lista em `.factory/config/secrets_required.json`) e grava `secrets\factory.enc.yaml` cifrado.
6. Confira: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py --check` (codigo 0 = tudo cadastrado).
7. Em outra maquina: instale as ferramentas, gere a chave dela, adicione a chave publica ao `.sops.yaml` e rode `sops updatekeys C:\dev\DarkFac\secrets\factory.enc.yaml` numa maquina que ja le o cofre.

Nunca copie `keys.txt` para o repositorio. Agentes de codigo nao recebem `SOPS_*` nem `AGE_*`; so os processos da fabrica (broker, Hub, despachante) leem o cofre via `core.infra.secrets_vault.load_into_env()`.
