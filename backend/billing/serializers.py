from rest_framework import serializers
from .models import VoteHead, Deposit, Invoice, InvoiceItem


class VoteHeadSerializer(serializers.ModelSerializer):
    class Meta:
        model = VoteHead
        fields = ['id', 'name', 'title', 'code', 'description', 'status', 'priority_order']


class DepositSerializer(serializers.ModelSerializer):
    tenant_name = serializers.ReadOnlyField(source='tenant.name', default='-')
    property_name = serializers.ReadOnlyField(source='unit.property.name', default='-')
    unit_name = serializers.ReadOnlyField(source='unit.unit_name', default='-')
    tenant_pk = serializers.ReadOnlyField(source='tenant_id')
    unit_pk = serializers.ReadOnlyField(source='unit_id')

    class Meta:
        model = Deposit
        fields = [
            'id', 'deposit_code', 'reference_no', 'tenant_pk', 'unit_pk', 'tenant_name', 'property_name', 'unit_name',
            'amount', 'status',
        ]


class InvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceItem
        fields = ['item_type', 'description', 'amount']


class PaymentDetailSerializer(serializers.Serializer):
    """One payment applied to an invoice (a PaymentSettlement row)."""
    receipt_no = serializers.CharField(source='transaction.trans_id')
    bank = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    date_paid = serializers.SerializerMethodField()
    via = serializers.SerializerMethodField()
    amount = serializers.DecimalField(source='amount_applied', max_digits=12, decimal_places=2)

    def get_bank(self, obj):
        return f"Lipa Na M-pesa REF#: {obj.transaction.trans_id}"

    def get_type(self, obj):
        return 'C2B'

    def get_date_paid(self, obj):
        return obj.settled_at.strftime("%d-%m-%Y %H:%M")

    def get_via(self, obj):
        return 'Auto' if obj.assigned_by_id is None else 'Manual'


class InvoiceDetailSerializer(serializers.ModelSerializer):
    agency_name = serializers.SerializerMethodField()
    agency_address = serializers.SerializerMethodField()
    agency_email = serializers.SerializerMethodField()
    agency_contact = serializers.SerializerMethodField()

    id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    invoice_no = serializers.CharField(source='invoice_number')
    period = serializers.CharField(source='period_formatted')
    date = serializers.DateField(source='issue_date', format="%d-%b-%Y")
    # Machine-readable counterparts for the web app, which formats dates and groups by month itself.
    period_key = serializers.DateField(source='billing_period', format="%Y-%m", read_only=True)
    issued_on = serializers.DateField(source='issue_date', read_only=True)
    due_date = serializers.DateField(read_only=True)
    tenant_pk = serializers.ReadOnlyField(source='tenant_id')
    unit_pk = serializers.ReadOnlyField(source='unit_id')

    tenant_name_code = serializers.SerializerMethodField()
    property_name = serializers.CharField(source='unit.property.name')
    unit_no = serializers.CharField(source='unit.unit_name')
    tenant_contact = serializers.SerializerMethodField()

    items = InvoiceItemSerializer(many=True, read_only=True)
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    net_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    
    rent_paid = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    balance = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    payment_details = serializers.SerializerMethodField()
    total_paid = serializers.DecimalField(source='rent_paid', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'status', 'agency_name', 'agency_address', 'agency_email', 'agency_contact',
            'invoice_no', 'period', 'date', 'period_key', 'issued_on', 'due_date', 'tenant_pk', 'unit_pk',
            'tenant_name_code', 'property_name', 'unit_no', 'tenant_contact',
            'items', 'subtotal', 'net_total', 'rent_paid', 'balance',
            'payment_details', 'total_paid'
        ]

    def get_tenant_name_code(self, obj):
        tenant_name = getattr(obj.tenant, 'name', None) or getattr(obj.tenant, 'full_name', '')
        tenant_code = getattr(obj.tenant, 'tenant_id', None) or getattr(obj.tenant, 'tenant_code', '')
        return f"{str(tenant_name).upper()} ({tenant_code})"

    def get_tenant_contact(self, obj):
        return getattr(obj.tenant, 'phone_number', getattr(obj.tenant, 'phone', '-'))

    def get_payment_details(self, obj):
        return PaymentDetailSerializer(obj.settlements.all(), many=True).data

    # The agency's own details come from the Agency profile in Configuration.
    def _agency(self):
        if not hasattr(self, '_agency_cache'):
            from config_core.models import AgencyProfile
            self._agency_cache = AgencyProfile.objects.first()
        return self._agency_cache

    def get_agency_name(self, obj):
        return self._agency().company_name if self._agency() else ''

    def get_agency_address(self, obj):
        return self._agency().physical_address if self._agency() else ''

    def get_agency_email(self, obj):
        return self._agency().email if self._agency() else ''

    def get_agency_contact(self, obj):
        agency = self._agency()
        return ' | '.join(c for c in (agency.contact, agency.contact_2) if c) if agency else ''
