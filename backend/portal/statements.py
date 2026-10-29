"""Landlord figures: what was billed and collected per property and month, and what the landlord is owed."""
from decimal import Decimal

from billing.models import Invoice

ZERO = Decimal('0.00')


def rent_billed(invoice: Invoice) -> Decimal:
    """Everything billed except the deposit, which is held for the tenant and is not the landlord's income."""
    return sum((i.amount for i in invoice.items.all() if i.item_type != 'Deposit'), ZERO)


def rent_collected(invoice: Invoice) -> Decimal:
    """
    What has been paid towards the invoice, capped at its non-deposit total. Payments are not tracked per charge, so
    the deposit is assumed to be cleared last; this errs on the side of paying the landlord less, never more.
    """
    return min(invoice.rent_paid, rent_billed(invoice))


def statement_rows(invoices, commission_by_property: dict[int, Decimal]) -> list[dict]:
    """One row per (month, property), newest month first."""
    rows: dict[tuple, dict] = {}
    for inv in invoices:
        prop = inv.unit.property
        key = (inv.billing_period, prop.id)
        row = rows.setdefault(key, {
            'period': f'{inv.billing_period:%Y-%m}', 'property_id': prop.id, 'property': prop.name,
            'billed': ZERO, 'collected': ZERO,
        })
        row['billed'] += rent_billed(inv)
        row['collected'] += rent_collected(inv)
    out = []
    for (_, prop_id), row in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[1]['property']), reverse=True):
        if row['billed'] <= 0:
            continue
        commission = (row['collected'] * commission_by_property[prop_id] / 100).quantize(Decimal('0.01'))
        out.append({**row, 'arrears': row['billed'] - row['collected'], 'commission': commission,
                    'net': row['collected'] - commission})
    return out
