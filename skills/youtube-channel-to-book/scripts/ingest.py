#!/usr/bin/env python3
"""Normalize a directory of YouTube transcripts into a clean, timestamped corpus.

Handles .vtt, .srt, .json (yt-dlp json3 / youtube-transcript-api), and .txt.
Removes the rolling-caption duplication that auto-generated VTT is full of.

Usage:
    python ingest.py --input ./transcripts --project ./my-book
"""

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path

TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")
TAG_RE = re.compile(r"<[^>]+>")
# yt-dlp default naming: "Title [videoid].en.vtt"
ID_BRACKET_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
DATE_RE = re.compile(r"(20\d{2})[-.]?(\d{2})[-.]?(\d{2})")


def hms_to_seconds(h, m, s, ms="0"):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def seconds_to_hms(total):
    total = int(total)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def clean_text(line):
    line = TAG_RE.sub("", line)
    line = html.unescape(line)
    line = re.sub(r"\[(Music|Applause|Laughter|Sound|Silence)[^\]]*\]", "", line, flags=re.I)
    return re.sub(r"\s+", " ", line).strip()


def parse_vtt_srt(path):
    """Return [(seconds, text)] from a WebVTT or SRT file."""
    cues = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    raw = raw.replace("\r\n", "\n").replace("\ufeff", "")
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        start = None
        text_lines = []
        for ln in lines:
            if "-->" in ln:
                m = TIME_RE.search(ln)
                if m:
                    start = hms_to_seconds(*m.groups())
                else:  # SRT/VTT short form mm:ss.mmm
                    m2 = re.search(r"(\d{1,2}):(\d{2})[.,](\d{1,3})", ln)
                    if m2:
                        start = hms_to_seconds(0, m2.group(1), m2.group(2), m2.group(3))
                continue
            if ln.strip().isdigit() and start is None:
                continue
            if ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
                continue
            text_lines.append(ln)
        if start is None or not text_lines:
            continue
        text = clean_text(" ".join(text_lines))
        if text:
            cues.append((start, text))
    return cues


