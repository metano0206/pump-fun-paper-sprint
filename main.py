"""Paper-trading bot for the make.fun pump.fun replay sprint.

The idea in one paragraph: a coin is only worth buying when the tape
agrees with the chart. For every mint the harness shows us we remember
the last few minutes of trades (who bought, who sold, how much) and a
trail of prices implied by the bonding-curve reserves. A full-size
entry needs all of it at once -- a busy window, buyers clearly in
control of the volume, several different wallets on the buy side, a
double-digit price rise over the lookback, and deep enough real
reserves that getting out later is realistic. Anything less is
ignored, except for a smaller "scout" ticket that keeps the required
rotation of finished round trips on schedule when it falls behind.

Positions stay deliberately small against the 10 SOL of paper capital
and never number more than four, which keeps the worst plausible
drawdown nowhere near the 50% disqualification line. Sales are pure
bookkeeping: fixed take-profit and stop-loss levels, a ceiling on how
long anything may be held, and a resting period for a mint once it has
been sold. A trailing stop guards the winners: once a holding has
marked +15% it is sold if it later falls 10 points from its best mark.
Every decision is a deterministic function of the batches received so
far -- no clock, no randomness, and candidates are always sorted
before any order is placed.
"""

import os
import sys
from collections import deque

# The grader starts the bot with -P, so the script's own directory is
# missing from sys.path; add the vendored SDK by hand.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL

# ---- ticket sizes -------------------------------------------------------
FULL_TICKET = 60_000_000       # 0.06 SOL for a full-strength signal
SCOUT_TICKET = 25_000_000      # 0.025 SOL for a rotation scout
SMALLEST_BUY = 12_000_000      # a round trip only counts from 0.01 SOL up
OPEN_LIMIT = 4                 # the harness allows 5; leave headroom
RESERVE_FLOOR = 6 * LAMPORTS   # 1% of real reserves must cover a ticket

# ---- what a full entry must show ----------------------------------------
TAPE_SPAN = 750                # slots of trade history kept per mint
GAIN_SPAN = 250                # slots over which the price rise is read
FULL_MIN_TRADES = 20
FULL_MIN_BUYERS = 6
FULL_MIN_BUY_SHARE = 0.70
FULL_MIN_GAIN = 0.10
SCOUT_MIN_TRADES = 5
SCOUT_MIN_BUY_SHARE = 0.58
SCOUT_MIN_GAIN = 0.03

# ---- selling rules --------------------------------------------------------
PROFIT_TARGET = 0.35           # bank a position at +35%
LOSS_CUT = -0.12               # abandon it at -12%
TRAIL_ARM = 0.15               # trailing stop wakes up at +15%
TRAIL_DROP = 0.10              # ...and sells 10 points below the best mark
HOLD_LIMIT = 6000              # slots; after this, rotate out regardless
REST_SLOTS = 3000              # a sold mint sits out this long

# ---- brakes -----------------------------------------------------------------
EQUITY_DIP = 0.25              # scout flow pauses 25% under the equity peak
SLOTS_PER_TRIP = 2000          # target rhythm for finished round trips

BUY_SLIP_BPS = 1200
SELL_SLIP_BPS = 2000

# ---- memory -------------------------------------------------------------------
tape = {}          # mint -> deque[(slot, bought, sol_amount, wallet)]
marks = {}         # mint -> deque[(slot, price)]
opened_at = {}     # mint -> slot where the current holding was first seen
best_ratio = {}    # mint -> best value/cost ratio marked while held
biggest_buy = {}   # mint -> largest quote we have sent for it
trips_done = set() # mints whose round trip is finished (counted only)
resting_until = {} # mint -> first slot at which it may be bought again
run_start = [None] # slot of the first batch of the run
equity_peak = [0]  # best mark-to-market equity seen so far


def spot_price(curve):
    """Price implied by a curve's virtual reserves, or None if unusable."""
    if curve is None or curve.virtual_token_reserves <= 0:
        return None
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


def absorb_batch(state, batch):
    """Record this batch's events into the tape and the price trails."""
    here = state.slot
    for event in batch["events"]:
        mint = event["mint"]
        if event["event"] == "TradeEvent":
            window = tape.setdefault(mint, deque())
            window.append(
                (here, bool(event.get("is_buy")),
                 int(event.get("sol_amount", 0)), event.get("user", ""))
            )
        price = spot_price(state.curves.get(mint))
        if price is not None:
            trail = marks.setdefault(mint, deque())
            if not trail or trail[-1][0] != here:
                trail.append((here, price))
    # Keep the windows from growing without bound on long replays.
    tape_floor = here - TAPE_SPAN
    for window in tape.values():
        while window and window[0][0] < tape_floor:
            window.popleft()
    mark_floor = here - max(TAPE_SPAN, GAIN_SPAN) - 5
    for trail in marks.values():
        while len(trail) > 1 and trail[0][0] < mark_floor:
            trail.popleft()


def tape_summary(mint):
    """(number of trades, buy share of volume, distinct buyers) on the tape."""
    window = tape.get(mint)
    if not window:
        return 0, 0.0, 0
    bought = sold = 0
    wallets = set()
    for _slot, is_buy, amount, wallet in window:
        if is_buy:
            bought += amount
            wallets.add(wallet)
        else:
            sold += amount
    both = bought + sold
    return len(window), (bought / both if both else 0.0), len(wallets)


