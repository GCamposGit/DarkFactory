# DF-07 & DF-08 — Segurança e Contenção do DarkHub

## Resultado

Os tickets **DF-07** e **DF-08** foram implementados com sucesso, remediando integralmente as vulnerabilidades de segurança **F06** (XSS persistente no catálogo) e **F07** (superfície local sem fronteiras de acesso e SSRF).

### DF-07: Proteção contra XSS e injeção no navegador
- **Backend (`hub/backend/models.py`)**:
  - Validadores Pydantic v2 com `@field_validator` em `ServiceItem`, `ServiceCreate`, `ServiceUpdate` e `ImportCatalogRequest`.
  - Protocolos restritos estritamente a `http` e `https`. Rejeição explícita de `javascript:`, `data:`, `vbscript:`, `file:`, `ftp:`, caracteres de controle e null bytes.
  - Cores validadas estritamente contra formato hexadecimal seguro (`#RGB`, `#RGBA`, `#RRGGBB`, `#RRGGBBAA`), bloqueando injeção em estilos CSS e quebra de atributos.
  - IDs/slugs validados contra padrão seguro `^[a-zA-Z0-9_-]{1,100}$`, bloqueando aspas, tags HTML, espaços e injeções de script.
- **Frontend (`hub/frontend/app.js`)**:
  - Introduzidos helpers universais: `escapeHtml`, `sanitizeUrl`, `sanitizeColor`, `sanitizeId`.
  - Remoção de handlers inline com variáveis interpoladas (`onclick="deleteService('${item.id}')"`, `onclick="copyToClipboard('${item.url}')"`).
  - Adoção de atributos `data-action` / `data-service-id` com delegação de eventos segura via `addEventListener` nos containers (`services-grid` e `prompt-vault-list`).
  - Renderização segura em `renderQuickDock`, `renderServices`, `renderPaletteResults` e `renderPromptsList`, garantindo que texto ativo (`<script>`, `<img onerror>`, etc.) permaneça 100% inerte no DOM.
- **Dependências (`requirements.txt`)**:
  - Declarado `lxml>=5,<7` para validação determinística de parsing DOM/HTML nos testes.

### DF-08: Contenção de rotas, SSRF e restrição de Host/Origem
- **Fronteira de Acesso (`hub/backend/main.py`)**:
  - Restrição de CORS via `allow_origin_regex` para origens loopback confiáveis (`localhost`, `127.0.0.1`, `[::1]`, `testserver`).
  - Middleware ASGI `SecurityContainmentMiddleware`:
    - Validação de `Host`: hosts externos (ex: `evil.com`, `attacker.local`) rejeitados com HTTP 400.
    - Validação de `Origin`: origens cross-origin não-autorizadas rejeitadas com HTTP 403.
    - Validação de sessão: tokens de sessão `X-Hub-Session` inválidos rejeitados com HTTP 401.
- **Política de Destinos e Proteção SSRF (`hub/backend/service.py`)**:
  - Função `is_destination_allowed(url: str)`: bloqueia cloud metadata (`169.254.169.254`, `169.254.170.2`, `169.254.0.0/16`), nomes de host de metadados (`metadata.google.internal`, `instance-data`), broadcast, redes reservadas e esquemas não-HTTP.
  - `SafeRedirectHandler(urllib.request.HTTPRedirectHandler)`: intercepta cabeçalhos `Location` de respostas 3xx (redirecionamentos) e bloqueia tentativas de SSRF bypass via redirect para metadados ou IPs proibidos (`DisallowedDestinationError`).
  - `ping_url`: usa `SafeRedirectHandler` e `is_destination_allowed`. Falhas de política retornam status OFFLINE seguro com erro estruturado.
  - Gerenciamento de sessão: `session_token`, `validate_session`, `rotate_session`.
- **API e Rotas (`hub/backend/api.py`)**:
  - `/api/health/ping`: exige `service_id` cadastrado no catálogo e valida que a URL opcionalmente fornecida corresponde à URL do serviço registrado. Sondagens arbitrárias de URLs não cadastradas são rejeitadas.
  - `/api/session`: expõe token de sessão ativo local para o cliente.

## Arquivos Modificados / Criados

- `hub/backend/models.py`
- `hub/frontend/app.js`
- `requirements.txt`
- `hub/backend/main.py`
- `hub/backend/service.py`
- `hub/backend/api.py`
- `tests/test_hub_browser.py` (Novo)
- `tests/test_hub_access.py` (Novo)

## Validação e Gates Determinísticos

1. `python -m pytest tests/test_hub_browser.py -v`: **10 passed** (0.10s).
2. `python -m pytest tests/test_hub_access.py -v`: **7 passed** (0.40s).
3. `python -m pytest tests/test_hub.py -v`: **10 passed** (0.39s) — regressão zero.
4. `python core/harness/runner.py --quick`: **273 passed, 1 skipped**, `[HARNESS_PASS]`.
5. `python -m pytest tests -v --ignore=tests/test_canaletto.py`: **291 passed, 1 skipped** (20.72s).
