"""
Research Intent Classifier.
Determines whether a research request targets:
- TOPIC_CONCEPT: General topics, best practices, architecture, scientific papers.
- CODE_REUSE: Existing codebases, open-source repositories, pre-built tested components.
Operates deterministically using fast keyword/pattern rules with optional local LLM arbitration.
"""

import re
from dataclasses import dataclass
from typing import List, Optional
from core.research.models import ResearchTopicType


@dataclass
class ClassificationResult:
    topic_type: ResearchTopicType
    confidence: float
    reason: str
    matched_patterns: List[str]


# Keywords & regex patterns signalling code reuse / existing repository search
CODE_PATTERNS = [
    r"\breposit[oó]rio(s)?\b",
    r"\bgithub\b",
    r"\bcodebase(s)?\b",
    r"\bc[oó]digo(s)?\s+(j[aá]\s+)?(existente|escrito|pronto|codado)\b",
    r"\breaproveit(ar|amento)\b",
    r"\bn[aã]o\s+reinventar\s+a\s+roda\b",
    r"\bcomponente(s)?\s+(testado(s)?|pronto(s)?|reutiliz[aá]ve(l|is))\b",
    r"\bbiblioteca(s)?\s+(pronta(s)?|existente(s)?|python|npm|rust|go)\b",
    r"\blib(s)?\b",
    r"\bpackage(s)?\b",
    r"\bopen-source\b",
    r"\bexisting\s+code\b",
    r"\bimplementation\s+(repo|repository|github)\b",
    r"\bpre-built\b",
    r"\balready\s+(written|implemented|coded)\b",
]

# Keywords & regex patterns signalling conceptual, architectural, or scientific research
TOPIC_PATTERNS = [
    r"\bpaper(s)?\b",
    r"\barxiv\b",
    r"\bpesquisa(\s+cient[ií]fica)?\b",
    r"\bmelhores\s+pr[aá]ticas\b",
    r"\barquitetura\b",
    r"\bconceito(s)?\b",
    r"\bestado\s+da\s+arte\b",
    r"\bstate\s+of\s+the\s+art\b",
    r"\btradeoff(s)?\b",
    r"\brfc\b",
    r"\bacademic(o)?\b",
    r"\bteoria\b",
    r"\bscientific\b",
    r"\bdeep\s+research\b",
    r"\bliterature\s+review\b",
    r"\bhow\s+does\s+.+\s+work\b",
    r"\bbenchmark(s)?\b",
]


def classify_research_intent(query: str, explicit_override: Optional[str] = None) -> ClassificationResult:
    """
    Classifies the user query or ticket into TOPIC_CONCEPT or CODE_REUSE.
    """
    if explicit_override:
        override_clean = explicit_override.strip().lower()
        if override_clean in ["code", "repo", "code_reuse", "repository"]:
            return ClassificationResult(
                topic_type=ResearchTopicType.CODE_REUSE,
                confidence=1.0,
                reason="Explicit override set to CODE_REUSE.",
                matched_patterns=["explicit_override"]
            )
        elif override_clean in ["topic", "concept", "paper", "architecture", "topic_concept"]:
            return ClassificationResult(
                topic_type=ResearchTopicType.TOPIC_CONCEPT,
                confidence=1.0,
                reason="Explicit override set to TOPIC_CONCEPT.",
                matched_patterns=["explicit_override"]
            )

    normalized = query.lower()

    matched_code = [p for p in CODE_PATTERNS if re.search(p, normalized)]
    matched_topic = [p for p in TOPIC_PATTERNS if re.search(p, normalized)]

    code_score = len(matched_code)
    topic_score = len(matched_topic)

    if code_score > topic_score:
        conf = min(0.95, 0.6 + (0.1 * code_score))
        return ClassificationResult(
            topic_type=ResearchTopicType.CODE_REUSE,
            confidence=conf,
            reason=f"Detected {code_score} code-reuse indicators (e.g. repos, components, existing code).",
            matched_patterns=matched_code
        )
    elif topic_score > code_score:
        conf = min(0.95, 0.6 + (0.1 * topic_score))
        return ClassificationResult(
            topic_type=ResearchTopicType.TOPIC_CONCEPT,
            confidence=conf,
            reason=f"Detected {topic_score} conceptual/academic indicators (e.g. papers, architecture, best practices).",
            matched_patterns=matched_topic
        )
    else:
        # Default fallback heuristic:
        # If query contains explicit programming terms or "github.com", treat as code.
        # Otherwise, default to conceptual research to build proper mental model first.
        if "github.com" in normalized or "clone" in normalized or "git" in normalized:
            return ClassificationResult(
                topic_type=ResearchTopicType.CODE_REUSE,
                confidence=0.6,
                reason="Tie-breaker: Query references git/github URLs.",
                matched_patterns=["github/git reference"]
            )
        return ClassificationResult(
            topic_type=ResearchTopicType.TOPIC_CONCEPT,
            confidence=0.55,
            reason="Tie-breaker / Default: Investigating conceptual foundation and best practices first.",
            matched_patterns=[]
        )


STOP_WORDS = {
    "procurar", "buscar", "pesquisar", "encontrar", "código", "codigo", "já", "ja", "existente",
    "existentes", "escrito", "escritos", "repositório", "repositorio", "repositórios", "repositorios",
    "github", "biblioteca", "bibliotecas", "lib", "libs", "componente", "componentes", "pronto",
    "pronta", "prontos", "prontas", "testado", "testada", "testados", "testadas", "não", "nao",
    "reinventar", "a", "o", "as", "os", "roda", "sobre", "qual", "quais", "melhores", "práticas",
    "praticas", "papers", "paper", "artigo", "artigos", "recentes", "no", "na", "nos", "nas",
    "em", "para", "de", "do", "da", "dos", "das", "e", "com", "por", "um", "uma", "uns", "umas",
    "how", "what", "where", "why", "best", "practices", "find", "search", "code", "existing",
    "already", "written", "library", "component", "components", "tested"
}

KNOWN_LANGUAGES = {
    "python": "python",
    "typescript": "typescript",
    "javascript": "javascript",
    "rust": "rust",
    "golang": "go",
    "go": "go",
    "java": "java",
    "c++": "c++",
    "cpp": "c++",
    "c#": "c#",
    "csharp": "c#",
}


def detect_programming_language(text: str) -> Optional[str]:
    """Detects programming language mentioned in the text."""
    lowered = text.lower()
    for word, lang in KNOWN_LANGUAGES.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return lang
    return None


def extract_search_keywords(query: str, max_words: int = 5) -> str:
    """
    Cleans natural language noise from query to maximize hits in GitHub API / arXiv search.
    """
    # Remove punctuation
    cleaned = re.sub(r"[^\w\s-]", " ", query.lower())
    tokens = cleaned.split()

    filtered = [t for t in tokens if t not in STOP_WORDS and len(t) > 1]
    if not filtered:
        return query.strip()

    # Prioritize acronyms or upper terms if they existed
    return " ".join(filtered[:max_words])

