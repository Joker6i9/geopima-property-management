from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config_core.permissions import FINANCE, RolePermission
from .models import SmsMessage
from .serializers import SendSerializer, SmsMessageSerializer
from .services import build_messages, provider_name, send_batch

MESSAGING = FINANCE + ('Secretary',)


class SmsMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The message log (newest first). POST .../send/ {"message", "tenants": [ids], "landlords": [ids]} sends one
    personalised message to each person; {name}, {unit}, {balance}... in the text are filled in per recipient.
    """
    serializer_class = SmsMessageSerializer
    permission_classes = [RolePermission]
    read_roles = MESSAGING
    write_roles = MESSAGING
    filter_backends = [filters.SearchFilter]
    search_fields = ['recipient_name', 'phone', 'body']

    def get_queryset(self):
        return SmsMessage.objects.select_related('sent_by')

    @action(detail=False, methods=['get'])
    def provider(self, request):
        """Whether messages really leave the system. 'console' means test mode: they are only logged."""
        return Response({'provider': provider_name()})

    @action(detail=False, methods=['post'])
    def send(self, request):
        form = SendSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        d = form.validated_data
        result = send_batch(build_messages(d['message'], d['tenants'], d['landlords']), request.user)
        return Response(result, status=201)
