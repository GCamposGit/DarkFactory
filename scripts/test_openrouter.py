import os, sys, json, time, urllib.request, urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

key = os.environ.get('OPENROUTER_API_KEY')
if not key and sys.platform == 'win32':
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Environment') as k:
            key, _ = winreg.QueryValueEx(k, 'OPENROUTER_API_KEY')
    except Exception:
        pass

if not key:
    print('[ERRO] OPENROUTER_API_KEY nao encontrada.')
    sys.exit(1)

key = key.strip()
print('=' * 60)
print('VALIDACAO DE CONEXAO - OPENROUTER')
print(f'Chave identificada: {key[:12]}...{key[-4:]}')
print('=' * 60)

# 1. Auth check
print('\n[1/2] Verificando credenciais e saldo...')
req_auth = urllib.request.Request(
    'https://openrouter.ai/api/v1/auth/key',
    headers={'Authorization': f'Bearer {key}'}
)
try:
    with urllib.request.urlopen(req_auth, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8')).get('data', {})
        print('  [OK] Autenticado com sucesso!')
        print(f'  [OK] Label da chave: {data.get("label")}')
        print(f'  [OK] Tipo de conta: {"Free Tier" if data.get("is_free_tier") else "Standard / Paid Tier"}')
        print(f'  [OK] Gasto acumulado: ${data.get("usage", 0)}')
except Exception as e:
    print(f'  [FALHA] Erro na autenticacao: {e}')
    sys.exit(1)

# 2. Chat completion check
print('\n[2/2] Testando inferencia em tempo real (Chat Completion)...')
payload = {
    'model': 'nvidia/nemotron-3.5-lightning:free',
    'messages': [{'role': 'user', 'content': 'Diga: OpenRouter conectado!'}],
    'max_tokens': 30
}
req_chat = urllib.request.Request(
    'https://openrouter.ai/api/v1/chat/completions',
    headers={
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://github.com/DarkFac'
    },
    data=json.dumps(payload).encode('utf-8')
)
t0 = time.time()
try:
    with urllib.request.urlopen(req_chat, timeout=20) as resp:
        duration_ms = round((time.time() - t0) * 1000, 2)
        res = json.loads(resp.read().decode('utf-8'))
        print(f'  [OK] Resposta recebida em {duration_ms}ms!')
        print(f'  [OK] Modelo utilizado: {res.get("model")}')
        print(f'  [OK] Total de tokens processados: {res.get("usage", {}).get("total_tokens", 0)}')
except Exception as e:
    print(f'  [FALHA] Erro no chat completion: {e}')
    sys.exit(1)

print('\n' + '=' * 60)
print('FLUXO DO OPENROUTER TESTADO E 100% OPERACIONAL!')
print('=' * 60)
