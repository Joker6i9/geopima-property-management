import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers as drf_serializers

from billing.models import Invoice
from tenants.models import Tenant
from tenants.utils import normalize_phone_number
from . import daraja
from .models import MpesaTransaction, PaymentSettlement, StkRequest

logger = logging.getLogger(__name__)


class InvalidPayload(ValueError):
    """The callback did not contain a usable transaction."""


def open_invoices(tenant: Tenant):
    """The tenant's unpaid invoices, oldest due date first, locked for the rest of the transaction."""
    return (
        Invoice.objects.select_for_update()
        .filter(tenant=tenant)
        .exclude(status='paid')
        .order_by('due_date', 'billing_period', 'created_at')
    )


class MpesaSettlementEngine:

    @classmethod
    def process_payment(cls, payload: dict) -> MpesaTransaction:
        """
        Stores an incoming C2B confirmation, then tries to settle it. The transaction is saved first and on its own,
        so a problem while matching never loses the payment: it stays unmatched for staff to assign. Re-delivery of
        the same TransID is harmless.
        """
        trans_id = str(payload.get('TransID') or '').strip()
        try:
            amount = Decimal(str(payload.get('TransAmount')))
        except (InvalidOperation, TypeError):
            raise InvalidPayload('TransAmount is not a number.')
        if not trans_id:
            raise InvalidPayload('TransID is missing.')
        if amount <= 0:
            raise InvalidPayload('TransAmount must be positive.')

        tx, _ = MpesaTransaction.objects.get_or_create(
            trans_id=trans_id,
            defaults={
                'trans_time': str(payload.get('TransTime', '')),
                'trans_amount': amount,
                'business_short_code': str(payload.get('BusinessShortCode', '')),
                'bill_ref_number': str(payload.get('BillRefNumber', '')).strip(),
                'msisdn': str(payload.get('MSISDN', '')),
                'first_name': str(payload.get('FirstName', '')),
                'last_name': str(payload.get('LastName', '')),
                'raw_payload': payload,
            },
        )
        if not tx.is_settled:
            try:
                cls.settle_transaction(tx)
            except Exception:
                logger.exception('Settlement failed for M-Pesa transaction %s; left unmatched.', trans_id)
        return tx

    @classmethod
    def find_tenant(cls, tx: MpesaTransaction) -> Tenant | None:
        """
        Matches a payment to exactly one tenant, or returns None. In order: the tenant ID typed as the account,
        the unit name typed as the account, then the sender's phone. If a step finds more than one candidate (the same
        unit name in two properties) it narrows by the sender's phone, and gives up rather than guess.
        """
        ref = tx.bill_ref_number.strip()
        phone = normalize_phone_number(tx.msisdn)
        current = Tenant.objects.exclude(status='vacated')

        if ref:
            by_id = Tenant.objects.filter(tenant_id__iexact=ref).first()
            if by_id:
                return by_id
            by_unit = list(current.filter(unit__unit_name__iexact=ref))
            if len(by_unit) == 1:
                return by_unit[0]
            if len(by_unit) > 1:
                narrowed = [t for t in by_unit if phone and t.phone_number == phone]
                return narrowed[0] if len(narrowed) == 1 else None

        by_phone = list(current.filter(phone_number=phone)) if phone else []
        return by_phone[0] if len(by_phone) == 1 else None

    @classmethod
    @transaction.atomic
    def settle_transaction(cls, tx: MpesaTransaction, tenant: Tenant | None = None, assigned_by=None) -> bool:
        """
        Applies the payment to the tenant's open invoices, oldest first. Anything left over is kept as credit and used
        on the next invoice. `tenant` and `assigned_by` are for a manual assignment by staff. Returns False if no
        tenant could be matched.
        """
        caller_tx, tx = tx, MpesaTransaction.objects.select_for_update().get(pk=tx.pk)
        caller_tx.is_settled = tx.is_settled
        if tx.is_settled:
            return True
        tenant = tenant or cls.find_tenant(tx)
        if tenant is None:
            return False

        remaining = tx.trans_amount
        for invoice in open_invoices(tenant):
            if remaining <= 0:
                break
            due = invoice.balance
            if due <= 0:
                invoice.refresh_status()
                continue
            applied = min(remaining, due)
            PaymentSettlement.objects.create(
                transaction=tx, tenant=tenant, invoice=invoice, amount_applied=applied, assigned_by=assigned_by
            )
            invoice.refresh_status()
            remaining -= applied

        if remaining > 0:
            PaymentSettlement.objects.create(
                transaction=tx, tenant=tenant, invoice=None, amount_applied=remaining, assigned_by=assigned_by
            )

        tx.is_settled = caller_tx.is_settled = True
        tx.save(update_fields=['is_settled'])
        # After the payment is committed, tell the tenant. Only the first settlement of a payment gets here.
        from communication.services import notify_payment_received
        transaction.on_commit(lambda: notify_payment_received(tx.pk, tenant.pk))
        return True

    @classmethod
    @transaction.atomic
    def apply_credits(cls, tenant: Tenant) -> None:
        """Uses a tenant's unspent credit (overpayments) on their open invoices."""
        credits = list(PaymentSettlement.objects.select_for_update().filter(tenant=tenant, invoice__isnull=True))
        for credit in credits:
            for invoice in open_invoices(tenant):
                if credit.amount_applied <= 0:
                    break
                due = invoice.balance
                if due <= 0:
                    continue
                applied = min(credit.amount_applied, due)
                PaymentSettlement.objects.create(
                    transaction=credit.transaction, tenant=tenant, invoice=invoice,
                    amount_applied=applied, assigned_by=credit.assigned_by,
                )
                credit.amount_applied -= applied
                invoice.refresh_status()
            if credit.amount_applied <= 0:
                credit.delete()
            else:
                credit.save(update_fields=['amount_applied'])

    @staticmethod
    def credit_balance(tenant: Tenant) -> Decimal:
        return sum(
            (s.amount_applied for s in PaymentSettlement.objects.filter(tenant=tenant, invoice__isnull=True)),
            Decimal('0.00'),
        )


