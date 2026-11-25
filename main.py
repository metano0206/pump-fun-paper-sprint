"""Momentum + volume-confirmation paper-trading bot for pump.fun replays.

Strategy (written for the make.fun pumpfun-replay/v2 harness):

- Watch every bonding curve the harness shows us and keep, per mint, a
  rolling window of recent trades (slot, direction, size, trader) plus a
  short price history taken from the curve reserves.
- Enter only when several signs agree inside the window: enough trades,
  buy volume clearly dominating sell volume, several distinct buyers, the
  price up over the lookback, and enough real reserves that the position
  can be exited again. This is the "momentum with volume confirmation"
  pattern: a rising price alone is not trusted.
- Keep positions small and few (at most 4 open, about 0.06 SOL each
  against 10 SOL of paper capital), so the worst realistic drawdown
  stays far below the harness's 50% guardrail.
- Exit on rules, not hope: take profit at +35%, stop at -18%, a trailing
  stop once a position has been up 15% (sell if it gives back 12 points),
  and a hard maximum holding time. Exits are decided before entries on
  every batch.
- Rotate: after selling a mint it goes on cooldown, and if completed
  round trips fall behind the pace the guardrail needs (20 distinct
  mints), a smaller "scout" stake is allowed on weaker signals so the
  rotation keeps moving.

Everything is a deterministic function of the batches seen so far: no
randomness, no wall clock, no iteration over unordered sets when making
decisions (candidates are always sorted first).
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- sizing -------------------------------------------------------------
STAKE_LAMPORTS = 60_000_000          # 0.06 SOL per full position
SCOUT_STAKE_LAMPORTS = 25_000_000    # 0.025 SOL per scout position
MIN_BUY_LAMPORTS = 12_000_000        # never open dust (guardrail wants >= 0.01 SOL trips)
MAX_POSITIONS = 4                    # harness cap is 5; keep one slot of headroom
MIN_REAL_QUOTE = 6 * SOL             # need >= 6 SOL real reserves so the 1% cap fits a stake

# --- entry signal ---------------------------------------------------------
WINDOW_SLOTS = 750                   # rolling trade window (~5 minutes of slots)
LOOKBACK_SLOTS = 250                 # price momentum lookback (~100 seconds)
MIN_TRADES = 20
MIN_UNIQUE_BUYERS = 6
MIN_BUY_SHARE = 0.70                 # buy volume / total volume inside the window
MIN_PRICE_GAIN = 0.10                # +10% over the lookback
SCOUT_MIN_TRADES = 5
SCOUT_MIN_BUY_SHARE = 0.58
SCOUT_MIN_PRICE_GAIN = 0.03

# --- exits ----------------------------------------------------------------
TAKE_PROFIT = 0.35
STOP_LOSS = -0.12
TRAIL_ARM = 0.15                     # trailing stop arms at +15%
TRAIL_GIVEBACK = 0.10                # ...and fires after giving back 10 points
MAX_HOLD_SLOTS = 6000                # ~40 minutes, then rotate out
COOLDOWN_SLOTS = 3000                # a sold mint rests before it can be re-entered

# --- risk -----------------------------------------------------------------
DRAWDOWN_PAUSE = 0.25                # stop opening if equity is 25% under its peak
PACE_SLOTS_PER_TRIP = 2000           # want one finished round trip per this many slots

BUY_SLIPPAGE_BPS = 1200
SELL_SLIPPAGE_BPS = 2000

# --- rolling per-mint state -------------------------------------------------
trades = {}        # mint -> deque of (slot, is_buy, sol_amount, user)
price_hist = {}    # mint -> deque of (slot, price as float)
first_slot = None  # slot of the first batch seen (for pacing)
peak_value = {}    # mint -> best gross value ratio seen while held (for trailing)
entry_slot = {}    # mint -> slot the position was first seen held
bought_size = {}   # mint -> largest buy quote_lamports we sent for it
tripped = set()    # mints with a completed round trip (only counted, never iterated)
cooldown_until = {}  # mint -> slot until which re-entry is blocked


def price_of(curve):
    if curve is None or curve.virtual_token_reserves <= 0:
        return None
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


def note_events(state, batch):
    """Fold this batch's raw events into the rolling windows."""
    slot = state.slot
    for e in batch["events"]:
        mint = e["mint"]
        if e["event"] == "TradeEvent":
            dq = trades.get(mint)
            if dq is None:
                dq = trades[mint] = deque()
            dq.append((slot, bool(e.get("is_buy")), int(e.get("sol_amount", 0)), e.get("user", "")))
        curve = state.curves.get(mint)
        p = price_of(curve)
        if p is not None:
            dq = price_hist.get(mint)
            if dq is None:
                dq = price_hist[mint] = deque()
            if not dq or dq[-1][0] != slot:
                dq.append((slot, p))
    # Trim windows so memory stays bounded over long replays.
    cutoff = slot - WINDOW_SLOTS
    for dq in trades.values():
        while dq and dq[0][0] < cutoff:
            dq.popleft()
    pcut = slot - max(WINDOW_SLOTS, LOOKBACK_SLOTS) - 5
    for dq in price_hist.values():
        while len(dq) > 1 and dq[0][0] < pcut:
            dq.popleft()


def window_stats(mint):
    """(trade count, buy share, unique buyers) inside the rolling window."""
    dq = trades.get(mint)
    if not dq:
        return 0, 0.0, 0
    buy_vol = 0
    sell_vol = 0
    buyers = set()
    for _slot, is_buy, amount, user in dq:
        if is_buy:
            buy_vol += amount
            buyers.add(user)
        else:
            sell_vol += amount
    total = buy_vol + sell_vol
    share = (buy_vol / total) if total > 0 else 0.0
    return len(dq), share, len(buyers)


