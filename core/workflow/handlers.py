"""Canonical stage handlers and registry for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md and ticket HF-05-04.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from core.workflow.control_contracts import (
    ControlError,
    HandlerDescriptor,
    InvalidResultError,
    StageContext,
    StageResult,
)

logger = logging.getLogger(__name__)


class MissingHandlerError(ControlError):
    """Raised when a requested stage handler or version is not registered."""


@runtime_checkable
class StageHandler(Protocol):
    """Protocol for executing a workflow stage."""

    def handle(self, context: StageContext) -> StageResult:
        """Execute the stage within the given context and return a validated StageResult."""
        ...


STANDARD_STAGES: tuple[str, ...] = (
    "grill",
    "planning",
    "research",
    "environment",
    "development",
    "validation",
    "independent_review",
    "integration",
    "build_deploy",
    "target_journey",
    "memory_observation",
    "learning_eval",
    "catalog_refresh",
)

STANDARD_STAGE_DESCRIPTORS: dict[str, HandlerDescriptor] = {
    "grill": HandlerDescriptor(
        stage="grill",
        version="v1",
        input_schema_ref="schema://contracts/IntakeCommand",
        output_schema_ref="schema://contracts/GrillRecord",
        role="grill_engine",
    ),
    "planning": HandlerDescriptor(
        stage="planning",
        version="v1",
        input_schema_ref="schema://contracts/GrillRecord",
        output_schema_ref="schema://contracts/WorkflowHandoff",
        role="planner",
    ),
    "research": HandlerDescriptor(
        stage="research",
        version="v1",
        input_schema_ref="schema://contracts/ResearchQuery",
        output_schema_ref="schema://contracts/ResearchLedger",
        role="researcher",
    ),
    "environment": HandlerDescriptor(
        stage="environment",
        version="v1",
        input_schema_ref="schema://contracts/EnvironmentManifest",
        output_schema_ref="schema://contracts/EnvironmentEvidence",
        role="environment_probe",
    ),
    "development": HandlerDescriptor(
        stage="development",
        version="v1",
        input_schema_ref="schema://contracts/WorkflowHandoff",
        output_schema_ref="schema://contracts/ImplementationCandidate",
        role="developer",
    ),
    "validation": HandlerDescriptor(
        stage="validation",
        version="v1",
        input_schema_ref="schema://contracts/ImplementationCandidate",
        output_schema_ref="schema://contracts/ValidationReport",
        role="validator",
    ),
    "independent_review": HandlerDescriptor(
        stage="independent_review",
        version="v1",
        input_schema_ref="schema://contracts/ValidationReport",
        output_schema_ref="schema://contracts/EvidenceReceipt",
        role="reviewer",
    ),
    "integration": HandlerDescriptor(
        stage="integration",
        version="v1",
        input_schema_ref="schema://contracts/EvidenceReceipt",
        output_schema_ref="schema://contracts/IntegrationSnapshot",
        role="integrator",
    ),
    "build_deploy": HandlerDescriptor(
        stage="build_deploy",
        version="v1",
        input_schema_ref="schema://contracts/IntegrationSnapshot",
        output_schema_ref="schema://contracts/DeployReceipt",
        role="deployer",
    ),
    "target_journey": HandlerDescriptor(
        stage="target_journey",
        version="v1",
        input_schema_ref="schema://contracts/DeployReceipt",
        output_schema_ref="schema://contracts/JourneyEvidence",
        role="journey_tester",
    ),
    "memory_observation": HandlerDescriptor(
        stage="memory_observation",
        version="v1",
        input_schema_ref="schema://contracts/StageResult",
        output_schema_ref="schema://contracts/MemoryHypothesis",
        role="memory_agent",
    ),
    "learning_eval": HandlerDescriptor(
        stage="learning_eval",
        version="v1",
        input_schema_ref="schema://contracts/MemoryHypothesis",
        output_schema_ref="schema://contracts/PromotionReceipt",
        role="evaluator",
    ),
    "catalog_refresh": HandlerDescriptor(
        stage="catalog_refresh",
        version="v1",
        input_schema_ref="schema://contracts/BenchmarkTrigger",
        output_schema_ref="schema://contracts/CatalogMatrix",
        role="benchmarker",
    ),
}


class DefaultStageHandler:
    """Default stage handler that executes default stage logic or dispatches to a service."""

    def __init__(
        self,
        stage: str,
        version: str = "v1",
        service: Any = None,
        descriptor: HandlerDescriptor | None = None,
    ) -> None:
        self.stage = stage
        self.version = version
        self.service = service
        self.descriptor = descriptor or STANDARD_STAGE_DESCRIPTORS.get(stage)

    def handle(self, context: StageContext) -> StageResult:
        """Execute stage and enforce fail-closed invariants."""
        if self.service is not None:
            if callable(self.service):
                raw = self.service(context)
            elif hasattr(self.service, "handle") and callable(self.service.handle):
                raw = self.service.handle(context)
            elif hasattr(self.service, "run") and callable(self.service.run):
                raw = self.service.run(context)
            else:
                raw = self._default_execute(context)
        else:
            raw = self._default_execute(context)

        # Type conversion & validation
        if isinstance(raw, dict):
            if raw.get("outcome") == "success" and not raw.get("output_refs"):
                raise InvalidResultError(
                    f"Handler for stage '{self.stage}' returned outcome='success' with empty output_refs."
                )
            result = StageResult(**raw)
        elif isinstance(raw, StageResult):
            result = raw
        else:
            raise InvalidResultError(
                f"Handler for stage '{self.stage}' produced unsupported result type: {type(raw).__name__}"
            )

        # Enforce outcome == 'success' MUST have non-empty output_refs
        if result.outcome == "success" and not result.output_refs:
            raise InvalidResultError(
                f"Handler for stage '{self.stage}' produced outcome='success' with empty output_refs."
            )

        return result

    def _default_execute(self, context: StageContext) -> StageResult:
        """Reject an unbound stage instead of fabricating output/evidence refs."""
        return StageResult(
            outcome="failed", cause_code="missing_stage_service"
        )


class HandlerRegistry(dict[tuple[str, str], StageHandler]):
    """Registry mapping (stage, version) to StageHandler with missing-handler enforcement."""

    def __missing__(self, key: tuple[str, str]) -> StageHandler:
        raise MissingHandlerError(f"Missing handler for stage tuple: {key}")

    def get_handler(self, stage: str, version: str = "v1") -> StageHandler:
        """Lookup handler for stage and version, raising MissingHandlerError if missing."""
        key = (stage, version)
        if key not in self:
            raise MissingHandlerError(f"Missing handler for stage '{stage}' version '{version}'")
        return self[key]

    def dispatch(self, context: StageContext, version: str = "v1") -> StageResult:
        """Dispatch context to registered handler or return fail-closed result."""
        stage = context.claim.job_key.stage
        key = (stage, version)
        if key not in self:
            return StageResult(outcome="failed", cause_code="missing_handler")
        return self[key].handle(context)


def dispatch_stage(
    handlers: Mapping[tuple[str, str], StageHandler],
    context: StageContext,
    version: str = "v1",
) -> StageResult:
    """Dispatch context using handler mapping or return fail-closed result."""
    if isinstance(handlers, HandlerRegistry):
        return handlers.dispatch(context, version=version)

    stage = context.claim.job_key.stage
    key = (stage, version)
    if key not in handlers:
        return StageResult(outcome="failed", cause_code="missing_handler")
    try:
        return handlers[key].handle(context)
    except MissingHandlerError:
        return StageResult(outcome="failed", cause_code="missing_handler")


def build_handlers(
    bindings: Mapping[str | tuple[str, str], Any] | None = None,
    services: Mapping[str | tuple[str, str], Any] | None = None,
) -> HandlerRegistry:
    """Register handlers for standard stages and user bindings."""
    registry = HandlerRegistry()
    bindings = bindings or {}
    services = services or {}

    # 1. Register default handlers for all standard stages (version="v1")
    for stage in STANDARD_STAGES:
        key = (stage, "v1")
        service = services.get(key) or services.get(stage)
        registry[key] = DefaultStageHandler(stage=stage, version="v1", service=service)

    # 2. Register additional or overridden bindings
    for key, binding in bindings.items():
        if isinstance(key, str):
            stage_tuple = (key, "v1")
        else:
            stage_tuple = key

        if isinstance(binding, StageHandler):
            registry[stage_tuple] = binding
        elif callable(binding):
            registry[stage_tuple] = DefaultStageHandler(
                stage=stage_tuple[0],
                version=stage_tuple[1],
                service=binding,
            )
        else:
            registry[stage_tuple] = DefaultStageHandler(
                stage=stage_tuple[0],
                version=stage_tuple[1],
                service=binding,
            )

    # 3. Register additional services for stages not covered by standard stages
    for key, srv in services.items():
        if isinstance(key, str):
            stage_tuple = (key, "v1")
        else:
            stage_tuple = key
        if stage_tuple not in registry:
            registry[stage_tuple] = DefaultStageHandler(
                stage=stage_tuple[0],
                version=stage_tuple[1],
                service=srv,
            )

    return registry
