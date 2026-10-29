from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import Deposit, Invoice
from billing.serializers import InvoiceDetailSerializer
from config_core.models import AgencyProfile
from mpesa.models import MpesaTransaction
from mpesa.serializers import MpesaTransactionSerializer
from mpesa.services import MpesaSettlementEngine
from properties.models import Property, Unit
from tenants.models import LeaseAgreement
from .permissions import IsLandlord, IsTenant
from .statements import statement_rows, rent_billed, rent_collected

ZERO = Decimal('0.00')


def plain(value):
    """Money goes out as 2-decimal strings, like every serializer in this API, so a response never mixes the two."""
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def respond(data):
    return Response(plain(data))
OCCUPIED = ('active', 'suspended')


def invoice_queryset():
    return (
        Invoice.objects.select_related('tenant', 'unit__property')
        .prefetch_related('items', 'settlements__transaction')
        .order_by('-billing_period', '-created_at')
    )


def agency_info() -> dict:
    agency = AgencyProfile.objects.first()
    return {
        'name': agency.company_name if agency else '',
        'phone': agency.contact if agency else '',
        'email': agency.email if agency else '',
        'paybill': settings.MPESA_PAYBILL,
    }


# ---------------------------------------------------------------- tenant portal

class TenantPortalView(APIView):
    """Base for tenant endpoints: `self.tenant` is the signed-in tenant and nobody else."""
    permission_classes = [IsTenant]

    @property
    def tenant(self):
        return self.request.user.tenant_profile


class TenantHomeView(TenantPortalView):
    """Everything the tenant's home page shows: what they owe, how to pay, latest invoices and payments."""

    def get(self, request):
        tenant = self.tenant
        invoices = list(invoice_queryset().filter(tenant=tenant))
        open_invoices = sorted((i for i in invoices if i.balance > 0), key=lambda i: i.due_date)
        payments = (
            MpesaTransaction.objects.filter(settlements__tenant=tenant).distinct()
            .prefetch_related('settlements__invoice', 'settlements__tenant').order_by('-created_at')[:5]
        )
        unit = tenant.unit
        context = {'request': request}
        return respond({
            'tenant': {
                'id': tenant.id, 'code': tenant.tenant_id, 'name': tenant.name, 'status': tenant.status,
                'property': unit.property.name if unit else None, 'unit': unit.unit_name if unit else None,
            },
            'balance': sum((i.balance for i in open_invoices), ZERO),
            'credit': MpesaSettlementEngine.credit_balance(tenant),
            'overdue': any(i.status == 'overdue' for i in open_invoices),
            'next_due': InvoiceDetailSerializer(open_invoices[0], context=context).data if open_invoices else None,
            'recent_invoices': InvoiceDetailSerializer(invoices[:3], many=True, context=context).data,
            'recent_payments': MpesaTransactionSerializer(payments, many=True, context=context).data,
            # Tenants pay to the paybill using their unit name as the account number.
            'payment': {'paybill': settings.MPESA_PAYBILL, 'account_number': unit.unit_name if unit else tenant.tenant_id},
        })


class TenantInvoiceListView(TenantPortalView):
    def get(self, request):
        qs = invoice_queryset().filter(tenant=self.tenant)
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return respond(InvoiceDetailSerializer(qs, many=True, context={'request': request}).data)


class TenantInvoiceDetailView(TenantPortalView):
    def get(self, request, pk):
        # Another tenant's invoice is a 404, not a 403, so ids cannot be probed.
        invoice = get_object_or_404(invoice_queryset().filter(tenant=self.tenant), pk=pk)
        return respond(InvoiceDetailSerializer(invoice, context={'request': request}).data)


class TenantPaymentListView(TenantPortalView):
    def get(self, request):
        payments = (
            MpesaTransaction.objects.filter(settlements__tenant=self.tenant).distinct()
            .prefetch_related('settlements__invoice', 'settlements__tenant').order_by('-created_at')
        )
        return respond(MpesaTransactionSerializer(payments, many=True, context={'request': request}).data)


class TenantLeaseView(TenantPortalView):
    def get(self, request):
        tenant = self.tenant
        leases = LeaseAgreement.objects.filter(tenant=tenant).select_related('unit__property__landlord')
        lease = leases.filter(is_active=True).order_by('-start_date').first() or leases.order_by('-start_date').first()
        deposit = Deposit.objects.filter(tenant=tenant).order_by('-created_at').first()
        unit = lease.unit if lease else tenant.unit
        return respond({
            'tenant': {
                'name': tenant.name, 'code': tenant.tenant_id, 'phone': tenant.phone_number, 'email': tenant.email,
                'status': tenant.status, 'entry_date': tenant.entry_date, 'due_day': tenant.due_date,
            },
            'unit': {
                'name': unit.unit_name, 'type': unit.unit_type, 'property': unit.property.name,
                'location': unit.property.location,
            } if unit else None,
            'lease': {
                'start_date': lease.start_date, 'end_date': lease.end_date, 'rent': lease.rent_amount,
                'is_active': lease.is_active,
            } if lease else None,
            'deposit': {'amount': deposit.amount, 'status': deposit.status} if deposit else None,
            # The landlord's name is shown; their contact and bank details are not the tenant's business.
            'landlord': unit.property.landlord.name if unit else None,
            'agency': agency_info(),
        })


