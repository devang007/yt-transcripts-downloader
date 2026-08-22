#!/usr/bin/env python3
"""Decide which videos are worth extracting, before spending anything on agents.

Three cheap filters, in order:

  1. Ephemera   — date-stamped content with no doctrinal shelf life ("Market
                  Analysis for Tomorrow"). A channel that posts daily analysis
                  can be 20-30% ephemera, and none of it belongs in a book about
                  the creator's *method*.
  2. Clusters   — near-duplicate uploads (re-uploads, "Part 1 / Part 2" pairs
                  sharing an opening, the same explanation filmed twice). One
                  representative is deep-extracted; the cluster size is carried
                  forward as the repetition weight, which is all the ledger
                  wanted from the duplicates anyway.
  3. Thin       — below a word floor, kept for the skim pass but never deep.

Nothing is deleted. The output is a plan, reviewed before it is acted on.

Usage:
    python prune.py --project ./bb --drop-pattern "market analysis|nifty" --report
    python prune.py --project ./bb --drop-pattern "..." --write
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# Channel-name boilerplate that survives into every title and defeats clustering.
BOILERPLATE = re.compile(
    r"\|\||#\w+|\b(booming bulls|academy|official|hindi|english|part\s*\d+|"
    r"episode\s*\d+|ep\.?\s*\d+|full video|shorts?)\b", re.I)


def norm_title(t):
    t = BOILERPLATE.sub(" ", t or "")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def cluster(videos, threshold):
    """Group by normalized title similarity. O(n*k), k = distinct title buckets."""
    buckets = defaultdict(list)
    for v in videos:
        buckets[norm_title(v["title"])].append(v)

    keys = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    merged, used = [], set()
    for i, a in enumerate(keys):
        if a in used or not a:
            continue
        group = list(buckets[a])
        used.add(a)
        for b in keys[i + 1:]:
            if b in used or not b:
                continue
            # cheap length gate before the expensive ratio
            if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.4:
                continue
            if SequenceMatcher(None, a, b).ratio() >= threshold:
                group.extend(buckets[b])
                used.add(b)
        merged.append(group)
    for k in keys:
        if k not in used:
            merged.append(buckets[k])
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--drop-pattern", default="",
                    help="regex on title; matches are ephemera (case-insensitive)")
    ap.add_argument("--min-words", type=int, default=250,
                    help="below this, skim only — never deep-extract")
    ap.add_argument("--similarity", type=float, default=0.86,
                    help="title similarity for clustering (0-1)")
    ap.add_argument("--write", action="store_true",
                    help="write ledger/prune.json; otherwise report only")
    args = ap.parse_args()

    proj = Path(args.project)
    man = json.loads((proj / "corpus/manifest.json").read_text(encoding="utf-8"))
    videos = man["videos"]
    drop_re = re.compile(args.drop_pattern, re.I) if args.drop_pattern else None

    ephemera = [v for v in videos if drop_re and drop_re.search(v["title"] or "")]
    eph_ids = {v["video_id"] for v in ephemera}
    rest = [v for v in videos if v["video_id"] not in eph_ids]

    groups = cluster(rest, args.similarity)
    reps, dupes, weights = [], [], {}
    for g in groups:
        # the longest transcript is the best representative of a repeated lesson
        g.sort(key=lambda v: -v.get("word_count", 0))
        reps.append(g[0])
        weights[g[0]["video_id"]] = len(g)
        dupes.extend(g[1:])

    deep = [v for v in reps if v.get("word_count", 0) >= args.min_words]
    thin = [v for v in reps if v.get("word_count", 0) < args.min_words]

    tw = lambda xs: sum(x.get("word_count", 0) for x in xs)
    total = len(videos)
    print(f"{'category':<26}{'videos':>8}{'words':>12}   share")
    for name, xs in (("ephemera (drop)", ephemera), ("duplicate of a cluster", dupes),
                     ("thin (skim only)", thin), ("DEEP-EXTRACT SET", deep)):
        print(f"{name:<26}{len(xs):>8}{tw(xs):>12,}{len(xs)*100//max(total,1):>7}%")
    print(f"{'TOTAL':<26}{total:>8}{tw(videos):>12,}")

    big = sorted(((n, vid) for vid, n in weights.items() if n > 1), reverse=True)[:10]
    if big:
        title = {v["video_id"]: v["title"] for v in videos}
        print(f"\nlargest repetition clusters (size = doctrinal weight):")
        for n, vid in big:
            print(f"  x{n:<3} {vid:<13} {(title.get(vid) or '')[:60]}")

    if args.write:
        (proj / "ledger").mkdir(exist_ok=True)
        out = {
            "deep_set": {"video_ids": [v["video_id"] for v in deep]},
            "skim_set": {"video_ids": [v["video_id"] for v in reps]},
            "cluster_weights": weights,
            "dropped": {"ephemera": [v["video_id"] for v in ephemera],
                        "duplicates": [v["video_id"] for v in dupes]},
            "params": {"drop_pattern": args.drop_pattern, "min_words": args.min_words,
                       "similarity": args.similarity},
        }
        (proj / "ledger/prune.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        (proj / "ledger/deep_set.json").write_text(
            json.dumps(out["deep_set"], indent=1), encoding="utf-8")
        print(f"\nwrote {proj/'ledger/prune.json'} and {proj/'ledger/deep_set.json'}")
    else:
        print("\n(report only — pass --write to commit this plan to disk)")


if __name__ == "__main__":
    main()
