"""CMS & Blog Publishing Engine for Astro 5 Content Collections (HF-21).

Integrates with Skill 15 (Anti-Slop Content Engine) to format, validate, and inject
articles ('thinking') and case studies ('cases') directly into target sites (e.g. Atrium / Site_ggcampos).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from core.content.anti_slop_linter import AntiSlopLinter
from .models import BlogPost, CaseStudy, PublishResult

logger = logging.getLogger("darkfac.marketing.publisher")

DEFAULT_ATRIUM_PATH = Path(r"C:\dev\Site_ggcampos")


def slugify(text: str) -> str:
    """Generate a clean URL slug from title."""
    clean = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", clean).strip("-")


class ContentPublisher:
    """Publishes validated, anti-slop Markdown/MDX into project content collections."""

    def __init__(self, target_site_dir: Optional[Path] = None, max_slop_score: float = 20.0) -> None:
        self.target_site_dir = target_site_dir or Path(
            os.getenv("ATRIUM_SITE_DIR", str(DEFAULT_ATRIUM_PATH))
        )
        self.max_slop_score = max_slop_score
        self.linter = AntiSlopLinter()

    def publish_blog_post(self, post: BlogPost, auto_scrub: bool = True) -> PublishResult:
        """Publish a thinking article to src/content/thinking/."""
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

        # 3. Determine destination path
        dest_dir = self.target_site_dir / "src" / "content" / "thinking"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / f"{slug}.md"

        try:
            dest_file.write_text(full_content, encoding="utf-8")
            logger.info("Published thinking post to '%s'", dest_file)
            return PublishResult(
                success=True,
                file_path=str(dest_file),
                collection="thinking",
                title=post.title,
                slop_score=lint_report.slop_score,
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
                error_message=str(exc),
            )

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
                error_message=str(exc),
            )
