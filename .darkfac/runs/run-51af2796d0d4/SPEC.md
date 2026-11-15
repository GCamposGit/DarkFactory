# SPEC

## Objetivo
Produzir, para cada projeto habilitado (com perfil e acesso explícitos), métricas de cobertura do domínio de segurança. Cada controle e cada dependência do universo do perfil cai em exatamente uma de quatro categorias, com precedência fixa: evidência vencida, omitido, desconhecido, avaliado. A base são versões resolvidas do ambiente identificado (lockfile com pins, `pip inspect` do interpretador do venv ou do sistema, ou imagem). Requisito declarado sem versão resolvida conta como desconhecido. Falha de fonte de advisories ou ecossistema sem cobertura conta como desconhecido ou falha, nunca como limpo. Toda métrica leva proveniência: fonte (nome, versão ou data do snapshot, hora da consulta), ambiente (id, método de resolução, hash do lockfile, da saída do inspect ou da imagem) e commit avaliado. A exposição é feita por biblioteca em core/security e por CLI headless com JSON UTF-8. A demonstração é real de ponta a ponta no projeto darkfac.

## Fora de escopo
- Limiares de cobertura (percentual mínimo aceitável) e portões que bloqueiam por cobertura: dependem de baseline e de decisão futura do owner.
- Painel do DarkHub para as métricas: consome a biblioteca depois.
- O ciclo recorrente de qualidade de USR-449 (agendador, fila de achados, remediação): esta entrega só oferece a função de cobertura que o ciclo vai chamar.
- Os demais módulos do roadmap security-quality (SAST, secret scan, IaC, ZAP, red team). Controles desses domínios sem evidência registrada aparecem como desconhecidos, sem implementar os scanners.
- Ecossistemas além de Python/PyPI na primeira entrega. Projetos com outros ecossistemas têm essas dependências contadas como desconhecidas com motivo ecosystem_unsupported.
- Abrir ticket automático de remediação por vulnerabilidade encontrada.

## Desenho
Reconciliação: core/security e QualityCycle não existem no repositório. USR-449 está implementing e cita quality_cycle.py apenas como sugestão; o roadmap docs/plans/security-quality-2026-10-08/roadmap.json reserva core/security/{models,profiles,evidence,evidence_store,sca_adapter}.py. Para não divergir, o pacote nasce em core/security com esses nomes e escopo mínimo, e USR-449 passa a consumir compute_coverage() em vez de reimplementar. (1) models.py, em Pydantic v2: CoverageProfile (project_id, enabled, access_granted, evidence_ttl_hours, controls[], omissions[{item, reason}], environments[{id, kind: venv|system|lockfile|image, path/ref}], storage{kind, path} opcional), CoverageItem (kind: control|dependency, id, status: evaluated|unknown|omitted|stale, reason, evidence_ref), Provenance (source_name, source_version_or_snapshot, queried_at, environment_id, resolution_method, environment_hash, commit_sha), ProjectCoverage (counts, items, provenance, blocked_reason, source_failures[]). (2) profiles.py carrega .factory/security/profiles/<project>.json validado com o ProjectRegistry. Sem perfil, ou com enabled/access_granted falso, o projeto vira blocked com motivo visível e não entra como avaliado. (3) environment_resolution.py resolve versões pelo interpretador do ProjectEnvironment (reusa venv_dir/venv_bin_dir de core/projects/environment.py) via `python -m pip inspect` e por lockfiles com pin exato (requirements com == ou hash, uv.lock). Linha sem pin vira dependência unknown com reason declared_not_resolved. O hash do ambiente é o sha256 do conjunto canônico nome==versão, ou do lockfile. (4) sca_adapter.py consulta OSV.dev /v1/querybatch (sem segredo), com timeout e retry curtos. Resposta ok sem vulns vira evaluated com evidência. Timeout, erro HTTP ou ecossistema não suportado viram unknown com reason source_failure ou ecosystem_unsupported e registro em source_failures. A proveniência guarda o header de data e a hora da consulta. (5) classify.py é a função pura que aplica a precedência stale > omitted > unknown > evaluated. Stale é evidência anterior com idade maior que o TTL do perfil, que nunca herda o resultado anterior. Controles sem registro de evidência viram unknown. (6) evidence_store.py é append-only por projeto no destino durável declarado no perfil (darkfac: .factory/security/coverage/darkfac/), com isolamento por project_id e retenção permanente. Sem destino, o status é storage_pending e o cálculo é retornado sem persistir. (7) coverage.py tem compute_coverage(project_id | None) e load_latest(), e cli.py roda `python -m core.security.coverage [--project X] [--no-persist]`, com JSON UTF-8 no stdout. Códigos de saída: 0 ok, 2 falha de fonte em ao menos um projeto, 3 projeto pedido bloqueado ou sem perfil, 1 erro de uso. (8) alerts.py emite pelo NotificationService existente (DarkHub e Telegram) só quando o fingerprint do estado acionável muda (nova vulnerabilidade, nova falha de fonte, evidência que passou a vencida), com estado persistido para não repetir alerta inalterado.

## Arquivos a tocar
- core/security/__init__.py
- core/security/models.py
- core/security/profiles.py
- core/security/environment_resolution.py
- core/security/sca_adapter.py
- core/security/classify.py
- core/security/evidence_store.py
- core/security/coverage.py
- core/security/__main__.py ou core/security/cli.py
- core/security/alerts.py
- core/projects/environment.py (somente reuso; sem mudar comportamento)
- core/notifications/service.py (reuso; ajuste mínimo se faltar categoria)
- .factory/security/profiles/darkfac.json
- tests/test_security_coverage_classify.py
- tests/test_security_environment_resolution.py
- tests/test_security_sca_adapter.py
- tests/test_security_coverage_cli.py
- docs/manual/ (nova página cobertura-seguranca.md e índice)
- docs/FASE_ATUAL.md
- importlinter / .importlinter (contrato: core/security não importa camada HTTP/UI)

## Riscos
- Sobreposição com USR-449 (implementing): risco de duas implementações de 'cobertura explícita' e 'vencimento de evidência'. Mitigação: comentar na demanda USR-449 que a cobertura vive em core.security.coverage e alinhar antes do merge do T4.
- OSV.dev indisponível ou com rate limit no CI. Mitigação: os testes unitários usam transporte injetável com fixtures, e o e2e real roda como smoke manual ou noturno. A entrega só conta com a execução real registrada como evidência.
- `pip inspect` exige pip >= 22.2, e o ambiente darkfac herda o processo, sem venv próprio. Mitigação: o ambiente system usa sys.executable com método de resolução registrado, e falha de inspect vira unknown com motivo.
- Precedência mal aplicada mascarar uma falha como avaliado. Mitigação: testes de tabela cobrindo todas as combinações de categoria.
- Isolamento por projeto: o caminho de armazenamento precisa ficar contido no destino declarado. Mitigação: validação de path como em venv_dir.
- Crescimento do núcleo contra o saldo de código. Mitigação: reusar ProjectRegistry, ProjectEnvironment, NotificationService e o padrão de estado do daily_digest, sem criar cliente HTTP genérico novo.
