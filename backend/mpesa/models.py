from django.conf import settings
from django.db import models
from tenants.models import Tenant
from billing.models import Invoice


class MpesaTransaction(models.Model):
    """A payment received. Mostly M-Pesa paybill payments; staff also record cash, bank and cheque payments here so
    one settlement engine and one list cover every way a tenant pays."""
    CHANNEL_CHOICES = [('mpesa', 'M-Pesa'), ('cash', 'Cash'), ('bank', 'Bank transfer'), ('cheque', 'Cheque')]

    trans_id = models.CharField(max_length=100, unique=True)
    trans_time = models.CharField(max_length=50)
    trans_amount = models.DecimalField(max_digits=12, decimal_places=2)
    business_short_code = models.CharField(max_length=20)
    bill_ref_number = models.CharField(max_length=100)  # Contains Unit Code or Tenant ID
    msisdn = models.CharField(max_length=20)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_settled = models.BooleanField(default=False)
    # The payload exactly as Safaricom sent it, kept for audits and disputes.
    raw_payload = models.JSONField(default=dict, blank=True)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default='mpesa')
    # Manual payments: the bank account it went into, a free note, and who recorded it.
    paid_to = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )

    class Meta:
        ordering = ['-created_at']

    @property
    def status(self) -> str:
        return 'settled' if self.is_settled else 'unmatched'

    def __str__(self):
        return f"{self.trans_id} - {self.trans_amount} KES"


class PaymentSettlement(models.Model):
    # One payment can be split across several invoices, plus a leftover credit.
    transaction = models.ForeignKey(MpesaTransaction, on_delete=models.CASCADE, related_name='settlements')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='payments')
    # No invoice means the amount is unspent credit that is applied to the tenant's next invoice.
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, null=True, blank=True, related_name='settlements')
    amount_applied = models.DecimalField(max_digits=12, decimal_places=2)
    settled_at = models.DateTimeField(auto_now_add=True)
    # Empty when the system matched the payment itself; the staff member for a manual assignment.
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )

    class Meta:
        ordering = ['settled_at', 'id']

    def __str__(self):
        return f"Settlement: {self.transaction.trans_id} -> {self.tenant.name}"

class StkRequest(models.Model):
    """
    A PIN prompt we asked Safaricom to put on a tenant's phone. It starts pending; Safaricom's callback (or a status
    check) moves it to success, cancelled, failed or timeout. On success the payment is recorded and applied like any other.
    """
    STATUS_CHOICES = [
        ('pending', 'Waiting for the PIN'), ('success', 'Paid'), ('cancelled', 'Cancelled by the customer'),
        ('failed', 'Failed'), ('timeout', 'No response'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='stk_requests')
    phone = models.CharField(max_length=20)
    amount = models.PositiveIntegerField()
    account_reference = models.CharField(max_length=20)
    merchant_request_id = models.CharField(max_length=64, blank=True)
    checkout_request_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    result_desc = models.CharField(max_length=255, blank=True)
    receipt = models.CharField(max_length=30, blank=True)
    transaction = models.ForeignKey(MpesaTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='stk_requests')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"STK {self.checkout_request_id} {self.status}"
