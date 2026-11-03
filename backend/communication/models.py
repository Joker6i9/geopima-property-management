import uuid

from django.conf import settings
from django.db import models


class SmsMessage(models.Model):
    """One text message to one person. Messages sent together share a batch id."""
    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        # No SMS provider is configured, so nothing left the building. Lets the screens work before go-live.
        ('test', 'Test only'),
    ]

    batch = models.UUIDField(default=uuid.uuid4, db_index=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.SET_NULL, null=True, blank=True, related_name='sms')
    landlord = models.ForeignKey('properties.Landlord', on_delete=models.SET_NULL, null=True, blank=True, related_name='sms')
    recipient_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error = models.CharField(max_length=255, blank=True)
    # The M-Pesa/bank reference of the payment this receipt is for. Stops a payment being texted about twice.
    payment_ref = models.CharField(max_length=100, blank=True, db_index=True)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.recipient_name}: {self.body[:30]}"
