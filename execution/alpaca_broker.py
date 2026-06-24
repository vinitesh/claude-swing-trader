"""Alpaca broker implementation (paper or live)."""

from __future__ import annotations

from datetime import datetime, timezone


def _as_naive_utc(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-naive UTC for safe comparison.

    Alpaca returns tz-AWARE UTC datetimes; our DB stores tz-NAIVE UTC
    (datetime.utcnow()). Comparing the two directly raises TypeError. We
    normalize both sides to naive-UTC before any comparison.
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from core.signal import Position, Signal
from execution.broker_base import Account, Broker


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.client = TradingClient(api_key, secret_key, paper=paper)
        self.paper = paper

    def get_account(self) -> Account:
        a = self.client.get_account()
        return Account(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
        )

    def get_positions(self) -> list[Position]:
        positions = self.client.get_all_positions()
        out: list[Position] = []
        for p in positions:
            out.append(
                Position(
                    symbol=p.symbol,
                    qty=int(float(p.qty)),
                    avg_entry_price=float(p.avg_entry_price),
                    side="long" if float(p.qty) > 0 else "short",
                    strategy_name=p.symbol,  # broker doesn't know strategy; engine should map
                    opened_at=datetime.utcnow(),
                )
            )
        return out

    def get_position_marks(self) -> dict[str, dict[str, float]]:
        """Live mark-to-market per held symbol, straight from Alpaca.

        Returns {symbol: {current_price, market_value, unrealized_pl,
        unrealized_plpc}}. Alpaca computes these authoritatively, so the
        dashboard shows the broker's own numbers rather than recomputing.
        """
        out: dict[str, dict[str, float]] = {}
        for p in self.client.get_all_positions():
            try:
                out[p.symbol] = {
                    "current_price": float(p.current_price),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                }
            except (TypeError, ValueError):
                continue
        return out

    def submit_bracket_order(self, signal: Signal, qty: int) -> str:
        if qty <= 0:
            raise ValueError(f"Invalid qty {qty} for {signal.symbol}")

        side = OrderSide.BUY if signal.action.value == "BUY" else OrderSide.SELL

        order_req = MarketOrderRequest(
            symbol=signal.symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class="bracket",
            stop_loss=StopLossRequest(stop_price=round(signal.stop_loss, 2)),
            take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 2)),
        )
        order = self.client.submit_order(order_req)
        return str(order.id)

    def close_position(self, symbol: str) -> None:
        self.client.close_position(symbol)

    def get_open_order_symbols(self) -> set[str]:
        """Symbols that currently have a working (not-yet-terminal) order.

        Includes pending entry orders that haven't filled AND resting bracket
        stop-loss / take-profit legs on held positions. `sync` uses this so it
        does NOT prematurely close a DB position whose entry order is still
        queued (e.g. an after-close run whose market order fills next open).
        """
        # get_orders does NOT auto-paginate (default 50, max 500 per call). With
        # bracket orders each leaving resting SL+TP legs, a large account can
        # have >50 working orders — truncation would silently drop symbols from
        # the "alive" set and re-trigger the false-close bug. So we page on
        # created_at until a short page comes back.
        symbols: set[str] = set()
        until: datetime | None = None
        PAGE = 500
        for _ in range(50):  # hard cap: 50 * 500 = 25k orders, far beyond reality
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN, nested=True,
                limit=PAGE, direction="desc", until=until,
            )
            orders = self.client.get_orders(filter=req)
            if not orders:
                break
            for o in orders:
                if getattr(o, "symbol", None):
                    symbols.add(o.symbol)
                # nested=True attaches bracket child legs under `.legs`
                for leg in (getattr(o, "legs", None) or []):
                    if getattr(leg, "symbol", None):
                        symbols.add(leg.symbol)
            if len(orders) < PAGE:
                break
            # Next page: orders strictly older than the oldest in this page.
            until = min((o.created_at for o in orders if o.created_at), default=None)
            if until is None:
                break
        return symbols

    def get_last_exit_fill(
        self, symbol: str, opened_after: datetime | None = None
    ) -> tuple[float, datetime] | None:
        """Return (fill_price, filled_at) of the FILLED sell that exited this
        position, or None if no matching filled exit is found.

        ``opened_after`` (the position's opened_at) scopes the search so we
        don't stamp a *prior* trade's exit price onto a re-entered symbol — we
        only consider sells filled at/after the position was opened. Returns
        None when the symbol left the broker without a filled sell (e.g. an
        entry order that was canceled/expired and never opened a real
        position); the caller treats that distinctly.
        """
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            side=OrderSide.SELL,
            limit=500,
            direction="desc",
        )
        orders = self.client.get_orders(filter=req)
        for o in orders:
            filled_qty = getattr(o, "filled_qty", None)
            filled_px = getattr(o, "filled_avg_price", None)
            if not filled_qty or float(filled_qty) <= 0:
                continue
            if filled_px is None or float(filled_px) <= 0:
                continue
            # Normalize BOTH sides to naive-UTC: Alpaca's filled_at is tz-aware,
            # our DB opened_at is tz-naive. Comparing them raw raises TypeError,
            # which previously got swallowed → real fills discarded as $0.
            filled_at = _as_naive_utc(o.filled_at or o.updated_at)
            cutoff = _as_naive_utc(opened_after)
            # Only count an exit that happened after this position opened — a
            # stale sell from a prior trade on the same symbol is not our exit.
            if cutoff is not None and filled_at is not None and filled_at < cutoff:
                continue
            return float(filled_px), (filled_at or datetime.utcnow())
        return None

    def find_exit_fills_in_window(
        self, symbol: str, start: datetime, end: datetime | None = None,
        qty: int | None = None,
    ) -> list[tuple[float, datetime, int]]:
        """All FILLED sells for ``symbol`` within [start, end] (naive-UTC).

        Returns a list of (fill_price, filled_at, filled_qty), newest first. If
        ``qty`` is given, only sells whose filled_qty matches are returned —
        this disambiguates a symbol traded multiple times. The backfill caller
        inspects the list length: exactly one match → safe to apply; zero or
        many → flag for human review rather than guessing.
        """
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.SELL,
            limit=500, direction="desc",
        )
        start_n = _as_naive_utc(start)
        end_n = _as_naive_utc(end)
        out: list[tuple[float, datetime, int]] = []
        for o in self.client.get_orders(filter=req):
            fq = getattr(o, "filled_qty", None)
            fp = getattr(o, "filled_avg_price", None)
            if not fq or float(fq) <= 0 or fp is None or float(fp) <= 0:
                continue
            fq_int = int(float(fq))
            if qty is not None and fq_int != int(qty):
                continue
            filled_at = _as_naive_utc(o.filled_at or o.updated_at)
            if filled_at is None:
                continue
            if start_n is not None and filled_at < start_n:
                continue
            if end_n is not None and filled_at > end_n:
                continue
            out.append((float(fp), filled_at, fq_int))
        return out

    def cancel_orders_for_symbol(self, symbol: str) -> bool:
        """Cancel ALL open orders for ``symbol`` (its resting bracket SL/TP legs).

        Must be called BEFORE a manual market close, otherwise a still-resting
        stop or take-profit leg can fill after the close and re-open / flip the
        position (a double-sell → short).

        Returns True only if EVERY matching open order was canceled. If any
        single cancel fails, returns False so the caller can ABORT the close —
        a partial cancel would leave a resting leg that could still flip us
        short. Returns True when there were no orders to cancel.
        """
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol],
            limit=500, direction="desc", nested=True,
        )
        orders = self.client.get_orders(filter=req)
        ids: set = set()
        for o in orders:
            if str(getattr(o, "symbol", "")) == symbol:
                ids.add(o.id)
            for leg in (getattr(o, "legs", None) or []):
                if str(getattr(leg, "symbol", "")) == symbol:
                    ids.add(leg.id)
        all_ok = True
        for oid in ids:
            try:
                self.client.cancel_order_by_id(oid)
            except Exception as e:
                all_ok = False  # do NOT swallow — caller must not proceed to close
        return all_ok

    def update_stop_price(self, symbol: str, new_stop: float) -> bool:
        """Raise the resting stop-loss leg for ``symbol`` to ``new_stop``.

        Finds the open STOP / STOP_LIMIT sell order for the symbol (the bracket
        SL leg) and replaces its stop_price. Returns True if an order was
        replaced, False if no stop leg was found. Used by the trailing-stop
        exit pass so a ratcheted stop is reflected at the broker, not just in
        our DB.
        """
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol], side=OrderSide.SELL,
            limit=500, direction="desc", nested=True,
        )
        orders = self.client.get_orders(filter=req)

        def _is_stop(o) -> bool:
            t = str(getattr(o, "order_type", "") or getattr(o, "type", "")).lower()
            return "stop" in t and getattr(o, "stop_price", None) is not None

        # The stop leg may be a top-level order or a nested bracket child.
        # Guard on symbol in BOTH branches — a nested parent can carry legs for
        # the queried symbol while the parent itself is a different instrument.
        candidates = []
        for o in orders:
            if str(getattr(o, "symbol", "")) == symbol and _is_stop(o):
                candidates.append(o)
            for leg in (getattr(o, "legs", None) or []):
                if str(getattr(leg, "symbol", "")) == symbol and _is_stop(leg):
                    candidates.append(leg)

        if not candidates:
            return False
        target = candidates[0]
        self.client.replace_order_by_id(
            target.id, order_data=ReplaceOrderRequest(stop_price=round(float(new_stop), 2))
        )
        return True

    def cancel_all_orders(self) -> int:
        """Cancel every open order (incl. resting bracket stop/take-profit legs).

        Returns the number of cancel requests acknowledged. Must run BEFORE
        liquidating positions, otherwise a resting SL/TP child order can fill
        after the flatten and re-open a position.
        """
        resp = self.client.cancel_orders()
        try:
            return len(resp)
        except TypeError:
            return 0

    def is_market_open(self) -> bool:
        clock = self.client.get_clock()
        return bool(clock.is_open)
