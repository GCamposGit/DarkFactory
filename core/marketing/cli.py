"""CLI Interface for Enterprise Marketing, CMS, CRM, SEO, and Google Ads (HF-21).

Governed by Universal Engineering Standards (AGENTS.md).
Accessible headlessly via library and CLI commands with UTF-8 safety and deterministic outputs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from .crm_n8n import LeadManager
from .google_ads import GoogleAdsManager
from .models import BlogPost, CaseStudy, GA4Event, LeadCapture
from .publisher import ContentPublisher
from .seo_analytics import GA4Client, SEOValidator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="darkfac-marketing",
        description="Dark Factory Marketing & Growth Engine (HF-21)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: publish-post
    p_post = subparsers.add_parser("publish-post", help="Publish a blog article to Astro content collection.")
    p_post.add_argument("--title", required=True, help="Title of the article.")
    p_post.add_argument("--slug", default=None, help="Custom URL slug.")
    p_post.add_argument("--desc", default=None, help="Article meta description.")
    p_post.add_argument("--file", default=None, help="Path to markdown file containing body.")
    p_post.add_argument("--content", default=None, help="Inline markdown body content.")
    p_post.add_argument("--tags", default="", help="Comma-separated tags.")
    p_post.add_argument("--lang", choices=["pt", "en"], default="pt", help="Publication language.")
    p_post.add_argument("--featured", action="store_true", help="Mark article as featured.")
    p_post.add_argument("--site-dir", default=None, help="Target site root directory (defaults to Atrium).")
    p_post.add_argument("--no-scrub", action="store_true", help="Reject high-slop content instead of auto-scrubbing.")
    p_post.add_argument("--approve", action="store_true", help="Explicit human approval flag to publish directly to production.")
    p_post.add_argument("--approver", default="owner", help="Identifier of approving human operator.")

    # Subcommand: approve-post
    p_app = subparsers.add_parser("approve-post", help="Approve a staged draft blog article and promote to production.")
    p_app.add_argument("--slug", required=True, help="Slug of the staged post.")
    p_app.add_argument("--approver", default="owner", help="Identifier of approving operator.")
    p_app.add_argument("--site-dir", default=None, help="Target site root directory.")

    # Subcommand: list-drafts
    p_drafts = subparsers.add_parser("list-drafts", help="List staged draft blog posts awaiting human gate.")
    p_drafts.add_argument("--site-dir", default=None, help="Target site root directory.")

    # Subcommand: publish-case
    p_case = subparsers.add_parser("publish-case", help="Publish a case study to Astro content collection.")
    p_case.add_argument("--title", required=True, help="Title of the case.")
    p_case.add_argument("--slug", default=None, help="Custom URL slug.")
    p_case.add_argument("--era", required=True, choices=["bain", "starboard", "via-appia", "darkfac"], help="Era tag.")
    p_case.add_argument("--anchor-metric", required=True, help="Core KPI metric name (e.g. EBITDA).")
    p_case.add_argument("--anchor-value", required=True, help="Quantified KPI value (e.g. +R$ 140M).")
    p_case.add_argument("--period", required=True, help="Time period (e.g. 2025-2026).")
    p_case.add_argument("--file", default=None, help="Path to markdown file containing body.")
    p_case.add_argument("--content", default=None, help="Inline markdown case body.")
    p_case.add_argument("--tags", default="", help="Comma-separated tags.")
    p_case.add_argument("--featured", action="store_true", help="Mark case as featured.")
    p_case.add_argument("--order", type=int, default=1, help="Display sort order.")
    p_case.add_argument("--confidentiality", choices=["draft", "pending-rights", "approved"], default="approved")
    p_case.add_argument("--site-dir", default=None, help="Target site root directory.")

    # Subcommand: lead-capture
    p_lead = subparsers.add_parser("lead-capture", help="Ingest inbound lead into CRM and trigger automations.")
    p_lead.add_argument("--name", required=True, help="Lead full name.")
    p_lead.add_argument("--email", required=True, help="Lead email address.")
    p_lead.add_argument("--project-id", default="atrium", help="Target project identifier.")
    p_lead.add_argument("--company", default=None, help="Company name.")
    p_lead.add_argument("--phone", default=None, help="Contact phone.")
    p_lead.add_argument("--source", default=None, help="utm_source.")
    p_lead.add_argument("--medium", default=None, help="utm_medium.")
    p_lead.add_argument("--campaign", default=None, help="utm_campaign.")
    p_lead.add_argument("--notes", default=None, help="Additional context or inquiry.")
    p_lead.add_argument("--no-n8n", action="store_true", help="Skip n8n webhook forwarding.")
    p_lead.add_argument("--no-telegram", action="store_true", help="Skip Telegram owner alert.")

    # Subcommand: list-leads
    p_list = subparsers.add_parser("list-leads", help="List captured leads from local SQLite CRM.")
    p_list.add_argument("--project-id", default=None, help="Filter by project identifier.")
    p_list.add_argument("--limit", type=int, default=20, help="Maximum number of leads.")

    # Subcommand: seo-audit
    p_seo = subparsers.add_parser("seo-audit", help="Run deterministic SEO audit on an HTML file or remote URL.")
    p_seo.add_argument("--target", required=True, help="Path to HTML file or HTTP/HTTPS URL.")
    p_seo.add_argument("--domain", default="https://ggcampos.com", help="Target site domain.")

    # Subcommand: ga4-track
    p_ga4 = subparsers.add_parser("ga4-track", help="Dispatch a GA4 event and record locally.")
    p_ga4.add_argument("--event", required=True, help="Event name (e.g. generate_lead, page_view).")
    p_ga4.add_argument("--client-id", default=None, help="GA4 client identifier.")
    p_ga4.add_argument("--user-id", default=None, help="Optional user ID.")
    p_ga4.add_argument("--params", default="{}", help="JSON string of event parameters.")

    # Subcommand: ads-report
    p_ads = subparsers.add_parser("ads-report", help="Generate Google Ads performance and budget guardrail report.")
    p_ads.add_argument("--project-id", default="atrium", help="Project identifier.")
    p_ads.add_argument("--budget-limit", type=float, default=None, help="Budget limit in USD.")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "publish-post":
        content_md = ""
        if args.file:
            content_md = Path(args.file).read_text(encoding="utf-8")
        elif args.content:
            content_md = args.content
        else:
            print("[ERROR] Must provide either --file or --content.", file=sys.stderr)
            return 1

        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        post = BlogPost(
            title=args.title,
            slug=args.slug,
            description=args.desc,
            date=datetime.date.today(),
            originalYear=datetime.date.today().year,
            tags=tags,
            featured=args.featured,
            lang=args.lang,
            content_md=content_md,
        )
        target_dir = Path(args.site_dir) if args.site_dir else None
        publisher = ContentPublisher(target_site_dir=target_dir)
        res = publisher.publish_blog_post(
            post,
            auto_scrub=not args.no_scrub,
            approved=args.approve,
            approver=args.approver if args.approve else None,
        )
        print(res.model_dump_json(indent=2))
        return 0 if res.success else 1

    elif args.command == "approve-post":
        target_dir = Path(args.site_dir) if args.site_dir else None
        publisher = ContentPublisher(target_site_dir=target_dir)
        res = publisher.approve_post(slug=args.slug, approver=args.approver)
        print(res.model_dump_json(indent=2))
        return 0 if res.success else 1

    elif args.command == "list-drafts":
        target_dir = Path(args.site_dir) if args.site_dir else None
        publisher = ContentPublisher(target_site_dir=target_dir)
        drafts = publisher.list_staged_posts()
        print(json.dumps(drafts, indent=2))
        return 0

    elif args.command == "publish-case":
        content_md = ""
        if args.file:
            content_md = Path(args.file).read_text(encoding="utf-8")
        elif args.content:
            content_md = args.content
        else:
            print("[ERROR] Must provide either --file or --content.", file=sys.stderr)
            return 1

        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        case = CaseStudy(
            title=args.title,
            slug=args.slug,
            era=args.era,
            anchorMetric=args.anchor_metric,
            anchorValue=args.anchor_value,
            period=args.period,
            featured=args.featured,
            order=args.order,
            confidentiality=args.confidentiality,
            tags=tags,
            content_md=content_md,
        )
        target_dir = Path(args.site_dir) if args.site_dir else None
        publisher = ContentPublisher(target_site_dir=target_dir)
        res = publisher.publish_case_study(case)
        print(res.model_dump_json(indent=2))
        return 0 if res.success else 1

    elif args.command == "lead-capture":
        lead = LeadCapture(
            lead_id=f"lead-{uuid.uuid4().hex[:8]}",
            project_id=args.project_id,
            name=args.name,
            email=args.email,
            company=args.company,
            phone=args.phone,
            utm_source=args.source,
            utm_medium=args.medium,
            utm_campaign=args.campaign,
            notes=args.notes,
        )
        mgr = LeadManager()
        res = mgr.capture_lead(
            lead,
            forward_n8n=not args.no_n8n,
            notify_telegram=not args.no_telegram,
        )
        print(res.model_dump_json(indent=2))
        return 0 if res.success else 1

    elif args.command == "list-leads":
        mgr = LeadManager()
        leads = mgr.list_leads(project_id=args.project_id, limit=args.limit)
        print(json.dumps([l.model_dump() for l in leads], indent=2))
        return 0

    elif args.command == "seo-audit":
        validator = SEOValidator(target_domain=args.domain)
        target = args.target.strip()
        if target.startswith("http://") or target.startswith("https://"):
            res = validator.audit_url(target)
        else:
            res = validator.audit_file(Path(target))
        print(res.model_dump_json(indent=2))
        return 0 if res.valid else 1

    elif args.command == "ga4-track":
        try:
            params = json.loads(args.params)
        except Exception:
            params = {}
        client_id = args.client_id or f"client-{uuid.uuid4().hex[:8]}"
        event = GA4Event(
            event_name=args.event,
            client_id=client_id,
            user_id=args.user_id,
            params=params,
        )
        client = GA4Client()
        ok = client.send_event(event)
        print(json.dumps({"success": ok, "event": event.model_dump()}, indent=2))
        return 0 if ok else 1

    elif args.command == "ads-report":
        mgr = GoogleAdsManager()
        report = mgr.generate_report(project_id=args.project_id, budget_limit_usd=args.budget_limit)
        print(report.model_dump_json(indent=2))
        return 0 if not report.budget_exceeded else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
