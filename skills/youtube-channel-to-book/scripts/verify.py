#!/usr/bin/env python3
"""Verify drafted chapters against the evidence-card database.

Checks:
  1. every [EV-xxxx] citation resolves to a real card
  2. substantive Tier A paragraphs carry at least one citation
  3. Editor's-note (Tier B) blocks are anchored to a citation in the same section
  4. quoted spans stay short (long verbatim reproduction is both bad synthesis
     and a copyright problem)
  5. per-chapter citation density and source-video spread
  6. numbers in the prose appear in some cited card

Exit code 1 if any blocking failure is found.

Usage:
    python verify.py --project ./my-book
    python verify.py --project ./my-book --chapter chapters/04-sessions.md
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CITE_RE = re.compile(r"EV-(?:[A-Za-z0-9_-]{11}-\d{2,}|\d{4,})")

def is_internal(path):
    """True for the phase's own bookkeeping files, false for real card files.

    A YouTube video ID is exactly 11 characters and is allowed to begin with
    an underscore, so a leading "_" alone cannot be the test — using it silently
    hides real cards. Length is what actually separates the two.
    """
    return path.stem.startswith("_") and len(path.stem) != 11

QUOTE_RE = re.compile(r"[\"“]([^\"”]{2,})[\"”]")
NUM_RE = re.compile(r"(?<![\w-])(\d+(?:\.\d+)?)\s*(%|R\b|:1\b|x\b|pips?|ticks?|bps)",
                    re.I)
NOTE_RE = re.compile(r"^>\s*\*\*(Editor'?s note|Editor'?s Note)", re.M)
MAX_QUOTE_WORDS = 15
MIN_CLAIM_CHARS = 120  # paragraphs shorter than this may be transitions

# Sections that legitimately carry no citations: statements about what is absent
# from the archive can't cite a card, because the point is that no card exists.
GAP_HEADING_RE = re.compile(
    r"(doesn'?t\s+say|does\s+not\s+say|doesn'?t\s+cover|does\s+not\s+cover|"
    r"never\s+addresses|not\s+covered|gaps?|silent|open\s+questions?|"
    r"try\s+this|in\s+this\s+chapter|about\s+this\s+book)", re.I)


def load_cards(proj):
    cards = {}
    for path in (proj / "cards").glob("*.jsonl"):
        if is_internal(path):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                cards[c["id"]] = c
            except (json.JSONDecodeError, KeyError):
                continue
    return cards


def split_blocks(text):
    """Yield (kind, block_text) where kind is 'note', 'heading', 'code' or 'prose'."""
    blocks = re.split(r"\n\s*\n", text)
    in_code = False
    for b in blocks:
        stripped = b.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            in_code = not in_code or stripped.count("```") % 2 == 0
            yield "code", stripped
            continue
        if in_code:
            yield "code", stripped
            continue
        if NOTE_RE.search(stripped):
            yield "note", stripped
        elif stripped.startswith("#"):
            yield "heading", stripped
        elif stripped.startswith(("|", "![", "- [", "> **In this chapter")):
            yield "other", stripped
        else:
            yield "prose", stripped


def verify_chapter(path, cards):
    text = path.read_text(encoding="utf-8")
    errors, warnings, info = [], [], {}
    cited = CITE_RE.findall(text)
    cited_set = set(cited)

    # 1. citation resolution
    dangling = sorted(c for c in cited_set if c not in cards)
    for d in dangling:
        errors.append(f"citation {d} does not resolve to any card")

    # 2/3. per-block checks
    uncited, orphan_notes = [], []
    last_prose_cites = set()
    in_gap_section = False
    for kind, block in split_blocks(text):
        block_cites = set(CITE_RE.findall(block))
        if kind == "heading":
            # A new section resets note anchoring: an Editor's note must attach to
            # a claim in its own section, not one three sections back.
            in_gap_section = bool(GAP_HEADING_RE.search(block))
            last_prose_cites = set()
        elif kind == "prose":
            body = re.sub(r"\[[^\]]*\]", "", block)
            if len(body) >= MIN_CLAIM_CHARS and not block_cites and not in_gap_section:
                uncited.append(block[:110].replace("\n", " "))
            last_prose_cites = block_cites or last_prose_cites
        elif kind == "note":
            if not block_cites and not last_prose_cites:
                orphan_notes.append(block[:110].replace("\n", " "))

    for u in uncited:
        errors.append(f"uncited Tier A paragraph: “{u}…”")
    for o in orphan_notes:
        errors.append(f"Editor's note with no anchoring citation: “{o}…”")

    # 4. quote length
    for q in QUOTE_RE.findall(text):
        n = len(q.split())
        if n > MAX_QUOTE_WORDS:
            errors.append(f"quoted span is {n} words (limit {MAX_QUOTE_WORDS}) — "
                          f"paraphrase instead: “{q[:70]}…”")

    # 5. density + spread
    prose_words = len(re.sub(r"\[[^\]]*\]", "", text).split())
    vids = {cards[c]["video_id"] for c in cited_set if c in cards}
    info = {"words": prose_words, "citations": len(cited),
            "unique_cards": len(cited_set), "source_videos": len(vids),
            "cites_per_100w": round(len(cited) / max(prose_words, 1) * 100, 1)}
    if prose_words > 800 and info["cites_per_100w"] < 1.0:
        warnings.append(f"low citation density ({info['cites_per_100w']}/100 words) — "
                        f"check for unsupported assertions")
    if len(vids) < 4 and prose_words > 1500:
        warnings.append(f"chapter draws on only {len(vids)} source video(s) — "
                        f"likely under-researched")

    # 6. numbers traceable to cited cards
    cited_text = " ".join(
        (cards[c].get("claim", "") + " " + str(cards[c].get("anchor", "")) + " "
         + str(cards[c].get("conditions", "")))
        for c in cited_set if c in cards)
    for value, unit in NUM_RE.findall(text):
        if value not in cited_text:
            warnings.append(f"number '{value}{unit}' not found in any cited card — "
                            f"verify it was actually stated")

    return errors, warnings, info


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--chapter", help="verify a single chapter file")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures too")
    args = ap.parse_args()

    proj = Path(args.project)
    cards = load_cards(proj)
    if not cards:
        sys.exit("No cards found — run extraction first.")

    files = ([Path(args.chapter)] if args.chapter
             else sorted((proj / "chapters").glob("*.md")))
    if not files:
        sys.exit("No chapter files found in chapters/")

    total_err = total_warn = 0
    all_cited = set()
    print(f"Verifying {len(files)} chapter(s) against {len(cards)} cards\n")
    for path in files:
        errors, warnings, info = verify_chapter(path, cards)
        all_cited.update(CITE_RE.findall(path.read_text(encoding="utf-8")))
        status = "FAIL" if errors else ("WARN" if warnings else "PASS")
        print(f"[{status}] {path.name}  "
              f"{info['words']:,}w · {info['citations']} cites · "
              f"{info['unique_cards']} cards · {info['source_videos']} videos · "
              f"{info['cites_per_100w']}/100w")
        for e in errors[:15]:
            print(f"    ERROR   {e}")
        if len(errors) > 15:
            print(f"    ERROR   ... and {len(errors)-15} more")
        for w in warnings[:10]:
            print(f"    warning {w}")
        if len(warnings) > 10:
            print(f"    warning ... and {len(warnings)-10} more")
        total_err += len(errors)
        total_warn += len(warnings)
        print()

    print(f"{total_err} errors, {total_warn} warnings across {len(files)} chapters")
    print(f"{len(all_cited)} distinct cards cited of {len(cards)} extracted "
          f"({len(all_cited)*100//len(cards)}% of the evidence base used)")
    print("\nMechanical checks only. Now sample 10-15 cited claims per chapter and "
          "confirm each card actually supports the sentence citing it — a resolving "
          "citation is not the same as a supporting one.")
    if total_err or (args.strict and total_warn):
        sys.exit(1)


if __name__ == "__main__":
    main()
