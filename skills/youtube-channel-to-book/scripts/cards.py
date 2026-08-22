#!/usr/bin/env python3
"""Manage the evidence-card database.

Subcommands:
    validate  check schema, duplicate IDs, dangling video refs, anchor length
    stats     topic distribution and extraction progress
    ledger    aggregate cards into ledger/ledger.json + ledger.md, flag conflicts
    fetch     pull cards for given topics/types into a JSON file for drafting
    coverage  report which videos contributed cards, and which chapters cite them

Usage:
    python cards.py validate --project ./my-book
    python cards.py ledger   --project ./my-book
    python cards.py fetch    --project ./my-book --topics entry,stops --out /tmp/ch5.json
"""

import argparse
import json
import unicodedata
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

REQUIRED = ["id", "video_id", "timestamp", "type", "topics", "claim", "anchor", "stated"]
TYPES = {"rule", "procedure", "rationale", "definition", "example", "number",
         "psychology", "caveat", "opinion", "anecdote", "contradiction"}
ID_RE = re.compile(r"^EV-(?:[A-Za-z0-9_-]{11}-\d{2,}|\d{4,})$")
CITE_RE = re.compile(r"EV-(?:[A-Za-z0-9_-]{11}-\d{2,}|\d{4,})")
MAX_ANCHOR_WORDS = 15

NEGATION = re.compile(r"\b(never|don'?t|not|no longer|avoid|stopped|without)\b", re.I)

def is_internal(path):
    """True for the phase's own bookkeeping files, false for real card files.

    A YouTube video ID is exactly 11 characters and is allowed to begin with
    an underscore, so a leading "_" alone cannot be the test — using it silently
    hides real cards. Length is what actually separates the two.
    """
    return path.stem.startswith("_") and len(path.stem) != 11



def load_cards(proj):
    cards, errors = [], []
    for path in sorted((proj / "cards").glob("*.jsonl")):
        if is_internal(path):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError as exc:
                errors.append(f"{path.name}:{n} invalid JSON — {exc}")
    return cards, errors


def load_manifest(proj):
    p = proj / "corpus/manifest.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {v["video_id"]: v for v in data.get("videos", [])}


def load_taxonomy(proj):
    p = proj / "ledger/taxonomy.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    mapping = {}
    for canonical, aliases in raw.items():
        mapping[canonical] = canonical
        for a in aliases:
            mapping[a] = canonical
    return mapping


def canon(topics, tax):
    return sorted({tax.get(t, t) for t in topics})


def corpus_body(proj, video_id):
    """Transcript text with timestamp markers and whitespace normalised away."""
    f = proj / "corpus/videos" / f"{video_id}.txt"
    if not f.exists():
        return None
    t = f.read_text(encoding="utf-8")
    t = t.split("---", 1)[1] if "---" in t else t
    t = re.sub(r"\[\d{2}:\d{2}:\d{2}\]", " ", t)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", t))


def norm_anchor(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(s or ""))).strip()


