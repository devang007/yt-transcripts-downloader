#!/usr/bin/env python3
"""Assemble verified chapters into a finished book.

Produces a single markdown file, a styled standalone HTML, and optionally PDF
(requires weasyprint). Generates the table of contents, the source index that
maps every cited evidence ID to a timestamped deep link, and the method,
coverage, and glossary appendices.

Usage:
    python build_book.py --project ./my-book --format md,html
    python build_book.py --project ./my-book --format pdf
"""

import argparse
import html as html_mod
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

EV_ID = r"EV-(?:[A-Za-z0-9_-]{11}-\d{2,}|\d{4,})"
LIST_RE = re.compile(r"^\s*[-*]\s+")
CITE_RE = re.compile(r"\[((?:" + EV_ID + r"(?:,\s*)?)+)\]")

def is_internal(path):
    """True for the phase's own bookkeeping files, false for real card files.

    A YouTube video ID is exactly 11 characters and is allowed to begin with
    an underscore, so a leading "_" alone cannot be the test — using it silently
    hides real cards. Length is what actually separates the two.
    """
    return path.stem.startswith("_") and len(path.stem) != 11



def load_cards(proj):
    cards = {}
    for path in (proj / "cards").glob("*.jsonl"):
        if is_internal(path):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    c = json.loads(line)
                    cards[c["id"]] = c
                except (json.JSONDecodeError, KeyError):
                    pass
    return cards


def load_meta(proj):
    p = proj / "book/meta.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def front_matter(meta, manifest):
    channel = meta.get("channel", "[Channel Name]")
    title = meta.get("title", f"The {channel} Method")
    subtitle = meta.get("subtitle", "A study guide derived from the complete channel archive")
    n = len(manifest.get("videos", []))
    dates = sorted(v["published"] for v in manifest.get("videos", []) if v.get("published"))
    span = (f" published between {dates[0]} and {dates[-1]}" if dates
            else " from the channel archive")
    url = meta.get("channel_url", "")
    disclaimer = meta.get("disclaimer", "")

    return f"""# {title}

### {subtitle}

---

**About this book.** This is an independent study guide to the publicly available
video work of {channel}{f" ({url})" if url else ""}. It was compiled by reading
the transcripts of {n} videos{span}, extracting the ideas taught in them, and
reorganising that material by concept rather than by upload date. It is not
affiliated with, authorised by, or endorsed by {channel}.

Everything presented as {channel}'s view is paraphrased from what was actually
said on the channel and carries a source marker such as `[EV-0142]`. The Source
Index at the back turns every one of those markers into a link to the exact
second of the exact video, so any passage in this book can be checked against
the original in a few seconds. If you find this material useful, the original
videos are the primary source and are worth watching in full.

Passages set in marked **Editor's note** boxes are explanatory context added by
the compiler — definitions, background mechanics, worked examples — to make the
material easier to follow. Those boxes are never {channel}'s claims. Where the
channel does not cover something, this book says so rather than filling the gap.

{disclaimer}

---
"""


def build_toc(chapters):
    lines = ["## Contents", ""]
    for path, text in chapters:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else path.stem
        anchor = re.sub(r"[^a-z0-9\s-]", "", title.lower()).strip().replace(" ", "-")
        lines.append(f"- [{title}](#{anchor})")
    lines += ["- [Appendix A — Glossary](#appendix-a--glossary)",
              "- [Appendix B — Source Index](#appendix-b--source-index)",
              "- [Appendix C — Method and Coverage](#appendix-c--method-and-coverage)",
              "- [Appendix D — What the Channel Does Not Cover](#appendix-d--what-the-channel-does-not-cover)",
              ""]
    return "\n".join(lines)


