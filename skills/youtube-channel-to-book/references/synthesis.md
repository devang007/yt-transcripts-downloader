# Phases 3–4 Reference — Synthesis and Outline

Extraction gave you thousands of atomic claims scattered across hundreds of videos. Synthesis is where they become a *system* — and where you discover the shape of what the creator actually teaches, which is almost never the shape of his upload schedule.

## Building the taxonomy

Run `cards.py ledger` first, then read the topic distribution.

The tags you assigned during extraction were bottom-up and messy. Now consolidate: merge synonyms (`stop-loss`, `stops`, `risk-per-trade` → `risk-management`), split overloaded tags (a 400-card `psychology` bucket is really `emotional-control`, `discipline-systems`, `identity-and-self-image`, `handling-losses`), and promote recurring sub-patterns to their own topics.

Write the consolidated map to `ledger/taxonomy.json` and re-run the ledger. Show the user the before/after — this is a structural decision they should see.

**Weighting.** For each consolidated topic the ledger reports: total cards, distinct videos, date span, and strength mix. Read these as doctrine weight:

- 30+ videos → almost certainly a chapter of its own
- 8–30 videos → a chapter section
- 3–8 videos → a subsection or sidebar
- 1–2 videos → a footnote, or omit unless it's uniquely illuminating

A concept mentioned in five videos across five years is more load-bearing than one mentioned five times in a single video.

## Handling contradictions and drift

The ledger flags candidate conflicts. Sort each into one of four cases:

**Genuine evolution** — dated positions that changed. This is the most valuable thing you'll find, and readers of a long-running channel need it because they'll otherwise apply 2019 advice to a 2026 market. Write it as a timeline: what he taught, when, and what he said changed. If he gave a reason, cite it.

**Conditional difference** — not a contradiction at all, two different contexts. "Never trade the open" and "the open is my best window" resolve once you notice one is about indices and the other about FX. Fix by tightening the `conditions` on both cards.

**Casual imprecision** — he says 1% in one video and "about a percent, sometimes half" in another. Report the range as a range, not a false constant.

**Unresolved** — he really does say both, with no reason given. Say so in the book: present both with dates, note the tension, and don't pick a side. Inventing a reconciliation is fabrication, and readers who watch the channel will catch it.

Record every resolution in `ledger/contradictions.md` so it can feed a chapter or appendix rather than being silently dropped.

## Finding the through-line

Before outlining, write a one-paragraph answer to: *what does this person actually believe, and what is the mechanism they think produces results?* Every strong how-to book has this spine. For a trading channel it might be "edge comes from a small number of repeatable liquidity events, and the real constraint is behavioural, not analytical."

Derive it from the ledger's densest `rationale` and `psychology` clusters, cite the cards that support it, and check it against the highest-repetition rules. If the spine doesn't predict the rules, it's wrong — redo it. This paragraph becomes the introduction and the organizing logic of the whole book.

## Outline patterns

Organize by the reader's learning path, not by topic frequency. Prerequisites first: a reader can't evaluate an entry trigger before understanding the market model that makes the trigger meaningful.

A dependable arc for a method-teaching channel:

1. **The worldview** — what he thinks is going on and why most people fail at it
2. **The vocabulary** — his terms, defined precisely, with the Editor's-note translations to standard terminology
3. **The framework** — the analytical model, top-down
4. **The setups** — one section per named pattern, in his own naming
5. **Execution** — triggers, entries, stops, targets
6. **Risk and sizing** — the arithmetic, exactly as he states it
7. **Trade management** — after entry: scaling, moving stops, when to bail
8. **Psychology** — the mental model, the failure modes, the fixes he prescribes
9. **Process and routine** — prep, journaling, review, the weekly loop
10. **Failure modes** — everything he warns against, consolidated
11. **Putting it together** — annotated end-to-end walkthroughs built from his own `example` cards
12. **Where the method is silent** — honest gaps

Then appendices: glossary, source index, method and coverage, evolution timeline.

## Outline format

For each chapter in `outline.md`:

```markdown
## Chapter 4 — Reading the Session Before You Trade It
**Thesis:** Preparation, not prediction — the pre-session routine defines what he'll accept all day.
**Topics:** session-timing, pre-market-prep, key-levels
**Cards:** 87 cards / 34 videos / 2021-03 → 2026-01
**Figures:** F4.1 pre-session checklist flow; F4.2 annotated level-marking schematic
**Editor's notes planned:** what a liquidity pool is; time-zone math for non-US readers
**Open questions:** does he mark weekly levels? thin coverage — check videos 118, 204
**Est. words:** 4,200
```

Get user sign-off on the outline before drafting. Restructuring after four chapters are written is expensive; restructuring an outline costs nothing.

## Sizing the book honestly

Estimate from the cards, not from a target page count. A rough working ratio: a well-supported card yields 40–80 words of finished prose. 2,000 usable cards ≈ 100,000–150,000 words. If the user wants a longer book than the cards support, the answer is more extraction (or more transcripts), never more elaboration — padding is where invented content enters.