def recent_gain(mint):
    """Price change across the lookback span, or None without the history."""
    trail = marks.get(mint)
    if not trail or len(trail) < 2:
        return None
    last_slot, last_price = trail[-1]
    wanted = last_slot - GAIN_SPAN
    base = None
    for slot, price in trail:
        if slot > wanted:
            break
        base = price
    if not base:
        return None
    return last_price / base - 1.0


def mints_with_pending(state, side):
    found = set()
    for pending in state.pending.values():
        if pending.order["side"] == side:
            found.add(pending.order["mint"])
    return found


def marked_equity(state):
    """Cash plus holdings priced at the newest curve marks."""
    total = state.cash
    for mint, holding in state.positions.items():
        price = spot_price(state.curves.get(mint))
        if price is not None:
            total += int(holding.token_amount * price)
    return total


def choose_sales(state, orders):
    """Apply the fixed selling rules to everything held; returns sold mints."""
    already = mints_with_pending(state, "sell")
    sold = []
    for mint in sorted(state.positions):
        if mint in already:
            continue
        holding = state.positions[mint]
        curve = state.curves.get(mint)
        opened_at.setdefault(mint, state.slot)
        if curve is not None and curve.complete:
            continue  # the harness settles finished curves itself
        price = spot_price(curve)
        if price is None or holding.cost_lamports <= 0:
            continue
        ratio = holding.token_amount * price / holding.cost_lamports
        gain = ratio - 1.0
        best = best_ratio.get(mint, ratio)
        if ratio > best:
            best = best_ratio[mint] = ratio
        age = state.slot - opened_at[mint]
        trailed = best - 1.0 >= TRAIL_ARM and ratio <= best - TRAIL_DROP
        if gain >= PROFIT_TARGET or gain <= LOSS_CUT or trailed or age >= HOLD_LIMIT:
            orders.append({
                "side": "sell",
                "mint": mint,
                "token_amount": holding.token_amount,
                "max_slippage_bps": SELL_SLIP_BPS,
            })
            sold.append(mint)
    return sold


def choose_buys(state, orders, being_sold):
    caps = state.hello["caps"]
    fee = state.tx_fee_lamports()
    spendable = state.free_cash_lamports()
    busy = set(state.positions) | mints_with_pending(state, "buy")
    room = OPEN_LIMIT - len(busy)
    if room <= 0:
        return
    blocked = busy | set(being_sold) | mints_with_pending(state, "sell")

    # Rotation pacing: one finished trip per SLOTS_PER_TRIP slots.
    elapsed = max(0, state.slot - (run_start[0] or state.slot))
    trips_due = elapsed // SLOTS_PER_TRIP
    lagging = len(trips_done) + 2 < trips_due

    # Equity brake: while the account marks well under its best level,
    # the weaker scout flow is switched off (full signals still trade).
    equity = marked_equity(state)
    if equity > equity_peak[0]:
        equity_peak[0] = equity
    braking = equity_peak[0] > 0 and equity < equity_peak[0] * (1.0 - EQUITY_DIP)

    scored = []
    for mint in sorted(state.curves):
        if mint in blocked or state.slot < resting_until.get(mint, -1):
            continue
        curve = state.curves[mint]
        if curve.complete or curve.real_quote_reserves < RESERVE_FLOOR:
            continue
        trades_n, share, buyers = tape_summary(mint)
        gain = recent_gain(mint)
        if gain is None:
            continue
        full = (
            trades_n >= FULL_MIN_TRADES and share >= FULL_MIN_BUY_SHARE
            and buyers >= FULL_MIN_BUYERS and gain >= FULL_MIN_GAIN
        )
        scout = (
            lagging and not braking
            and trades_n >= SCOUT_MIN_TRADES and share >= SCOUT_MIN_BUY_SHARE
            and gain >= SCOUT_MIN_GAIN
        )
        if full:
            scored.append((0, -gain, -share, mint))
        elif scout:
            scored.append((1, -gain, -share, mint))
    scored.sort()
    for kind, _neg_gain, _neg_share, mint in scored:
        if room <= 0 or len(orders) >= caps["max_orders_per_batch"]:
            break
        ticket = FULL_TICKET if kind == 0 else SCOUT_TICKET
        amount = min(ticket, state.max_buy_lamports(mint))
        if amount < SMALLEST_BUY or spendable < amount + fee:
            continue
        orders.append({
            "side": "buy",
            "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIP_BPS,
        })
        biggest_buy[mint] = max(biggest_buy.get(mint, 0), amount)
        spendable -= amount + fee
        room -= 1


def settle_departed(state, held_before):
    """Holdings that vanished since the last batch close a round trip."""
    for mint in held_before:
        if mint in state.positions:
            continue
        if biggest_buy.get(mint, 0) >= SMALLEST_BUY:
            trips_done.add(mint)
        resting_until[mint] = state.slot + REST_SLOTS
        opened_at.pop(mint, None)
        best_ratio.pop(mint, None)


_held_before = [set()]


def on_batch(state, batch):
    if run_start[0] is None:
        run_start[0] = state.slot
    settle_departed(state, _held_before[0])
    absorb_batch(state, batch)
    orders = []
    selling = choose_sales(state, orders)
    choose_buys(state, orders, selling)
    _held_before[0] = set(state.positions)
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