def source_index(cited_ids, cards, manifest_by_id):
    lines = ["## Appendix B — Source Index", "",
             "Every source marker used in this book, with a link to the moment it "
             "came from.", "",
             "| ID | Video | Time | What it supports |", "|---|---|---|---|"]
    for cid in sorted(cited_ids):
        c = cards.get(cid)
        if not c:
            lines.append(f"| {cid} | *unresolved* | — | — |")
            continue
        meta = manifest_by_id.get(c["video_id"], {})
        title = (meta.get("title") or c.get("video_title") or c["video_id"])[:70]
        url = c.get("url") or meta.get("url") or ""
        ts = c.get("timestamp", "")
        link = f"[{ts}]({url})" if url else ts
        claim = c.get("claim", "").replace("|", "/")[:110]
        lines.append(f"| `{cid}` | {title} | {link} | {claim} |")
    return "\n".join(lines) + "\n"


def method_appendix(cards, cited_ids, manifest, chapters):
    videos = manifest.get("videos", [])
    dates = sorted(v["published"] for v in videos if v.get("published"))
    card_videos = {c["video_id"] for c in cards.values()}
    cited_videos = {cards[i]["video_id"] for i in cited_ids if i in cards}
    words = sum(len(t.split()) for _, t in chapters)
    return f"""## Appendix C — Method and Coverage

This book was assembled by a transcript-driven pipeline rather than written from
memory or impression, which is what makes the source markers meaningful.

**Coverage**

| | |
|---|---|
| Videos in the archive | {len(videos)} |
| Videos read and processed | {len(card_videos)} ({len(card_videos)*100//max(len(videos),1)}%) |
| Videos cited in this book | {len(cited_videos)} |
| Total transcript words read | {sum(v.get('word_count',0) for v in videos):,} |
| Publication range | {dates[0] if dates else '?'} to {dates[-1] if dates else '?'} |
| Distinct claims extracted | {len(cards)} |
| Claims cited in this book | {len(cited_ids)} |
| Words of finished text | {words:,} |

**How it was built.** Each video's transcript was read individually and reduced to
atomic, timestamped claims — one idea per record, with the conditions attached and
a short locator phrase so the moment can be found again. Those records were then
grouped by concept across the whole archive, which is how the chapter structure was
determined: topics returned to across many videos became chapters, topics mentioned
once became footnotes or were left out. Chapters were drafted only from the records
belonging to that concept, then checked mechanically so that every claim-bearing
sentence resolves to a real record, and checked again by sampling to confirm the
records actually support the sentences citing them.

**What this method cannot do.** Transcripts capture speech, not screens. Anything
shown visually and not described aloud — chart annotations, on-screen text, code —
is absent from the source material and therefore absent here. Automatic transcripts
also mishear technical terms, so specific names and numbers were treated with
suspicion and cross-checked across videos where possible. Where the creator was
ambiguous, this book reports the ambiguity rather than resolving it.

**On the two tiers.** Material attributed to the creator is paraphrased from the
transcripts and carries a source marker. Material in Editor's note boxes is added
context from the compiler and is marked as such throughout. The separation is
deliberate and strict: nothing in an Editor's note should be read as something the
creator said.
"""


