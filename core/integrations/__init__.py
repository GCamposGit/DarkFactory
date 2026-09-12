"""External service adapters used by the headless DarkFac core."""

from core.integrations.github import (
    GitHubApiError,
    GitHubCheck,
    GitHubClient,
    PullRequestSnapshot,
)
from core.integrations.n8n import (
    N8nConfig,
    N8nInstanceReport,
    N8nManifestGenerator,
    N8nProbe,
    N8nWorkflowManager,
)
from core.integrations.telegram import (
    TelegramActionType,
    TelegramConfig,
    TelegramDispatchResult,
    TelegramGateway,
    TelegramUpdate,
    redact_secrets,
)

__all__ = [
    "GitHubApiError",
    "GitHubCheck",
    "GitHubClient",
    "PullRequestSnapshot",
    "N8nConfig",
    "N8nInstanceReport",
    "N8nManifestGenerator",
    "N8nProbe",
    "N8nWorkflowManager",
    "TelegramActionType",
    "TelegramConfig",
    "TelegramDispatchResult",
    "TelegramGateway",
    "TelegramUpdate",
    "redact_secrets",
]

