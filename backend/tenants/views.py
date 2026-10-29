from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config_core.permissions import PORTFOLIO, STAFF, RolePermission
from . import services
from .models import LeaseAgreement, Tenant
from .serializers import LeaseAgreementSerializer, MoveInSerializer, TenantSerializer


class TenantViewSet(viewsets.ModelViewSet):
    """
    Tenant records. Filter with ?status=, ?property=<name> and ?cluster=; search by ID, name, email, phone or status.
    Tenants are moved in and out with the `move-in` and `vacate` actions so the lease, deposit, unit and first invoice
    stay consistent, not by editing rows by hand.
    """
    serializer_class = TenantSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = PORTFOLIO
    filter_backends = [filters.SearchFilter]
    search_fields = ['tenant_id', 'name', 'email', 'phone_number', 'status']

    def get_queryset(self):
        queryset = Tenant.objects.select_related('unit__property__landlord').order_by('-id')
        params = self.request.query_params
        if params.get('status') and params['status'] != 'All':
            queryset = queryset.filter(status=params['status'])
        if params.get('property') and params['property'] != 'All':
            queryset = queryset.filter(unit__property__name=params['property'])
        if params.get('cluster') and params['cluster'] != 'All':
            queryset = queryset.filter(unit__property__cluster=params['cluster'])
        return queryset

    @action(detail=False, methods=['post'], url_path='move-in')
    def move_in(self, request):
        serializer = MoveInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            tenant = services.move_in(
                unit=data['unit'], name=data['name'], phone_number=data['phone_number'], email=data.get('email'),
                start_date=data['start_date'], end_date=data.get('end_date'), due_date=data['due_date'],
            )
        except services.TenancyError as exc:
            raise serializers.ValidationError({'detail': str(exc)})
        return Response(self.get_serializer(tenant).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def vacate(self, request, pk=None):
        tenant = self.get_object()
        try:
            balance = services.vacate(tenant)
        except services.TenancyError as exc:
            raise serializers.ValidationError({'detail': str(exc)})
        return Response({'status': 'vacated', 'outstanding_balance': balance})

    @action(detail=True, methods=['post'])
    def suspend(self, request, pk=None):
        return self._set_status(self.get_object(), 'suspended')

    @action(detail=True, methods=['post'])
    def reactivate(self, request, pk=None):
        return self._set_status(self.get_object(), 'active')

    def _set_status(self, tenant, new_status):
        if tenant.status == 'vacated':
            raise serializers.ValidationError({'detail': 'Only current tenants can be suspended or reactivated.'})
        tenant.status = new_status
        tenant.save(update_fields=['status'])
        return Response(self.get_serializer(tenant).data)


class LeaseAgreementViewSet(viewsets.ModelViewSet):
    """Lease agreements; search by tenant, unit or template."""
    queryset = LeaseAgreement.objects.select_related('tenant', 'unit__property', 'template').order_by('-id')
    serializer_class = LeaseAgreementSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = PORTFOLIO
    filter_backends = [filters.SearchFilter]
    search_fields = ['tenant__name', 'tenant__tenant_id', 'unit__unit_name', 'template__name']
