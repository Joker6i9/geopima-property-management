import calendar
from dataclasses import dataclass, field
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Q

from tenants.models import LeaseAgreement, Tenant
from .models import Invoice, InvoiceItem

# unit column -> (item_type, description)
UNIT_FEES = (
    ('water_rate', 'Water', 'Water'),
    ('garbage_fee', 'Garbage', 'Garbage collection'),
    ('security_fee', 'Security', 'Security'),
    ('electricity_fee', 'Electricity', 'Electricity'),
    ('internet_fee', 'Internet', 'Internet'),
)


@dataclass
class GenerationResult:
    created: list[Invoice] = field(default_factory=list)
    skipped: int = 0


def first_of_month(value: date) -> date:
    return value.replace(day=1)


def invoice_lines(lease: LeaseAgreement, include_deposit: bool) -> list[tuple[str, str, object]]:
    """What a lease is billed for in one month: rent, the unit's fees and, once, the deposit."""
    unit = lease.unit
    lines = []
    rent = lease.rent_amount or unit.rent_amount
    if rent > 0:
        lines.append(('Rent', 'House rent', rent))
    for column, item_type, description in UNIT_FEES:
        amount = getattr(unit, column)
        if amount > 0:
            lines.append((item_type, description, amount))
    if include_deposit and lease.deposit_amount > 0:
        lines.append(('Deposit', 'Security deposit', lease.deposit_amount))
    return lines


def generate_invoices(period: date, today: date | None = None, tenant: Tenant | None = None) -> GenerationResult:
    """
    Bills every active lease for `period` (the first of a month). Safe to run repeatedly: a tenant who already
    has an invoice for the unit and month is skipped. The deposit is billed only on a lease's first invoice.
    Pass `tenant` to bill just one tenant.
    """
    from mpesa.services import MpesaSettlementEngine  # imported here because mpesa depends on billing

    today = today or date.today()
    period = first_of_month(period)
    month_end = period.replace(day=calendar.monthrange(period.year, period.month)[1])

    leases = (
        LeaseAgreement.objects.filter(is_active=True, start_date__lte=month_end, tenant__status='active')
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=period))
        .select_related('tenant', 'unit')
    )
    if tenant is not None:
        leases = leases.filter(tenant=tenant)

    result = GenerationResult()
    for lease in leases:
        if Invoice.objects.filter(tenant=lease.tenant, unit=lease.unit, billing_period=period).exists():
            result.skipped += 1
            continue
        first_invoice = not Invoice.objects.filter(tenant=lease.tenant, unit=lease.unit).exists()
        lines = invoice_lines(lease, include_deposit=first_invoice)
        if not lines:
            result.skipped += 1
            continue
        try:
            with transaction.atomic():
                invoice = Invoice.objects.create(
                    tenant=lease.tenant,
                    unit=lease.unit,
                    billing_period=period,
                    due_date=Invoice.due_date_for(period, lease.tenant.due_date),
                )
                InvoiceItem.objects.bulk_create(
                    [InvoiceItem(invoice=invoice, item_type=t, description=d, amount=a) for t, d, a in lines]
                )
        except IntegrityError:
            # Another run created it between our check and the insert.
            result.skipped += 1
            continue
        # Money the tenant paid in advance settles the new invoice straight away.
        MpesaSettlementEngine.apply_credits(lease.tenant)
        invoice.refresh_status(today)
        result.created.append(invoice)
    return result


def mark_overdue(today: date | None = None) -> int:
    """Moves unpaid invoices past their due date to 'overdue'. Returns how many changed."""
    today = today or date.today()
    return Invoice.objects.filter(status='unpaid', due_date__lt=today).update(status='overdue')
