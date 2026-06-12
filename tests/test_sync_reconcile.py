"""Tests for sync reconciliation logic (repo.classify_for_sync).

Regression guard for the desync bug: an after-close run-live queues market
orders that don't fill until the next open. The 19:00 sync must NOT close
those DB positions just because the broker doesn't HOLD them yet — they have
a working order. Closing them wrote them off and blinded the capital cap,
leading to over-deployment (negative cash, 60 stranded broker positions).
"""

from __future__ import annotations

from persistence import repository as repo


def test_held_position_kept():
    b = repo.classify_for_sync(["AAPL"], held_symbols={"AAPL"}, pending_symbols=set())
    assert b["keep_held"] == ["AAPL"]
    assert b["close"] == []


def test_pending_entry_not_closed():
    # THE BUG: AAPL has a queued (not-yet-filled) entry order. Broker holds
    # nothing yet. It must NOT be closed.
    b = repo.classify_for_sync(["AAPL"], held_symbols=set(), pending_symbols={"AAPL"})
    assert b["keep_pending"] == ["AAPL"]
    assert b["close"] == []


def test_genuinely_gone_is_closed():
    # Not held, no working order anywhere → really exited → close it.
    b = repo.classify_for_sync(["AAPL"], held_symbols=set(), pending_symbols=set())
    assert b["close"] == ["AAPL"]
    assert b["keep_held"] == [] and b["keep_pending"] == []


def test_mixed_bucketing():
    b = repo.classify_for_sync(
        db_open_symbols=["HELD", "PENDING", "GONE"],
        held_symbols={"HELD"},
        pending_symbols={"PENDING"},
    )
    assert b["keep_held"] == ["HELD"]
    assert b["keep_pending"] == ["PENDING"]
    assert b["close"] == ["GONE"]


def test_held_takes_precedence_over_pending():
    # If a symbol is both held and has a working bracket leg, it stays open
    # via keep_held — never double-counted, never closed.
    b = repo.classify_for_sync(["AAPL"], held_symbols={"AAPL"}, pending_symbols={"AAPL"})
    assert b["keep_held"] == ["AAPL"]
    assert b["keep_pending"] == []
    assert b["close"] == []


def test_empty_db_is_noop():
    b = repo.classify_for_sync([], held_symbols={"AAPL"}, pending_symbols={"MSFT"})
    assert b == {"keep_held": [], "keep_pending": [], "close": []}


def test_every_symbol_classified_exactly_once():
    syms = ["A", "B", "C", "D"]
    b = repo.classify_for_sync(syms, held_symbols={"A"}, pending_symbols={"B", "C"})
    out = b["keep_held"] + b["keep_pending"] + b["close"]
    assert sorted(out) == sorted(syms)
    assert len(out) == len(syms)  # no symbol in two buckets
