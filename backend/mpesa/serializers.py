from datetime import datetime

from rest_framework import serializers

from tenants.models import Tenant
from .models import MpesaTransaction


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
            'status', 'tenant', 'allocations', 'unallocated',
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
