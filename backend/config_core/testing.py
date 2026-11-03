"""Small factories shared by the test suites."""
from datetime import date
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from config_core.models import UserRole
from properties.models import Landlord, Property, Unit
from tenants.models import LeaseAgreement, Tenant

_seq = count(1)


def make_user(role: str | None = None, **extra):
    user = get_user_model().objects.create_user(username=f'user{next(_seq)}', password='pw', **extra)
    if role:
        user.roles.add(UserRole.objects.get_or_create(name=role)[0])
    return user


def client_for(user=None) -> APIClient:
    client = APIClient()
    if user:
        client.force_authenticate(user)
    return client


def make_property(name='Riverside', **extra) -> Property:
    n = next(_seq)
    landlord = Landlord.objects.create(landlord_id=f'LL{n}', name=f'Landlord {n}')
    return Property.objects.create(property_id=f'PR{n}', name=name, landlord=landlord, **extra)


def make_unit(prop: Property | None = None, name='A1', rent='20000', water='500', garbage='300', deposit='20000', **extra) -> Unit:
    prop = prop or make_property(name=f'Property {next(_seq)}')
    return Unit.objects.create(
        unit_no=name, unit_name=name, property=prop, rent_amount=Decimal(rent), water_rate=Decimal(water),
        garbage_fee=Decimal(garbage), deposit=Decimal(deposit), **extra,
    )


def make_tenant(unit: Unit | None = None, name='Grace Wanjiku', phone='0712345678', start=date(2026, 1, 1),
                deposit=None, lease=True, **extra) -> Tenant:
    unit = unit or make_unit()
    tenant = Tenant.objects.create(name=name, phone_number=phone, unit=unit, entry_date=start, **extra)
    if lease:
        LeaseAgreement.objects.create(
            tenant=tenant, unit=unit, start_date=start, rent_amount=unit.rent_amount,
            deposit_amount=Decimal(deposit) if deposit is not None else Decimal('0'),
        )
    return tenant
