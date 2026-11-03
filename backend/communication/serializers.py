from rest_framework import serializers

from .models import SmsMessage
from .services import LANDLORD_FIELDS, TENANT_FIELDS, unknown_placeholders


class SmsMessageSerializer(serializers.ModelSerializer):
    sent_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SmsMessage
        fields = ['id', 'batch', 'recipient_name', 'phone', 'body', 'status', 'error', 'sent_by_name', 'created_at']

    def get_sent_by_name(self, obj):
        if obj.sent_by:
            return obj.sent_by.get_full_name() or obj.sent_by.username
        return 'Automatic' if obj.payment_ref else ''


class SendSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=640)
    tenants = serializers.ListField(child=serializers.IntegerField(), required=False, default=list, max_length=2000)
    landlords = serializers.ListField(child=serializers.IntegerField(), required=False, default=list, max_length=500)

    def validate_message(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('Write a message first.')
        return value

    def validate(self, attrs):
        if not attrs['tenants'] and not attrs['landlords']:
            raise serializers.ValidationError({'message': 'Choose at least one recipient.'})
        allowed = TENANT_FIELDS if not attrs['landlords'] else TENANT_FIELDS & LANDLORD_FIELDS
        bad = unknown_placeholders(attrs['message'], allowed)
        if bad:
            raise serializers.ValidationError({'message': 'Unknown placeholder: ' + ', '.join('{%s}' % b for b in bad)})
        return attrs
