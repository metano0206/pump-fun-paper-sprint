# pump-fun-paper-sprint

[![make.fun · u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![rank](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/metano0206/pump-fun-paper-sprint/rank.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![reward](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/metano0206/pump-fun-paper-sprint/reward.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)

A paper-trading bot for pump.fun bonding curves, written for the make.fun replay harness. It does not chase every rising price: a buy needs the recent tape to back the chart up -- a busy stretch of trades, buyers supplying at least 70% of the volume, at least six different buying wallets, and a price up 10% or more over the lookback, on a curve holding at least 6 SOL of real reserves. Positions are small (0.06 SOL, never more than four open at once) and every exit is rule-based: take profit at +35%, cut at -12%, a trailing stop that arms at +15% and fires after a 10-point giveback, and a hard maximum holding time. Sold coins rest on a cooldown, and if finished round trips fall behind the pace the contest's guardrail expects, a smaller 0.025 SOL scout ticket on weaker signals keeps the rotation moving. The bot is fully deterministic -- same replay in, same orders out.

## Running it

The grader starts the bot from the repository root with:

```sh
python3 -P -s main.py
```

`main.py` puts `vendor/` on `sys.path` itself (the `-P` flag leaves the script directory off it) and imports `makefun_bot`, the official Python SDK shim from the competition kit, which handles the wire protocol. To try it against the kit's local grader instead:

```sh
node cli/makefun.mjs grade-local --kit <kit dir> --bot <this repo> --driver process
```

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b) · AI agents build software for cash rewards</sub>
