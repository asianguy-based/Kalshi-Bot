# Wanax — Brand Reference

*Above All Orders.*

Wanax (Mycenaean Greek, Linear B *wa-na-ka*) was the highest ruler in Bronze
Age palace society — above all other elites. Not a warrior-king: a
**bureaucratic sovereign**, whose power ran through records, allocation and
administration rather than force. Pronounced /ˈwɑː.nɒks/ (WAH-noks).

The archetype is **The Administrator**. The bot does not predict the market,
it administers a position: the edge is allocated, the position is taken, the
record is kept.

## Palette

| Role      | Name       | Hex       | Use                              |
| --------- | ---------- | --------- | -------------------------------- |
| Primary   | Deep Black | `#0D0D0D` | Backgrounds                      |
| Accent    | Bronze     | `#8B6914` | Logo, highlights, CTA            |
| Surface   | Bone       | `#E8E4DC` | Text on dark, light cards        |
| Secondary | Ash        | `#6B6B6B` | Secondary text, borders          |
| Success   | Olive      | `#4A5D23` | Gains, confirmations             |
| Danger    | Rust       | `#8B3A2F` | Losses, alerts                   |

Mood: archaeological — a tablet pulled from a palace archive.
**No gradients. No blues. No "tech" colours.**

## Type

| Element  | Font          | Notes                                        |
| -------- | ------------- | -------------------------------------------- |
| Wordmark | Cinzel        | All caps, 0.4em tracking. Carved in stone.   |
| Headings | Space Grotesk | Medium, tight tracking                       |
| Body     | Inter         | 16px / 1.6                                   |
| Data     | IBM Plex Mono | Every number, tabular-nums, so ledgers align |

Cinzel is for the wordmark only — never body text.

## Logo

A Linear B tablet glyph: a square containing an X, on a stem, feeding a
smaller square.

- The X in the top square is the **decision node**.
- The stem is the **chain of command** flowing down.
- The lower square is the **record**.
- **No curves. Right angles and diagonals only.** Rigid. Bureaucratic.

Implemented in pure CSS at `web/static/wanax.css` (`.wx-glyph`), so it scales
to any size as a single-colour mark with no asset pipeline. Bronze on black
is the primary lockup; bone on black is the alternate.

## Voice

Cold. Administrative. The name of a function, not a person. State what was
done and what is true; never sell, never pad. Digest messages sign off
"The record is kept."

## Domain

**wanax.app** — in use, live at https://bot.wanax.app.
The brand deck originally favoured `wanax.trade`; the owner chose to keep
`wanax.app`, which was already registered and serving the deployment.

## Trademark note

Wanax Ltd (Cyprus) holds a mark in advertising/business services — a
different class from finance and trading, so no direct conflict. No
finance or trading entity uses the name.

## Applying it

`web/static/wanax.css` is the single source of truth. Use the tokens
(`--wx-bronze`, `--wx-font-data`, ...) rather than re-typing hex codes, and
use the primitives (`.wx-card`, `.wx-btn`, `.wx-wordmark`, `.wx-glyph`)
rather than restyling per page.
