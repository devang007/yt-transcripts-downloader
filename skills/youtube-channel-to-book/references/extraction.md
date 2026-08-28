# Phase 2 Reference — Evidence Extraction

Extraction converts one transcript into a set of atomic, timestamped, verifiable claims. Everything downstream is built from these cards, so a claim that isn't extracted cannot appear in the book, and a claim extracted sloppily will propagate its sloppiness into print.

## Card schema

One JSON object per line in `cards/<video_id>.jsonl`:

```json
{
  "id": "EV-dQw4w9WgXcQ-07",
  "video_id": "dQw4w9WgXcQ",
  "video_title": "Why I Stopped Trading the First 15 Minutes",
  "published": "2023-04-11",
  "timestamp": "00:12:34",
  "seconds": 754,
  "url": "https://youtu.be/dQw4w9WgXcQ?t=754",
  "type": "rule",
  "topics": ["session-timing", "risk-management"],
  "claim": "He does not take entries during the first 15 minutes of the New York open because the initial range is still forming.",
  "anchor": "I just don't touch the first fifteen",
  "conditions": "New York session only; he says the London open is different",
  "stated": "explicit",
  "strength": "strong",
  "links": []
}
```

Field notes:

- **id** — `EV-<video_id>-<nn>`, where `<nn>` is a two-digit counter within that video (`EV-dQw4w9WgXcQ-07`). This is unique **by construction**, so parallel extraction agents never need coordinating, never need pre-allocated number blocks, and an interrupted run can be resumed by simply re-running the videos that have no card file. Do not use a project-wide sequential counter: it forces every restart to re-derive a high-water mark, which is pure overhead and the usual source of collisions. Never renumber after chapters cite them.
- **claim** — one sentence, one idea, in your words. If you need "and" to join two rules, make two cards.
- **anchor** — a short verbatim fragment, roughly a dozen words at most, purely so a human can locate the moment. It is a locator, not a quotation for reuse. Never store a paragraph here. For Devanagari or any non-Latin script, copy the anchor by slicing it directly from the transcript file rather than retyping it — nukta letters (ड़, ज़) can be represented as one precomposed codepoint or as a base letter plus a combining mark, visually identical but byte-different, and retyping silently picks the wrong one. `cards.py anchors` normalizes to NFC before comparing, but a hand-typed anchor can still fail the check for a reason that has nothing to do with fidelity.
- **conditions** — the scope. Most trading advice is conditional and most summaries destroy the conditions. This field is the difference between a useful book and a dangerous one.
- **stated** — `explicit` when he says it, `implied` when you inferred it from a demonstration or an aside. Implied cards can be used in the book but must be phrased with visible hedging ("in the walkthroughs he consistently does X, though he never states it as a rule").
- **strength** — `strong` (he presents it as a rule/principle), `moderate` (a preference or habit), `weak` (an offhand remark). Feeds chapter weighting.
- **links** — IDs of related cards, especially the counterpart when you spot a contradiction.

## Type taxonomy

| Type | What it captures | Example claim |
|---|---|---|
| `rule` | A do/don't he states as policy | "Never adds to a losing position." |
| `procedure` | An ordered process or checklist step | "Marks the daily high and low before the session opens." |
| `rationale` | The *why* behind a rule | "Says the first 15 minutes is where liquidity gets taken, not where direction shows." |
| `definition` | A term he uses in a specific way | "Uses 'displacement' to mean a large impulsive candle that breaks structure." |
| `example` | A concrete walkthrough or trade recap | "Walks through a EURUSD short taken after a sweep of the Asian high." |
| `number` | Any figure he states | "Risks 0.5% per trade." |
| `psychology` | Mindset, emotion, discipline, identity | "Says the urge to re-enter immediately after a loss is the main account killer." |
| `caveat` | Limits, exceptions, warnings | "Says this setup fails in low-volatility summer conditions." |
| `opinion` | A view on markets/tools/others, not a rule | "Thinks indicators are mostly redundant with price structure." |
| `anecdote` | Personal story used to teach | "Describes blowing an account early on by revenge trading." |
| `contradiction` | Explicit revision of a past position | "Says he no longer uses the approach he taught in earlier videos." |

## Extraction method per video

1. Read the full transcript. Note the timestamps as you go — they're your citation anchors.
2. Ask what a careful student would write in their notebook, then write those as cards. Aim for density proportional to substance: a tight 12-minute lesson might yield 15–25 cards; a 3-hour livestream might yield 40 and hours of noise.
3. Skip: sponsor reads, greetings, sign-offs, giveaway mechanics, chat banter, repeated intros. But *don't* skip the throwaway psychology lines buried in livestream chatter — they're often the most honest material on the channel.
4. When he demonstrates rather than states, extract the demonstrated behavior as `implied`.
5. When he says something that conflicts with an earlier card you've written, create the card anyway and populate `links`. Never harmonize at extraction time.

## Common extraction errors

**Upgrading to the textbook version.** He says "wait for it to come back to the level." You know the canonical formulation involves a retest with a confirming close. Writing that down invents precision he didn't give. Extract what was said; if the ambiguity matters, the book can note it's ambiguous.

**Dropping conditions.** "Risk 1%" and "risk 1% on A-setups, half that on B-setups" are different systems.

**Merging distinct ideas.** A card that reads "He waits for structure to break and then enters on the retest with a stop below the swing low" is three cards. Merged cards can't be cited precisely and can't be counted for weighting.

**Extracting the interviewer/guest as the creator.** In collab videos, tag the speaker. If the transcript makes speakers indistinguishable, mark those cards `stated: "implied"` with a note, or skip the video.

**Missing the meta-content.** How he reviews trades, journals, sizes up after a good week, what he does after three losses — this is the material readers value most and it's rarely in the video title.

## Progress tracking

Maintain `cards/_progress.json`:

```json
{"processed": ["vid1", "vid2"], "skipped": {"vid7": "sponsor-only short"}, "next_card_id": 143}
```

Update after each video so an interrupted run resumes cleanly instead of duplicating IDs.
