from datetime import datetime

from rest_framework import serializers

from tenants.models import Tenant
from .models import MpesaTransaction, StkRequest


class MpesaTransactionSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)
    time = serializers.SerializerMethodField()
    tenant = serializers.SerializerMethodField()
    allocations = serializers.SerializerMethodField()
    unallocated = serializers.SerializerMethodField()

    class Meta:
        model = MpesaTransaction
        fields = [
            'id', 'trans_id', 'time', 'trans_amount', 'msisdn', 'bill_ref_number', 'first_name', 'last_name',
            'status', 'tenant', 'allocations', 'unallocated', 'channel', 'paid_to', 'note',
        ]

    def get_time(self, obj):
        # Safaricom sends YYYYMMDDHHMMSS; fall back to when we received it.
        try:
            return datetime.strptime(obj.trans_time, '%Y%m%d%H%M%S').isoformat()
        except ValueError:
            return obj.created_at.isoformat()

    def _first_settlement(self, obj):
        return next(iter(obj.settlements.all()), None)

    def get_tenant(self, obj):
        first = self._first_settlement(obj)
        return {'id': first.tenant_id, 'name': first.tenant.name, 'tenant_id': first.tenant.tenant_id} if first else None

    def get_allocations(self, obj):
        return [
            {'invoice': s.invoice.invoice_number, 'invoice_id': s.invoice_id, 'amount': s.amount_applied}
            for s in obj.settlements.all() if s.invoice_id
        ]

    def get_unallocated(self, obj):
        return sum((s.amount_applied for s in obj.settlements.all() if s.invoice_id is None), 0)


class AssignSerializer(serializers.Serializer):
    tenant = serializers.PrimaryKeyRelatedField(queryset=Tenant.objects.all())



class RecordPaymentSerializer(serializers.Serializer):
    """A payment staff received outside the paybill: cash, a bank transfer or a cheque."""
    tenant = serializers.PrimaryKeyRelatedField(queryset=Tenant.objects.all())
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=1)
    channel = serializers.ChoiceField(choices=[c for c in MpesaTransaction.CHANNEL_CHOICES if c[0] != 'mpesa'])
    date = serializers.DateField()
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
    paid_to = serializers.CharField(max_length=100, required=False, allow_blank=True)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_date(self, value):
        from django.utils import timezone
        if value > timezone.localdate():
            raise serializers.ValidationError('The payment date cannot be in the future.')
        return value

    def validate(self, attrs):
        # Cash has no slip number, so one is made up; bank and cheque payments must carry theirs.
        if not attrs.get('reference'):
            if attrs['channel'] != 'cash':
                raise serializers.ValidationError({'reference': 'Enter the bank reference or cheque number.'})
        elif MpesaTransaction.objects.filter(trans_id__iexact=attrs['reference']).exists():
            raise serializers.ValidationError({'reference': 'A payment with this reference was already recorded.'})
        return attrs


class StkStartSerializer(serializers.Serializer):
    tenant = serializers.PrimaryKeyRelatedField(queryset=Tenant.objects.all())
    amount = serializers.IntegerField(min_value=1)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)


class StkRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = StkRequest
        fields = ['id', 'status', 'amount', 'phone', 'result_desc', 'receipt', 'created_at']
