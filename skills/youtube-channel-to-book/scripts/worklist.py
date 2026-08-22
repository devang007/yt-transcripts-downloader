#!/usr/bin/env python3
"""Compute what is left to do, from disk, every time.

The disk is the source of truth. Nothing here reads a pre-generated batch spec,
because pre-generated specs go stale the moment a run is interrupted — which is
what produced batches2/ through batches11/ on the previous book and what caused
three chapters to be drafted twice.

Subcommands:
    todo      list (or batch) the units of work not yet done for a phase
    audit     find work that was scheduled but produced nothing — the silent-loss check
    stats     one-line progress summary per phase

Phases:
    skim      cheap pass 1 over every video      -> cards_skim/<video_id>.jsonl
    extract   deep pass 2 over selected videos   -> cards/<video_id>.jsonl
    draft     one chapter per outline entry      -> chapters/<NN-slug>.md

Usage:
    python worklist.py todo  --project ./bb --phase extract --batch-size 6 --limit 30
    python worklist.py audit --project ./bb --phase extract
    python worklist.py stats --project ./bb
"""

import argparse
import json
import time
import re
import sys
from pathlib import Path

# A YouTube ID is exactly 11 characters and may legally begin with "_".
VIDEO_ID_LEN = 11

PHASES = {
    "skim":    {"outdir": "cards_skim", "ext": ".jsonl"},
    "extract": {"outdir": "cards",      "ext": ".jsonl"},
    "draft":   {"outdir": "chapters",   "ext": ".md"},
}


def is_internal(path):
    """True for a phase's own bookkeeping file, false for a real output file."""
    return path.stem.startswith("_") and len(path.stem) != VIDEO_ID_LEN


def manifest_videos(proj):
    p = proj / "corpus/manifest.json"
    if not p.exists():
        sys.exit(f"No manifest at {p} — run ingest.py first.")
    return json.loads(p.read_text(encoding="utf-8")).get("videos", [])


def selection(proj, phase):
    """The set of video IDs a phase is supposed to cover.

    `extract` honours ledger/deep_set.json when it exists (the pruned, deduped
    shortlist chosen after the skim). Absent that file it means every video,
    so a missing shortlist over-covers rather than silently under-covering.
    """
    sel_file = {"extract": "ledger/deep_set.json", "skim": "ledger/prune.json"}.get(phase)
    if sel_file:
        sel = proj / sel_file
        if sel.exists():
            data = json.loads(sel.read_text(encoding="utf-8"))
            key = "skim_set" if phase == "skim" else "deep_set"
            node = data.get(key, data)
            return set(node["video_ids"])
    return None


def done_ids(proj, phase):
    d = proj / PHASES[phase]["outdir"]
    if not d.is_dir():
        return set()
    return {p.stem for p in d.glob("*" + PHASES[phase]["ext"]) if not is_internal(p)}


def lease_dir(proj, phase):
    return proj / PHASES[phase]["outdir"] / "_leases"


def leased_ids(proj, phase, minutes):
    """Ids handed to an agent recently enough that it is probably still working.

    The output file is the source of truth for what is DONE, but it says nothing
    about what is IN FLIGHT — an agent that has been running for four minutes has
    written nothing yet. Without a lease, the next wave re-hands out the same
    batch and two agents do identical work. This is the drafting-phase bug from
    the previous book, in its extraction-phase form.
    """
    d = lease_dir(proj, phase)
    if not d.is_dir() or minutes <= 0:
        return set()
    cutoff = time.time() - minutes * 60
    return {p.name for p in d.iterdir() if p.stat().st_mtime > cutoff}


def claim(proj, phase, ids):
    d = lease_dir(proj, phase)
    d.mkdir(parents=True, exist_ok=True)
    for i in ids:
        (d / i).write_text(str(int(time.time())), encoding="utf-8")


def outline_chapters(proj):
    """Chapter slugs from outline.md, e.g. '## 04 — Reading the chart' -> 04-reading-the-chart."""
    p = proj / "outline.md"
    if not p.exists():
        sys.exit("No outline.md — phase 4 must be signed off before drafting.")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#{2,3}\s+(?:Chapter\s+)?(\d{1,2})\s*[—:.\-]\s*(.+?)\s*$", line)
        if m:
            num, title = m.group(1).zfill(2), m.group(2)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(f"{num}-{slug}")
    return out


def units(proj, phase):
    if phase == "draft":
        return [{"id": c} for c in outline_chapters(proj)]
    sel = selection(proj, phase)
    vids = manifest_videos(proj)
    return [{"id": v["video_id"], "title": v.get("title", ""),
             "words": v.get("word_count", 0), "published": v.get("published")}
            for v in vids if sel is None or v["video_id"] in sel]


