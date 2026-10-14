"""USR-239: static guard against the "clock bomb" test defect.

See ``tests/_clock_bomb_guard.py`` for the full incident background (the 2026-10-11 ``main``/PR outage
caused by ``tests/test_line_live.py`` mixing a fixed 2026 seed with the real wall clock) and for how the
scan works. This test is the enforcement: it fails fast (AST only, no test execution) for any file
under ``tests/`` that reintroduces the same pattern and is not in ``CLOCK_BOMB_EXCEPTIONS`` below.

USR-239 audit (criterion 1 "levantamento"): every file the ticket named as a suspect --
``test_artifact_retention``, ``test_cloud_worker_active_loop``, ``test_control_store_failure_evidence``,
``test_line_acceptance``, ``test_line_dogfood``, ``test_line_live``, ``test_line_owner_intake_expired``,
``test_local_progress_visibility``, ``test_local_progress``, ``test_postgres_real_integration``,
``test_run_ticket_cancel``, ``test_telegram_status_usr197`` -- was read end to end, tracing the exact
production code path each test exercises. Only ``test_line_live.py`` had a live bug; it was fixed by
PR 302 (the ``_pin_line_live_clock`` autouse fixture) before this ticket. The other eleven already
thread their fixed 2026 date explicitly into the `now=`/`clock=`/`now_fn=` parameter of the production
function under test (or into a field that is only ever stored/asserted verbatim), so the production
code never falls back to the real clock while comparing against that date; they need no code change and
pin the clock in-file in the sense this guard checks for (see each one's entry in
``CLOCK_BOMB_EXCEPTIONS`` only where the guard's keyword-based detection does not already recognize the
pattern it uses).

A much wider sweep (every file under ``tests/`` that merely contains the substring ``"2026-"`` or
constructs a ``datetime(2026, ...)``) turned up ~56 further hits. Each was read and falls into one of:
docstring/comment prose, a non-date identifier (filename, model name, workflow/eval id) that happens to
contain four digits followed by a dash, or fixture/content data that is stored and asserted back
verbatim without ever being diffed against a real clock read in the file or the production path it
exercises. Each is listed below with the specific reason. A new entry here must have the same
standard: read the test AND the production code it calls, don't just silence the scanner.
"""

from __future__ import annotations

import time
from pathlib import Path

from core.paths import project_root
from tests._clock_bomb_guard import scan_tests_tree

