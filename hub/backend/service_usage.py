"""Hub service domain: telemetry, account/model usage, API credits and infra cards."""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Any

from hub.backend.models import (
    UsageSyncPayload,
    UsageSyncResponse,
)
from core.usage.ledger import infer_model_tier
from core.usage.models import ModelCallEvent, ModelModality, ModelTier
from core.usage.api_credits import (
    ApiCreditsReport,
    CreditAccountUpdateRequest,
    ProviderCreditCard,
)
from core.infra.cards import (
    InfraCard,
    InfraCardsReport,
    build_infra_cards_report,
)
from core.infra.metrics import (
    InfraMetricsReport,
    build_infra_metrics_report,
)




logger = logging.getLogger("darkhub.service")


class UsageMixin:

    def _record_model_call(
        self,
        *,
        provider: str,
        model: str,
        harness: str = "darkhub",
        modality: ModelModality = ModelModality.TEXT,
        success: bool = True,
        input_tokens: Optional[int] = None,
        processing_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        latency_ms: Optional[float] = None,
        source: str,
        ticket_id: Optional[str] = None,
        project_id: str = "darkfac",
        execution_mode: Optional[str] = "ui",
    ) -> None:
        """Persist telemetry fail-open so observability never breaks inference."""
        try:
            self.model_usage_ledger.record(ModelCallEvent(
                provider=provider,
                model=model,
                tier=ModelTier(infer_model_tier(provider, model)),
                harness=harness,
                modality=modality,
                success=success,
                input_tokens=input_tokens,
                processing_tokens=processing_tokens or 0,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                source=source,
                ticket_id=ticket_id,
                project_id=project_id,
                execution_mode=execution_mode,
            ))
        except Exception as exc:
            logger.warning("Model telemetry write failed: %s", exc)


    def get_telemetry_runs(self, filters: Any = None) -> Any:
        """Query paginated model telemetry runs with filters."""
        return self.telemetry_store.query_runs(filters)


    def get_telemetry_stats(self, filters: Any = None) -> Any:
        """Calculate analytical statistics across telemetry runs."""
        return self.telemetry_store.get_stats(filters)


    def list_telemetry_tickets(self) -> list[str]:
        """Return distinct tickets with recorded runs."""
        return self.telemetry_store.list_tickets()


    def record_telemetry_run(self, payload: Any) -> Any:
        """Record an incoming telemetry run directly into SQLite store."""
        return self.telemetry_store.record(payload)


    def get_account_usage(self, force: bool = False) -> Any:
        """Return a partial-success report for every known AI platform."""
        return self.account_usage_monitor.inspect(force=force)


    def request_account_refresh(self, target_node: Optional[str] = None) -> Any:
        """Record a refresh request for the metering host; never probe CLIs on a synced-only Hub."""
        from core.usage.refresh_requests import RefreshRequestUpdate, RefreshStatus

        request = self.refresh_request_store.create(target_node)
        if self._probes_locally:
            # Local Hub is itself the metering host: measure now and close the request.
            try:
                report = self.account_usage_monitor.inspect(force=True)
                change = RefreshRequestUpdate(
                    status=RefreshStatus.DONE, checked_at=str(getattr(report, "checked_at", "") or "") or None
                )
            except Exception as exc:  # noqa: BLE001
                change = RefreshRequestUpdate(status=RefreshStatus.FAILED, failure_reason=str(exc)[:300])
            request = self.refresh_request_store.update(request.id, change) or request
        return request


    def get_model_usage(self, project_id: Optional[str] = None) -> Any:
        """Return project-wide model counters and recent attempts."""
        return self.model_usage_ledger.report(project_id=project_id)


    def record_model_usage(self, event: ModelCallEvent) -> Any:
        """Allow Codex, Grok, Gemini and other harnesses to report calls."""
        self.model_usage_ledger.record(event)
        return self.model_usage_ledger.report()


    def get_api_credits_report(self, force: bool = False) -> ApiCreditsReport:
        """Returns API credit balance and current month expenditure in USD ($)."""
        return self.api_credits_monitor.generate_report(force=force)


    def refresh_api_credits_report(self) -> ApiCreditsReport:
        """Forces live refresh of API credit balance and expenditures ($)."""
        return self.api_credits_monitor.refresh_report()


    def update_credit_account(self, provider_id: str, payload: CreditAccountUpdateRequest) -> ProviderCreditCard:
        """Updates and persists credit balances or notes for a provider."""
        return self.api_credits_monitor.update_account(provider_id, payload)


    def sync_usage_data(self, payload: UsageSyncPayload) -> UsageSyncResponse:
        """Persists incoming account quota and credit snapshots from a workstation or worker node."""
        accounts_updated = 0
        credits_updated = 0

        # 1. Ingest account quota snapshots
        for account in payload.accounts:
            if not isinstance(account, dict):
                continue
            provider_id = account.get("provider_id")
            if not provider_id:
                continue
            try:
                # Record which node sent the snapshot so the Hub can show its origin (USR-233).
                account = {**account, "source_node": payload.client_node_id}
                self.account_usage_monitor.save_snapshot(str(provider_id), account)
                accounts_updated += 1
            except Exception as exc:
                logger.warning("Failed to save account snapshot for %s: %s", provider_id, exc)

        # 2. Ingest API credit balance snapshots
        for credit in payload.credits:
            if not isinstance(credit, dict):
                continue
            provider_id = credit.get("provider_id")
            if not provider_id:
                continue
            try:
                self.api_credits_monitor.save_snapshot(str(provider_id), credit)
                credits_updated += 1
            except Exception as exc:
                logger.warning("Failed to save credit snapshot for %s: %s", provider_id, exc)

        self.usage_sync_watch.record_success()
        now_str = datetime.now(timezone.utc).isoformat()
        return UsageSyncResponse(
            status="synchronized",
            client_node_id=payload.client_node_id,
            accounts_updated=accounts_updated,
            credits_updated=credits_updated,
            synced_at=now_str,
            message=f"Synchronized {accounts_updated} account quotas and {credits_updated} credit balances from {payload.client_node_id}.",
        )


    def get_infra_cards_report(
        self,
        probe_liveness: bool = False,
        probe_timeout: float = 0.5,
        project_id: Optional[str] = None,
    ) -> InfraCardsReport:
        """Returns the infrastructure cards report for the Hub."""
        inventory = self.infra_manager.load_or_initialize()
        return build_infra_cards_report(
            inventory,
            probe_network_liveness=probe_liveness,
            probe_timeout=probe_timeout,
            project_id=project_id,
        )


    def get_infra_card(self, node_id: str, probe_liveness: bool = False, probe_timeout: float = 0.5) -> Optional[InfraCard]:
        """Returns a single infrastructure card by node ID."""
        report = self.get_infra_cards_report(probe_liveness=probe_liveness, probe_timeout=probe_timeout)
        for card in report.cards:
            if card.id == node_id:
                return card
        return None


    def get_infra_metrics_report(self, force_refresh: bool = False, timeout: float = 3.0) -> InfraMetricsReport:
        """Returns the real-time infrastructure metrics report (INFRA-11)."""
        now = time.time()
        if not force_refresh and self._cached_infra_metrics is not None:
            cached_at, cached_report = self._cached_infra_metrics
            if now - cached_at < 5.0:
                return cached_report

        report = build_infra_metrics_report(timeout=timeout)
        self._cached_infra_metrics = (now, report)
        return report
