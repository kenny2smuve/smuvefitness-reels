# Experiments (pre-registered; at most 2 running at once: X1 for slot A, X2 for slot B first)

Rules: a result never becomes a rule on its own. Rule changes go through `rules-keeper` and RULES.md §6.
Decision rule for every experiment: compare the arms on **median views at 72h** and **shares per 1K**
(not the hit rate: at 2 posts a day and a 7-25% base rate, n=6 per arm cannot tell hit rates apart).
Call an arm the winner only when its median beats the other by 1.5x or more AND it has at least as
many 5K+ hits. Otherwise extend by 6 more per arm, once; then close as "no difference".

| id | status | slot | hypothesis | arms | metric | n per arm | started | decision |
|----|--------|------|------------|------|--------|-----------|---------|----------|
| X1 | RUNNING | A | A morning post (06:30-08:30 ET, alone) reaches more than 11:00-12:30 alone. The three Oct outliers went up alone in daytime (06:08, 10:16, 11:06). | morning (delivered the day before, "post tomorrow ~07:00, alone") vs late-morning | median views 72h, shares/1K | 6 | | |
| X2 | RUNNING | B | B-roll with hour labels beats unlabelled. Labelled montages did 2,477 and 1,470 vs 1,144 and 498. | labels (from pieces.csv only) vs none | median views 72h, saves/1K | 6 | | |
| X3 | QUEUED | A | A story ending that teases the healed version ("healed version next") lifts follows/day vs a plain ending. | tease vs plain | follows/day (estimate), saves/1K | 6 | | |
| X4 | QUEUED | B | Talking head shot on the street beats green screen. | street vs green screen | median views 72h, avg watch | 6 | | |
| X5 | QUEUED | any | Sharing a trial to the grid at ~3h when it runs at 1.5x median pace lifts 72h views vs not sharing. | share vs hold | views 72h, reach | 6 | | |
| X6 | QUEUED | A | Inviting the client as an IG Collab on story reels lifts reach vs no Collab (Meta: Collab amplification). | collab vs none | median views 72h, follows/day | 6 | | |

## Log
(date, experiment, reel id, arm, result at 24h/72h, note)
