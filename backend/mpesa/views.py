import hmac
import json
import logging
import uuid

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import filters, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from rest_framework.views import APIView

from config_core.permissions import ADMINS, FINANCE, RolePermission
from .models import MpesaTransaction
from . import daraja
from .models import StkRequest
from .serializers import AssignSerializer, StkRequestSerializer, StkStartSerializer, MpesaTransactionSerializer, RecordPaymentSerializer
from .services import InvalidPayload, MpesaSettlementEngine, apply_stk_callback, refresh_stk, start_stk

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

    @action(detail=False, methods=['post'])
    def record(self, request):
        """Records a cash, bank or cheque payment against a tenant and applies it to their oldest open invoices."""
        form = RecordPaymentSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        d = form.validated_data
        tenant = d['tenant']
        stamp = timezone.localtime().strftime('%H%M%S')
        tx = MpesaTransaction.objects.create(
            trans_id=d.get('reference') or f"CASH-{uuid.uuid4().hex[:8].upper()}",
            trans_time=f"{d['date']:%Y%m%d}{stamp}",
            trans_amount=d['amount'],
            bill_ref_number=tenant.tenant_id,
            msisdn=tenant.phone_number,
            channel=d['channel'],
            paid_to=d.get('paid_to', ''),
            note=d.get('note', ''),
            recorded_by=request.user,
        )
        MpesaSettlementEngine.settle_transaction(tx, tenant=tenant, assigned_by=request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=tx.pk)).data, status=201)

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Tries the automatic match again, e.g. after fixing a tenant's phone number."""
        tx = self.get_object()
        matched = MpesaSettlementEngine.settle_transaction(tx)
        data = self.get_serializer(self.get_queryset().get(pk=tx.pk)).data
        return Response({'matched': matched, 'transaction': data})



@method_decorator(csrf_exempt, name='dispatch')
class StkCallbackView(CallbackView):
    """Safaricom's answer to a PIN prompt. Always acknowledged, so Safaricom does not keep retrying."""

    def post(self, request, *args, **kwargs):
        try:
            apply_stk_callback(json.loads(request.body.decode('utf-8')))
        except (ValueError, UnicodeDecodeError, InvalidPayload):
            logger.warning('Ignored malformed STK callback')
        except Exception:
            logger.exception('STK callback failed')
            return JsonResponse({"ResultCode": 1, "ResultDesc": "Failed"}, status=500)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


class StkPushView(APIView):
    """Staff send a PIN prompt to a tenant's phone: POST {"tenant", "amount", "phone"?}. Phone defaults to the tenant's."""
    permission_classes = [RolePermission]
    write_roles = FINANCE

    def post(self, request):
        form = StkStartSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        d = form.validated_data
        stk = start_stk(tenant=d['tenant'], phone=d.get('phone') or d['tenant'].phone_number, amount=d['amount'], user=request.user)
        return Response(StkRequestSerializer(stk).data, status=201)


class StkStatusView(APIView):
    permission_classes = [RolePermission]
    read_roles = FINANCE

    def get(self, request, pk):
        stk = refresh_stk(get_object_or_404(StkRequest, pk=pk))
        return Response(StkRequestSerializer(stk).data)


class IntegrationStatusView(APIView):
    """What is and is not set up for real M-Pesa. Never returns a secret, only whether each one is present."""
    permission_classes = [RolePermission]
    read_roles = ADMINS
    write_roles = ADMINS

    def get(self, request):
        public = settings.MPESA_PUBLIC_URL
        return Response({
            'environment': settings.MPESA_ENV,
            'paybill': settings.MPESA_PAYBILL,
            'missing_for_urls': daraja.missing_settings(),
            'missing_for_prompts': daraja.missing_settings(stk=True),
            'https': public.startswith('https://') if public else False,
            'callback_urls': {
                'confirmation': daraja.callback_url('confirmation').replace(settings.MPESA_CALLBACK_SECRET, '••••') if public and settings.MPESA_CALLBACK_SECRET else '',
                'validation': daraja.callback_url('validation').replace(settings.MPESA_CALLBACK_SECRET, '••••') if public and settings.MPESA_CALLBACK_SECRET else '',
            },
            'last_payment_at': MpesaTransaction.objects.filter(channel='mpesa').values_list('created_at', flat=True).first(),
        })

    def post(self, request):
        """Registers the paybill's confirmation and validation URLs with Safaricom."""
        try:
            daraja.register_urls()
        except daraja.DarajaError as e:
            raise serializers.ValidationError({'detail': str(e)})
        return Response({'registered': True})
