#!/usr/bin/env python3
"""Find videos whose transcript text substantially reappears in another video.

Two distinct problems, one signature:

  * Re-uploads and montages — the same lesson published twice. Harmless to the
    book's accuracy, but it inflates repetition counts, and repetition count is
    exactly what decides chapter weight.
  * Clipped guest content — a short cut from a guest interview, carrying no
    speaker cue. Read alone it reads as the creator teaching in first person, and
    it yields clean, confident, completely misattributed cards. Title clustering
    cannot see this; only the text can.

Shingle overlap (Jaccard on word 5-grams), which survives ASR noise better than
exact matching.

Usage:
    python dupe_text.py --project ./bb --min-overlap 0.25
    python dupe_text.py --project ./bb --min-overlap 0.25 --short-max-words 400
"""

import argparse
import json
import re
import unicodedata
from pathlib import Path


def body(proj, vid):
    f = proj / "corpus/videos" / f"{vid}.txt"
    if not f.exists():
        return ""
    t = f.read_text(encoding="utf-8")
    t = t.split("---", 1)[1] if "---" in t else t
    t = re.sub(r"\[\d{2}:\d{2}:\d{2}\]", " ", t)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", t)).strip()


def shingles(text, n=5):
    w = text.split()
    return {" ".join(w[i:i + n]) for i in range(max(len(w) - n + 1, 0))}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--min-overlap", type=float, default=0.25,
                    help="report pairs above this containment score (0-1)")
    ap.add_argument("--short-max-words", type=int, default=500,
                    help="treat videos at or below this as clips to be matched "
                         "against longer sources")
    ap.add_argument("--out", help="write JSON report here")
    args = ap.parse_args()

    proj = Path(args.project)
    man = json.loads((proj / "corpus/manifest.json").read_text(encoding="utf-8"))["videos"]
    meta = {v["video_id"]: v for v in man}

    sh, words = {}, {}
    for v in man:
        t = body(proj, v["video_id"])
        s = shingles(t)
        if len(s) >= 10:                      # too short to judge
            sh[v["video_id"]] = s
            words[v["video_id"]] = len(t.split())

    shorts = [v for v in sh if words[v] <= args.short_max_words]
    longs = sorted((v for v in sh if words[v] > args.short_max_words),
                   key=lambda v: -words[v])

    # containment of the SHORT inside the LONG: asymmetric on purpose, since a
    # 60-word clip lifted from a 9,000-word interview has tiny Jaccard but ~1.0
    # containment.
    hits = []
    for c in shorts:
        best, src = 0.0, None
        for l in longs:
            ov = len(sh[c] & sh[l]) / max(len(sh[c]), 1)
            if ov > best:
                best, src = ov, l
        if best >= args.min_overlap:
            hits.append({"clip": c, "clip_words": words[c], "source": src,
                         "containment": round(best, 3),
                         "clip_title": (meta[c].get("title") or "")[:70],
                         "source_title": (meta[src].get("title") or "")[:70]})

    hits.sort(key=lambda h: -h["containment"])
    print(f"{len(shorts)} clips checked against {len(longs)} longer videos")
    print(f"{len(hits)} clips overlap a longer video at >= {args.min_overlap}\n")
    for h in hits[:40]:
        print(f"  {h['containment']:.2f}  {h['clip']} ({h['clip_words']}w) "
              f"<- {h['source']}")
        print(f"        clip: {h['clip_title']}")
        print(f"        src : {h['source_title']}")
    if len(hits) > 40:
        print(f"  ... and {len(hits)-40} more")
    if args.out:
        Path(args.out).write_text(json.dumps(hits, indent=1, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}")
    print("\nA high-containment clip from a GUEST interview carries the guest's "
          "teaching, not the creator's. Check the source before trusting its cards.")


if __name__ == "__main__":
    main()
