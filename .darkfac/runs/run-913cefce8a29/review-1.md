# Review round 1

Verdict: approve

## Nao-bloqueantes
- `core/infra/deploy_classifier.py:42` — runtime_image_paths() re-reads and re-parses Dockerfile.hub on every call, with no caching. (fix: Optionally cache the parsed result (e.g. functools.lru_cache or module-level memo) if call frequency ever becomes a concern; not required now.)
- `core/infra/container_memory.py:39` — cgroup v1 fallback reads an 'oom_kill' key from memory.oom_control, which real Linux v1 cgroups don't expose (only oom_kill_disable/under_oom); the counter will likely stay None on genuine v1 hosts. (fix: If v1 support matters in production, source oom_kill counts from memory.stat's 'oom_kill' event or document that v1 OOM alerting is best-effort/unsupported; not blocking since the stated acceptance criteria only require parsing a given file format, which the tests verify.)
- `core/demands/store.py:166` — DemandsStore._local_overrides grows unbounded for the life of the process. (fix: Consider dropping an override once the remote payload itself reflects the same ticket id/content, or cap the overlay size; low priority given typical demand volume.)
