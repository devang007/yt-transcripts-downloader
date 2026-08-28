---
name: youtube-channel-to-book
description: Turn a folder of downloaded YouTube transcripts from an entire channel into a chapter-wise, publication-quality educational book that faithfully explains that creator's complete system — their strategies, methods, decision rules, and psychology — with every claim traceable to a timestamped source. Use this skill whenever the user has bulk transcripts (.vtt/.srt/.json/.txt), a channel archive, or a playlist dump and wants a book, ebook, handbook, distilled guide, "everything X teaches" document, knowledge base, or structured synthesis out of them. Trigger even when the user only says "make a book from these transcripts", "summarize this whole channel", "turn this trader's videos into a guide", or names a creator and a folder of subtitle files. Also use when the user asks to extend, verify, re-verify, or add chapters to a book previously built this way.
---

# YouTube Channel → Book

Convert an entire channel's transcripts into a book a reader can learn from — organized by concept rather than by upload date, dense with the creator's actual method, and free of invented content.

## The two problems this skill exists to solve

**Scale.** A 300-video channel is 3–8 million tokens. It cannot be read in one pass, so the book cannot be written in one pass. The pipeline below is map-reduce: extract structured evidence per video, aggregate into a concept ledger, then write chapters *from the ledger*, never from memory of a video read long ago.

**Fidelity.** "No hallucinations" fails if it's only an instruction. It has to be mechanical: every substantive claim in the book carries an evidence ID, a script checks that each ID resolves to a real extracted card, and unsupported sentences get deleted before assembly. A claim without a card is not a claim — it's a guess, and guesses are what destroy the value of a book like this.

## Pipeline

Run these in order. Each phase writes to disk so work survives context resets and can resume.

```
transcripts/ → [1 ingest] → corpus/ → [2a skim ALL] → cards_skim/ → [3 ledger] →
ledger/ → [4 outline + deep_set] → [2b deep-extract SELECTED] → cards/ →
[5 draft] → chapters/ → [6 verify] → [7 build] → book/
```

`worklist.py` computes the remaining work for every phase by reading the output
directory. Run it before launching agents, and after — never hand-maintain a list
of what is left.

Create the project scaffold first:

```bash
python scripts/ingest.py --input <transcripts-dir> --project <project-dir>
```

### Phase 1 — Ingest and inventory

`ingest.py` normalizes `.vtt`, `.srt`, `.json` (yt-dlp / youtube-transcript-api), and `.txt` into clean timestamped text, strips the rolling-caption duplication that auto-generated VTT is full of, and writes `manifest.json` with video ID, title, upload date if recoverable, duration, and word count.

Then read the manifest and **report to the user before proceeding**: number of videos, total words, date range, and any files that failed to parse. Also flag obvious non-content videos (livestream Q&A with 4 hours of chatter, shorts under ~200 words, sponsor-only clips) and ask whether to include them — Q&A streams often hold the best material on psychology, so don't drop them silently.

Sort the manifest chronologically. Chronology matters: it's how you detect that the creator's method changed in 2023, which is one of the most valuable things a book like this can surface.

### Phase 2 — Extract evidence cards (the map step)

**Extract in two passes, not one.** The ledger only needs topic tags and enough
cards to count distinct videos per topic; it does not need 25 fully-conditioned
cards from a promo video no chapter will ever cite. On the previous book, ~90% of
extracted cards were never cited.

- **2a — skim, every video.** Topics plus 3–5 cards each, into `cards_skim/`.
  Purely to build the ledger weights. Cheap model, low effort.
- **2b — deep, selected videos only.** After the outline is signed off, write the
  shortlist the approved chapters actually draw on to `ledger/deep_set.json`
  (`{"video_ids": [...]}`) and extract those properly into `cards/`.

Before 2a, prune with a script rather than an agent: drop date-stamped ephemera
(daily market analysis, "analysis for tomorrow"), collapse duplicate-title clusters
to one representative and carry the cluster size as the repetition weight, and
treat shorts as skim-only. This is free and routinely removes 20–30% of a channel.