def to_html(md_text, title, css):
    """Minimal markdown → HTML. Adequate for the book's own generated markdown."""
    out, in_code, in_table, in_list, in_quote = [], False, False, False, False

    def inline(s):
        s = html_mod.escape(s, quote=False)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2">', s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"(?<![\w-])(" + EV_ID + r")(?![\w-])",
                   r'<sup class="cite">\1</sup>', s)
        return s

    def flush_para():
        """Emit buffered lines as ONE paragraph.

        Markdown joins consecutive non-blank lines into a single paragraph and
        only breaks on a blank line. Emitting one <p> per source line is what
        shattered the PDF into thousands of one-line paragraphs.
        """
        if para:
            out.append(f"<p>{' '.join(para)}</p>")
            para.clear()

    def close_blocks():
        nonlocal in_table, in_list, in_quote
        flush_para()
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    para = []
    for line in md_text.split("\n"):
        if line.strip().startswith("```"):
            close_blocks()
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html_mod.escape(line))
            continue
        if not line.strip():
            close_blocks()
            continue
        if line.startswith("---"):
            close_blocks()
            out.append("<hr>")
            continue
        h = re.match(r"^(#{1,4})\s+(.*)", line)
        if h:
            close_blocks()
            lvl = len(h.group(1))
            txt = h.group(2)
            anchor = re.sub(r"[^a-z0-9\s-]", "", txt.lower()).strip().replace(" ", "-")
            out.append(f'<h{lvl} id="{anchor}">{inline(txt)}</h{lvl}>')
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                close_blocks()
                out.append("<table><thead><tr>"
                           + "".join(f"<th>{inline(c)}</th>" for c in cells)
                           + "</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if line.startswith(">"):
            if not in_quote:
                close_blocks()
                out.append('<blockquote class="editor-note">')
                in_quote = True
            para.append(inline(line.lstrip('> ')))
            continue
        if LIST_RE.match(line):
            flush_para()
            if not in_list:
                close_blocks()
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(LIST_RE.sub('', line))}</li>")
            continue
        if in_table or in_list or in_quote:
            close_blocks()
        para.append(inline(line))
    close_blocks()
    body = "\n".join(out)
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{html_mod.escape(title)}</title><style>{css}</style></head>"
            f"<body><main>{body}</main></body></html>")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--format", default="md,html", help="md,html,pdf")
    ap.add_argument("--out", help="output basename (default book/<slug>)")
    args = ap.parse_args()

    proj = Path(args.project)
    formats = {f.strip() for f in args.format.split(",")}
    cards = load_cards(proj)
    meta = load_meta(proj)
    manifest = json.loads((proj / "corpus/manifest.json").read_text(encoding="utf-8")) \
        if (proj / "corpus/manifest.json").exists() else {"videos": []}
    manifest_by_id = {v["video_id"]: v for v in manifest.get("videos", [])}

    chapter_files = sorted((proj / "chapters").glob("*.md"))
    if not chapter_files:
        sys.exit("No chapters found in chapters/")
    chapters = [(p, p.read_text(encoding="utf-8")) for p in chapter_files]

    cited = set()
    for _, text in chapters:
        for group in CITE_RE.findall(text):
            cited.update(re.findall(r"EV-\d{4,}", group))

    parts = [front_matter(meta, manifest), build_toc(chapters)]
    parts += [text for _, text in chapters]

    glossary = proj / "book/glossary.md"
    parts.append(glossary.read_text(encoding="utf-8") if glossary.exists()
                 else "## Appendix A — Glossary\n\n_Not yet compiled._\n")
    parts.append(source_index(cited, cards, manifest_by_id))
    parts.append(method_appendix(cards, cited, manifest, chapters))
    gaps = proj / "book/gaps.md"
    parts.append(gaps.read_text(encoding="utf-8") if gaps.exists()
                 else "## Appendix D — What the Channel Does Not Cover\n\n_Not yet compiled._\n")

    book_md = "\n\n---\n\n".join(parts)
    base = Path(args.out) if args.out else proj / "book" / (
        re.sub(r"[^a-z0-9]+", "-", meta.get("title", "book").lower()).strip("-"))
    base.parent.mkdir(parents=True, exist_ok=True)

    unresolved = sorted(c for c in cited if c not in cards)
    if unresolved:
        print(f"WARNING: {len(unresolved)} citations do not resolve "
              f"(e.g. {', '.join(unresolved[:5])}). Run verify.py.")

    if "md" in formats:
        p = base.with_suffix(".md")
        p.write_text(book_md, encoding="utf-8")
        print(f"Markdown → {p}")

    if "html" in formats or "pdf" in formats:
        css_path = Path(__file__).parent.parent / "assets/book.css"
        css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
        html_out = to_html(book_md, meta.get("title", "Book"), css)
        p = base.with_suffix(".html")
        p.write_text(html_out, encoding="utf-8")
        print(f"HTML → {p}")
        if "pdf" in formats:
            try:
                from weasyprint import HTML
                HTML(string=html_out, base_url=str(base.parent)).write_pdf(
                    str(base.with_suffix(".pdf")))
                print(f"PDF → {base.with_suffix('.pdf')}")
            except ImportError:
                print("PDF skipped: pip install weasyprint")

    words = len(book_md.split())
    print(f"\n{len(chapters)} chapters · {words:,} words · {len(cited)} citations "
          f"· {len({cards[c]['video_id'] for c in cited if c in cards})} source videos")


if __name__ == "__main__":
    main()
