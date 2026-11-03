from rest_framework import serializers

from properties.models import Unit
from .models import Tenant, LeaseAgreement
from .utils import normalize_phone_number


class TenantSerializer(serializers.ModelSerializer):
    tenant_id = serializers.CharField(required=False, allow_blank=True)
    tenantId = serializers.CharField(source='tenant_id', read_only=True)
    contact = serializers.CharField(source='phone_number', read_only=True)
    property_name = serializers.ReadOnlyField(source='unit.property.name', default=None)
    unit_name = serializers.ReadOnlyField(source='unit.unit_name', default=None)
    landlord_name = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id',
            'tenant_id',
            'tenantId',
            'name',
            'email',
            'phone_number',
            'contact',
            'id_number',
            'secondary_contact',
            'unit',
            'property_name',
            'unit_name',
            'landlord_name',
            'entry_date',
            'due_date',
            'status',
        ]
        read_only_fields = ['id']

    def validate_phone_number(self, value):
        phone = normalize_phone_number(value)
        if len(phone) != 12:
            raise serializers.ValidationError('Enter a valid Kenyan phone number, e.g. 0712 345 678.')
        clash = Tenant.objects.exclude(status='vacated').filter(phone_number=phone)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('A current tenant already uses that phone number.')
        return phone

    def validate_due_date(self, value):
        if not 1 <= value <= 28:
            raise serializers.ValidationError('Rent due day must be between 1 and 28.')
        return value

    def validate_secondary_contact(self, value):
        return normalize_phone_number(value) if value else ''

    def get_landlord_name(self, obj):
        try:
            if obj.unit and obj.unit.property:
                prop = obj.unit.property
                landlord = getattr(prop, 'landlord', None) or getattr(prop, 'owner', None)
                if landlord:
                    return str(landlord)
        except AttributeError:
            pass
        return None


class LeaseAgreementSerializer(serializers.ModelSerializer):
    tenant_name = serializers.ReadOnlyField(source='tenant.name', default=None)
    unit_number = serializers.ReadOnlyField(source='unit.unit_name', default=None)
    property_name = serializers.ReadOnlyField(source='unit.property.name', default=None)

    class Meta:
        model = LeaseAgreement
        fields = [
            'id',
            'tenant',
            'tenant_name',
            'unit',
            'unit_number',
            'property_name',
            'start_date',
            'end_date',
            'rent_amount',
            'deposit_amount',
            'is_active',
            'agreement_file',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class MoveInSerializer(serializers.Serializer):
    unit = serializers.PrimaryKeyRelatedField(queryset=Unit.objects.all())
    name = serializers.CharField(max_length=255)
    phone_number = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True)
    due_date = serializers.IntegerField(required=False, default=5, min_value=1, max_value=28)
    id_number = serializers.CharField(max_length=30, required=False, allow_blank=True, default='')
    secondary_contact = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
