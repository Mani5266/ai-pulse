"""Rendering a briefing as a static HTML page.

The public artefact. No framework, no build step, no JavaScript, no external requests: a
single self-contained file per day, which is what makes GitHub Pages a complete hosting
solution rather than a compromise.

The same escaping rule as the Telegram renderer applies, for the same reason — every string
here originated in text harvested from the open internet.
"""

from __future__ import annotations

from html import escape

from app.briefing.models import Briefing, Story

STYLE = """\
:root { color-scheme: light dark; --fg:#111; --muted:#666; --bg:#fdfdfc; --line:#e5e5e2;
        --accent:#b4470f; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e8e6; --muted:#9a9a96; --bg:#161614; --line:#2e2e2b; --accent:#e8935c; }
}
* { box-sizing: border-box; }
body { margin:0 auto; padding:2rem 1.25rem 4rem; max-width:44rem; background:var(--bg);
       color:var(--fg); font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",
       Roboto,Helvetica,Arial,sans-serif; }
header { border-bottom:2px solid var(--fg); padding-bottom:.75rem; margin-bottom:2rem; }
h1 { font-size:1.4rem; margin:0; letter-spacing:-.01em; }
.sub { color:var(--muted); font-size:.85rem; margin-top:.35rem; }
article { border-bottom:1px solid var(--line); padding:1.5rem 0; }
article:last-of-type { border-bottom:none; }
h2 { font-size:1.1rem; margin:0 0 .5rem; line-height:1.35; }
.lead h2 { font-size:1.35rem; }
.meta { color:var(--muted); font-size:.78rem; text-transform:uppercase;
        letter-spacing:.06em; margin-bottom:.4rem; }
.tag { color:var(--accent); font-weight:600; }
p { margin:.5rem 0; }
.why { color:var(--muted); }
.dev { border-left:2px solid var(--line); padding-left:.75rem; font-size:.94rem; }
.sources { font-size:.82rem; margin-top:.6rem; }
.sources a { color:var(--accent); text-decoration:none; border-bottom:1px solid transparent; }
.sources a:hover { border-bottom-color:var(--accent); }
footer { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line);
         color:var(--muted); font-size:.78rem; }
.empty { color:var(--muted); font-style:italic; }
"""


def _esc(text: str) -> str:
    return escape(" ".join(text.split()), quote=True)


def render_story_html(story: Story, *, lead: bool) -> str:
    """One story as an article element."""
    classes = "lead" if lead else ""
    meta = [f'<span class="tag">{_esc(story.category.value.replace("_", " "))}</span>']
    if story.source_count > 1:
        meta.append(f"{story.source_count} sources")
    if story.is_developing:
        meta.append("developing")
    meta.append(f"score {story.score:.1f}")

    parts = [
        f'<article class="{classes}">',
        f'  <div class="meta">{" · ".join(meta)}</div>',
        f"  <h2>{_esc(story.headline)}</h2>",
        f"  <p>{_esc(story.what_happened)}</p>",
        f'  <p class="why"><strong>Why it matters.</strong> {_esc(story.why_it_matters)}</p>',
    ]

    if story.developer_impact:
        parts.append(f'  <p class="dev">{_esc(story.developer_impact)}</p>')

    if story.sources:
        links = " · ".join(
            f'<a href="{_esc(source.url)}" rel="noopener nofollow">{_esc(source.source_id)}</a>'
            for source in story.sources
        )
        parts.append(f'  <p class="sources">Sources: {links}</p>')

    parts.append("</article>")
    return "\n".join(parts)


def render_html(briefing: Briefing, *, title: str = "AI-Pulse") -> str:
    """Render a complete, self-contained page for one day."""
    stats = briefing.stats
    day = briefing.day.strftime("%A, %d %B %Y")

    if briefing.is_empty:
        body = (
            '<p class="empty">No story could be verified today. Nothing is published '
            "rather than publishing something unsupported.</p>"
        )
    else:
        body = "\n".join(
            render_story_html(story, lead=index == 0)
            for index, story in enumerate(briefing.stories)
        )

    footer_bits = [
        f"{stats.articles} articles",
        f"{stats.duplicates_removed} duplicates removed",
        f"{stats.events} events",
        f"{stats.events_shortlisted} shortlisted",
        f"{stats.model_calls} model calls",
    ]
    if stats.feeds_failed:
        footer_bits.append(f"{stats.feeds_failed} feeds failed")
    if stats.model_failures:
        footer_bits.append(f"{stats.model_failures} model failures")
    footer_bits.append(f"{stats.runtime_seconds:.0f}s")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · {_esc(day)}</title>
<meta name="description" content="A daily briefing on AI, built from {stats.articles} \
articles across {stats.feeds_ok} sources.">
<style>{STYLE}</style>
</head>
<body>
<header>
  <h1>AI-PULSE</h1>
  <div class="sub">{_esc(day)} · {len(briefing.stories)} stories from \
{stats.articles} articles across {stats.feeds_ok} sources</div>
</header>
{body}
<footer>
  <p>{" · ".join(_esc(bit) for bit in footer_bits)}</p>
  <p>Generated by <a href="https://github.com/OWNER/ai-pulse">AI-Pulse</a>, \
model {_esc(stats.provider)}. Every claim links to its source.</p>
</footer>
</body>
</html>
"""


def render_index(briefings: list[Briefing]) -> str:
    """An archive page listing every briefing, newest first.

    This is the timeline made browsable, and it is the page a reader lands on.
    """
    rows = "\n".join(
        f'<article><div class="meta">{_esc(briefing.day.isoformat())} · '
        f"{len(briefing.stories)} stories</div>"
        f'<h2><a href="{briefing.day.isoformat()}.html">'
        f"{_esc(briefing.lead.headline) if briefing.lead else 'No verified stories'}</a></h2>"
        "</article>"
        for briefing in briefings
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-Pulse · archive</title>
<style>{STYLE}
h2 a {{ color:inherit; text-decoration:none; }}
h2 a:hover {{ color:var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>AI-PULSE</h1>
  <div class="sub">{len(briefings)} daily briefings</div>
</header>
{rows}
<footer><p>Generated by \
<a href="https://github.com/OWNER/ai-pulse">AI-Pulse</a>.</p></footer>
</body>
</html>
"""