# path (POSIX, relative to repo root) -> why the fixed 2026 date in that file does not depend on the
# real clock at test time. Keep reasons specific enough that a future reader can re-verify them.
CLOCK_BOMB_EXCEPTIONS: dict[str, str] = {
    # -- docstring/comment prose: the "2026-" text is narrative, not a literal fed to any comparison.
    "tests/line/test_no_route_contract.py": "prosa de docstring/comentario; nao e literal de data usada pelo teste",
    "tests/line/test_planning_grill_artifacts.py": "docstring do modulo; nao e literal de data",
    "tests/line/test_stage_review.py": "comentario ('Canary 2026-10-04/05: ...'); nao e literal de data usada pelo teste",
    "tests/line/test_retrospective.py": "seed SQL de um run buscado diretamente por run_id; RetrospectiveStageHandler nao filtra por RECENT_RUN_WINDOW, so lookups diretos por id",
    "tests/test_dokploy_release_identity.py": "docstring do modulo; nao e literal de data",
    "tests/test_gate_isolation.py": "docstring do modulo; nao e literal de data",
    "tests/test_hf12_release_staging_production_backup.py": "docstring do modulo; nao e literal de data",
    "tests/test_hf15_acceptance_environment.py": "docstring do modulo; nao e literal de data",
    "tests/test_hf15_runner.py": "docstring do modulo; nao e literal de data",
    "tests/test_model_router_hf07.py": "docstring do modulo; nao e literal de data",
    "tests/test_notification_system.py": "docstring do modulo; nao e literal de data",
    "tests/test_remote_job_recovery.py": "docstring do modulo; nao e literal de data",
    "tests/test_reusable_pilots.py": "comentario; nao e literal de data usada pelo teste",
    "tests/test_suite_lock_stalled.py": "comentario; nao e literal de data usada pelo teste",
    "tests/test_worker_capacity.py": "comentario ('O incidente de 2026-09-30: ...'); nao e literal de data",
    "tests/test_darkhub_priority_interventions_and_grill.py": "comentario ('Canario diario 2026-09-30'); nao e literal de data",
    # -- identifiers that are shaped like a date but are not one.
    "tests/line/test_stage_build.py": "'gpt-6-luna-2026-10-01' e nome de modelo, nao uma data",
    "tests/test_run_ticket_routing.py": "'gpt-6-luna-2026-10-01' e nome de modelo, nao uma data",
    "tests/test_baseline_catalog.py": "'DEVELOPMENT_PLAN_2026-09-05.md' e nome de arquivo versionado, nao uma data lida do relogio",
    "tests/test_roadmap.py": "'DEVELOPMENT_PLAN_2026-09-05.md' e nome de arquivo versionado, nao uma data lida do relogio",
    "tests/test_roadmap_history.py": "'DEVELOPMENT_PLAN_2026-09-05.md' e nome de arquivo versionado, nao uma data lida do relogio",
    "tests/test_worktree_protocol.py": "'DEVELOPMENT_PLAN_2026-09-05.md' e nome de arquivo versionado, nao uma data lida do relogio",
    "tests/test_hub_hf13.py": "'HYBRID_WORKFLOW_PLAN_2026-09-08.md' e nome de arquivo versionado, nao uma data lida do relogio",
    "tests/test_cloud_artifacts.py": "'wf-2026-001' e id de workflow, nao uma data",
    "tests/test_quota_meter_contract.py": "'xai_2026-10-01.json' e nome de arquivo de fixture versionado em tests/fixtures/usage/, nao uma data lida do relogio",
    "tests/test_hub_catalog_revisions.py": "'applied_revision' e um marcador de revisao do catalogo comparado so com o proprio conteudo versionado, nunca com o relogio real",
    "tests/test_hub_cloud_seed.py": "'applied_revision' e um marcador de revisao do catalogo comparado so com o proprio conteudo versionado, nunca com o relogio real",
    # -- fixed value threaded explicitly into the production call (DI the scanner's keyword check
    # does not recognize by name) or inert, confirmed by reading the exercised production path.
    "tests/test_atrium_jarvis_activation.py": "campo de conteudo 'date' de um post de marketing, apenas armazenado/comparado como string; sem leitura do relogio real",
    "tests/test_baseline_reconcile.py": "NOW so alimenta observed_at/SourceObservation; core/planning/baseline_reconcile.py nao le o relogio real em nenhum ponto",
    "tests/test_continuous_planning.py": "NOW usado para construir um expires_at sempre no passado (lease ja expirada); seguro para sempre porque o relogio real so avanca",
    "tests/test_control_store_failure_evidence.py": "_T0 passado explicitamente em todo accept/claim/finish; SQLiteControlStore só lê o relógio real quando now é omitido, nunca é o caso aqui",
    "tests/test_infra08_three_tier_backup.py": "dt fixo so verifica se o assinador SigV4 formata UM datetime DADO corretamente; nunca comparado ao relogio real",
    "tests/test_line_acceptance.py": "base_date so alimenta green_streak (compara relatorios entre si) e jobs ja 'succeeded'; o branch de relogio real de run_state nao e alcancado",
    "tests/test_line_dogfood.py": "created_at so usado como chave de ordenacao relativa; o fallback de relogio real de submit_dogfood_item nao e exercitado com esse valor",
    "tests/test_line_owner_intake.py": "jobs semeados via SQL bruto nunca tem cause_code='no_route_available', unico caso em que run_state cairia no fallback de relogio real",
    "tests/test_local_progress_visibility.py": "T0 so alimenta o campo 'at' de um LocalRunEvent escrito via conexao psycopg fake; nunca e lido de volta nem comparado",
    "tests/test_marketing_engine.py": "campo de conteudo 'date' de um post de marketing, apenas armazenado/comparado como string; sem leitura do relogio real",
    "tests/test_postgres_hybrid_persistence.py": "timestamps sao apenas colunas simuladas de linha de banco (conflito de sync por versao); o arquivo nao le o relogio real em nenhum ponto",
    "tests/test_release_evidence_chain.py": "BASE_TIME e uma constante sem uso no arquivo; todo outro horario do arquivo e calculado relativo a datetime.now(UTC) no momento do teste",
    "tests/test_workflow_safe_export.py": "NOW passado explicitamente como observed_at para uma funcao pura de exportacao",
    # -- fixture/content data: stored and asserted back verbatim, never diffed against the real clock
    # in the file or in the production path the test exercises (confirmed by reading that path, not
    # just grepping for datetime.now()).
    "tests/test_baseline_sources.py": "observed_at de uma SourceObservation de fixture; comparado so com outros valores fixos no mesmo teste",
    "tests/test_control_job_board.py": "linhas de seed SQL para core/workflow/job_board.py, que nao le o relogio real nem filtra por idade",
    "tests/test_delivery_policy.py": "campo 'updated_at' de um payload mock da API do GitHub; comparado so com outro campo do mesmo mock",
    "tests/test_harness_cache.py": "timestamp de uma linha de cache, armazenada/lida de volta verbatim",
    "tests/test_hf10_persistent_memory_learning_pack.py": "campo 'created_at'/'timestamp' de fixtures de PolicyDecision/LearningSession, armazenado e comparado verbatim",
    "tests/test_hub_factory_health.py": "campo de payload mock de webhook/API, armazenado e comparado verbatim",
    "tests/test_hub_service_legacy_cleanup.py": "linha de seed SQL para um teste de limpeza de servico legado; sem filtro por idade",
    "tests/test_infra11_unified_metrics.py": "campo 'boot_time' de um snapshot de metricas, armazenado e comparado verbatim",
    "tests/test_learning_event_chain.py": "campo 'expires_at' de uma fixture de claim, armazenado e comparado verbatim (nao um TTL contra o relogio real)",
    "tests/test_learning_pack.py": "campo 'timestamp' de uma fixture de sessao de learning pack, armazenado e comparado verbatim",
    "tests/test_model_benchmark.py": "campo 'observed_at' de uma fixture de provenance, comparado so com o proprio valor esperado no teste",
    "tests/test_owner_actions_core.py": "'resolved_at' exercita apenas o caminho de validacao (ValueError); nao ha comparacao com o relogio real",
    "tests/test_research_engine.py": "campo 'pushed_at' de um payload mock da API do GitHub, armazenado e comparado verbatim",
    "tests/test_task_dashboard.py": "linha de seed SQL para o dashboard de tarefas do Hub, que conta status sem filtrar por idade",
    "tests/test_usage_monitor.py": "campo 'resets_at' de uma QuotaWindow stub; os asserts do teste checam used_percent/remaining_percent, nunca o reset contra o relogio real",
    "tests/test_context_policy.py": "'eval-2026-09-07' e um identificador de versao de avaliacao, armazenado verbatim; nao e uma data lida do relogio",
    "tests/test_dokploy_real_api_transport.py": "createdAt dos deployments simulados so e comparado como string com o baseline (outro createdAt fixo) em deployment_adapter; sem leitura do relogio real nessa comparacao",
    "tests/test_hf11_resilient_execution_budget.py": "docstring do modulo (nome do plano HYBRID_WORKFLOW_PLAN_2026-09-08); nao e literal de data",
    "tests/test_waiting_human_consultation.py": "linhas de seed SQL para um teste de dashboard de waiting_human; sem filtro por idade",
}


