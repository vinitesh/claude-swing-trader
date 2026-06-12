"""Alpaca broker implementation (paper or live)."""

from __future__ import annotations

from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
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
            filled_at = o.filled_at or o.updated_at
            # Only count an exit that happened after this position opened — a
            # stale sell from a prior trade on the same symbol is not our exit.
            if opened_after is not None and filled_at is not None and filled_at < opened_after:
                continue
            return float(filled_px), (filled_at or datetime.utcnow())
        return None

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
