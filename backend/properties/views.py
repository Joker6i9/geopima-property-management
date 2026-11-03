from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config_core.accounts import issue_login
from config_core.permissions import PORTFOLIO, STAFF, RolePermission
from tenants.serializers import TenantSerializer
from properties.models import Landlord, Property, Unit
from .serializers import LandlordSerializer, PropertySerializer, UnitSerializer


class LandlordViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for managing Landlord records.
    """
    queryset = Landlord.objects.all()
    serializer_class = LandlordSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = PORTFOLIO
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'landlord_id', 'phone_number', 'address']

    def get_queryset(self):
        return Landlord.objects.all().order_by('-id')

    @action(detail=True, methods=['post'])
    def login(self, request, pk=None):
        """Create the landlord's portal login, or reset its password. The password is returned once."""
        landlord = self.get_object()
        return Response(issue_login(landlord, landlord.landlord_id.lower(), getattr(landlord, 'email', None)))


class PropertyViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for managing Property records. Supports filtering properties by landlord primary key via ?landlord=<id>.
    """
    queryset = Property.objects.all()
    serializer_class = PropertySerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = PORTFOLIO
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'property_id', 'location', 'landlord__name']
    
    # Tells Django REST Framework to query properties by property_id instead of integer pk
    lookup_field = 'property_id' 
    lookup_url_kwarg = 'property_id'

    def get_queryset(self):
        queryset = Property.objects.select_related('landlord').all().order_by('-id')
        
        # Filter by landlord parameter if passed in request URL (e.g., ?landlord=7)
        landlord_param = self.request.query_params.get('landlord')
        if landlord_param:
            queryset = queryset.filter(landlord_id=landlord_param)

        return queryset


class UnitViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for managing Property Unit records.
    Supports filtering units by string property_id, property name, or landlord via query parameters.
    """
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    permission_classes = [RolePermission]
    read_roles = STAFF
    write_roles = PORTFOLIO
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
    filter_backends = [filters.SearchFilter]
    search_fields = ['unit_no', 'unit_name', 'property__name', 'property__property_id']

    def get_queryset(self):
        queryset = Unit.objects.select_related('property').all().order_by('-id')
        
        # Check for string property_id, property name, or landlord parameters
        property_id_param = self.request.query_params.get('property_id')
        property_param = self.request.query_params.get('property')
        landlord_param = self.request.query_params.get('landlord')
        
        if property_id_param:
            queryset = queryset.filter(property__property_id=property_id_param)
        elif property_param and property_param != 'All':
            # Handle both numeric primary keys (sent from frontend dataset) and text names seamlessly
            if property_param.isdigit():
                queryset = queryset.filter(property_id=property_param)
            else:
                matched_property_ids = Property.objects.filter(name__iexact=property_param).values_list('id', flat=True)
                if matched_property_ids.exists():
                    queryset = queryset.filter(property_id__in=matched_property_ids)
                else:
                    queryset = queryset.none()
            
        if landlord_param and landlord_param != 'All':
            queryset = queryset.filter(property__landlord_id=landlord_param)
            
        # Optional filters for vacancy and status if passed from frontend
        vacancy_param = self.request.query_params.get('vacancy')
        if vacancy_param and vacancy_param != 'All':
            is_vacant = (vacancy_param.lower() == 'vacant')
            queryset = queryset.filter(is_occupied=not is_vacant)
            
        status_param = self.request.query_params.get('status')
        if status_param and status_param != 'All':
            queryset = queryset.filter(status__iexact=status_param)
            
        return queryset

    @action(detail=True, methods=['get'])
    def tenants(self, request, pk=None):
        """Tenants currently in this unit."""
        unit = self.get_object()
        current = unit.tenants.exclude(status='vacated').select_related('unit__property__landlord')
        return Response(TenantSerializer(current, many=True).data)