# ---------------------------------------------------------------- STK push (PIN prompt on the tenant's phone)

STK_MIN, STK_MAX = 1, 150_000  # KES per prompt
STK_WAIT = timedelta(seconds=90)      # while a prompt is waiting, do not send another to the same tenant
STK_GIVE_UP = timedelta(minutes=5)    # after this with no word from Safaricom the prompt is treated as lapsed

# Safaricom result codes worth telling the customer about.
CANCELLED_CODES = {1032}
TIMEOUT_CODES = {1037}
# A status check answering 4999 means Safaricom is still waiting for the customer: not a result yet.
IN_PROGRESS_CODES = {4999}


def start_stk(*, tenant: Tenant, phone: str, amount: int, user=None) -> StkRequest:
    phone = normalize_phone_number(phone)
    if len(phone) != 12 or not phone.startswith('254'):
        raise drf_serializers.ValidationError({'phone': 'Enter a valid Safaricom number, e.g. 0712 345 678.'})
    if not STK_MIN <= amount <= STK_MAX:
        raise drf_serializers.ValidationError({'amount': f'Enter an amount from {STK_MIN} to {STK_MAX:,} KES.'})
    recent = StkRequest.objects.filter(tenant=tenant, status='pending', created_at__gte=timezone.now() - STK_WAIT).exists()
    if recent:
        raise drf_serializers.ValidationError({'detail': 'A payment request was just sent to the phone. Enter the PIN there, or wait a minute to try again.'})

    reference = (tenant.unit.unit_name if tenant.unit else tenant.tenant_id)[:12]
    try:
        reply = daraja.stk_push(phone=phone, amount=amount, account_reference=reference)
    except daraja.DarajaError as e:
        raise drf_serializers.ValidationError({'detail': str(e)})
    return StkRequest.objects.create(
        tenant=tenant, phone=phone, amount=amount, account_reference=reference,
        merchant_request_id=reply.get('MerchantRequestID', ''), checkout_request_id=reply['CheckoutRequestID'],
        requested_by=user if getattr(user, 'is_authenticated', False) else None,
    )


