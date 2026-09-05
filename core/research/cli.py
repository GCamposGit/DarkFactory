"""
Command-line Interface for the DarkFac Research Engine.
Supports headless execution, script pipelines, and agent tools.
"""

import sys
import json
import argparse
from pathlib import Path
from typing import Optional

# Ensure repository root is in sys.path when executed directly
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure UTF-8 on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.research.models import ResearchTopicType, SourceInsight, AuthorityTier
from core.research.classifier import (
    classify_research_intent,
    extract_search_keywords,
    detect_programming_language,
)
from core.research.arxiv_client import ArxivClient
from core.research.github_scout import GitHubScout
from core.research.trends_scout import ExpertTrendsScout
from core.research.ledger import KnowledgeLedgerManager





def handle_classify(args):
    result = classify_research_intent(args.query, explicit_override=args.override)
    output = {
        "query": args.query,
        "topic_type": result.topic_type.value,
        "confidence": result.confidence,
        "reason": result.reason,
        "matched_patterns": result.matched_patterns,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def handle_papers(args):
    client = ArxivClient(timeout_sec=args.timeout)
    sources = client.search_papers(args.query, max_results=args.limit)
    out = [s.to_dict() for s in sources]
    print(json.dumps(out, indent=2, ensure_ascii=False))


def handle_scout(args):
    scout = GitHubScout(timeout_sec=args.timeout)
    sources = scout.search_repositories(
        query=args.query,
        language=args.language,
        min_stars=args.min_stars,
        limit=args.limit,
        permissive_only=args.permissive_only,
    )
    out = [s.to_dict() for s in sources]
    print(json.dumps(out, indent=2, ensure_ascii=False))


def handle_trends(args):
    scout = ExpertTrendsScout(timeout_sec=args.timeout)
    sources = scout.search_trends(args.query, limit=args.limit)
    out = [s.to_dict() for s in sources]
    print(json.dumps(out, indent=2, ensure_ascii=False))


def handle_auto(args):
    """
    Dual-mode autonomous research:
    1. Classifies the query
    2. Mines papers or repositories based on classification (High Credibility Tier)
    3. Mines community & expert trends for feature ideation (Trend Signal Tier)
    4. Populates Knowledge Ledger with canonical sources & trend insights
    5. Persists to .factory/research/<ledger_id>/
    """
    mgr = KnowledgeLedgerManager()
    intent = classify_research_intent(args.query, explicit_override=args.override)

    ledger = mgr.create_ledger(
        query=args.query,
        topic_type=intent.topic_type,
        ledger_id=args.ledger_id
    )

    if intent.topic_type == ResearchTopicType.TOPIC_CONCEPT:
        print(f"[*] Modo: Topic Deep Research (Papers, Arquitetura, Conceitos) -> {intent.reason}")
        client = ArxivClient(timeout_sec=args.timeout)
        keywords = extract_search_keywords(args.query)
        print(f"[*] Termos de busca acadêmica no arXiv: '{keywords}'")
        sources = client.search_papers(keywords, max_results=args.limit)
        if not sources and keywords != args.query:
            sources = client.search_papers(args.query, max_results=args.limit)

        for s in sources:
            ledger.add_source(s)
            insight = SourceInsight(
                source_id=s.id,
                source_title=s.title,
                key_insight=f"Fundamentação teórica extraída de: {s.summary[:240]}...",
                architectural_implications="Basear o design do componente nos padrões formais documentados neste paper.",
                future_reference_value="Consultar equações e garantias de consistência em versões futuras.",
                authority_tier=AuthorityTier.HIGH_CREDIBILITY,
                code_patterns_or_algorithms=[s.metadata.get("arxiv_id", "algorithm")]
            )
            ledger.add_insight(insight)

        ledger.summary_executive = (
            f"Pesquisa conceitual estruturada sobre '{args.query}'. "
            f"Catalogadas {len(sources)} publicações acadêmicas de alta relevância com DOI/arXiv IDs."
        )

    else:
        print(f"[*] Modo: Repo Code Scout (Código Existente & Reúso de Componentes) -> {intent.reason}")
        scout = GitHubScout(timeout_sec=args.timeout)
        detected_lang = args.language or detect_programming_language(args.query)
        keywords = extract_search_keywords(args.query)
        print(f"[*] Termos técnicos de busca no GitHub: '{keywords}' (Linguagem: {detected_lang or 'todas'})")

        sources = scout.search_repositories(
            query=keywords,
            language=detected_lang,
            min_stars=args.min_stars,
            limit=args.limit,
            permissive_only=args.permissive_only
        )
        if not sources and args.min_stars > 0:
            # Fallback relaxing min_stars
            sources = scout.search_repositories(
                query=keywords,
                language=detected_lang,
                min_stars=0,
                limit=args.limit,
                permissive_only=args.permissive_only
            )

        for s in sources:
            ledger.add_source(s)
            is_permissive = s.license_category.value == "permissive"
            insight = SourceInsight(
                source_id=s.id,
                source_title=s.title,
                key_insight=(
                    f"Componente maduro ({s.stars} ⭐, licença {s.license}). "
                    f"Descrição: {s.summary}"
                ),
                architectural_implications=(
                    "Reutilizar ou adaptar módulo testado como dependência/serviço isolado "
                    if is_permissive else
                    "ATENÇÃO: Licença restritiva/desconhecida. Usar apenas como referência de arquitetura, sem cópia direta."
                ),
                future_reference_value=f"Referência canônica de implementação testada para a feature: {s.url}",
                authority_tier=AuthorityTier.HIGH_CREDIBILITY,
                code_patterns_or_algorithms=[s.title]
            )
            ledger.add_insight(insight)

        ledger.summary_executive = (
            f"Mineração de repositórios para '{args.query}'. "
            f"Identificados {len(sources)} repositórios existentes com componentes de alta qualidade para evitar reinventar a roda."
        )

    # Secondary Tier: Community & Expert Trends (For Feature Ideation)
    if getattr(args, "include_trends", True):
        trend_keywords = extract_search_keywords(args.query)
        print(f"[*] Radar de Tendências & Experts: Consultando discussões de engenheiros para '{trend_keywords}'...")
        trend_scout = ExpertTrendsScout(timeout_sec=args.timeout)
        trend_sources = trend_scout.search_trends(trend_keywords, limit=2)
        if trend_sources:
            print(f"[*] Incorporadas {len(trend_sources)} tendências/ideias de features da comunidade.")
            for ts in trend_sources:
                ledger.add_source(ts)
            for ti in trend_scout.generate_trend_insights(trend_sources):
                ledger.add_insight(ti)

    saved_dir = mgr.save_ledger(ledger)

    print(f"\n[OK] Knowledge Ledger salvo com sucesso!")
    print(f"     Diretório: {saved_dir}")
    print(f"     JSON Canônico: {saved_dir / 'ledger.json'}")
    print(f"     Dossiê de Insights: {saved_dir / 'INSIGHTS.md'}")
    print("\n[HEADER DE CÓDIGO RECOMENDADO]")
    print(mgr.generate_code_header_reference(ledger.ledger_id))


def handle_list(args):
    mgr = KnowledgeLedgerManager()
    ledgers = mgr.list_ledgers()
    print(json.dumps(ledgers, indent=2, ensure_ascii=False))


def handle_show(args):
    mgr = KnowledgeLedgerManager()
    ledger = mgr.load_ledger(args.ledger_id)
    if not ledger:
        print(f"[ERROR] Ledger '{args.ledger_id}' não encontrado.", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(ledger.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(ledger.to_markdown())


def main():
    parser = argparse.ArgumentParser(description="DarkFac Research Engine CLI (2026 Edition)")
    subparsers = parser.add_subparsers(dest="command")

    # Command: classify
    cmd_class = subparsers.add_parser("classify", help="Classify research intent (concept vs code reuse)")
    cmd_class.add_argument("query", help="Query or ticket description to evaluate")
    cmd_class.add_argument("--override", choices=["concept", "code"], help="Explicit override")

    # Command: papers
    cmd_papers = subparsers.add_parser("papers", help="Search arXiv scientific papers")
    cmd_papers.add_argument("query", help="Academic or architectural topic")
    cmd_papers.add_argument("--limit", type=int, default=5, help="Max results")
    cmd_papers.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")

    # Command: scout
    cmd_scout = subparsers.add_parser("scout", help="Search GitHub for reusable repositories and components")
    cmd_scout.add_argument("query", help="Codebase search terms")
    cmd_scout.add_argument("--language", help="Filter by programming language")
    cmd_scout.add_argument("--min-stars", type=int, default=30, help="Minimum star count")
    cmd_scout.add_argument("--permissive-only", action="store_true", help="Only permissive licenses (MIT/Apache/BSD)")
    cmd_scout.add_argument("--limit", type=int, default=5, help="Max results")
    cmd_scout.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")

    # Command: trends
    cmd_trends = subparsers.add_parser("trends", help="Search developer trends and expert discussions for feature ideation")
    cmd_trends.add_argument("query", help="Topic to track trends")
    cmd_trends.add_argument("--limit", type=int, default=3, help="Max results")
    cmd_trends.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")

    # Command: auto
    cmd_auto = subparsers.add_parser("auto", help="Autonomous dual-mode research with Knowledge Ledger persistence")
    cmd_auto.add_argument("query", help="Research question, ticket or specification")
    cmd_auto.add_argument("--override", choices=["concept", "code"], help="Explicit override")
    cmd_auto.add_argument("--language", help="Filter language if code reuse")
    cmd_auto.add_argument("--min-stars", type=int, default=30, help="Min stars if code reuse")
    cmd_auto.add_argument("--permissive-only", action="store_true", help="Enforce permissive licenses only")
    cmd_auto.add_argument("--limit", type=int, default=5, help="Max sources")
    cmd_auto.add_argument("--ledger-id", help="Custom ledger slug/id")
    cmd_auto.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")
    cmd_auto.add_argument("--no-trends", dest="include_trends", action="store_false", help="Disable community/expert trend signals")
    cmd_auto.set_defaults(include_trends=True)

    # Command: list
    subparsers.add_parser("list", help="List all saved research dossiers in .factory/research/")

    # Command: show
    cmd_show = subparsers.add_parser("show", help="Display an existing research ledger")
    cmd_show.add_argument("ledger_id", help="Ledger ID or slug")
    cmd_show.add_argument("--json", action="store_true", help="Output as JSON instead of Markdown")

    args = parser.parse_args()

    if args.command == "classify":
        handle_classify(args)
    elif args.command == "papers":
        handle_papers(args)
    elif args.command == "scout":
        handle_scout(args)
    elif args.command == "trends":
        handle_trends(args)
    elif args.command == "auto":
        handle_auto(args)
    elif args.command == "list":
        handle_list(args)
    elif args.command == "show":
        handle_show(args)
    else:
        parser.print_help()



if __name__ == "__main__":
    main()