# -------------------------------------------------------------- landlord portal

class LandlordPortalView(APIView):
    """Base for landlord endpoints: only properties owned by `self.landlord` are ever reachable."""
    permission_classes = [IsLandlord]

    @property
    def landlord(self):
        return self.request.user.landlord_profile

    def properties(self):
        return Property.objects.filter(landlord=self.landlord).annotate(
            unit_count=Count('units', distinct=True),
            occupied_count=Count('units', filter=Q(units__tenants__status__in=OCCUPIED), distinct=True),
        ).order_by('name')

    def invoices(self, **filters):
        return invoice_queryset().filter(unit__property__landlord=self.landlord, **filters)

    def property_figures(self, properties, period: date) -> dict[int, dict]:
        """Billed and collected for one month, keyed by property id."""
        figures = {p.id: {'billed': ZERO, 'collected': ZERO, 'rent_roll': ZERO} for p in properties}
        # Rent roll: what the occupied units are charged each month.
        occupied_units = Unit.objects.filter(
            property__in=[p.id for p in properties], tenants__status__in=OCCUPIED
        ).distinct()
        for unit in occupied_units:
            figures[unit.property_id]['rent_roll'] += unit.rent_amount
        for inv in self.invoices(billing_period=period):
            figures[inv.unit.property_id]['billed'] += rent_billed(inv)
            figures[inv.unit.property_id]['collected'] += rent_collected(inv)
        return figures

    @staticmethod
    def property_row(p, fig) -> dict:
        return {
            'id': p.id, 'code': p.property_id, 'name': p.name, 'location': p.location, 'cluster': p.cluster,
            'status': p.status, 'commission_pct': p.commission_percentage,
            'units': p.unit_count, 'occupied': p.occupied_count,
            'occupancy': round(100 * p.occupied_count / p.unit_count) if p.unit_count else 0,
            'billed': fig['billed'], 'collected': fig['collected'], 'rent_roll': fig['rent_roll'],
        }


def this_month() -> date:
    return date.today().replace(day=1)


class LandlordOverviewView(LandlordPortalView):
    def get(self, request):
        props = list(self.properties())
        period = this_month()
        figures = self.property_figures(props, period)
        rows = [self.property_row(p, figures[p.id]) for p in props]
        billed = sum((r['billed'] for r in rows), ZERO)
        collected = sum((r['collected'] for r in rows), ZERO)
        commission = sum(
            ((r['collected'] * r['commission_pct'] / 100).quantize(Decimal('0.01')) for r in rows), ZERO
        )
        units = sum(r['units'] for r in rows)
        occupied = sum(r['occupied'] for r in rows)
        landlord = self.landlord
        return respond({
            'landlord': {'id': landlord.id, 'code': landlord.landlord_id, 'name': landlord.name},
            'period': f'{period:%Y-%m}',
            'properties': rows,
            'totals': {
                'properties': len(rows), 'units': units, 'occupied': occupied,
                'occupancy': round(100 * occupied / units) if units else 0,
                'billed': billed, 'collected': collected, 'commission': commission, 'net': collected - commission,
            },
        })


class LandlordPropertyListView(LandlordPortalView):
    def get(self, request):
        props = list(self.properties())
        figures = self.property_figures(props, this_month())
        return respond([self.property_row(p, figures[p.id]) for p in props])


class LandlordPropertyDetailView(LandlordPortalView):
    def get(self, request, pk):
        prop = get_object_or_404(self.properties(), pk=pk)
        figures = self.property_figures([prop], this_month())
        units = Unit.objects.filter(property=prop).prefetch_related('tenants').order_by('unit_name')
        unit_rows = []
        for u in units:
            # Landlords see who lives where by name only; contact details stay with the agency.
            tenant = next((t for t in u.tenants.all() if t.status in OCCUPIED), None)
            unit_rows.append({
                'id': u.id, 'name': u.unit_name, 'type': u.unit_type, 'rent': u.rent_amount,
                'water': u.water_rate, 'garbage': u.garbage_fee,
                'status': 'occupied' if tenant else 'vacant',
                'tenant_name': tenant.name if tenant else None,
            })
        return respond({'property': self.property_row(prop, figures[prop.id]), 'units': unit_rows})


class LandlordStatementsView(LandlordPortalView):
    """Monthly statements. ?months=N limits how far back to go (default 12)."""

    def get(self, request):
        try:
            months = int(request.query_params.get('months', 12))
        except ValueError:
            raise serializers.ValidationError({'months': 'Enter a number.'})
        if not 1 <= months <= 60:
            raise serializers.ValidationError({'months': 'Choose between 1 and 60 months.'})
        today = date.today()
        total = today.year * 12 + today.month - 1 - (months - 1)
        since = date(total // 12, total % 12 + 1, 1)
        commission = {p.id: p.commission_percentage for p in self.properties()}
        rows = statement_rows(self.invoices(billing_period__gte=since), commission)
        landlord = self.landlord
        return respond({'landlord': {'id': landlord.id, 'code': landlord.landlord_id, 'name': landlord.name}, 'rows': rows})
