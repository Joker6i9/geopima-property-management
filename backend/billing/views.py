from datetime import datetime

from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config_core.permissions import FINANCE, STAFF, RolePermission
from tenants.models import Tenant
from .models import Deposit, Invoice, VoteHead
from .serializers import DepositSerializer, InvoiceDetailSerializer, VoteHeadSerializer
from .services import first_of_month, generate_invoices, mark_overdue


def parse_period(value: str):
    try:
        return datetime.strptime(value, '%Y-%m').date()
    except (TypeError, ValueError):
        raise serializers.ValidationError({'period': 'Use the format YYYY-MM, e.g. 2026-10.'})


class VoteHeadViewSet(viewsets.ModelViewSet):
    """Charge types and the order payments are applied in."""
    queryset = VoteHead.objects.all().order_by('priority_order')
    serializer_class = VoteHeadSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'title', 'code']

    @action(detail=False, methods=['post'], url_path='reorder')
    def reorder(self, request):
        priority_ids = request.data.get('priority_order', [])
        for index, item_id in enumerate(priority_ids):
            VoteHead.objects.filter(id=item_id).update(priority_order=index)
        return Response({'status': 'priority sequence updated'}, status=status.HTTP_200_OK)


class DepositViewSet(viewsets.ModelViewSet):
    """Tenant deposits. A deposit can be refunded once its tenant has vacated."""
    queryset = Deposit.objects.select_related('tenant', 'unit__property').order_by('-created_at')
    serializer_class = DepositSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE
    filter_backends = [filters.SearchFilter]
    search_fields = ['deposit_code', 'reference_no', 'tenant__name', 'tenant__tenant_id']

    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        deposit = self.get_object()
        if deposit.status == 'refunded':
            raise serializers.ValidationError({'detail': 'This deposit has already been refunded.'})
        if deposit.tenant.status != 'vacated':
            raise serializers.ValidationError({'detail': 'A deposit can only be refunded after the tenant has vacated.'})
        deposit.status = 'refunded'
        deposit.save(update_fields=['status'])
        return Response(self.get_serializer(deposit).data)


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Invoices. They are created by billing, never edited by hand: use `generate-monthly` for everyone or `raise` for
    one tenant. Filter with ?status=, ?period=YYYY-MM, ?tenant=<id>, ?property=<id>.
    """
    serializer_class = InvoiceDetailSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE
    filter_backends = [filters.SearchFilter]
    search_fields = ['invoice_number', 'tenant__name', 'tenant__tenant_id', 'unit__unit_name']

    def get_queryset(self):
        queryset = (
            Invoice.objects.select_related('tenant', 'unit__property')
            .prefetch_related('items', 'settlements__transaction')
            .order_by('-billing_period', '-created_at')
        )
        params = self.request.query_params
        if params.get('status'):
            queryset = queryset.filter(status=params['status'])
        if params.get('period'):
            queryset = queryset.filter(billing_period=first_of_month(parse_period(params['period'])))
        if params.get('tenant'):
            queryset = queryset.filter(tenant_id=params['tenant'])
        if params.get('property'):
            queryset = queryset.filter(unit__property_id=params['property'])
        return queryset

    @action(detail=False, methods=['post'], url_path='generate-monthly')
    def generate_monthly(self, request):
        """Bills every active lease for `period` (default: this month). Running it twice creates nothing new."""
        period = parse_period(request.data.get('period')) if request.data.get('period') else datetime.today().date()
        result = generate_invoices(period)
        mark_overdue()
        return Response({'period': f'{first_of_month(period):%Y-%m}', 'created': len(result.created), 'skipped': result.skipped})

    @action(detail=False, methods=['post'], url_path='raise')
    def raise_invoice(self, request):
        """Bills one tenant for a period. Body: {"tenant": <id>, "period": "YYYY-MM"}."""
        tenant = Tenant.objects.filter(pk=request.data.get('tenant'), status='active').first()
        if tenant is None:
            raise serializers.ValidationError({'tenant': 'Choose a current tenant.'})
        period = parse_period(request.data.get('period'))
        result = generate_invoices(period, tenant=tenant)
        if not result.created:
            raise serializers.ValidationError(
                {'detail': 'Nothing was raised: the tenant already has an invoice for that month, or has no active lease.'}
            )
        return Response(self.get_serializer(result.created[0]).data, status=status.HTTP_201_CREATED)
