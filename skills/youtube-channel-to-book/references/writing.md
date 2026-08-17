# Phase 5 Reference — Writing the Chapters

The book has to be readable enough that someone finishes it and rigorous enough that someone can verify it. Those pull in opposite directions; the tier system is how you hold both.

## Chapter template

```markdown
# Chapter 4 — Reading the Session Before You Trade It

> **In this chapter:** the pre-session routine, the levels he marks and why,
> and how preparation constrains what he'll take later in the day.

## 4.1 Why he prepares at all
[Opening: the thesis, grounded in his own rationale cards]

## 4.2 The routine, step by step
[Procedure cards, in his order]

## 4.3 [Concept]
...

## What he doesn't say about this
[Honest gaps]

## Chapter summary
- [5–8 one-line takeaways, each carrying its citations]

## Try this
[A concrete exercise derived from his own instructions, not invented]
```

Keep sections to 400–900 words. Long undifferentiated stretches are where readers stop and where drift creeps in.

## Tier A — the creator's material

Everything attributed to the creator, every substantive sentence carrying `[EV-xxxx]`.

Write it as clean expository prose, not as a transcript in disguise. Your job is to say in one well-built paragraph what he said across nine videos in fragments — that synthesis *is* the product. Reproducing his phrasing at length would be worse writing and would turn a study guide into a repackaging of someone else's work.

Direct quotation is for the rare line where exact wording carries meaning that paraphrase would lose — a coined term, a memorable formulation of a rule. Keep such quotes to a handful of words, no more than one or two per chapter, always attributed and cited. If you find yourself wanting a longer quote, that's a signal to paraphrase and cite the timestamp so the reader can hear it themselves.

Citation density: roughly every claim-bearing sentence. Consecutive sentences drawing on the same card can share one citation at the end of the run. Transitions, framing, and connective tissue don't need citations, but they also must not smuggle in claims.

**Attribution verbs carry information — use them precisely.** "He states" (explicit rule), "he consistently does" (implied from demonstration), "he suggests" (soft preference), "he has said, in a 2022 video" (dated, possibly superseded). Flattening everything to "he says" erases the distinction between doctrine and offhand remark.

## Tier B — editorial explanation

Marked blocks, always anchored to a Tier A citation in the same section:

```markdown
> **Editor's note — why the 2 p.m. reversal is a real phenomenon**
> He treats the early-afternoon shift as given [EV-0412]. For readers unfamiliar
> with why it exists: US index futures see a liquidity trough over the lunch
> hours as desk activity thins, and volume returns when the bond market's
> afternoon flows begin. That's the mechanical reason behind the pattern he's
> pointing at, though he never explains it in these terms.
```

Legitimate Tier B: defining terms, supplying mechanism, working through arithmetic he leaves implicit, translating his private vocabulary into standard terminology, adding a real-world case that illustrates his point, flagging where his approach sits relative to well-known alternatives.

Not legitimate: any rule, threshold, number, setup, or market opinion that reads as his. If a reader could come away thinking the creator said it, it's a violation. When in doubt, write "he does not address this; more generally, ..." and keep the boundary loud.

Budget: Tier B should be roughly 15–25% of the book. Below that, hard chapters stay opaque. Above it, you've written your own book with his name on it.

**Include a concrete real-world example wherever a Tier B note explains a mechanism** — a named market, product, system, or event where the concept visibly played out. Abstract explanation is forgettable; correlated explanation sticks.

## Voice

Write like a good textbook author who has watched every video and respects both the creator and the reader. Present tense for the method, past tense for history. Second person for instruction ("you mark the level before the open") only where he's instructing; third person for description.

Avoid: hype the transcripts don't contain, hedging language stacked on hedging language, the word "delve", section-opening throat-clearing, and any implication that following the method produces returns. If the creator makes claims about his own results, report them as his claims with a citation — never as established fact.

Keep his hedges. If he says a setup works "most of the time, not always", the book says that. A method rendered more certain than its author intended is the most consequential form of distortion available here.

## Figures

Author figures as SVG into `book/figures/FX.Y-slug.svg`, or matplotlib for real data. Reference them as `![F4.1 — Pre-session checklist](figures/F4.1-presession.svg)` with a caption that carries its own citations.

Figure types that earn their place:

- **Decision trees** — his if/then logic made explicit. Extremely valuable; usually never drawn on the channel.
- **Process flows** — the routine as a diagram.
- **Schematic price diagrams** — an idealized, clearly-labeled illustration of a pattern he names. Label it as a schematic. Do not draw it as if it were a real historical chart with real prices.
- **Comparison tables** — setups side by side across criteria he actually specifies.
- **Timelines** — how a view changed over the years.
- **Numeric charts** — only when plotting numbers he actually stated.

**Never** fabricate an equity curve, a win-rate chart, a backtest, or a "typical" price series. **Never** embed video frames, thumbnails, or channel artwork. If a real chart is genuinely needed, tell the reader which instrument and date to pull up — that's more useful than a drawing anyway.

Keep SVGs self-contained: no external fonts, no scripts, explicit `viewBox`, readable at 600px wide, and legible in grayscale for print.

## Ending a chapter

The summary bullets should be usable as a standalone revision sheet — a reader who reads only the summaries should still get the system, in order. Each bullet carries its citations.

"Try this" exercises must derive from his own instructions ("he suggests marking the prior day's high and low on twenty charts before trading them" [EV-0233]). An invented exercise is invented content, however harmless it feels.

## Before you call a chapter done

- Every claim-bearing sentence in Tier A cites a card
- Every Tier B block is marked and anchored
- Conditions survived: no rule stated more broadly than he stated it
- Hedges survived
- Nothing quoted at length; quotes short, rare, attributed
- Figures contain only stated numbers
- The "what he doesn't say" section is filled in honestly
- Cards cited span multiple videos — a chapter leaning on one video is an under-researched chapter
