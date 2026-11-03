import calendar
import uuid
from datetime import date
from decimal import Decimal
from django.db import models, transaction


class Agency(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, default="PEMWA AGENCY")
    address = models.CharField(max_length=255, default="1254-00200, GPO Nairobi")
    email = models.EmailField(default="pemwaagency@yahoo.com")
    contact_numbers = models.CharField(max_length=100, default="0725768616 | 0101768816")

    class Meta:
        verbose_name_plural = "Agencies"

    def __str__(self):
        return self.name


class VoteHead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    title = models.CharField(max_length=100, blank=True, null=True)
    code = models.CharField(max_length=30, unique=True)
    description = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    priority_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['priority_order']

    def __str__(self):
        return f"{self.code} - {self.name}"


class Deposit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deposit_code = models.CharField(max_length=50, unique=True)
    reference_no = models.CharField(max_length=50, blank=True, null=True)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.PROTECT, related_name='deposits')
    unit = models.ForeignKey('properties.Unit', on_delete=models.PROTECT, related_name='deposits')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default='held')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.deposit_code} - {self.tenant}"


class InvoiceSequence(models.Model):
    """One row that hands out invoice numbers. Locked while incrementing so two requests never get the same number."""
    name = models.CharField(max_length=20, primary_key=True, default='invoice')
    last_value = models.PositiveBigIntegerField(default=0)

    @classmethod
    def next_number(cls) -> str:
        with transaction.atomic():
            seq, created = cls.objects.select_for_update().get_or_create(name='invoice')
            if created:
                # Continue after any numbers issued before this table existed.
                existing = Invoice.objects.filter(invoice_number__startswith='INV').values_list('invoice_number', flat=True)
                seq.last_value = max((int(n[3:]) for n in existing if n[3:].isdigit()), default=0)
            seq.last_value += 1
            seq.save()
            return f"INV{seq.last_value:06d}"


class Invoice(models.Model):
    STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=30, unique=True, editable=False)
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.PROTECT, related_name='invoices')
    unit = models.ForeignKey('properties.Unit', on_delete=models.PROTECT, related_name='invoices')
    
    billing_period = models.DateField()
    issue_date = models.DateField(auto_now_add=True)
    due_date = models.DateField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='unpaid')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'billing_invoice'
        unique_together = ('tenant', 'unit', 'billing_period')
        ordering = ['-billing_period', '-created_at']

    @property
    def period_formatted(self) -> str:
        return self.billing_period.strftime('%B-%Y').upper()

    @property
    def subtotal(self) -> Decimal:
        # Iterating .all() uses prefetched rows when the caller did prefetch_related, so lists stay one query.
        return sum((item.amount for item in self.items.all()), Decimal('0.00'))

    @property
    def net_total(self) -> Decimal:
        return self.subtotal

    @property
    def rent_paid(self) -> Decimal:
        """Sum of everything settled against this invoice."""
        return sum((s.amount_applied for s in self.settlements.all()), Decimal('0.00'))

    @property
    def balance(self) -> Decimal:
        return self.net_total - self.rent_paid

    def refresh_status(self, today: date | None = None, save: bool = True) -> str:
        """Sets paid / partially_paid / overdue / unpaid from the current balance and due date."""
        today = today or date.today()
        paid, total = self.rent_paid, self.net_total
        if total > 0 and paid >= total:
            self.status = 'paid'
        elif paid > 0:
            self.status = 'partially_paid'
        elif self.due_date < today:
            self.status = 'overdue'
        else:
            self.status = 'unpaid'
        if save:
            self.save(update_fields=['status'])
        return self.status

    @staticmethod
    def due_date_for(period: date, due_day: int) -> date:
        """The tenant's rent due day inside the billing month (clamped, so day 31 works in February)."""
        last = calendar.monthrange(period.year, period.month)[1]
        return period.replace(day=min(max(due_day, 1), last))

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = InvoiceSequence.next_number()
        super().save(*args, **kwargs)


class InvoiceItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    item_type = models.CharField(max_length=50, default="Rent")
    description = models.CharField(max_length=255, default="House Rent")
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.item_type}: {self.amount}"