def test_no_unpinned_fixed_2026_dates_outside_the_exceptions_list() -> None:
    """Fails for any tests/ file mixing a fixed 2026 date with an un-pinned real clock (USR-239)."""
    started = time.monotonic()
    repo_root = project_root()
    hits = scan_tests_tree(repo_root / "tests", repo_root=repo_root)

    unexplained = [hit for hit in hits if hit.path not in CLOCK_BOMB_EXCEPTIONS]
    assert not unexplained, "Clock-bomb guard (USR-239): fixed 2026 date without a pinned clock:\n" + "\n".join(
        f"  {hit.path}:{hit.line}: {hit.detail}\n"
        f"    -> either monkeypatch the clock seam (e.g. `_utc_now`), use freeze_time, pass `now=`/"
        f"`clock=` explicitly, or add a reasoned entry to CLOCK_BOMB_EXCEPTIONS in this file."
        for hit in unexplained
    )

    stale = sorted(set(CLOCK_BOMB_EXCEPTIONS) - {hit.path for hit in hits})
    assert not stale, (
        "Clock-bomb guard (USR-239): these CLOCK_BOMB_EXCEPTIONS entries no longer match anything the "
        "scanner flags (the fixed date was removed or the clock is now pinned) -- remove the stale "
        f"entries:\n" + "\n".join(f"  {path}" for path in stale)
    )

    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"Clock-bomb guard took {elapsed:.2f}s, must stay under 5s (AST only, no test execution)"


