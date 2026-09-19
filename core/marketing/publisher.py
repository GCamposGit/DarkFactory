"""CMS & Blog Publishing Engine for Astro 5 Content Collections (HF-21).

Integrates with Skill 15 (Anti-Slop Content Engine) to format, validate, and inject
articles ('thinking') and case studies ('cases') into target sites (e.g. Atrium / Site_ggcampos).
Enforces mandatory Gate G1 (Human Approval Gate) before publishing authorial content to production.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.content.anti_slop_linter import AntiSlopLinter
from core.integrations.telegram import TelegramGateway, load_telegram_config
from .models import BlogPost, CaseStudy, PublishResult

import unicodedata

logger = logging.getLogger("darkfac.marketing.publisher")

DEFAULT_ATRIUM_PATH = Path(r"C:\dev\Site_ggcampos")
DEFAULT_STAGING_DIR = Path(".factory/marketing/staged_posts")


def slugify(text: str) -> str:
    """Generate a clean ASCII URL slug from title."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r"[^\w\s-]", "", normalized.lower())
    return re.sub(r"[-\s]+", "-", clean).strip("-")


class ContentPublisher:
    """Publishes validated, anti-slop Markdown/MDX into project content collections."""

    def __init__(
        self,
        target_site_dir: Optional[Path] = None,
        staging_dir: Optional[Path] = None,
        max_slop_score: float = 20.0,
        telegram_gateway: Optional[TelegramGateway] = None,
    ) -> None:
        self.target_site_dir = target_site_dir or Path(
            os.getenv("ATRIUM_SITE_DIR", str(DEFAULT_ATRIUM_PATH))
        )
        self.staging_dir = staging_dir or Path(
            os.getenv("MARKETING_STAGING_DIR", str(DEFAULT_STAGING_DIR))
        )
        self.max_slop_score = max_slop_score
        self.linter = AntiSlopLinter()
        self._telegram_gateway = telegram_gateway

    def _get_telegram_gateway(self) -> Optional[TelegramGateway]:
        if self._telegram_gateway is None:
            try:
                cfg = load_telegram_config()
                if cfg.bot_token:
                    self._telegram_gateway = TelegramGateway(config=cfg)
            except Exception:
                pass
        return self._telegram_gateway

    def publish_blog_post(
        self,
        post: BlogPost,
        auto_scrub: bool = True,
        approved: bool = False,
        approver: Optional[str] = None,
        notify_telegram: bool = True,
    ) -> PublishResult:
        """Publish a thinking article. Defaults to staging unless human approval is provided."""
        # 1. Anti-Slop Audit
        lint_report = self.linter.audit(post.content_md)
        content_to_write = post.content_md

        if lint_report.slop_score > self.max_slop_score:
            if auto_scrub:
                logger.info("Content exceeded slop threshold (%.1f); applying auto-scrub.", lint_report.slop_score)
                content_to_write, _ = self.linter.scrub(post.content_md)
                lint_report = self.linter.audit(content_to_write)
            else:
                found_terms = [v.term for v in lint_report.violations]
                return PublishResult(
                    success=False,
                    file_path="",
                    collection="thinking",
                    title=post.title,
                    slop_score=lint_report.slop_score,
                    status="rejected",
                    error_message=f"Post rejected due to high slop score ({lint_report.slop_score:.1f} > {self.max_slop_score:.1f}). Found buzzwords: {', '.join(found_terms)}",
                )

        # 2. Build Frontmatter compliant with Astro 5 schema
        slug = post.slug or slugify(post.title)
        date_str = post.date.strftime("%Y-%m-%d")
        tags_yaml = "\n".join([f'  - "{t}"' for t in post.tags]) if post.tags else "  []"

        frontmatter_lines = [
            "---",
            f'title: "{post.title}"',
            f'description: "{post.description or ""}"',
            f"date: {date_str}",
            f"originalYear: {post.originalYear}",
            f"featured: {'true' if post.featured else 'false'}",
            f'lang: "{post.lang}"',
        ]
        if post.image:
            frontmatter_lines.append(f'image: "{post.image}"')
        if post.url:
            frontmatter_lines.append(f'url: "{post.url}"')
        if post.readTime:
            frontmatter_lines.append(f'readTime: "{post.readTime}"')

        frontmatter_lines.append("tags:")
        frontmatter_lines.append(tags_yaml)
        frontmatter_lines.append("---\n\n")

        full_content = "\n".join(frontmatter_lines) + content_to_write.strip() + "\n"

        # 3. Human Gate G1 Enforcement
        if not approved:
            # Staging: require human approval before production promotion
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            staging_file = self.staging_dir / f"{slug}.md"
            try:
                staging_file.write_text(full_content, encoding="utf-8")
                logger.info("Staged thinking post '%s' in %s awaiting human approval.", slug, staging_file)

                # Send Telegram notification if gateway available
                tg = self._get_telegram_gateway()
                if notify_telegram and tg and tg.config.authorized_chat_ids:
                    chat_id = tg.config.authorized_chat_ids[0]
                    text = (
                        f"📝 <b>Novo Artigo Aguardando Aprovação Humana (Gate G1)</b>\n"
                        f"<b>Título:</b> {post.title}\n"
                        f"<b>Slug:</b> <code>{slug}</code>\n"
                        f"<b>Slop Score:</b> {lint_report.slop_score:.1f}\n\n"
                        f"Para revisar e aprovar para produção:\n"
                        f"<code>python C:\\dev\\DarkFac\\core\\marketing\\cli.py approve-post --slug {slug}</code>"
                    )
                    tg.send_message(chat_id=chat_id, text=text)

                return PublishResult(
                    success=True,
                    file_path=str(staging_file),
                    collection="thinking",
                    title=post.title,
                    slop_score=lint_report.slop_score,
                    status="staged_pending_approval",
                    requires_human_approval=True,
                    approver=None,
                    error_message=None,
                )
            except Exception as exc:
                return PublishResult(
                    success=False,
                    file_path=str(staging_file),
                    collection="thinking",
                    title=post.title,
                    slop_score=lint_report.slop_score,
                    status="rejected",
                    requires_human_approval=True,
                    error_message=str(exc),
                )

        # Approved by Human: Deploy to live production Astro collection
        dest_dir = self.target_site_dir / "src" / "content" / "thinking"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"{slug}.md"

        try:
            dest_file.write_text(full_content, encoding="utf-8")
            # Clean up staging file if present
            staging_file = self.staging_dir / f"{slug}.md"
            if staging_file.is_file():
                staging_file.unlink()
            logger.info("Published thinking post to production '%s' (Approved by: %s)", dest_file, approver or "owner")
            return PublishResult(
                success=True,
                file_path=str(dest_file),
                collection="thinking",
                title=post.title,
                slop_score=lint_report.slop_score,
                status="published",
                requires_human_approval=False,
                approver=approver or "owner",
                error_message=None,
            )
        except Exception as exc:
            logger.error("Failed to write post to %s: %s", dest_file, exc)
            return PublishResult(
                success=False,
                file_path=str(dest_file),
                collection="thinking",
                title=post.title,
                slop_score=lint_report.slop_score,
                status="rejected",
                error_message=str(exc),
            )

    def approve_post(self, slug: str, approver: str = "owner") -> PublishResult:
        """Promote a staged draft post to live production Astro collection upon human approval."""
        staging_file = self.staging_dir / f"{slug}.md"
        if not staging_file.is_file():
            return PublishResult(
                success=False,
                file_path="",
                collection="thinking",
                title=slug,
                status="rejected",
                error_message=f"No staged post found for slug '{slug}' in {self.staging_dir}",
            )

        content = staging_file.read_text(encoding="utf-8")
        dest_dir = self.target_site_dir / "src" / "content" / "thinking"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"{slug}.md"

        try:
            dest_file.write_text(content, encoding="utf-8")
            staging_file.unlink()
            logger.info("Approved and promoted '%s' to production by '%s'", slug, approver)
            return PublishResult(
                success=True,
                file_path=str(dest_file),
                collection="thinking",
                title=slug,
                status="published",
                requires_human_approval=False,
                approver=approver,
            )
        except Exception as exc:
            return PublishResult(
                success=False,
                file_path=str(dest_file),
                collection="thinking",
                title=slug,
                status="rejected",
                error_message=str(exc),
            )

    def list_staged_posts(self) -> List[Dict[str, Any]]:
        """List all draft posts awaiting human gate approval."""
        if not self.staging_dir.exists():
            return []
        drafts = []
        for file in sorted(self.staging_dir.glob("*.md")):
            slug = file.stem
            title = slug
            try:
                text = file.read_text(encoding="utf-8")
                match = re.search(r'title:\s*["\'](.*?)["\']', text)
                if match:
                    title = match.group(1)
            except Exception:
                pass
            drafts.append({
                "slug": slug,
                "title": title,
                "staged_path": str(file),
            })
        return drafts

    def publish_case_study(self, case: CaseStudy) -> PublishResult:
        """Publish a case deliverable to src/content/cases/."""
        slug = case.slug or slugify(case.title)
        tags_yaml = "\n".join([f'  - "{t}"' for t in case.tags]) if case.tags else "  []"

        anchor_val = f'"{case.anchorValue}"' if isinstance(case.anchorValue, str) else case.anchorValue

        frontmatter_lines = [
            "---",
            f'title: "{case.title}"',
            f'era: "{case.era}"',
            f'anchorMetric: "{case.anchorMetric}"',
            f"anchorValue: {anchor_val}",
            f'period: "{case.period}"',
            f"featured: {'true' if case.featured else 'false'}",
            f"order: {case.order}",
            f'confidentiality: "{case.confidentiality}"',
            "tags:",
            tags_yaml,
            "---\n\n",
        ]

        full_content = "\n".join(frontmatter_lines) + case.content_md.strip() + "\n"

        dest_dir = self.target_site_dir / "src" / "content" / "cases"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"{slug}.md"

        try:
            dest_file.write_text(full_content, encoding="utf-8")
            logger.info("Published case study to '%s'", dest_file)
            return PublishResult(
                success=True,
                file_path=str(dest_file),
                collection="cases",
                title=case.title,
                slop_score=0.0,
                status="published",
                requires_human_approval=False,
                error_message=None,
            )
        except Exception as exc:
            logger.error("Failed to write case to %s: %s", dest_file, exc)
            return PublishResult(
                success=False,
                file_path=str(dest_file),
                collection="cases",
                title=case.title,
                slop_score=0.0,
                status="rejected",
                error_message=str(exc),
            )
