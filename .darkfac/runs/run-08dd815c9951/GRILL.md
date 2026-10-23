# GRILL

## Premissas
- Esta rodada é somente de leitura e esclarecimento; não inclui implementação, gravação de arquivos, execução de probes ou validação.
- A correção abrange core/line/routing.py, os adaptadores de uso em core/usage/adapters.py e a configuração canônica .factory/config/line_routing.json. A cascata development atual contém Claude e Codex; um harness_pin de Grok sozinho não o inclui como candidato.
- Grok deve participar da seleção de development quando disponível no host e com capacidade write, preservando a preferência pelo harness de operação e o ranking existentes.
- O piso permanece estrito: cota efetiva de 15% ou menos é inelegível. Cota acima de 15% não elimina verificações de disponibilidade, capacidade write, cooldown, exclusões explícitas, modelos proibidos e validade da medição; cada descarte deve informar o motivo.
- Probe Anthropic só permite renovar o snapshot com medição nova e verificável de cota. Sucesso de autenticação ou conectividade sem medição não autoriza atualizar artificialmente checked_at nem reutilizar cota antiga como nova.
- No Windows, DARKFAC_CODEX_HEADLESS_ALLOWED ausente permite Codex; valores afirmativos continuam permitindo e valores negativos explícitos representam opt-out. Os demais filtros de elegibilidade permanecem.
- Os logs devem explicar também ausência na cascata, ausência de capacidade no host, exclusões explícitas e cooldown, sem expor credenciais.
- Os testes devem simular o incidente de 22/10 com snapshot Anthropic de aproximadamente 115538 segundos, probe bem-sucedido e Grok com 17,4%, além de cobrir Grok como única capacidade, Codex no Windows sem variável e com opt-out, e os limites de cota. A implementação posterior exige o portão oficial runner.py --quick.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
