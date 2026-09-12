"""
Multi-Target Renderer for Session Learning Packs.
Outputs GitHub-flavored Markdown, standalone interactive HTML widgets,
and Anki / Spaced Repetition TSV decks.
"""

import html
import json
from typing import Optional
from core.learning_pack.models import SessionLearningPack, LearningConcept, ActiveRecallCard


class LearningPackRenderer:
    """Renders SessionLearningPack into Markdown, Standalone HTML, or Anki TSV."""

    @staticmethod
    def render_markdown(pack: SessionLearningPack, brief_mode: bool = False) -> str:
        """Renders learning pack as GitHub-flavored Markdown for chat and documentation."""
        md = []
        md.append(f"# 🧠 {pack.title}")
        md.append(f"**Session**: `{pack.session_id}` | **Generated**: `{pack.timestamp[:19].replace('T', ' ')} UTC`")
        md.append(f"**Read Time**: ~{pack.metrics.get('estimated_read_time_min', 3)} min | **Concepts**: {len(pack.concepts)} | **Flashcards**: {len(pack.flashcards)}")
        md.append("")
        md.append(f"> 💡 **Executive Summary**: {pack.executive_summary}")
        md.append("")
        md.append("---")
        md.append("")

        for idx, c in enumerate(pack.concepts, 1):
            md.append(f"## {idx}. {c.name} (`{c.category.value if hasattr(c.category, 'value') else c.category}`)")
            if c.code_anchor:
                md.append(f"*Applied in*: `{c.code_anchor}`")
            md.append("")
            md.append(f"⚓ **The Mental Anchor (Sticky Metaphor)**:  \n*{c.mental_anchor}*")
            md.append("")
            md.append("### 🎙️ 1. The 30-Second Elevator Pitch (For Clients, PMs & Executives)")
            md.append(f"> {c.tiers.pitch_30s}")
            md.append("")
            md.append("### 🏛️ 2. Staff+ Architectural Rationale (For Tech Leads & Senior Peers)")
            md.append(f"{c.tiers.staff_architect}")
            md.append("")

            if not brief_mode:
                md.append("### ⚙️ 3. Engine Room Mechanics (Under The Hood)")
                md.append(f"{c.tiers.under_the_hood}")
                md.append("")

            if c.defense:
                md.append("### 🛡️ Third-Party Defense Shield (Answering Skeptics & Reviewers)")
                for d in c.defense:
                    md.append(f"- **Q: {d.question}**")
                    md.append(f"  - **A:** {d.bulletproof_answer}")
                md.append("")

            if c.trade_offs and not brief_mode:
                md.append("### ⚖️ Trade-off Decision Matrix")
                md.append("| Option | Pros | Cons | Why Chosen? |")
                md.append("| :--- | :--- | :--- | :--- |")
                for t in c.trade_offs:
                    md.append(f"| **{t.option}** | {t.pros} | {t.cons} | {t.why_chosen} |")
                md.append("")

            md.append("---")
            md.append("")

        # Active Recall Section
        if pack.flashcards:
            md.append("## 🃏 Active Recall Flashcards (Spaced Repetition)")
            md.append("Test your retention right now. Cover the answer, speak your explanation out loud, then verify:")
            md.append("")
            for idx, card in enumerate(pack.flashcards, 1):
                md.append(f"#### Card {idx}: {card.front_prompt}")
                md.append("<details>")
                md.append("<summary>🔍 Reveal Technical Explanation</summary>")
                md.append("")
                md.append(f"**Answer:** {card.back_solution}")
                md.append("")
                md.append(f"*Cognitive Takeaway:* {card.why_it_matters}")
                md.append(f"*Tags:* `{'`, `'.join(card.tags)}`")
                md.append("</details>")
                md.append("")

        if pack.discussion_topics:
            md.append("## 💬 Tópicos para Alinhamento & Discussão do Owner (Opcional)")
            md.append("> ℹ️ *A leitura e discussão destes tópicos é opcional e NÃO bloqueia a produção nem o próximo ticket.*")
            md.append("")
            for idx, topic in enumerate(pack.discussion_topics, 1):
                md.append(f"{idx}. {topic}")
            md.append("")

        return "\n".join(md)

    @staticmethod
    def render_anki_tsv(pack: SessionLearningPack) -> str:
        """Exports flashcards in standard Anki TSV format (Front \\t Back \\t Tags)."""
        lines = []
        for card in pack.flashcards:
            front = card.front_prompt.replace("\t", " ").replace("\n", "<br>")
            back = f"{card.back_solution}<br><br><i>Why it matters:</i> {card.why_it_matters}".replace("\t", " ").replace("\n", "<br>")
            tags = " ".join([t.replace(" ", "_") for t in card.tags])
            lines.append(f"{front}\t{back}\t{tags}")
        return "\n".join(lines)

    @staticmethod
    def render_html(pack: SessionLearningPack) -> str:
        """
        Renders a fully self-contained, offline interactive HTML widget
        with 3D card flips, tabbed tier switching, and instant pitch copying.
        """
        pack_json = json.dumps(pack.to_dict())

        html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(pack.title)} | DarkFac Learning Pack</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: rgba(22, 30, 49, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent-blue: #3b82f6;
      --accent-cyan: #06b6d4;
      --accent-purple: #8b5cf6;
      --accent-emerald: #10b981;
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    }}
    body {{
      background-color: var(--bg);
      color: var(--text-main);
      padding: 2rem 1.5rem;
      min-height: 100vh;
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
    }}
    header {{
      margin-bottom: 2.5rem;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 1.5rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      background: rgba(59, 130, 246, 0.15);
      color: var(--accent-cyan);
      border: 1px solid rgba(6, 182, 212, 0.3);
      margin-bottom: 0.75rem;
    }}
    h1 {{
      font-size: 2.2rem;
      font-weight: 800;
      background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 50%, #94a3b8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 0.5rem;
    }}
    .meta-bar {{
      display: flex;
      gap: 1.5rem;
      color: var(--text-muted);
      font-size: 0.85rem;
      margin-top: 0.5rem;
    }}
    .summary-box {{
      background: linear-gradient(135deg, rgba(30, 41, 59, 0.6) 0%, rgba(15, 23, 42, 0.9) 100%);
      border: 1px solid rgba(59, 130, 246, 0.3);
      border-radius: 12px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 2.5rem;
      box-shadow: 0 8px 24px -6px rgba(0,0,0,0.5);
    }}
    .concept-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 1.75rem;
      margin-bottom: 2rem;
      backdrop-filter: blur(12px);
      box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
      transition: border-color 0.2s;
    }}
    .concept-card:hover {{
      border-color: rgba(99, 102, 241, 0.4);
    }}
    .concept-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 1rem;
    }}
    .concept-title {{
      font-size: 1.4rem;
      font-weight: 700;
      color: #ffffff;
    }}
    .anchor-quote {{
      font-style: italic;
      color: #93c5fd;
      background: rgba(30, 58, 138, 0.25);
      border-left: 3px solid var(--accent-blue);
      padding: 0.75rem 1rem;
      border-radius: 0 8px 8px 0;
      margin-bottom: 1.25rem;
      font-size: 0.95rem;
    }}
    .tab-bar {{
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
      padding-bottom: 0.5rem;
    }}
    .tab-btn {{
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      padding: 0.4rem 0.8rem;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 600;
      transition: all 0.2s;
    }}
    .tab-btn:hover {{
      color: #ffffff;
      background: rgba(255, 255, 255, 0.05);
    }}
    .tab-btn.active {{
      color: var(--accent-cyan);
      background: rgba(6, 182, 212, 0.12);
      border-color: rgba(6, 182, 212, 0.3);
    }}
    .tier-content {{
      display: none;
      padding: 1rem;
      background: rgba(15, 23, 42, 0.5);
      border-radius: 8px;
      font-size: 0.95rem;
      line-height: 1.6;
      margin-bottom: 1rem;
    }}
    .tier-content.active {{
      display: block;
    }}
    .defense-box {{
      background: rgba(139, 92, 246, 0.08);
      border: 1px solid rgba(139, 92, 246, 0.25);
      border-radius: 8px;
      padding: 1rem;
      margin-top: 1rem;
    }}
    .defense-title {{
      font-size: 0.85rem;
      font-weight: 700;
      text-transform: uppercase;
      color: #c4b5fd;
      margin-bottom: 0.5rem;
    }}
    .copy-btn {{
      background: rgba(59, 130, 246, 0.2);
      border: 1px solid var(--accent-blue);
      color: #ffffff;
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      font-size: 0.75rem;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .copy-btn:hover {{
      background: var(--accent-blue);
    }}
    /* Interactive Flashcards 3D flip */
    .flashcard-section {{
      margin-top: 3rem;
      border-top: 1px solid var(--card-border);
      padding-top: 2rem;
    }}
    .cards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 1.5rem;
      margin-top: 1.5rem;
    }}
    .flip-card {{
      background-color: transparent;
      height: 220px;
      perspective: 1000px;
      cursor: pointer;
    }}
    .flip-card-inner {{
      position: relative;
      width: 100%;
      height: 100%;
      text-align: center;
      transition: transform 0.5s;
      transform-style: preserve-3d;
      border-radius: 12px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.4);
    }}
    .flip-card.flipped .flip-card-inner {{
      transform: rotateY(180deg);
    }}
    .flip-card-front, .flip-card-back {{
      position: absolute;
      width: 100%;
      height: 100%;
      -webkit-backface-visibility: hidden;
      backface-visibility: hidden;
      border-radius: 12px;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
    }}
    .flip-card-front {{
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      border: 1px solid rgba(255, 255, 255, 0.1);
      color: #f8fafc;
    }}
    .flip-card-back {{
      background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%);
      border: 1px solid rgba(139, 92, 246, 0.4);
      color: #e2e8f0;
      transform: rotateY(180deg);
      font-size: 0.88rem;
      text-align: left;
      overflow-y: auto;
    }}
    .flip-hint {{
      font-size: 0.72rem;
      color: var(--accent-cyan);
      margin-top: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .toast {{
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      background: var(--accent-emerald);
      color: #ffffff;
      padding: 0.75rem 1.25rem;
      border-radius: 8px;
      font-weight: 600;
      opacity: 0;
      transition: opacity 0.3s;
      pointer-events: none;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
    }}
    .toast.show {{
      opacity: 1;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <span class="badge">Session Technical Learning Pack</span>
      <h1>{html.escape(pack.title)}</h1>
      <div class="meta-bar">
        <span>Session: <strong>{html.escape(pack.session_id)}</strong></span>
        <span>Generated: <strong>{html.escape(pack.timestamp[:19].replace('T', ' '))} UTC</strong></span>
        <span>Concepts: <strong>{len(pack.concepts)}</strong></span>
        <span>Flashcards: <strong>{len(pack.flashcards)}</strong></span>
      </div>
    </header>

    <div class="summary-box">
      <strong>Executive Brief:</strong> {html.escape(pack.executive_summary)}
    </div>

    <!-- Concepts List -->
    <div id="concepts-container">
"""

        for idx, c in enumerate(pack.concepts):
            cat_name = c.category.value if hasattr(c.category, "value") else str(c.category)
            html_out += f"""
      <div class="concept-card" id="card-concept-{idx}">
        <div class="concept-header">
          <div>
            <div style="font-size: 0.8rem; color: var(--accent-cyan); font-weight: 600; text-transform: uppercase;">Concept {idx + 1} &bull; {html.escape(cat_name)}</div>
            <div class="concept-title">{html.escape(c.name)}</div>
          </div>
          <button class="copy-btn" onclick="copyPitch('{idx}')">📋 Copy Pitch</button>
        </div>

        <div class="anchor-quote">
          ⚓ <strong>Mental Anchor:</strong> {html.escape(c.mental_anchor)}
        </div>

        <div class="tab-bar">
          <button class="tab-btn active" onclick="switchTab('{idx}', 'pitch')">🎙️ 30s Pitch</button>
          <button class="tab-btn" onclick="switchTab('{idx}', 'staff')">🏛️ Staff Architect</button>
          <button class="tab-btn" onclick="switchTab('{idx}', 'hood')">⚙️ Under The Hood</button>
        </div>

        <div class="tier-content active" id="tab-{idx}-pitch">
          <strong>For Non-Technical Stakeholders & Executives:</strong><br>
          {html.escape(c.tiers.pitch_30s)}
        </div>

        <div class="tier-content" id="tab-{idx}-staff">
          <strong>Staff-Level Architectural Defense & Trade-offs:</strong><br>
          {html.escape(c.tiers.staff_architect)}
        </div>

        <div class="tier-content" id="tab-{idx}-hood">
          <strong>Engine Room Mechanics & Data Structures:</strong><br>
          {html.escape(c.tiers.under_the_hood)}
        </div>
"""
            if c.defense:
                d = c.defense[0]
                html_out += f"""
        <div class="defense-box">
          <div class="defense-title">🛡️ Third-Party Defense Shield</div>
          <strong>Q: {html.escape(d.question)}</strong><br>
          <span style="color: #cbd5e1; font-size: 0.9rem;">A: {html.escape(d.bulletproof_answer)}</span>
        </div>
"""
            html_out += "      </div>\n"

        # Flashcard section
        html_out += """
    <!-- Active Recall Flashcards -->
    <div class="flashcard-section">
      <h2>🃏 Active Recall Flashcards (Spaced Repetition)</h2>
      <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">
        Click any card to flip and verify your understanding:
      </p>
      <div class="cards-grid">
"""
        for card in pack.flashcards:
            html_out += f"""
        <div class="flip-card" onclick="this.classList.toggle('flipped')">
          <div class="flip-card-inner">
            <div class="flip-card-front">
              <strong style="font-size: 0.95rem;">{html.escape(card.front_prompt)}</strong>
              <div class="flip-hint">👆 Click to Flip</div>
            </div>
            <div class="flip-card-back">
              <strong>Answer:</strong><br>
              <div style="margin-top: 0.4rem;">{html.escape(card.back_solution)}</div>
              <div style="margin-top: 0.6rem; font-size: 0.78rem; color: #94a3b8; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 0.4rem;">
                <em>Key takeaway:</em> {html.escape(card.why_it_matters)}
              </div>
            </div>
          </div>
        </div>
"""

        html_out += f"""
      </div>
    </div>
"""
        if pack.discussion_topics:
            html_out += """
    <!-- Optional Discussion Topics -->
    <div style="margin-top: 3rem; border-top: 1px solid var(--card-border); padding-top: 2rem;">
      <h2 style="font-size: 1.3rem; margin-bottom: 0.5rem;">💬 Tópicos de Alinhamento & Discussão (Opcionais)</h2>
      <p style="color: var(--text-muted); font-size: 0.85rem; margin-bottom: 1rem;">
        <em>A leitura é 100% opcional e não bloqueia produção nem pipelines subsequentes.</em>
      </p>
      <ul style="list-style-type: decimal; padding-left: 1.5rem; color: #cbd5e1; line-height: 1.7;">
"""
            for topic in pack.discussion_topics:
                html_out += f"        <li style=\"margin-bottom: 0.5rem;\">{html.escape(topic)}</li>\n"
            html_out += """      </ul>
    </div>
"""

        html_out += """
  </div>

  <div id="toast" class="toast">Elevator pitch copied to clipboard!</div>

  <script>
    const PACK_DATA = {pack_json};

    function switchTab(conceptIdx, tabName) {{
      const card = document.getElementById('card-concept-' + conceptIdx);
      const btns = card.querySelectorAll('.tab-btn');
      btns.forEach(b => b.classList.remove('active'));
      const contents = card.querySelectorAll('.tier-content');
      contents.forEach(c => c.classList.remove('active'));

      const activeContent = document.getElementById('tab-' + conceptIdx + '-' + tabName);
      if (activeContent) activeContent.classList.add('active');

      event.target.classList.add('active');
    }}

    function copyPitch(conceptIdx) {{
      const concept = PACK_DATA.concepts[conceptIdx];
      if (!concept) return;
      const pitchText = concept.tiers.pitch_30s;
      navigator.clipboard.writeText(pitchText).then(() => {{
        const toast = document.getElementById('toast');
        toast.classList.add('show');
        setTimeout(() => toast.classList.remove('show'), 2200);
      }});
    }}
  </script>
</body>
</html>
"""
        return html_out
