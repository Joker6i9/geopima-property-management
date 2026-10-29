from datetime import date

from django.db import transaction

from billing.models import Deposit, Invoice
from billing.services import generate_invoices
from properties.models import Unit
from .models import LeaseAgreement, Tenant
from .utils import normalize_phone_number


class TenancyError(ValueError):
    """A move-in or vacate that cannot be done; the message is safe to show to staff."""


@transaction.atomic
def move_in(*, unit: Unit, name: str, phone_number: str, email: str | None, start_date: date,
            end_date: date | None, due_date: int = 5, today: date | None = None) -> Tenant:
    """
    Puts a new tenant in a vacant unit: creates the tenant, an active lease at the unit's rent, a held deposit,
    marks the unit occupied, and bills the first month (including the deposit).
    """
    today = today or date.today()
    unit = Unit.objects.select_for_update().get(pk=unit.pk)
    if Tenant.objects.exclude(status='vacated').filter(unit=unit).exists():
        raise TenancyError(f'Unit {unit.unit_name} already has a tenant.')
    phone = normalize_phone_number(phone_number)
    if not phone or len(phone) != 12:
        raise TenancyError('Enter a valid Kenyan phone number, e.g. 0712 345 678.')
    if Tenant.objects.exclude(status='vacated').filter(phone_number=phone).exists():
        raise TenancyError('A current tenant already uses that phone number.')
    if end_date and end_date <= start_date:
        raise TenancyError('The lease must end after it starts.')
    if not 1 <= due_date <= 28:
        raise TenancyError('Rent due day must be between 1 and 28.')

    tenant = Tenant.objects.create(
        name=name, phone_number=phone, email=email or None, unit=unit,
        entry_date=start_date, due_date=due_date, status='active',
    )
    LeaseAgreement.objects.create(
        tenant=tenant, unit=unit, start_date=start_date, end_date=end_date,
        rent_amount=unit.rent_amount, deposit_amount=unit.deposit, is_active=True,
    )
    Deposit.objects.create(
        deposit_code=f'DEP-{tenant.tenant_id}', tenant=tenant, unit=unit, amount=unit.deposit, status='held',
    )
    unit.is_occupied = True
    unit.status = 'Occupied'
    unit.save(update_fields=['is_occupied', 'status'])
    generate_invoices(today, today=today, tenant=tenant)
    return tenant


@transaction.atomic
def vacate(tenant: Tenant, today: date | None = None):
    """Ends the tenant's lease and frees the unit. Unpaid invoices and the held deposit stay on record."""
    today = today or date.today()
    if tenant.status == 'vacated' or tenant.unit_id is None:
        raise TenancyError('This tenant is not currently in a unit.')
    unit = tenant.unit
    LeaseAgreement.objects.filter(tenant=tenant, is_active=True).update(is_active=False, end_date=today)
    tenant.status = 'vacated'
    tenant.unit = None
    tenant.save(update_fields=['status', 'unit'])
    unit.is_occupied = False
    unit.status = 'Available'
    unit.save(update_fields=['is_occupied', 'status'])
    balance = sum((i.balance for i in Invoice.objects.filter(tenant=tenant).prefetch_related('items', 'settlements')), 0)
    return balance
