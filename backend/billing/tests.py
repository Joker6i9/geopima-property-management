from datetime import date
from decimal import Decimal

from django.test import TestCase

from config_core.testing import make_tenant, make_unit
from mpesa.models import MpesaTransaction, PaymentSettlement
from tenants.models import LeaseAgreement
from .models import Invoice
from .services import generate_invoices, mark_overdue

OCT = date(2026, 10, 1)


class GenerateInvoicesTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='30000', water='800', garbage='300')
        self.tenant = make_tenant(self.unit, deposit='30000', due_date=5)

    def test_bills_rent_fees_and_deposit_on_the_first_invoice(self):
        result = generate_invoices(OCT, today=date(2026, 10, 2))
        self.assertEqual(len(result.created), 1)
        invoice = result.created[0]
        self.assertEqual(invoice.net_total, Decimal('61100'))
        self.assertEqual({i.item_type for i in invoice.items.all()}, {'Rent', 'Water', 'Garbage', 'Deposit'})
        self.assertEqual(invoice.due_date, date(2026, 10, 5))
        self.assertEqual(invoice.status, 'unpaid')

    def test_deposit_is_billed_only_once(self):
        generate_invoices(date(2026, 10, 1), today=OCT)
        november = generate_invoices(date(2026, 11, 1), today=date(2026, 11, 1)).created[0]
        self.assertEqual(november.net_total, Decimal('31100'))
        self.assertFalse(november.items.filter(item_type='Deposit').exists())

    def test_running_twice_creates_nothing_new(self):
        generate_invoices(OCT, today=OCT)
        again = generate_invoices(OCT, today=OCT)
        self.assertEqual((len(again.created), again.skipped), (0, 1))
        self.assertEqual(Invoice.objects.count(), 1)

    def test_invoice_numbers_are_unique_across_months_and_tenants(self):
        make_tenant(make_unit(name='R2'), name='Second', phone='0722000111')
        generate_invoices(date(2026, 9, 1), today=OCT)
        generate_invoices(date(2026, 10, 1), today=OCT)
        numbers = list(Invoice.objects.values_list('invoice_number', flat=True))
        self.assertEqual(len(numbers), 4)
        self.assertEqual(len(set(numbers)), 4)

    def test_skips_vacated_tenants_ended_and_future_leases(self):
        vacated = make_tenant(make_unit(name='R2'), name='Gone', phone='0722000111', status='vacated')
        ended = make_tenant(make_unit(name='R3'), name='Ended', phone='0722000222')
        LeaseAgreement.objects.filter(tenant=ended).update(end_date=date(2026, 9, 30))
        future = make_tenant(make_unit(name='R4'), name='Future', phone='0722000333', start=date(2026, 12, 1))
        generate_invoices(OCT, today=OCT)
        billed = set(Invoice.objects.values_list('tenant__name', flat=True))
        self.assertEqual(billed, {'Grace Wanjiku'})
        self.assertNotIn(vacated.name, billed | {future.name})

    def test_due_day_is_clamped_to_the_end_of_short_months(self):
        self.tenant.due_date = 31
        self.tenant.save()
        invoice = generate_invoices(date(2026, 2, 1), today=date(2026, 2, 1)).created[0]
        self.assertEqual(invoice.due_date, date(2026, 2, 28))

    def test_past_due_invoices_are_created_overdue(self):
        invoice = generate_invoices(date(2026, 8, 1), today=date(2026, 10, 1)).created[0]
        self.assertEqual(invoice.status, 'overdue')

    def test_mark_overdue_updates_unpaid_invoices_only(self):
        generate_invoices(OCT, today=OCT)
        self.assertEqual(mark_overdue(today=date(2026, 10, 6)), 1)
        self.assertEqual(Invoice.objects.get().status, 'overdue')

    def test_one_tenant_can_be_billed_alone(self):
        make_tenant(make_unit(name='R2'), name='Other', phone='0722000111')
        result = generate_invoices(OCT, today=OCT, tenant=self.tenant)
        self.assertEqual([i.tenant for i in result.created], [self.tenant])


class InvoiceBalanceTests(TestCase):
    def test_balance_and_status_follow_settlements(self):
        tenant = make_tenant(make_unit(rent='10000', water='0', garbage='0'))
        invoice = generate_invoices(OCT, today=OCT).created[0]
        tx = MpesaTransaction.objects.create(trans_id='T1', trans_time='', trans_amount=4000, business_short_code='1', bill_ref_number='', msisdn='')
        PaymentSettlement.objects.create(transaction=tx, tenant=tenant, invoice=invoice, amount_applied=4000)
        invoice = Invoice.objects.get(pk=invoice.pk)
        self.assertEqual((invoice.rent_paid, invoice.balance), (Decimal('4000'), Decimal('6000')))
        self.assertEqual(invoice.refresh_status(today=OCT), 'partially_paid')
