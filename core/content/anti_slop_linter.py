"""
Deterministic Anti-AI-Slop Linter and Polish Engine.
Detects buzzwords, formulaic openings, robotic cadence monotony, and hollow fillers.
Produces deterministic quality scores and automated scrubbing suggestions.
"""

import re
import math
from typing import List, Dict, Tuple, Optional, Any
from core.content.models import (
    SlopCategory,
    SeverityLevel,
    CleanlinessRating,
    SlopViolation,
    SlopReport,
)

# Canonical AI Slop Lexicons with suggestions
SLOP_LEXICON: List[Dict[str, Any]] = [
    # English Slop
    {"pattern": r"\bdelv(e|es|ed|ing)\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.CRITICAL, "suggestion": "explore, examine, analyze, inspect, dive into"},
    {"pattern": r"\btapestry\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.CRITICAL, "suggestion": "system, architecture, network, fabric, combination"},
    {"pattern": r"\btestament to\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "proof of, evidence of, shows that"},
    {"pattern": r"\bbeacon of\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "example of, benchmark for"},
    {"pattern": r"\bgame[- ]changer\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "significant improvement, catalyst, key shift"},
    {"pattern": r"\bunleash(ing|ed|es)?\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "enable, activate, release, run"},
    {"pattern": r"\brevolutioniz(e|es|ed|ing)\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "transform, upgrade, rebuild, accelerate"},
    {"pattern": r"\bplethora\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.MEDIUM, "suggestion": "many, wide range, abundance of"},
    {"pattern": r"\bparadigm shift\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.HIGH, "suggestion": "structural change, new approach"},
    {"pattern": r"\bharness(ing)? the power of\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "use, utilize, leverage, run"},
    {"pattern": r"\bin today'?s (fast-paced|rapidly evolving|digital) world\b", "category": SlopCategory.FORMULAIC_OPENER, "severity": SeverityLevel.CRITICAL, "suggestion": "cut the intro; start directly with the core problem or data"},
    {"pattern": r"\blook no further\b", "category": SlopCategory.FORMULAIC_OPENER, "severity": SeverityLevel.HIGH, "suggestion": "state the solution directly without cheerleading"},
    {"pattern": r"\bit'?s not just (about )?[^,.]+,\s*it'?s (about )?", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.HIGH, "suggestion": "state both properties directly without the fake rhetorical contrast"},
    {"pattern": r"\bnavigat(e|ing) the (complexities|landscape)\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.MEDIUM, "suggestion": "solve, address, manage, handle"},
    {"pattern": r"\bever-evolving\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.LOW, "suggestion": "changing, modern, active"},
    {"pattern": r"\bdemystify(ing)?\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.MEDIUM, "suggestion": "explain, clarify, break down"},
    {"pattern": r"\bsupercharg(e|ing|ed)\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.MEDIUM, "suggestion": "accelerate, optimize, speed up"},
    {"pattern": r"\bfoster(ing)?\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.LOW, "suggestion": "build, encourage, develop"},
    {"pattern": r"\bpivotal\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.LOW, "suggestion": "critical, key, central"},
    {"pattern": r"\bparamount\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.LOW, "suggestion": "essential, primary"},
    {"pattern": r"\bmoreover,\b", "category": SlopCategory.ROBOTIC_TRANSITION, "severity": SeverityLevel.MEDIUM, "suggestion": "also, additionally, or merge thoughts"},
    {"pattern": r"\bfurthermore,\b", "category": SlopCategory.ROBOTIC_TRANSITION, "severity": SeverityLevel.MEDIUM, "suggestion": "also, in addition, or omit transition"},
    {"pattern": r"\bin conclusion,\b", "category": SlopCategory.EMPTY_CONCLUSION, "severity": SeverityLevel.HIGH, "suggestion": "cut the label; end with the final takeaway or call to action directly"},

    # Portuguese Slop
    {"pattern": r"\bmergulh(ar|e|ando|o profundo)\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.CRITICAL, "suggestion": "analisar, explorar, examinar, inspecionar"},
    {"pattern": r"\btapeçaria\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.CRITICAL, "suggestion": "sistema, arquitetura, malha, ecossistema"},
    {"pattern": r"\bdivisor de águas\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "ponto de inflexão, grande avanço, mudança-chave"},
    {"pattern": r"\brevolucion(ar|ando|ou)\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "transformar, modernizar, acelerar"},
    {"pattern": r"\bfarol de\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "referência de, modelo para"},
    {"pattern": r"\btestemunho de\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "evidência de, prova de"},
    {"pattern": r"\bmudança de paradigma\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.HIGH, "suggestion": "mudança estrutural, novo padrão"},
    {"pattern": r"\bno mundo (acelerado|dinâmico|digital) de hoje\b", "category": SlopCategory.FORMULAIC_OPENER, "severity": SeverityLevel.CRITICAL, "suggestion": "vá direto ao ponto; inicie com métricas ou problema concreto"},
    {"pattern": r"\bnão procure mais\b", "category": SlopCategory.FORMULAIC_OPENER, "severity": SeverityLevel.HIGH, "suggestion": "apresente a solução sem ufanismo"},
    {"pattern": r"\bnão se trata apenas de [^,.]+,\s*(mas )?sim de\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.HIGH, "suggestion": "declare os fatos diretamente sem falso contraste retórico"},
    {"pattern": r"\baproveitar o poder d[aeo]s?\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.HIGH, "suggestion": "utilizar, executar, rodar"},
    {"pattern": r"\bdesmistificar\b", "category": SlopCategory.BUZZWORD, "severity": SeverityLevel.MEDIUM, "suggestion": "explicar, detalhar, abrir o capô"},
    {"pattern": r"\bpotencializar\b", "category": SlopCategory.HOLLOW_SUPERLATIVE, "severity": SeverityLevel.MEDIUM, "suggestion": "aumentar, acelerar, otimizar"},
    {"pattern": r"\bvale destacar que\b", "category": SlopCategory.HEDGE_FILLER, "severity": SeverityLevel.LOW, "suggestion": "destaque o fato diretamente sem a muleta introdutória"},
    {"pattern": r"\bimportante ressaltar que\b", "category": SlopCategory.HEDGE_FILLER, "severity": SeverityLevel.LOW, "suggestion": "remova o preâmbulo; afirme a informação diretamente"},
    {"pattern": r"\bademais,\b", "category": SlopCategory.ROBOTIC_TRANSITION, "severity": SeverityLevel.MEDIUM, "suggestion": "além disso, ou una as sentenças"},
    {"pattern": r"\bem suma,\b", "category": SlopCategory.EMPTY_CONCLUSION, "severity": SeverityLevel.HIGH, "suggestion": "remova o rótulo; feche com o takeaway objetivo"},
    {"pattern": r"\bem conclusão,\b", "category": SlopCategory.EMPTY_CONCLUSION, "severity": SeverityLevel.HIGH, "suggestion": "vá direto para a ação ou síntese final"},
]

# Replacement dictionary for automatic deterministic scrubbing
SCRUB_REPLACEMENTS: Dict[str, str] = {
    # English
    r"\bdelve into\b": "examine",
    r"\bdelve\b": "explore",
    r"\bdelving into\b": "examining",
    r"\ba tapestry of\b": "a network of",
    r"\btapestry\b": "architecture",
    r"\ba testament to\b": "evidence of",
    r"\bgame-changer\b": "major step forward",
    r"\bgame changer\b": "major step forward",
    r"\bunleash the power of\b": "deploy",
    r"\bunleash\b": "enable",
    r"\brevolutionize\b": "transform",
    r"\bplethora of\b": "wide range of",
    r"\bparadigm shift\b": "strategic shift",
    r"\bharness the power of\b": "use",
    r"\bFurthermore,\b": "Also,",
    r"\bMoreover,\b": "In addition,",
    r"\bIn conclusion,\b": "Bottom line:",
    r"\blook no further\b": "here is the solution",

    # Portuguese
    r"\bmergulho profundo\b": "análise técnica",
    r"\bmergulhar em\b": "analisar",
    r"\bmergulhar\b": "explorar",
    r"\bdivisor de águas\b": "marco decisivo",
    r"\brevolucionar\b": "transformar",
    r"\bfarol de\b": "referência de",
    r"\btestemunho de\b": "prova de",
    r"\bmudança de paradigma\b": "mudança estrutural",
    r"\baproveitar o poder d[aeo]\b": "usar",
    r"\bdesmistificar\b": "explicar",
    r"\bpotencializar\b": "otimizar",
    r"\bvale destacar que\b": "",
    r"\bimportante ressaltar que\b": "",
    r"\bAdemais,\b": "Além disso,",
    r"\bEm suma,\b": "Resumo prático:",
    r"\bEm conclusão,\b": "Conclusão prática:",
}


class AntiSlopLinter:
    """Deterministic, rule-based and statistical analyzer of text purity."""

    def __init__(self, custom_banned_words: Optional[List[str]] = None) -> None:
        self.custom_banned_words = custom_banned_words or []

    def audit(self, text: str) -> SlopReport:
        """Runs complete lexical, cadence, and syntactic audit on given text."""
        if not text or not text.strip():
            return SlopReport(
                slop_score=0.0,
                cleanliness_rating=CleanlinessRating.PRISTINE,
                violations_count=0,
                violations=[],
                sentence_count=0,
                average_sentence_length=0.0,
                sentence_length_variance=0.0,
                cadence_rating="Empty",
                passive_voice_count=0,
                top_fixes=[],
            )

        lines = text.splitlines()
        violations: List[SlopViolation] = []

        # 1. Lexical pattern matching
        for line_idx, line in enumerate(lines, start=1):
            if not line.strip():
                continue

            for entry in SLOP_LEXICON:
                matches = list(re.finditer(entry["pattern"], line, re.IGNORECASE))
                for m in matches:
                    matched_text = m.group(0)
                    start = max(0, m.start() - 25)
                    end = min(len(line), m.end() + 25)
                    context_snippet = f"...{line[start:end]}..."

                    violations.append(
                        SlopViolation(
                            term=matched_text,
                            category=entry["category"],
                            severity=entry["severity"],
                            context=context_snippet,
                            line_number=line_idx,
                            suggestion=entry["suggestion"],
                        )
                    )

            # Custom user banned words
            for banned in self.custom_banned_words:
                if not banned:
                    continue
                pattern = rf"\b{re.escape(banned)}\b"
                for m in re.finditer(pattern, line, re.IGNORECASE):
                    violations.append(
                        SlopViolation(
                            term=m.group(0),
                            category=SlopCategory.BUZZWORD,
                            severity=SeverityLevel.HIGH,
                            context=f"...{line[max(0, m.start()-20):min(len(line), m.end()+20)]}...",
                            line_number=line_idx,
                            suggestion=f"User-specified banned word: remove or replace '{banned}'",
                        )
                    )

            # Em-dash abuse check (>2 per single line)
            em_dash_count = line.count("—") + line.count("--")
            if em_dash_count >= 3:
                violations.append(
                    SlopViolation(
                        term="Multiple em-dashes",
                        category=SlopCategory.EM_DASH_ABUSE,
                        severity=SeverityLevel.MEDIUM,
                        context=f"...{line[:60]}...",
                        line_number=line_idx,
                        suggestion="Break into separate concise sentences; reduce excessive em-dashes.",
                    )
                )

        # 2. Cadence & Rhythm Analysis
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip().split()) > 2]
        sentence_lengths = [len(s.split()) for s in sentences]
        sentence_count = len(sentence_lengths)

        avg_len = sum(sentence_lengths) / sentence_count if sentence_count > 0 else 0.0
        variance = 0.0
        if sentence_count > 1:
            variance = sum((l - avg_len) ** 2 for l in sentence_lengths) / (sentence_count - 1)

        # Determine cadence rating
        if sentence_count <= 2:
            cadence_rating = "Short Form"
        elif variance < 8.0 and avg_len >= 8.0:
            cadence_rating = "Monotonous Robotic Droning"
            violations.append(
                SlopViolation(
                    term="Low sentence variance",
                    category=SlopCategory.CADENCE_MONOTONY,
                    severity=SeverityLevel.HIGH,
                    context=f"Variance is {variance:.1f} words² across {sentence_count} sentences (avg {avg_len:.1f} w/sentence).",
                    line_number=1,
                    suggestion="Vary sentence length dynamically. Mix 4-word punchy lines with longer explanatory ones.",
                )
            )
        elif avg_len < 8.0:
            cadence_rating = "Punchy & Concise"
        elif variance < 16.0:
            cadence_rating = "Balanced"
        else:
            cadence_rating = "Punchy & Dynamic"

        # 3. Passive voice proxy heuristic ("is being", "was created by", "foi desenvolvido por", etc.)
        passive_matches = len(re.findall(r"\b(is|was|were|been|being)\s+\w+ed\b", text, re.IGNORECASE)) + \
                          len(re.findall(r"\b(foi|foram|sendo)\s+\w+(ado|ido)\b", text, re.IGNORECASE))

        # 4. Calculate Slop Score (0.0 to 100.0)
        base_score = 0.0
        for v in violations:
            if v.severity == SeverityLevel.CRITICAL:
                base_score += 15.0
            elif v.severity == SeverityLevel.HIGH:
                base_score += 8.0
            elif v.severity == SeverityLevel.MEDIUM:
                base_score += 4.0
            elif v.severity == SeverityLevel.LOW:
                base_score += 1.5

        # Normalize score with word count weight so longer texts aren't unfairly penalized for 1 isolated phrase
        word_count = len(text.split())
        if word_count > 0:
            density_factor = (len(violations) / max(1, word_count / 100)) * 10.0
            raw_score = (base_score * 0.6) + (density_factor * 0.4)
        else:
            raw_score = 0.0

        # Monotony penalty only for longer sentences lacking burstiness
        if sentence_count >= 4 and avg_len >= 8.0 and variance < 8.0:
            raw_score += 12.0

        final_score = min(100.0, max(0.0, raw_score))

        # Classify Cleanliness
        if final_score < 5.0 and len(violations) == 0:
            cleanliness = CleanlinessRating.PRISTINE
        elif final_score < 15.0:
            cleanliness = CleanlinessRating.CLEAN
        elif final_score < 35.0:
            cleanliness = CleanlinessRating.MILD_SLOP
        elif final_score < 60.0:
            cleanliness = CleanlinessRating.HIGH_SLOP
        else:
            cleanliness = CleanlinessRating.TOXIC_SLOP

        top_fixes = [
            f"Line {v.line_number}: replace '{v.term}' -> {v.suggestion}"
            for v in violations[:5]
        ]

        return SlopReport(
            slop_score=final_score,
            cleanliness_rating=cleanliness,
            violations_count=len(violations),
            violations=violations,
            sentence_count=sentence_count,
            average_sentence_length=avg_len,
            sentence_length_variance=variance,
            cadence_rating=cadence_rating,
            passive_voice_count=passive_matches,
            top_fixes=top_fixes,
        )

    def scrub(self, text: str) -> Tuple[str, int]:
        """Performs automatic deterministic scrubbing of recognized slop terms."""
        scrubbed = text
        replacement_count = 0

        for pattern, replacement in SCRUB_REPLACEMENTS.items():
            matches = list(re.finditer(pattern, scrubbed, re.IGNORECASE))
            if matches:
                replacement_count += len(matches)
                scrubbed = re.sub(pattern, replacement, scrubbed, flags=re.IGNORECASE)

        # Clean double spaces that might arise from deletions
        scrubbed = re.sub(r"[ \t]+", " ", scrubbed)
        scrubbed = re.sub(r" \n", "\n", scrubbed)
        scrubbed = re.sub(r"\n{3,}", "\n\n", scrubbed)

        return scrubbed.strip(), replacement_count
