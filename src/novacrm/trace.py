"""Printing a deal's audit trail for `novacrm trace` and `novacrm demo`."""

from __future__ import annotations

from .store import Store


def print_trail(store: Store, key: str) -> None:
    deal = store.get(key)
    if deal is None:
        raise SystemExit(f"no deal {key}")
    customer = deal.notification.customer_name if deal.notification else deal.subject
    tier = f"  tier={deal.tier}" if deal.tier else ""
    print(f"{deal.key}  {customer}  [{deal.state}]{tier}")
    if deal.review_reason:
        print(f"  waiting for a person: {deal.review_reason} - {deal.review_detail}")
    for row in store.trail(key):
        decision = f" -> {row['decision']}" if row["decision"] else ""
        print(f"  {row['ts'][11:19]} {row['agent']:>12} {row['action']:<17}{decision}")
        if row["reason"]:
            print(f"  {'':>8} {'':>12} {'':<17} {row['reason']}")
