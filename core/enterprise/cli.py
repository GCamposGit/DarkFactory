"""Headless CLI interface for Enterprise Profile On-Demand (HF-24)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.enterprise.models import (
    DataResidencyMode,
    EnterpriseProjectConfig,
    EnterpriseRole,
)
from core.enterprise.policy import EnterprisePolicyGuard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DarkFac Enterprise Profile CLI (HF-24)",
        prog="python core/enterprise/cli.py",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    status_p = subparsers.add_parser("status", help="Inspect enterprise configuration and compliance")
    status_p.add_argument("--project", required=True, help="Project identifier")
    status_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # configure
    cfg_p = subparsers.add_parser("configure", help="Configure enterprise profile parameters")
    cfg_p.add_argument("--project", required=True, help="Project identifier")
    cfg_p.add_argument("--enable", action="store_true", help="Enable enterprise hardening")
    cfg_p.add_argument("--disable", action="store_true", help="Disable enterprise hardening")
    cfg_p.add_argument("--residency", choices=[m.value for m in DataResidencyMode], help="Data residency mode")
    cfg_p.add_argument("--rpo", type=int, help="Max RPO minutes (e.g. 60)")
    cfg_p.add_argument("--rto", type=int, help="Max RTO minutes (e.g. 30)")

    # verify-audit
    audit_p = subparsers.add_parser("verify-audit", help="Verify cryptographic audit chain integrity")
    audit_p.add_argument("--project", help="Optional project filter")
    audit_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # check-sla
    sla_p = subparsers.add_parser("check-sla", help="Check disaster recovery RPO/RTO SLA compliance")
    sla_p.add_argument("--project", required=True, help="Project identifier")
    sla_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # evaluate-deploy
    dep_p = subparsers.add_parser("evaluate-deploy", help="Evaluate production deployment gate (Scenario G8)")
    dep_p.add_argument("--project", required=True, help="Project identifier")
    dep_p.add_argument("--env", default="production", help="Target deployment environment")
    dep_p.add_argument("--owner-approved", action="store_true", help="Assert owner has signed off")
    dep_p.add_argument("--role", default="owner", choices=[r.value for r in EnterpriseRole])
    dep_p.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args(argv)
    guard = EnterprisePolicyGuard()

    if args.command == "status":
        cfg = guard.get_config(args.project)
        sla = guard.sla_guard.check_sla(cfg)
        audit = guard.audit_chain.verify_integrity(args.project)
        res = {
            "config": cfg.model_dump(mode="json"),
            "sla": sla.model_dump(mode="json"),
            "audit_chain": audit.model_dump(mode="json"),
        }
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2))
        else:
            print(f"=== Enterprise Profile: {args.project} ===")
            print(f"Status           : {'ENABLED' if cfg.enabled else 'DISABLED'}")
            print(f"Residency Mode   : {cfg.residency_mode.value}")
            print(f"Max RPO Window   : {cfg.max_rpo_minutes} min")
            print(f"Max RTO Window   : {cfg.max_rto_minutes} min")
            print(f"Audit Chain Valid: {audit.is_valid} ({audit.total_events} events)")
            print(f"SLA Compliant    : {sla.is_compliant}")
            if sla.violations:
                for v in sla.violations:
                    print(f"  [!] {v}")
        return 0

    if args.command == "configure":
        current = guard.get_config(args.project)
        enabled = current.enabled
        if args.enable:
            enabled = True
        elif args.disable:
            enabled = False

        residency = DataResidencyMode(args.residency) if args.residency else current.residency_mode
        rpo = args.rpo if args.rpo is not None else current.max_rpo_minutes
        rto = args.rto if args.rto is not None else current.max_rto_minutes

        new_cfg = EnterpriseProjectConfig(
            project_id=args.project,
            enabled=enabled,
            residency_mode=residency,
            max_rpo_minutes=rpo,
            max_rto_minutes=rto,
            pii_sanitization_strict=current.pii_sanitization_strict,
            require_owner_signoff=current.require_owner_signoff,
            allowed_providers=current.allowed_providers,
        )
        guard.set_config(new_cfg)
        print(f"Enterprise config saved for '{args.project}': enabled={new_cfg.enabled}, residency={new_cfg.residency_mode.value}")
        return 0

    if args.command == "verify-audit":
        res = guard.audit_chain.verify_integrity(args.project)
        if getattr(args, "json", False):
            print(res.model_dump_json(indent=2))
        else:
            print(f"Audit Chain Integrity: {'VALID' if res.is_valid else 'TAMPERED / INVALID'}")
            print(f"Total Events         : {res.total_events}")
            print(f"Diagnostic           : {res.error_message}")
        return 0 if res.is_valid else 1

    if args.command == "check-sla":
        cfg = guard.get_config(args.project)
        res = guard.sla_guard.check_sla(cfg)
        if getattr(args, "json", False):
            print(res.model_dump_json(indent=2))
        else:
            print(f"SLA Compliance: {'COMPLIANT' if res.is_compliant else 'NON-COMPLIANT'}")
            print(f"Last Backup   : {res.last_backup_age_minutes}m ago")
            print(f"Measured RTO  : {res.measured_rto_minutes}m")
            for v in res.violations:
                print(f"  [X] {v}")
        return 0 if res.is_compliant else 1

    if args.command == "evaluate-deploy":
        dec = guard.evaluate_production_release(
            project_id=args.project,
            target_environment=args.env,
            owner_approved=args.owner_approved,
            actor_role=args.role,
        )
        if getattr(args, "json", False):
            print(dec.model_dump_json(indent=2))
        else:
            outcome = "APPROVED" if dec.approved else "REJECTED"
            print(f"Enterprise Deploy Gate Decision: {outcome}")
            print(f"Owner Signoff: {dec.owner_signoff_verified}")
            print(f"Audit Chain  : {dec.audit_chain_verified}")
            print(f"Residency    : {dec.residency_verified}")
            print(f"SLA Verified : {dec.sla_verified}")
            if dec.rejection_reasons:
                print("Rejections:")
                for r in dec.rejection_reasons:
                    print(f"  - {r}")
        return 0 if dec.approved else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
