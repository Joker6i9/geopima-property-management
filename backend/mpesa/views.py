import hmac
import json
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import filters, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config_core.permissions import FINANCE, RolePermission
from .models import MpesaTransaction
from .serializers import AssignSerializer, MpesaTransactionSerializer
from .services import InvalidPayload, MpesaSettlementEngine

logger = logging.getLogger(__name__)


def secret_is_valid(secret: str) -> bool:
    """
    The callback URLs contain a secret that only Safaricom and we know, so strangers cannot post fake payments.
    With no secret configured the callbacks are closed, except in DEBUG where they are open for local testing.
    """
    configured = getattr(settings, 'MPESA_CALLBACK_SECRET', '')
    if not configured:
        return settings.DEBUG
    return hmac.compare_digest(secret.encode(), configured.encode())


class CallbackView(View):
    def dispatch(self, request, secret, *args, **kwargs):
        if not secret_is_valid(secret):
            raise Http404  # same answer as an unknown URL, so the endpoint is not advertised
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name='dispatch')
class MpesaValidationView(CallbackView):
    """Safaricom asks whether to accept a payment before taking the money. We accept everything and sort it out
    afterwards, so a mistyped account number never blocks a tenant's payment."""

    def post(self, request, *args, **kwargs):
        return JsonResponse({"ResultCode": "0", "ResultDesc": "Accepted"})


@method_decorator(csrf_exempt, name='dispatch')
class MpesaConfirmationView(CallbackView):
    """Safaricom reports a completed payment. It is stored (with the raw payload) and settled if the tenant is clear."""

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode('utf-8'))
            MpesaSettlementEngine.process_payment(payload)
        except (ValueError, UnicodeDecodeError, InvalidPayload):
            logger.warning('Rejected malformed M-Pesa confirmation')
            return JsonResponse({"ResultCode": "C2B00016", "ResultDesc": "Rejected"}, status=400)
        except Exception:
            logger.exception('M-Pesa confirmation failed')
            return JsonResponse({"ResultCode": "1", "ResultDesc": "Failed"}, status=500)
        return JsonResponse({"ResultCode": "0", "ResultDesc": "Success"})


class MpesaTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Received payments. Filter with ?status=settled|unmatched. Staff assign an unmatched payment to a tenant with
    POST .../<id>/assign/ {"tenant": <id>}; it is applied to that tenant's oldest open invoices first.
    """
    serializer_class = MpesaTransactionSerializer
    permission_classes = [RolePermission]
    read_roles = FINANCE
    write_roles = FINANCE
    filter_backends = [filters.SearchFilter]
    search_fields = ['trans_id', 'bill_ref_number', 'msisdn', 'first_name', 'last_name']

    def get_queryset(self):
        queryset = MpesaTransaction.objects.prefetch_related('settlements__tenant', 'settlements__invoice')
        status = self.request.query_params.get('status')
        if status == 'settled':
            queryset = queryset.filter(is_settled=True)
        elif status == 'unmatched':
            queryset = queryset.filter(is_settled=False)
        return queryset

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        tx = self.get_object()
        if tx.is_settled:
            raise serializers.ValidationError({'detail': 'This payment has already been matched.'})
        form = AssignSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        MpesaSettlementEngine.settle_transaction(tx, tenant=form.validated_data['tenant'], assigned_by=request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=tx.pk)).data)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Tries the automatic match again, e.g. after fixing a tenant's phone number."""
        tx = self.get_object()
        matched = MpesaSettlementEngine.settle_transaction(tx)
        data = self.get_serializer(self.get_queryset().get(pk=tx.pk)).data
        return Response({'matched': matched, 'transaction': data})