def test_clock_bomb_scanner_flags_a_fixed_date_without_a_pinned_clock(tmp_path: Path) -> None:
    """Scanner self-check: a synthetic file mixing a fixed 2026 date and a real-clock read must be caught."""
    bad = tmp_path / "test_fake_bomb.py"
    bad.write_text(
        "from datetime import datetime, timezone\n\n"
        "SEED = datetime(2026, 10, 4, 15, 0, 0, tzinfo=timezone.utc)\n\n"
        "def test_something() -> None:\n"
        "    assert SEED is not None\n",
        encoding="utf-8",
    )
    hits = scan_tests_tree(tmp_path, repo_root=tmp_path)
    assert len(hits) == 1
    assert hits[0].path == "test_fake_bomb.py"
    assert hits[0].line == 3


def test_clock_bomb_scanner_accepts_a_file_that_pins_the_clock(tmp_path: Path) -> None:
    """Scanner self-check: the same fixed date is clean once the file pins the clock (now= explicit)."""
    good = tmp_path / "test_fake_safe.py"
    good.write_text(
        "from datetime import datetime, timezone\n\n"
        "SEED = datetime(2026, 10, 4, 15, 0, 0, tzinfo=timezone.utc)\n\n"
        "def test_something() -> None:\n"
        "    assert read_line_live(now=SEED) is not None\n",
        encoding="utf-8",
    )
    hits = scan_tests_tree(tmp_path, repo_root=tmp_path)
    assert hits == []


def _scan_snippet(tmp_path: Path, body: str) -> list:
    (tmp_path / "test_snippet.py").write_text(
        "from datetime import datetime\n\nSEED = datetime(2026, 1, 1)\n\n" + body, encoding="utf-8"
    )
    return scan_tests_tree(tmp_path, repo_root=tmp_path)


def test_clock_bomb_scanner_does_not_treat_substring_keywords_as_a_pinned_clock(tmp_path: Path) -> None:
    """`known=`/`unknown_ok=`/`snow=` contain 'now' only as a substring; they must not hide a bomb."""
    for body in ("f(known=1)\n", "f(unknown_ok=1)\n", "f(snow=1)\n"):
        assert len(_scan_snippet(tmp_path, body)) == 1, body


def test_clock_bomb_scanner_accepts_token_matched_clock_keywords_and_seams(tmp_path: Path) -> None:
    for body in (
        "f(now_fn=lambda: SEED)\n",
        "f(observed_now=SEED)\n",
        "f(clock=SEED)\n",
        'monkeypatch.setattr(m, "_utc_now", lambda: SEED)\n',
        'monkeypatch.setattr(m, "_now", lambda: SEED)\n',
    ):
        assert _scan_snippet(tmp_path, body) == [], body