def parse_json(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return []
    cues = []
    # youtube-transcript-api: [{"text":..,"start":..,"duration":..}]
    if isinstance(data, list):
        for seg in data:
            if isinstance(seg, dict) and "text" in seg:
                cues.append((float(seg.get("start", 0)), clean_text(str(seg["text"]))))
    # yt-dlp json3: {"events":[{"tStartMs":..,"segs":[{"utf8":..}]}]}
    elif isinstance(data, dict) and "events" in data:
        for ev in data["events"]:
            segs = ev.get("segs") or []
            text = clean_text("".join(s.get("utf8", "") for s in segs))
            if text:
                cues.append((ev.get("tStartMs", 0) / 1000.0, text))
    return [(s, t) for s, t in cues if t]


def parse_txt(path):
    """Plain text; recover inline [MM:SS] or (HH:MM:SS) markers if present."""
    cues = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        current = 0.0
        for line in fh:
            m = re.match(r"^\s*[\[\(]?(\d{1,2}):(\d{2})(?::(\d{2}))?[\]\)]?\s+(.*)", line)
            if m:
                a, b, c, rest = m.groups()
                current = hms_to_seconds(a, b, c) if c else hms_to_seconds(0, a, b)
                text = clean_text(rest)
            else:
                text = clean_text(line)
            if text:
                cues.append((current, text))
    return cues


def dedupe_rolling(cues):
    """Auto-generated VTT repeats each line as the caption scrolls.

    Collapse by walking the token stream and only keeping text not already
    emitted at the tail of the previous cue.
    """
    out = []
    prev = ""
    for start, text in cues:
        if not text:
            continue
        if text == prev:
            continue
        if prev and text.startswith(prev):
            text = text[len(prev):].strip()
        elif prev.endswith(text):
            continue
        else:
            # overlap suffix of prev == prefix of text
            words_prev, words_cur = prev.split(), text.split()
            max_ov = min(len(words_prev), len(words_cur), 12)
            for n in range(max_ov, 2, -1):
                if words_prev[-n:] == words_cur[:n]:
                    text = " ".join(words_cur[n:])
                    break
        text = text.strip()
        if text:
            out.append((start, text))
            prev = " ".join((prev + " " + text).split()[-25:])
    return out


def to_paragraphs(cues, block_seconds=30):
    """Group cues into timestamped paragraphs."""
    blocks, buf, block_start = [], [], None
    for start, text in cues:
        if block_start is None:
            block_start = start
        buf.append(text)
        if start - block_start >= block_seconds:
            blocks.append((block_start, " ".join(buf)))
            buf, block_start = [], None
    if buf:
        blocks.append((block_start or 0, " ".join(buf)))
    return blocks


def extract_meta(path):
    name = path.stem
    for suffix in (".en", ".en-US", ".en-GB", ".auto", ".live_chat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    vid = None
    m = ID_BRACKET_RE.search(name)
    if m:
        vid = m.group(1)
        name = ID_BRACKET_RE.sub("", name)
    date = None
    d = DATE_RE.search(path.stem)
    if d:
        date = f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
        name = name.replace(d.group(0), "")
    title = re.sub(r"[_\-\s]+", " ", name).strip(" -_")
    return vid, title or path.stem, date


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="directory of transcript files")
    ap.add_argument("--project", required=True, help="project directory to create")
    ap.add_argument("--block-seconds", type=int, default=30,
                    help="seconds per timestamped paragraph (default 30)")
    ap.add_argument("--min-words", type=int, default=150,
                    help="flag videos shorter than this as likely non-content")
    args = ap.parse_args()

    src = Path(args.input)
    proj = Path(args.project)
    if not src.is_dir():
        sys.exit(f"Input directory not found: {src}")

    for sub in ("corpus/videos", "cards", "ledger", "chapters", "book/figures"):
        (proj / sub).mkdir(parents=True, exist_ok=True)

    parsers = {".vtt": parse_vtt_srt, ".srt": parse_vtt_srt,
               ".json": parse_json, ".txt": parse_txt, ".md": parse_txt}

    manifest, failures = [], []
    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in parsers)
    if not files:
        sys.exit(f"No .vtt/.srt/.json/.txt files found under {src}")

    seen_ids = {}
    for path in files:
        try:
            cues = parsers[path.suffix.lower()](path)
            if not cues:
                failures.append({"file": str(path), "reason": "no cues parsed"})
                continue
            cues = dedupe_rolling(cues)
            blocks = to_paragraphs(cues, args.block_seconds)
            vid, title, date = extract_meta(path)
            if not vid:
                vid = re.sub(r"[^A-Za-z0-9]+", "_", path.stem)[:40]
            if vid in seen_ids:
                seen_ids[vid] += 1
                vid = f"{vid}_{seen_ids[vid]}"
            else:
                seen_ids[vid] = 0

            body = "\n\n".join(f"[{seconds_to_hms(s)}] {t}" for s, t in blocks)
            words = sum(len(t.split()) for _, t in blocks)
            duration = int(cues[-1][0]) if cues else 0
            out = proj / "corpus/videos" / f"{vid}.txt"
            header = (f"# {title}\nvideo_id: {vid}\npublished: {date or 'unknown'}\n"
                      f"duration: {seconds_to_hms(duration)}\nwords: {words}\n"
                      f"source_file: {path.name}\n\n---\n\n")
            out.write_text(header + body, encoding="utf-8")

            manifest.append({
                "video_id": vid, "title": title, "published": date,
                "duration_seconds": duration, "word_count": words,
                "path": str(out.relative_to(proj)), "source_file": str(path),
                "url": f"https://youtu.be/{vid}" if len(vid) == 11 else None,
                "flags": (["short"] if words < args.min_words else [])
                         + (["long_form"] if duration > 5400 else []),
            })
        except Exception as exc:  # keep going; report at the end
            failures.append({"file": str(path), "reason": repr(exc)})

    manifest.sort(key=lambda m: (m["published"] or "9999", m["title"]))
    (proj / "corpus/manifest.json").write_text(
        json.dumps({"videos": manifest, "failures": failures}, indent=2), encoding="utf-8")

    total_words = sum(m["word_count"] for m in manifest)
    dated = [m["published"] for m in manifest if m["published"]]
    print(f"Ingested {len(manifest)} videos → {proj/'corpus/videos'}")
    print(f"Total words: {total_words:,}  (~{total_words*1.35/1000:.0f}K tokens)")
    if dated:
        print(f"Date range: {min(dated)} → {max(dated)}")
    else:
        print("Date range: unknown (no dates in filenames — consider a metadata file)")
    shorts = [m for m in manifest if "short" in m["flags"]]
    longs = [m for m in manifest if "long_form" in m["flags"]]
    if shorts:
        print(f"Flagged {len(shorts)} very short videos (possible shorts/sponsor clips)")
    if longs:
        print(f"Flagged {len(longs)} videos over 90 min (likely livestreams)")
    if failures:
        print(f"\n{len(failures)} files failed to parse:")
        for f in failures[:10]:
            print(f"  - {Path(f['file']).name}: {f['reason']}")
    print(f"\nManifest: {proj/'corpus/manifest.json'}")


if __name__ == "__main__":
    main()
