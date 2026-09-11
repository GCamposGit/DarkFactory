import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import ApiCreditsMonitor

mon = AccountUsageMonitor(Path(".factory/usage/providers"))
rep = mon.inspect(force=True)
print("=== LOCAL ACCOUNTS ===")
print("Connected:", rep.connected_count, "Limited:", rep.limited_count, "Disconnected:", rep.disconnected_count)
for acc in rep.accounts:
    print(f" - {acc.provider_name}: {acc.status} ({acc.adapter}) -> {acc.message}")

c_mon = ApiCreditsMonitor(Path(".factory/usage/credits"))
c_rep = c_mon.generate_report(force=True)
print("\n=== LOCAL CREDITS ===")
for acc in c_rep.accounts:
    print(f" - {acc.provider_name}: {acc.status} connected={acc.is_connected} -> {acc.notes}")
