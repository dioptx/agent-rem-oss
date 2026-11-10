# REM's voice: the night-shift buddy

REM is a sleepy pal who worked the night shift on your agent sessions and tells you, over
coffee, what kept bugging you. Every word on the rem serve page, and every sentence the
server writes for it, sounds like this one character.

## Rules

1. **Short.** One idea per line. Most lines under 12 words.
2. **Plain.** Words a non-engineer gets on first read. No internals: never "pattern",
   "verdict", "signature", "drag", "lift", "surfaced", "backfill", "proposal", "tier".
3. **One wink per block, max.** The joke is a seasoning. If a line is already clear and warm,
   leave it.
4. **Numbers stay exact.** Never round a count away, never trade a number for a vibe.
   "80% of sessions hit a snag", not "most sessions were rough".
5. **Never overclaim.** If REM can't tell yet, it says so. Cheerful, never salesy.
6. **First person for REM, second person for you.** "I slept on 42 sessions." "5 snags are
   waiting on you."
7. **Sentence case, contractions welcome.** No all caps, no em dashes, no exclamation pile-ups
   (one "!" per page at most), no emoji (the GIFs bring the fun).
8. **Errors say what happened and what to do.** "Couldn't save: the server didn't answer.
   Try again in a sec." No apologies, no vagueness.

## Words

| Thing | Say | Not |
|---|---|---|
| a recurring friction | snag | pattern, friction pattern |
| a recurring good habit | win | lift, positive pattern |
| snags and wins together | things | patterns |
| a proposal | fix idea | proposal, T1 |
| the recommended one | my pick | recommended tier |
| accept (friction) | Fix it | Accept |
| accept (win) | Keep it | Accept |
| drop | Nah | Drop, dismiss |
| snooze a week | Later | Snooze 7d |
| mute | Hush for good | Mute |
| a decision | your call | verdict |
| the morning briefing | morning note | briefing, journal |
| the nightly run | night shift | consolidation run |
| came up in the briefing | came up | surfaced |
| waiting for a decision | waiting on you | carrying, open |
| settings | how I work | configuration |
| occurrences of a snag | came up N times | events, occurrences |
| the stamp while dragging | Fix it / Keep it / Nah / Later | FIX, DROP, SNOOZE |

The shift runs on your schedule, so "tonight" is wrong for anything that waits on it: say "on my next shift".

**Chart gaps:** REM not running → "I was off"; not tracked yet → "wasn't watching yet";
not judged yet → "waiting on my next shift"; transcripts pruned → "those sessions are gone".

**Night shift:** ok → "slept fine"; timed out → "ran out of time"; failed → "crashed";
stuck (exited clean but its run never finished, or still running past an hour) → "got stuck";
running → "on shift now" (never for a stuck run); paused → "on a break". Shifts that went wrong
since the last good one, together → "didn't finish".

**Fixes you said yes to:** an accepted fix with a linked edit → "made"; without → "still to make";
the copy button → "Copy for Claude".

**Modes:** Look back → "How far back I read"; Briefing size → "How long my morning note is";
Pickiness → "How many repeats before I speak up"; Nagging → "How loud I get when you ignore
something"; Focus → "What I care about most".

## Lines, before and after

| Before | After |
|---|---|
| 5 patterns in the newest briefing waiting on you | 5 things waiting on you |
| sessions with friction, latest full week | sessions hit a snag last week |
| nightly runs ok, last 14 | 11 of 14 nights went fine |
| Nothing surfaced in the newest briefing. | All caught up. Go touch grass. |
| Saved: applies from the next nightly run. | Got it. Kicks in on my next shift. |
| Not saved: server error | Couldn't save: the server didn't answer. Try again in a sec. |

## Where the words live

- `rem/static/index.html`: every label, button, empty state, tooltip and "how this is counted"
  note. API values (fix labels, gap codes, run statuses) stay stable; the page maps them to
  these words.
- `rem/stats.py` `headline()`: the one sentence the server writes.
- `rem/settings.py` `MODES`: group labels, help, option names.
