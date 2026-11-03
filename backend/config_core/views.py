from rest_framework import viewsets, mixins, serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Count, Q

from billing.services import first_of_month
from billing.models import Invoice
from properties.models import Property
from .permissions import ADMINS, FINANCE, STAFF, RolePermission, portal_role, role_names, role_permissions
from .models import (
    AgencyProfile, 
    UserRole, 
    PaymentBank, 
    SMSTemplate, 
    AgreementTemplate
)
from .serializers import (
    CustomTokenObtainPairSerializer,
    AgencyProfileSerializer, 
    UserRoleSerializer,
    PaymentBankSerializer, 
    SMSTemplateSerializer, 
    AgreementTemplateSerializer,
    UserAccountSerializer
)

User = get_user_model()


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class CurrentUserView(APIView):
    """Who is signed in, and which portal they belong in. The web app reads `role` to choose the portal."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        tenant = getattr(user, 'tenant_profile', None)
        landlord = getattr(user, 'landlord_profile', None)
        return Response({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": f"{user.first_name} {user.last_name}".strip() or user.username,
            "role": portal_role(user),
            "roles": sorted(role_names(user)),
            "permissions": role_permissions(user),
            "is_superuser": user.is_superuser,
            "tenant_id": tenant.id if tenant else None,
            "landlord_id": landlord.id if landlord else None,
        })


class DashboardMetricsView(APIView):
    """Units and occupied units per property. A unit is occupied when a current tenant lives in it."""
    permission_classes = [RolePermission]
    read_roles = STAFF

    def get(self, request):
        properties = Property.objects.annotate(
            total=Count('units', distinct=True),
            occupied=Count('units', filter=Q(units__tenants__status__in=['active', 'suspended']), distinct=True),
        ).order_by('name')
        return Response({
            'properties': [p.name for p in properties],
            'total_units': [p.total for p in properties],
            'occupied_units': [p.occupied for p in properties],
        })


class PaymentSummariesView(APIView):
    """How much of each charge type billed this month has been collected (payments are split pro rata)."""
    permission_classes = [RolePermission]
    read_roles = FINANCE

    def get(self, request):
        period = first_of_month(date.today())
        invoices = Invoice.objects.filter(billing_period=period).prefetch_related('items', 'settlements')
        totals: dict[str, Decimal] = {}
        collected: dict[str, Decimal] = {}
        for invoice in invoices:
            invoice_total = invoice.net_total
            ratio = (invoice.rent_paid / invoice_total) if invoice_total else Decimal('0')
            for item in invoice.items.all():
                totals[item.item_type] = totals.get(item.item_type, Decimal('0')) + item.amount
                collected[item.item_type] = collected.get(item.item_type, Decimal('0')) + item.amount * ratio
        return Response([
            {
                'name': item_type,
                'collected': round(collected[item_type]),
                'total': round(total),
                'pct': round(collected[item_type] / total * 100, 1) if total else 0,
            }
            for item_type, total in sorted(totals.items())
        ])


class AgencyProfileViewSet(viewsets.GenericViewSet, mixins.RetrieveModelMixin, mixins.UpdateModelMixin):
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = ADMINS
    serializer_class = AgencyProfileSerializer

    def get_object(self):
        obj, _ = AgencyProfile.objects.get_or_create(id=1)
        return obj


class UserRoleViewSet(viewsets.ModelViewSet):
    queryset = UserRole.objects.all()
    serializer_class = UserRoleSerializer
    permission_classes = [RolePermission]
    read_roles = ADMINS
    write_roles = ADMINS


class UserAccountViewSet(viewsets.ModelViewSet):
    serializer_class = UserAccountSerializer
    permission_classes = [RolePermission]
    read_roles = ADMINS
    write_roles = ADMINS

    def get_queryset(self):
        return User.objects.all()

    def perform_update(self, serializer):
        # Nobody can lock themselves out or demote themselves by accident.
        if serializer.instance == self.request.user:
            data = serializer.validated_data
            if data.get('is_active') is False:
                raise serializers.ValidationError({'detail': 'You cannot deactivate your own account.'})
            if 'role' in data and data['role'] not in role_names(self.request.user) and not self.request.user.is_superuser:
                raise serializers.ValidationError({'detail': 'You cannot change your own role.'})
        serializer.save()

    def perform_destroy(self, instance):
        # Accounts are kept (their history points at them); deactivating is the way to remove access.
        raise serializers.ValidationError({'detail': 'Accounts cannot be deleted. Deactivate the account instead.'})


class PaymentBankViewSet(viewsets.ModelViewSet):
    queryset = PaymentBank.objects.all()
    serializer_class = PaymentBankSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE


class SMSTemplateViewSet(viewsets.ModelViewSet):
    queryset = SMSTemplate.objects.all()
    serializer_class = SMSTemplateSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE + ('Secretary',)

    @action(detail=False, methods=['post'], url_path='bulk-save')
    def bulk_save(self, request):
        data = request.data
        if not isinstance(data, list):
            return Response(
                {"error": "Expected a list of templates."},
                status=status.HTTP_400_BAD_REQUEST
            )

        updated_objects = []
        for item in data:
            category = item.get('category')
            text = item.get('template_text')
            if category and text:
                defaults = {'template_text': text}
                if isinstance(item.get('enabled'), bool):
                    defaults['enabled'] = item['enabled']
                obj, _ = SMSTemplate.objects.update_or_create(category=category, defaults=defaults)
                updated_objects.append(obj)

        serializer = self.get_serializer(updated_objects, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='bulk-update')
    def bulk_update(self, request):
        return self.bulk_save(request)


class AgreementTemplateViewSet(viewsets.ModelViewSet):
    queryset = AgreementTemplate.objects.all()
    serializer_class = AgreementTemplateSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = FINANCE + ('Secretary',)