def cmd_todo(args, proj):
    all_units = units(proj, args.phase)
    done = done_ids(proj, args.phase)
    busy = leased_ids(proj, args.phase, args.lease_minutes)
    todo = [u for u in all_units if u["id"] not in done and u["id"] not in busy]
    if args.limit:
        todo = todo[:args.limit]

    if args.word_budget:
        batches, cur, run = [], [], 0
        for u in todo:
            w = u.get("words", 0) or 0
            if cur and run + w > args.word_budget:
                batches.append(cur); cur, run = [], 0
            cur.append(u); run += w
        if cur:
            batches.append(cur)
    elif args.batch_size:
        batches = [todo[i:i + args.batch_size]
                   for i in range(0, len(todo), args.batch_size)]
    else:
        batches = None
    if batches is not None:
        payload = {"phase": args.phase, "remaining": len(all_units) - len(done),
                   "in_flight": len(busy),
                   "batches": [{"batch": f"{args.phase[:2].upper()}{n:03d}",
                                "units": b} for n, b in enumerate(batches)]}
    else:
        payload = {"phase": args.phase, "remaining": len(all_units) - len(done),
                   "units": todo}

    if args.claim:
        handed = [u["id"] for b in payload.get("batches", [])[:args.claim]
                  for u in b["units"]] or [u["id"] for u in todo]
        claim(proj, args.phase, handed)

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for u in todo:
            print(u["id"])


def cmd_audit(args, proj):
    """Scheduled but empty: the check that would have caught the 7 lost videos."""
    all_units = units(proj, args.phase)
    d = proj / PHASES[args.phase]["outdir"]
    ext = PHASES[args.phase]["ext"]
    missing, empty = [], []
    for u in all_units:
        f = d / (u["id"] + ext)
        if not f.exists():
            missing.append(u)
        elif f.stat().st_size == 0 and args.phase != "draft":
            empty.append(u)          # legal: "processed, genuinely no cards"
    print(f"phase={args.phase}  scheduled={len(all_units)}  "
          f"missing={len(missing)}  deliberately-empty={len(empty)}")
    if missing:
        print(f"\n{len(missing)} scheduled but NO output file — these are losses, not skips:")
        for u in missing[:40]:
            flag = "  <-- id starts with '_'" if u["id"].startswith("_") else ""
            print(f"  {u['id']:<14} {u.get('title','')[:58]}{flag}")
        if len(missing) > 40:
            print(f"  ... and {len(missing)-40} more")
    if empty:
        print(f"\n{len(empty)} processed with zero cards (expected for promos/ephemera):")
        for u in empty[:15]:
            print(f"  {u['id']:<14} {u.get('title','')[:58]}")
    sys.exit(1 if missing else 0)


def cmd_release(args, proj):
    """Drop leases for work no agent is actually doing.

    A claim is a promise that an agent was launched. If a launcher claims and then
    the launch does not happen — a crashed dispatcher, a status command that
    claimed as a side effect — the work goes invisible until the lease ages out.
    Release is the manual undo. Pass --keep-file listing the ids that ARE genuinely
    in flight; everything else leased-but-unfinished is freed.
    """
    d = lease_dir(proj, args.phase)
    if not d.is_dir():
        print("no leases")
        return
    keep = set()
    if args.keep_file:
        keep = set(Path(args.keep_file).read_text(encoding="utf-8").split())
    done = done_ids(proj, args.phase)
    freed = 0
    for f in sorted(d.iterdir()):
        if f.name in keep or f.name in done:
            continue
        f.unlink()
        freed += 1
    print(f"released {freed} orphaned leases; kept {len(keep)} in-flight, "
          f"{len(done)} already done")


def cmd_stats(args, proj):
    for phase in ("skim", "extract", "draft"):
        try:
            all_units = units(proj, phase)
        except SystemExit:
            print(f"{phase:<9} (not started)")
            continue
        done = done_ids(proj, phase) & {u["id"] for u in all_units}
        n, t = len(done), len(all_units)
        bar = "#" * (n * 30 // max(t, 1))
        print(f"{phase:<9} {n:>5}/{t:<5} {100*n//max(t,1):>3}%  {bar}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["todo", "audit", "stats", "release"])
    ap.add_argument("--project", required=True)
    ap.add_argument("--phase", choices=list(PHASES), default="extract")
    ap.add_argument("--batch-size", type=int, help="group the todo list into batches of N")
    ap.add_argument("--word-budget", type=int,
                    help="group into batches of at most N transcript words (preferred: "
                         "keeps agent context even when video lengths vary wildly)")
    ap.add_argument("--limit", type=int, help="cap how many units are returned")
    ap.add_argument("--claim", type=int, metavar="N",
                    help="mark the first N batches as in-flight so a concurrent "
                         "launcher does not hand them out again")
    ap.add_argument("--lease-minutes", type=int, default=45,
                    help="how long a claim suppresses re-handout (0 disables)")
    ap.add_argument("--keep-file",
                    help="release: file of whitespace-separated ids that ARE in flight")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    proj = Path(args.project)
    if not proj.is_dir():
        sys.exit(f"Project directory not found: {proj}")
    {"todo": cmd_todo, "audit": cmd_audit, "stats": cmd_stats,
     "release": cmd_release}[args.command](args, proj)


if __name__ == "__main__":
    main()
