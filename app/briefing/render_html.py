"""Rendering a briefing as a static HTML page.

The public artefact. No framework, no build step, no JavaScript, no external requests: a
single self-contained file per day, which is what makes GitHub Pages a complete hosting
solution rather than a compromise.

The same escaping rule as the Telegram renderer applies, for the same reason — every string
here originated in text harvested from the open internet.
"""

from __future__ import annotations

from html import escape

from app.briefing.models import Briefing, Claim, Story
from app.intelligence.timeline import Timeline
from app.intelligence.verification import VerificationStatus
from app.storage.run_store import Health, RunRecord

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
.claims { list-style:none; padding:0; margin:.9rem 0 .4rem; font-size:.88rem; }
.claims li { padding:.35rem 0 .35rem .75rem; border-left:3px solid var(--line);
             margin-bottom:.3rem; }
.claims li.verified { border-left-color:#2f855a; }
.claims li.partial { border-left-color:#b7791f; }
.claims li.contradicted { border-left-color:#c53030; }
.badge { font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
         color:var(--muted); margin-right:.4rem; white-space:nowrap; }
.verified .badge { color:#2f855a; }
.partial .badge { color:#b7791f; }
.contradicted .badge { color:#c53030; }
.attr { color:var(--muted); font-size:.78rem; }
.sources { font-size:.82rem; margin-top:.6rem; }
.sources a { color:var(--accent); text-decoration:none; border-bottom:1px solid transparent; }
.sources a:hover { border-bottom-color:var(--accent); }
footer { margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line);
         color:var(--muted); font-size:.78rem; }
.empty { color:var(--muted); font-style:italic; }
"""

STATS_STYLE = """
.cards { display:flex; flex-wrap:wrap; gap:1rem; margin:1.5rem 0 2rem; }
.card { flex:1 1 8rem; border:1px solid var(--line); border-radius:6px; padding:.9rem; }
.num { font-size:1.6rem; font-weight:600; letter-spacing:-.02em; }
.lbl { color:var(--muted); font-size:.78rem; margin-top:.2rem; line-height:1.4; }
.ok { color:#2f855a; }
.bad { color:#c53030; }
table { width:100%; border-collapse:collapse; font-size:.82rem; margin-top:.5rem;
        display:block; overflow-x:auto; }
th, td { text-align:left; padding:.35rem .6rem .35rem 0; border-bottom:1px solid var(--line);
         white-space:nowrap; }
th { color:var(--muted); font-weight:500; text-transform:uppercase; font-size:.72rem;
     letter-spacing:.05em; }
.failing li { color:#c53030; }
h2 { font-size:1rem; margin-top:2rem; }
"""

TIMELINE_STYLE = """
.timeline { list-style:none; padding:0; margin:1.5rem 0; counter-reset:tl; }
.timeline li { position:relative; padding:0 0 1.4rem 1.5rem;
               border-left:2px solid var(--line); }
.timeline li:last-child { border-left-color:transparent; padding-bottom:0; }
.timeline li::before { content:""; position:absolute; left:-6px; top:.45rem; width:10px;
                       height:10px; border-radius:50%; background:var(--accent); }
.tl-day { font-size:.78rem; color:var(--muted); text-transform:uppercase;
          letter-spacing:.06em; margin-right:.6rem; }
.tl-what { font-size:.82rem; color:var(--accent); }
.tl-title { margin-top:.2rem; }
"""


BADGES: dict[VerificationStatus, tuple[str, str]] = {
    VerificationStatus.VERIFIED: ("verified", "✓ corroborated"),
    VerificationStatus.PARTIALLY_VERIFIED: ("partial", "~ partly corroborated"),
    VerificationStatus.UNVERIFIED: ("single", "· single source"),
    VerificationStatus.CONTRADICTED: ("contradicted", "⚠ sources disagree"),
}


def _esc(text: str) -> str:
    return escape(" ".join(text.split()), quote=True)


def render_claims_html(claims: list[Claim]) -> str:
    """The claims behind a story, each labelled by how many sources carry it.

    This is the part of the page worth reading twice. A briefing that asserts things is
    ordinary; one that shows which assertions more than one source stands behind, and
    which are a single company's word, is saying something a reader can act on.
    """
    if not claims:
        return ""

    rows = []
    for claim in claims:
        css, label = BADGES[claim.status]
        attribution = ""
        if claim.supported_by:
            attribution = f' <span class="attr">{_esc(", ".join(claim.supported_by))}</span>'
        elif claim.status is VerificationStatus.UNVERIFIED:
            attribution = ' <span class="attr">no source could be attributed</span>'
        rows.append(
            f'  <li class="{css}"><span class="badge">{_esc(label)}</span> '
            f"{_esc(claim.text)}{attribution}</li>"
        )

    return '  <ul class="claims">\n' + "\n".join(rows) + "\n  </ul>"


def render_story_html(story: Story, *, lead: bool) -> str:
    """One story as an article element."""
    classes = "lead" if lead else ""
    meta = [f'<span class="tag">{_esc(story.category.value.replace("_", " "))}</span>']
    if story.source_count > 1:
        meta.append(f"{story.source_count} sources")
    if story.verified_claim_count:
        meta.append(f"{story.verified_claim_count} corroborated claims")
    if story.contradicted_claims:
        meta.append("disputed")
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

    claims = render_claims_html(story.claims)
    if claims:
        parts.append(claims)

    if story.sources:
        links = " · ".join(
            f'<a href="{_esc(source.url)}" rel="noopener nofollow">{_esc(source.source_id)}</a>'
            for source in story.sources
        )
        parts.append(f'  <p class="sources">Sources: {links}</p>')

    parts.append(
        f'  <p class="sources"><a href="event-{_esc(story.event_id)}.html">'
        "How this story developed</a></p>"
    )

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
  <p><a href="archive.html">All briefings</a> · \
<a href="developing.html">Developing stories</a> · \
<a href="stats.html">Pipeline health</a></p>
  <p>{" · ".join(_esc(bit) for bit in footer_bits)}</p>
  <p>Generated by <a href="https://github.com/Mani5266/ai-pulse">AI-Pulse</a>, \
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
<a href="https://github.com/Mani5266/ai-pulse">AI-Pulse</a>.</p></footer>
</body>
</html>
"""


def render_timeline_html(timeline: Timeline) -> str:
    """One event's history as a dated list of what changed.

    Days on which nothing moved are already absent, so every row here is a real
    development rather than a heartbeat.
    """
    rows = []
    for entry in timeline.entries:
        marks = []
        if entry.is_first:
            marks.append("first reported")
        if entry.articles_added:
            noun = "article" if entry.articles_added == 1 else "articles"
            marks.append(f"+{entry.articles_added} {noun}")
        if entry.is_corroboration:
            marks.append(f"picked up by {_esc(', '.join(entry.sources_added))}")
        if entry.importance_score is not None:
            marks.append(f"score {entry.importance_score:.1f}")

        rows.append(
            f'  <li><span class="tl-day">{_esc(entry.day.isoformat())}</span>'
            f'<span class="tl-what">{_esc(" · ".join(marks))}</span>'
            f'<div class="tl-title">{_esc(entry.title)}</div></li>'
        )

    return '<ol class="timeline">\n' + "\n".join(rows) + "\n</ol>"


def render_event_page(timeline: Timeline) -> str:
    """A page for one event: what it is, and every day it moved."""
    span = (
        f"{timeline.days_running} days"
        if timeline.days_running > 1
        else "reported on one day so far"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(timeline.title)} · AI-Pulse</title>
<style>{STYLE}{TIMELINE_STYLE}</style>
</head>
<body>
<header>
  <h1>{_esc(timeline.title)}</h1>
  <div class="sub">{_esc(span)} · {len(timeline.entries)} developments · \
{timeline.total_sources} sources</div>
</header>
{render_timeline_html(timeline)}
<footer>
  <p><a href="archive.html">All briefings</a> · \
<a href="developing.html">Developing stories</a></p>
</footer>
</body>
</html>
"""


def render_developing(timelines: list[Timeline]) -> str:
    """The index of stories that are still moving.

    A newsletter has no such page, because a newsletter has no memory of what it said
    yesterday. This one is built entirely from committed snapshots.
    """
    if not timelines:
        body = (
            '<p class="empty">No story has developed across more than one day yet. '
            "This page fills itself in as the archive grows.</p>"
        )
    else:
        body = "\n".join(
            f'<article><div class="meta">{len(timeline.entries)} developments over '
            f"{timeline.days_running} days · {timeline.total_sources} sources</div>"
            f'<h2><a href="event-{_esc(timeline.event_id)}.html">'
            f"{_esc(timeline.title)}</a></h2></article>"
            for timeline in timelines
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Developing stories · AI-Pulse</title>
<style>{STYLE}
h2 a {{ color:inherit; text-decoration:none; }}
h2 a:hover {{ color:var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>DEVELOPING</h1>
  <div class="sub">{len(timelines)} stories still moving</div>
</header>
{body}
<footer><p><a href="index.html">Today</a> · <a href="archive.html">All briefings</a></p></footer>
</body>
</html>
"""


def render_stats_page(health: Health, records: list[RunRecord]) -> str:
    """The pipeline's own report card.

    Published rather than kept in a log, because a project that claims 95% reliability
    should show the number rather than assert it — including on the days it does not meet
    it. A dashboard that can only display good news is decoration.
    """
    target = "met" if health.meets_reliability_target else "not met"
    target_class = "ok" if health.meets_reliability_target else "bad"

    rows = "\n".join(
        f"    <tr><td>{_esc(record.started_at.strftime('%Y-%m-%d %H:%M'))}</td>"
        f'<td class="{"ok" if record.ok else "bad"}">{"ok" if record.ok else "failed"}</td>'
        f"<td>{record.feeds_ok}/{len(record.feeds)}</td>"
        f"<td>{record.articles_fetched}</td>"
        f"<td>{record.articles_in_window}</td>"
        f"<td>{record.events_touched}</td>"
        f"<td>{record.events_ranked}</td>"
        f"<td>{record.stories_published}</td>"
        f"<td>{record.model_calls}</td>"
        f"<td>{record.duration_seconds:.0f}s</td></tr>"
        for record in records[:30]
    )

    if health.failing_feeds:
        failing = "\n".join(
            f"    <li>{_esc(source_id)} — failed in {count} of the last {health.runs} runs</li>"
            for source_id, count in health.failing_feeds.items()
        )
        feeds_block = (
            f'<h2>Feeds needing attention</h2>\n  <ul class="failing">\n{failing}\n  </ul>'
        )
    else:
        feeds_block = "<h2>Feeds</h2>\n  <p>Every source succeeded in every recorded run.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline health · AI-Pulse</title>
<style>{STYLE}{STATS_STYLE}</style>
</head>
<body>
<header>
  <h1>PIPELINE HEALTH</h1>
  <div class="sub">{health.runs} recorded runs</div>
</header>

<div class="cards">
  <div class="card"><div class="num {target_class}">{health.success_rate:.0%}</div>
    <div class="lbl">runs produced a briefing<br>target 95%, {target}</div></div>
  <div class="card"><div class="num">{health.delivery_rate:.0%}</div>
    <div class="lbl">delivered to Telegram</div></div>
  <div class="card"><div class="num">{health.median_duration:.0f}s</div>
    <div class="lbl">median run</div></div>
  <div class="card"><div class="num">{health.total_model_calls}</div>
    <div class="lbl">model calls, {health.total_model_failures} failed</div></div>
</div>

<h2>A typical run</h2>
<p>{health.median_articles} articles fetched · {health.median_events} events ·
{health.median_stories} stories published. Medians, so one bad day does not move them.</p>

{feeds_block}

<h2>Recent runs</h2>
<table>
  <thead><tr><th>started</th><th></th><th>feeds</th><th>fetched</th><th>in window</th>
  <th>new events</th><th>ranked</th><th>stories</th><th>calls</th><th>took</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>

<footer><p><a href="index.html">Today</a> · <a href="archive.html">All briefings</a> · \
<a href="developing.html">Developing stories</a></p></footer>
</body>
</html>
"""