def apply_stk_callback(payload: dict) -> StkRequest | None:
    """
    Safaricom's answer to a PIN prompt. Matched by CheckoutRequestID, so only prompts we sent can ever be settled, and
    the tenant is the one we asked, not anything the callback says. Safe to receive twice.
    """
    try:
        cb = payload['Body']['stkCallback']
        checkout_id = str(cb['CheckoutRequestID'])
        code = int(cb.get('ResultCode', -1))
    except (KeyError, TypeError, ValueError):
        raise InvalidPayload('Not an STK callback.')

    with transaction.atomic():
        stk = StkRequest.objects.select_for_update().select_related('tenant').filter(checkout_request_id=checkout_id).first()
        # A paid prompt is final. Any other state can still be overtaken by a late success: the customer may enter the
        # PIN after we gave up waiting, and that money must not be lost.
        if stk is None or stk.status == 'success':
            return stk
        if code != 0:
            if stk.status != 'pending':
                return stk  # a late failure must not overwrite what we already recorded
            stk.status = 'cancelled' if code in CANCELLED_CODES else 'timeout' if code in TIMEOUT_CODES else 'failed'
            stk.result_desc = str(cb.get('ResultDesc', ''))[:255]
            stk.save(update_fields=['status', 'result_desc', 'updated_at'])
            return stk

        items = {i.get('Name'): i.get('Value') for i in (cb.get('CallbackMetadata') or {}).get('Item', [])}
        receipt = str(items.get('MpesaReceiptNumber') or '').strip()
        try:
            amount = Decimal(str(items.get('Amount')))
        except (InvalidOperation, TypeError):
            amount = Decimal(stk.amount)
        if not receipt:
            raise InvalidPayload('Successful STK callback without a receipt number.')

        # The paybill's own C2B confirmation for the same payment may already have stored it; either order works.
        tx, _ = MpesaTransaction.objects.get_or_create(
            trans_id=receipt,
            defaults={
                'trans_time': str(items.get('TransactionDate', '')), 'trans_amount': amount,
                'business_short_code': str(settings.MPESA_PAYBILL), 'bill_ref_number': stk.account_reference,
                'msisdn': str(items.get('PhoneNumber', stk.phone)), 'raw_payload': payload,
            },
        )
        stk.status, stk.receipt, stk.transaction, stk.result_desc = 'success', receipt, tx, str(cb.get('ResultDesc', ''))[:255]
        stk.save(update_fields=['status', 'receipt', 'transaction', 'result_desc', 'updated_at'])
        MpesaSettlementEngine.settle_transaction(tx, tenant=stk.tenant)
        return stk


def refresh_stk(stk: StkRequest) -> StkRequest:
    """
    Called when someone checks on a pending prompt. If the callback never came (server was down, URL not reachable)
    this asks Safaricom directly, and lets the prompt lapse after a few minutes so nobody waits forever. A paid prompt
    needs the callback for its receipt number, so it is never closed here; the paybill confirmation will cover it.
    """
    if stk.status != 'pending' or timezone.now() - stk.created_at < timedelta(seconds=20):
        return stk
    try:
        answer = daraja.stk_query(stk.checkout_request_id)
        code = int(answer.get('ResultCode', 0))
    except (daraja.DarajaError, ValueError, TypeError):
        code = 0  # still processing, or Safaricom is unreachable: keep waiting
    if code not in IN_PROGRESS_CODES and code != 0:
        stk.status = 'cancelled' if code in CANCELLED_CODES else 'timeout' if code in TIMEOUT_CODES else 'failed'
        stk.result_desc = str(answer.get('ResultDesc', ''))[:255]
        stk.save(update_fields=['status', 'result_desc', 'updated_at'])
    elif timezone.now() - stk.created_at > STK_GIVE_UP:
        stk.status, stk.result_desc = 'timeout', 'No answer from Safaricom.'
        stk.save(update_fields=['status', 'result_desc', 'updated_at'])
    return stk
