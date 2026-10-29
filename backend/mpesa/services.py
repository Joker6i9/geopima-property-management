import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction

from billing.models import Invoice
from tenants.models import Tenant
from tenants.utils import normalize_phone_number
from .models import MpesaTransaction, PaymentSettlement

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
