"""Alpaca broker implementation (paper or live)."""

from __future__ import annotations

from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
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

    def is_market_open(self) -> bool:
        clock = self.client.get_clock()
        return bool(clock.is_open)