def anchor_status(anchor, text):
    """exact | fragments | near:<ratio> | missing — how well an anchor locates.

    An anchor is a VERBATIM locator. A model that normalises the creator's words
    while writing it has quietly turned a quotation into a paraphrase, and no
    downstream check will notice. This is the one fidelity property that can be
    tested mechanically, so it should be.
    """
    a = norm_anchor(anchor)
    if not a:
        return "missing", None
    if a in text:
        return "exact", a
    parts = [p.strip() for p in re.split(r"\.\.\.|\u2026", a) if len(p.strip()) > 8]
    if parts and all(p in text for p in parts):
        return "fragments", None
    best, span = 0.0, None
    L = len(a)
    step = max(L // 4, 1)
    for i in range(0, max(len(text) - L, 1), step):
        w = text[i:i + int(L * 1.4)]
        r = SequenceMatcher(None, a, w).ratio()
        if r > best:
            best, span = r, w
    if best >= 0.75:
        return f"near:{best:.2f}", span
    return "missing", None


def repair_anchor(anchor, text):
    """Snap a near-miss anchor back onto the real transcript span.

    Near-misses are almost always mechanical: the model joined a line break,
    normalised a curly quote, or dropped punctuation while copying. The creator's
    actual words are still there, so the fix is to find the longest genuinely
    common run and use that rather than discard a good card. Returns None when
    nothing long enough is recoverable — better an honest failure than a
    plausible-looking anchor that locates the wrong moment.
    """
    a = norm_anchor(anchor)
    if not a:
        return None
    L = len(a)
    best, span = 0.0, None
    for i in range(0, max(len(text) - L, 1), max(L // 4, 1)):
        w = text[i:i + int(L * 1.6)]
        r = SequenceMatcher(None, a, w).ratio()
        if r > best:
            best, span = r, w
    if span is None or best < 0.7:
        return None
    m = SequenceMatcher(None, a, span).find_longest_match(0, len(a), 0, len(span))
    frag = span[m.b:m.b + m.size].strip()
    # trim to whole words so the anchor still reads as a phrase
    if " " in frag:
        parts = frag.split()
        if not span.startswith(frag) and len(parts) > 1 and not frag[0].isspace():
            pass
        frag = " ".join(parts)
    if len(frag) < 20 or frag not in text:
        return None
    return frag


def cmd_anchors(args, proj):
    cards, _ = load_cards(proj)
    counts = Counter()
    problems, repairs = [], []
    for c in cards:
        text = corpus_body(proj, c.get("video_id", ""))
        if text is None:
            counts["no_transcript"] += 1
            continue
        status, span = anchor_status(c.get("anchor"), text)
        key = status.split(":")[0]
        counts[key] += 1
        if key in ("near", "missing"):
            if args.repair:
                fixed = repair_anchor(c.get("anchor"), text)
                if fixed:
                    repairs.append((c["video_id"], c["id"], fixed))
                    counts["repaired"] += 1
                    continue
            problems.append((c.get("id"), status, norm_anchor(c.get("anchor"))[:70]))

    total = sum(counts[k] for k in ("exact", "fragments", "near", "missing")) or 1
    print(f"{len(cards)} cards checked against the corpus\n")
    for k, label in (("exact", "verbatim substring"), ("fragments", "ellipsis, all parts verbatim"),
                     ("near", "close but altered — the creator's words were normalised"),
                     ("missing", "not locatable in the transcript"),
                     ("no_transcript", "video not in corpus")):
        if counts[k]:
            print(f"  {counts[k]:>6}  {counts[k]*100//total:>3}%  {label}")
    if problems:
        print(f"\n{len(problems)} anchors need attention:")
        for cid, st, a in problems[:40]:
            print(f"  [{st}] {cid}  {a}")
        if len(problems) > 40:
            print(f"  ... and {len(problems)-40} more")
    if repairs:
        by_vid = defaultdict(dict)
        for vid, cid, fixed in repairs:
            by_vid[vid][cid] = fixed
        for vid, fixes in by_vid.items():
            f = proj / "cards" / f"{vid}.jsonl"
            if not f.exists():
                continue
            out = []
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                c = json.loads(line)
                if c.get("id") in fixes:
                    c["anchor"] = fixes[c["id"]]
                    c["anchor_repaired"] = True
                out.append(json.dumps(c, ensure_ascii=False))
            f.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nrepaired {len(repairs)} anchors in {len(by_vid)} files "
              f"(marked anchor_repaired)")

    bad = counts["near"] + counts["missing"]
    print(f"\nverbatim rate: {(counts['exact']+counts['fragments'])*100//total}%")
    sys.exit(1 if bad * 100 // total > args.max_bad_pct else 0)


def cmd_validate(args, proj):
    cards, errors = load_cards(proj)
    manifest = load_manifest(proj)
    seen = {}
    for c in cards:
        cid = c.get("id", "<missing>")
        for f in REQUIRED:
            if not c.get(f):
                errors.append(f"{cid}: missing required field '{f}'")
        if not ID_RE.match(str(cid)):
            errors.append(f"{cid}: id must look like EV-<video_id>-01")
        if cid in seen:
            errors.append(f"{cid}: duplicate id (also in {seen[cid]})")
        seen[cid] = c.get("video_id")
        if c.get("type") not in TYPES:
            errors.append(f"{cid}: unknown type '{c.get('type')}'")
        if c.get("stated") not in {"explicit", "implied"}:
            errors.append(f"{cid}: 'stated' must be explicit|implied")
        if manifest and c.get("video_id") not in manifest:
            errors.append(f"{cid}: video_id '{c.get('video_id')}' not in manifest")
        anchor_words = len(str(c.get("anchor", "")).split())
        if anchor_words > MAX_ANCHOR_WORDS:
            errors.append(f"{cid}: anchor is {anchor_words} words — anchors are short "
                          f"locators, keep under {MAX_ANCHOR_WORDS}")
        if len(str(c.get("claim", ""))) > 400:
            errors.append(f"{cid}: claim too long — split into separate cards")
        if not isinstance(c.get("topics"), list) or not c.get("topics"):
            errors.append(f"{cid}: topics must be a non-empty list")

    print(f"{len(cards)} cards loaded from {len(set(seen.values()))} videos")
    if errors:
        print(f"\n{len(errors)} problems:")
        for e in errors[:60]:
            print("  -", e)
        if len(errors) > 60:
            print(f"  ... and {len(errors)-60} more")
        sys.exit(1)
    print("Validation passed.")


def cmd_stats(args, proj):
    cards, _ = load_cards(proj)
    manifest = load_manifest(proj)
    tax = load_taxonomy(proj)
    topics = Counter()
    for c in cards:
        topics.update(canon(c.get("topics", []), tax))
    types = Counter(c.get("type") for c in cards)
    done = {c["video_id"] for c in cards}
    print(f"Videos in manifest : {len(manifest)}")
    print(f"Videos with cards  : {len(done)}"
          + (f"  ({len(done)*100//max(len(manifest),1)}%)" if manifest else ""))
    print(f"Total cards        : {len(cards)}")
    if done:
        print(f"Cards per video    : {len(cards)/len(done):.1f} avg")
    print("\nBy type:")
    for t, n in types.most_common():
        print(f"  {t:<14} {n}")
    print("\nTop topics:")
    for t, n in topics.most_common(40):
        vids = len({c['video_id'] for c in cards if t in canon(c.get('topics', []), tax)})
        print(f"  {t:<28} {n:>5} cards  {vids:>4} videos")


def cmd_ledger(args, proj):
    cards, errors = load_cards(proj)
    if errors:
        print("Fix validation errors first (python cards.py validate).")
        for e in errors[:10]:
            print("  -", e)
        sys.exit(1)
    manifest = load_manifest(proj)
    tax = load_taxonomy(proj)

    by_topic = defaultdict(list)
    for c in cards:
        for t in canon(c.get("topics", []), tax):
            by_topic[t].append(c)

    ledger = {}
    for topic, group in by_topic.items():
        vids = {c["video_id"] for c in group}
        dates = sorted(d for d in (manifest.get(c["video_id"], {}).get("published")
                                   for c in group) if d)
        strengths = Counter(c.get("strength", "moderate") for c in group)
        if len(vids) >= 30:
            weight = "chapter"
        elif len(vids) >= 8:
            weight = "section"
        elif len(vids) >= 3:
            weight = "subsection"
        else:
            weight = "footnote"
        ledger[topic] = {
            "cards": len(group), "videos": len(vids), "weight": weight,
            "date_span": [dates[0], dates[-1]] if dates else None,
            "types": dict(Counter(c["type"] for c in group)),
            "strength": dict(strengths),
            "card_ids": [c["id"] for c in group],
        }

    # candidate contradictions: same topic, opposing polarity on shared keywords
    conflicts = []
    for topic, group in by_topic.items():
        rules = [c for c in group if c.get("type") in ("rule", "number", "procedure")]
        for i, a in enumerate(rules):
            for b in rules[i + 1:]:
                if a["video_id"] == b["video_id"]:
                    continue
                ka = set(re.findall(r"[a-z]{5,}", a["claim"].lower()))
                kb = set(re.findall(r"[a-z]{5,}", b["claim"].lower()))
                overlap = ka & kb
                if len(overlap) < 3:
                    continue
                if bool(NEGATION.search(a["claim"])) != bool(NEGATION.search(b["claim"])):
                    conflicts.append({"topic": topic, "a": a["id"], "b": b["id"],
                                      "claim_a": a["claim"], "claim_b": b["claim"],
                                      "shared": sorted(overlap)[:6]})
    # explicit self-declared revisions
    for c in cards:
        if c.get("type") == "contradiction":
            conflicts.append({"topic": ",".join(c.get("topics", [])), "a": c["id"],
                              "b": None, "claim_a": c["claim"], "claim_b": None,
                              "shared": ["self-declared revision"]})

    (proj / "ledger").mkdir(exist_ok=True)
    (proj / "ledger/ledger.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    lines = ["# Concept Ledger", "",
             f"{len(cards)} cards · {len({c['video_id'] for c in cards})} videos · "
             f"{len(ledger)} topics", "",
             "| Topic | Cards | Videos | Weight | Date span |", "|---|---|---|---|---|"]
    for t, d in sorted(ledger.items(), key=lambda kv: -kv[1]["videos"]):
        span = " → ".join(d["date_span"]) if d["date_span"] else "—"
        lines.append(f"| {t} | {d['cards']} | {d['videos']} | {d['weight']} | {span} |")
    (proj / "ledger/ledger.md").write_text("\n".join(lines), encoding="utf-8")

    seen_pairs = set()
    clines = ["# Candidate contradictions", "",
              "Machine-flagged. Most are false positives (different contexts).",
              "Triage each into: evolution / conditional / imprecision / unresolved.", ""]
    for c in conflicts:
        key = (c["a"], c["b"])
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        clines.append(f"- **{c['topic']}** — `{c['a']}` vs `{c['b'] or '(self-declared)'}`")
        clines.append(f"  - A: {c['claim_a']}")
        if c["claim_b"]:
            clines.append(f"  - B: {c['claim_b']}")
        clines.append("  - Resolution: _TODO_")
    (proj / "ledger/contradictions.md").write_text("\n".join(clines), encoding="utf-8")

    chapters = [t for t, d in ledger.items() if d["weight"] == "chapter"]
    print(f"Ledger written: {len(ledger)} topics")
    print(f"  chapter-weight topics: {len(chapters)}")
    print(f"  candidate contradictions: {len(seen_pairs)}")
    print(f"\nSee {proj/'ledger/ledger.md'} and {proj/'ledger/contradictions.md'}")


def cmd_fetch(args, proj):
    cards, _ = load_cards(proj)
    manifest = load_manifest(proj)
    tax = load_taxonomy(proj)
    want_topics = {t.strip() for t in (args.topics or "").split(",") if t.strip()}
    want_types = {t.strip() for t in (args.types or "").split(",") if t.strip()}
    want_ids = {t.strip() for t in (args.ids or "").split(",") if t.strip()}

    out = []
    for c in cards:
        ctopics = set(canon(c.get("topics", []), tax))
        if want_ids and c["id"] not in want_ids:
            continue
        if want_topics and not (ctopics & want_topics):
            continue
        if want_types and c.get("type") not in want_types:
            continue
        meta = manifest.get(c["video_id"], {})
        enriched = dict(c)
        enriched["video_title"] = meta.get("title", c.get("video_title", ""))
        enriched["published"] = meta.get("published", c.get("published"))
        out.append(enriched)

    out.sort(key=lambda c: (c.get("published") or "9999", c["video_id"], c["timestamp"]))
    payload = {"count": len(out), "topics": sorted(want_topics),
               "videos": len({c['video_id'] for c in out}), "cards": out}
    text = json.dumps(payload, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(out)} cards from {payload['videos']} videos → {args.out}")
    else:
        print(text)


def cmd_coverage(args, proj):
    cards, _ = load_cards(proj)
    manifest = load_manifest(proj)
    by_video = Counter(c["video_id"] for c in cards)

    cited = set()
    for ch in sorted((proj / "chapters").glob("*.md")):
        cited.update(CITE_RE.findall(ch.read_text(encoding="utf-8")))
    card_video = {c["id"]: c["video_id"] for c in cards}
    videos_in_book = {card_video[i] for i in cited if i in card_video}

    no_cards = [v for v in manifest if by_video.get(v, 0) == 0]
    not_in_book = [v for v in manifest if v not in videos_in_book and by_video.get(v, 0) > 0]

    print(f"Videos in manifest      : {len(manifest)}")
    print(f"Videos with cards       : {len(by_video)}")
    print(f"Videos cited in the book: {len(videos_in_book)}")
    print(f"Cards cited             : {len(cited)} of {len(cards)} "
          f"({len(cited)*100//max(len(cards),1)}%)")
    if no_cards:
        print(f"\n{len(no_cards)} videos produced NO cards — verify each is genuinely "
              f"off-topic rather than under-extracted:")
        for v in no_cards[:25]:
            print(f"  - {v}: {manifest[v].get('title','')[:70]}")
    if not_in_book:
        print(f"\n{len(not_in_book)} videos have cards but nothing cited in any chapter:")
        for v in not_in_book[:25]:
            print(f"  - {v}: {manifest[v].get('title','')[:70]} ({by_video[v]} cards)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["validate", "stats", "ledger", "fetch",
                                    "coverage", "anchors"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--topics", help="comma-separated topics (fetch)")
    ap.add_argument("--types", help="comma-separated card types (fetch)")
    ap.add_argument("--ids", help="comma-separated card ids (fetch)")
    ap.add_argument("--out", help="output file (fetch)")
    ap.add_argument("--repair", action="store_true",
                    help="anchors: rewrite recoverable near-miss anchors to the real "
                         "transcript span, marking them anchor_repaired")
    ap.add_argument("--max-bad-pct", type=int, default=5,
                    help="anchors: fail above this %% of altered/missing anchors")
    args = ap.parse_args()

    proj = Path(args.project)
    if not proj.is_dir():
        sys.exit(f"Project directory not found: {proj}")
    {"validate": cmd_validate, "stats": cmd_stats, "ledger": cmd_ledger,
     "fetch": cmd_fetch, "coverage": cmd_coverage,
     "anchors": cmd_anchors}[args.command](args, proj)


if __name__ == "__main__":
    main()