Read `references/extraction.md` before starting this phase. It contains the card schema, the type taxonomy, and worked examples.

Process videos one at a time (or in small batches for short videos), reading each transcript from `corpus/videos/`. For each, emit JSONL evidence cards to `cards/<video_id>.jsonl`. A card is one atomic claim with:

- a one-sentence paraphrase of what the creator said,
- a short verbatim anchor (a handful of words, never a paragraph) so a human can find it,
- the timestamp and a deep link,
- a type (`rule`, `procedure`, `rationale`, `psychology`, `example`, `number`, `definition`, `caveat`, `anecdote`, `opinion`, `contradiction`),
- topic tags,
- whether the claim was stated explicitly or is your inference from what was shown.

Extraction is where fidelity is won or lost. Two habits matter most: **write down what the creator actually said, not the nearest well-known version of it** (if he says he waits for a close beyond the level, do not upgrade that to "waits for a retest and close beyond" because that's the more common formulation), and **capture conditions** — "cut risk in half" is useless without "on FOMC days".

Write `cards/<video_id>.jsonl` immediately on finishing each video — one file per
video, never a batch-wide file. If a video genuinely yields nothing, write an
**empty** `.jsonl` anyway and log the reason in `cards/_no_cards.jsonl`: an absent
file means "not done", and conflating the two is how videos get silently lost.
Never derive progress from anything but these files. Run `python scripts/cards.py anchors --project <dir>` after every batch. It checks
each anchor against the transcript and reports the verbatim rate. An anchor that is
*close but altered* is the quiet failure mode: the model normalised the creator's
words while claiming to quote them, and nothing downstream will catch it. Treat a
verbatim rate below ~95% as a signal that the extraction model is too weak for this
corpus, not as a batch to patch up.

After every ~20 videos, run `python scripts/cards.py stats --project <dir>` and show the user the emerging topic distribution. This is the earliest point where a wrong taxonomy becomes visible and cheap to fix.

### Phase 3 — Build the concept ledger (the reduce step)

```bash
python scripts/cards.py ledger --project <project-dir>
```

This groups cards by topic, counts how many distinct videos support each concept, spans the date range for each, and flags candidate contradictions (cards tagged to the same topic whose claims conflict).

Repetition count is the skill's proxy for doctrinal weight. Something the creator returns to across 40 videos is the spine of a chapter; something said once in passing is a footnote at most. Writing a book that gives equal airtime to both misrepresents the channel — this is the single most common failure mode of AI summarization, and the ledger is the defense against it.

Read `references/synthesis.md` for how to turn the ledger into a taxonomy, resolve contradictions, and handle concept drift over time.

### Phase 4 — Outline

Propose a chapter structure to the user and get sign-off before drafting. The organizing principle is **the reader's learning path, not the ledger's topic counts** — a book on a trading channel usually runs: what the creator believes markets are → the framework → the setups → entries/exits → risk and position sizing → trade management → psychology and process → routine and review → common failure modes → putting it together.

Each chapter entry in `outline.md` names: the chapter thesis in one sentence, the concepts it draws from, the evidence card IDs available, the estimated word count, and the planned figures. If a proposed chapter has fewer than ~15 supporting cards from fewer than ~5 videos, it is a section, not a chapter — merge it. Thin chapters are where fabrication creeps in, because there isn't enough real material to fill the space.

### Phase 5 — Draft chapters

Read `references/writing.md` before drafting. Draft one chapter per working session into `chapters/NN-slug.md`, pulling only that chapter's cards:

```bash
python scripts/cards.py fetch --project <dir> --topics entry,confirmation --out /tmp/ch04_cards.json
```

The mandatory content model:

**Tier A — the creator's material.** Everything attributed to the creator. Every such sentence carries one or more `[EV-0142]` citations. Paraphrased into clean prose — you are writing a book, not pasting a transcript. Direct quotation is reserved for lines where the exact wording carries the meaning, kept to a handful of words, at most a couple per chapter, always attributed. Never reproduce transcript passages at length; the value here is synthesis, and long verbatim reproduction is both bad writing and a copyright problem.

**Tier B — editorial explanation.** Your own general knowledge, used to make the creator's ideas *land*: defining a term he uses without explaining, adding a worked numeric example, drawing the analogy, giving the real-world case. Tier B always appears in a visually marked block and must attach to a Tier A anchor cited in the same section. An orphan Tier B block — explaining something the creator never raised — is out-of-scope content and gets cut. Tier B may explain, illustrate, and contextualize. It may never add a rule, a number, a setup, or an opinion and let it read as the creator's.

```markdown
> **Editor's note — what a stop-hunt actually is**
> [Creator] refers to "the sweep" constantly [EV-0142, EV-0311] without defining it...
```

If a chapter needs something the creator never covered, say so plainly in the text ("he never addresses position sizing for options; the transcripts contain no material on this"). Gaps are information. Filling them silently is the failure this whole skill is designed to prevent.

Figures: author them as SVG into `book/figures/` (diagrams, decision trees, annotated schematic charts) or matplotlib for real numbers. Read `references/writing.md` for the figure rules — the important one is that a chart may only plot numbers the creator actually stated. Do not draw a fabricated equity curve.

### Phase 6 — Verify

```bash
python scripts/verify.py --project <project-dir>
```

Checks that every `[EV-xxxx]` resolves to a real card, flags substantive paragraphs in Tier A prose with no citation, flags Tier B blocks with no nearby anchor, flags over-long quoted spans, and reports per-chapter citation density and source-video spread.

The script catches mechanical failures. It cannot catch a citation that resolves but doesn't actually support the sentence, so after it passes, do a semantic pass: sample 10–15 cited claims per chapter, open the card, and confirm the sentence says what the card says. Fix or cut anything that drifted. Report the sample result to the user honestly.

Then run coverage:

```bash
python scripts/cards.py coverage --project <project-dir>
```

Any video contributing zero cards to the book is either genuinely off-topic or was under-extracted. Check a few; under-extraction usually means a whole theme got missed.

### Phase 7 — Build

```bash
python scripts/build_book.py --project <project-dir> --format html,md
python scripts/build_book.py --project <project-dir> --format pdf   # needs weasyprint
```

Assembles front matter, TOC, chapters, and the required back matter: a **Source Index** (every evidence ID → video title, timestamp, deep link), a **Method & Coverage appendix** (how many videos processed, date range, how the book was made, what the tiers mean), and a **Gaps appendix** (what the channel doesn't cover).

The Source Index is not bureaucratic overhead — it's the feature that makes the book trustworthy. A reader who doubts a paragraph can be watching the exact second of the exact video in ten seconds.

## Cost control and model routing

Extraction dominates the budget: it is ~85% of the tokens a book like this costs.
It is also a mechanical transform, which is the cheapest kind of work to route.

| Phase | Model | Why |
|---|---|---|
| 1 ingest, prune, cluster | none / cheapest | scripts and title classification |
| 2a skim | cheap tier, low effort | mechanical, high volume |
| 3 ledger | none | script |
| 4 outline | strongest tier | pure judgment, one agent, tiny |
| 2b deep extract | cheap tier, medium effort | mechanical, validated by sample |
| 5 draft | strongest tier | this is the visible product |
| 6 verify (mechanical) | none | script |
| 6 verify (semantic sample) | strongest tier | subtle judgment; where it pays |
| 7 build | none | script |

**Set the model explicitly on every extraction agent.** A subagent inherits the
orchestrator's model when the parameter is omitted, so forgetting it silently runs
the whole map step on the expensive tier — not a wrong choice, an unmade one.

**Validate the cheap tier before committing to it**, on ~12-15 videos, diffed
against the strong tier. Score it mechanically with `cards.py anchors` (verbatim
rate) and by conditions-fill rate, not by reading the agents' own reports — a model
that writes a confident report while normalising anchors is the worst case, and only
the substring check separates it from a good one.

Re-run this validation whenever the corpus changes character. **An English-language
result does not transfer to a non-English channel.** On a Hinglish trading channel
the cheap tier held a 86% verbatim rate against the strong tier's 99%, and filled
`conditions` on 63% of cards against 100% — degrading precisely on the two fields
that make the difference between a useful book and a dangerous one, while its prose
reports still read as authoritative.

**Cap concurrency deliberately.** Small waves that checkpoint to disk turn a rate
limit into a pause; one large burst turns it into lost work.

## Standing rules

- **Give concurrent agents unique scratch filenames.** Agents sharing a scratch
  directory and using generic script names (`gen.py`, `script.py`) will silently
  overwrite each other mid-run. Card *output* survives this because each agent verifies
  its own anchors before writing, but the collision wastes a full agent run. Tell every
  extraction agent to name scratch files after its own assigned video id(s).
- **Separate reading progress from claiming work.** A launcher that hands out work
  must also mark it in-flight, or a second launcher re-hands the same batch while the
  first agent is still working — the completed-file check cannot see an agent that has
  not written anything yet. But a claim is a promise that a launch followed, so the
  claiming command must never sit inside a status loop or a background waiter. Keep a
  read-only status command that claims nothing, and a release path for undoing a claim
  whose launch never happened.
- **The disk is the source of truth, in every phase.** Every launcher recomputes
  its work list from the output directory immediately before spawning, and never
  from a spec generated earlier in the run. Drafting needs this exactly as much as
  extraction does — skipping it there redrew three finished chapters on the last book.
- **Attribute the work.** Front matter states plainly that this is an independent study guide derived from [Channel]'s public videos, links the channel, and notes it isn't affiliated with or endorsed by the creator. If the user plans to distribute or sell it, tell them once that a derivative work like this needs the creator's permission, and that the safe version is heavily synthesized commentary with links back to the originals rather than a transcript rehash.
- **Never invent numbers.** Win rates, R multiples, backtest results, price levels — if it isn't in a card, it doesn't go in the book, not even as "typically around...".
- **Preserve hedges.** If he says "I usually" or "this doesn't always work", the book says that too. Sanding hedges off is how a nuanced method turns into dangerous-sounding dogma.
- **Contradictions are content.** When the creator's view changed, write the evolution with dates rather than picking a winner or averaging them.
- **Match register to domain.** For a trading channel, the book will describe risky activity. Report the creator's rules as his rules, keep the standard note that it's educational content and not financial advice in the front matter, and don't add promotional energy the transcripts don't contain.

## Reference files

| File | Read when |
|---|---|
| `references/extraction.md` | Before Phase 2 — card schema, types, worked examples, common extraction errors |
| `references/synthesis.md` | Before Phases 3–4 — taxonomy building, contradiction handling, outline patterns |
| `references/writing.md` | Before Phase 5 — chapter template, voice, Tier A/B rules, figure authoring |
| `references/domains.md` | At Phase 4 — domain-specific chapter skeletons (trading, fitness, business, technical/tutorial channels) |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/ingest.py` | Normalize subtitle formats, dedupe rolling captions, build manifest |
| `scripts/worklist.py` | What is left per phase (`todo`), and what was scheduled but produced nothing (`audit`) |
| `scripts/cards.py` | Validate cards, build ledger, fetch by topic, stats, coverage |
| `scripts/prune.py` | Ephemera, near-duplicate clusters and thin videos — the plan for what to extract |
| `scripts/verify.py` | Citation resolution, uncited-claim detection, quote-length audit |
| `scripts/build_book.py` | Assemble chapters + back matter into MD/HTML/PDF |

Run any script with `--help` for arguments.