def price_gain(mint):
    """Price change over the lookback, or None when there is not enough history."""
    dq = price_hist.get(mint)
    if not dq or len(dq) < 2:
        return None
    now_slot, now_price = dq[-1]
    target = now_slot - LOOKBACK_SLOTS
    old_price = None
    for slot, price in dq:
        if slot <= target:
            old_price = price
        else:
            break
    if old_price is None or old_price <= 0:
        return None
    return now_price / old_price - 1.0


def pending_mints(state, side):
    out = set()
    for p in state.pending.values():
        if p.order["side"] == side:
            out.add(p.order["mint"])
    return out


def equity_marks(state):
    """Cash plus open positions valued at the latest curve price."""
    total = state.cash
    for mint, h in state.positions.items():
        p = price_of(state.curves.get(mint))
        if p is not None:
            total += int(h.token_amount * p)
    return total


peak_equity = [0]


def plan_exits(state, orders):
    """Rule-based exits for everything currently held. Returns mints being sold."""
    selling = []
    pending_sells = pending_mints(state, "sell")
    for mint in sorted(state.positions):
        if mint in pending_sells:
            continue
        h = state.positions[mint]
        curve = state.curves.get(mint)
        if mint not in entry_slot:
            entry_slot[mint] = state.slot
        if curve is not None and curve.complete:
            continue  # the replay closes completed curves itself
        p = price_of(curve)
        if p is None or h.cost_lamports <= 0:
            continue
        value = h.token_amount * p
        ratio = value / h.cost_lamports
        best = peak_value.get(mint, ratio)
        if ratio > best:
            best = peak_value[mint] = ratio
        held_for = state.slot - entry_slot[mint]
        reason = None
        if ratio - 1.0 >= TAKE_PROFIT:
            reason = "take profit"
        elif ratio - 1.0 <= STOP_LOSS:
            reason = "stop loss"
        elif best - 1.0 >= TRAIL_ARM and ratio <= best - TRAIL_GIVEBACK:
            reason = "trailing stop"
        elif held_for >= MAX_HOLD_SLOTS:
            reason = "time rotation"
        if reason is None:
            continue
        orders.append({
            "side": "sell",
            "mint": mint,
            "token_amount": h.token_amount,
            "max_slippage_bps": SELL_SLIPPAGE_BPS,
        })
        selling.append(mint)
    return selling


def plan_entries(state, orders, selling):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports()
    held = set(state.positions) | pending_mints(state, "buy") | set(selling)
    open_slots = MAX_POSITIONS - len(held & (set(state.positions) | pending_mints(state, "buy")))
    if open_slots <= 0:
        return
    # Pacing: how many round trips should be done by now?
    elapsed = max(0, state.slot - (first_slot or state.slot))
    trips_wanted = elapsed // PACE_SLOTS_PER_TRIP
    behind_pace = len(tripped) + 2 < trips_wanted
    # Drawdown guard: pause new entries while equity is well under its peak.
    eq = equity_marks(state)
    if eq > peak_equity[0]:
        peak_equity[0] = eq
    paused = peak_equity[0] > 0 and eq < peak_equity[0] * (1.0 - DRAWDOWN_PAUSE)

    candidates = []
    for mint in sorted(state.curves):
        if mint in held or mint in pending_mints(state, "sell"):
            continue
        if state.slot < cooldown_until.get(mint, -1):
            continue
        curve = state.curves[mint]
        if curve.complete or curve.real_quote_reserves < MIN_REAL_QUOTE:
            continue
        count, share, buyers = window_stats(mint)
        gain = price_gain(mint)
        if gain is None:
            continue
        strong = (
            count >= MIN_TRADES and share >= MIN_BUY_SHARE
            and buyers >= MIN_UNIQUE_BUYERS and gain >= MIN_PRICE_GAIN
        )
        scout = (
            behind_pace and count >= SCOUT_MIN_TRADES
            and share >= SCOUT_MIN_BUY_SHARE and gain >= SCOUT_MIN_PRICE_GAIN
        )
        if strong:
            candidates.append((0, -gain, -share, mint))
        elif scout and not paused:
            candidates.append((1, -gain, -share, mint))
    candidates.sort()
    for _kind, _ng, _ns, mint in candidates:
        if open_slots <= 0 or len(orders) >= caps["max_orders_per_batch"]:
            break
        stake = STAKE_LAMPORTS if _kind == 0 else SCOUT_STAKE_LAMPORTS
        cap = state.max_buy_lamports(mint)
        amount = min(stake, cap)
        if amount < MIN_BUY_LAMPORTS or free < amount + tx:
            continue
        orders.append({
            "side": "buy",
            "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIPPAGE_BPS,
        })
        bought_size[mint] = max(bought_size.get(mint, 0), amount)
        free -= amount + tx
        open_slots -= 1


def note_departed_positions(state, before):
    """Mints held last batch and gone now finished a round trip somehow."""
    for mint in before:
        if mint not in state.positions:
            if bought_size.get(mint, 0) >= MIN_BUY_LAMPORTS:
                tripped.add(mint)
            cooldown_until[mint] = state.slot + COOLDOWN_SLOTS
            peak_value.pop(mint, None)
            entry_slot.pop(mint, None)


_prev_positions = [set()]


def on_batch(state, batch):
    global first_slot
    if first_slot is None:
        first_slot = state.slot
    note_departed_positions(state, _prev_positions[0])
    note_events(state, batch)
    orders = []
    selling = plan_exits(state, orders)
    plan_entries(state, orders, selling)
    _prev_positions[0] = set(state.positions)
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
