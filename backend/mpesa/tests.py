import json
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from billing.models import Invoice
from billing.services import generate_invoices
from config_core.permissions import FINANCE
from config_core.testing import client_for, make_property, make_tenant, make_unit, make_user
from .models import MpesaTransaction, PaymentSettlement
from .services import InvalidPayload, MpesaSettlementEngine


def payload(trans_id='SJ1', amount='10000', ref='R1', msisdn='254712345678', **extra):
    return {
        'TransID': trans_id, 'TransAmount': amount, 'TransTime': '20261003101500', 'BusinessShortCode': '522522',
        'BillRefNumber': ref, 'MSISDN': msisdn, 'FirstName': 'Grace', 'LastName': 'W', **extra,
    }


class SettlementTests(TestCase):
    def setUp(self):
        # Rent only, so the numbers below are easy to follow: 10,000 per month.
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, phone='0712345678')
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))
        generate_invoices(date(2026, 9, 1), today=date(2026, 9, 1))
        self.aug, self.sep = Invoice.objects.order_by('billing_period')

    def refresh(self):
        return Invoice.objects.get(pk=self.aug.pk), Invoice.objects.get(pk=self.sep.pk)

    def test_exact_payment_clears_the_oldest_invoice(self):
        tx = MpesaSettlementEngine.process_payment(payload(amount='10000'))
        aug, sep = self.refresh()
        self.assertTrue(tx.is_settled)
        self.assertEqual((aug.status, sep.balance), ('paid', Decimal('10000')))

    def test_payment_is_split_across_invoices_oldest_first(self):
        MpesaSettlementEngine.process_payment(payload(amount='15000'))
        aug, sep = self.refresh()
        self.assertEqual((aug.balance, sep.balance), (Decimal('0'), Decimal('5000')))
        self.assertEqual((aug.status, sep.status), ('paid', 'partially_paid'))
        self.assertEqual(PaymentSettlement.objects.filter(transaction__trans_id='SJ1').count(), 2)

    def test_overpayment_is_kept_as_credit_and_used_on_the_next_invoice(self):
        MpesaSettlementEngine.process_payment(payload(amount='23000'))
        self.assertEqual(MpesaSettlementEngine.credit_balance(self.tenant), Decimal('3000'))
        generate_invoices(date(2026, 10, 1), today=date(2026, 10, 1))
        october = Invoice.objects.get(billing_period=date(2026, 10, 1))
        self.assertEqual((october.balance, october.status), (Decimal('7000'), 'partially_paid'))
        self.assertEqual(MpesaSettlementEngine.credit_balance(self.tenant), Decimal('0'))

    def test_same_transaction_delivered_twice_is_applied_once(self):
        MpesaSettlementEngine.process_payment(payload(amount='10000'))
        MpesaSettlementEngine.process_payment(payload(amount='10000'))
        self.assertEqual(MpesaTransaction.objects.count(), 1)
        self.assertEqual(PaymentSettlement.objects.count(), 1)

    def test_payment_with_unknown_reference_stays_unmatched(self):
        tx = MpesaSettlementEngine.process_payment(payload(ref='NOPE', msisdn='254799999999'))
        self.assertFalse(tx.is_settled)
        self.assertEqual(PaymentSettlement.objects.count(), 0)
        self.assertEqual(tx.raw_payload['BillRefNumber'], 'NOPE')

    def test_matches_by_tenant_id_and_unit_name_ignoring_case(self):
        MpesaSettlementEngine.process_payment(payload('A', '1000', ref=self.tenant.tenant_id.lower(), msisdn=''))
        MpesaSettlementEngine.process_payment(payload('B', '1000', ref='r1', msisdn=''))
        self.assertEqual(PaymentSettlement.objects.filter(tenant=self.tenant).count(), 2)

    def test_falls_back_to_the_sender_phone_when_the_reference_is_blank(self):
        tx = MpesaSettlementEngine.process_payment(payload(ref='', msisdn='0712345678'))
        self.assertTrue(tx.is_settled)

    def test_same_unit_name_in_two_properties_is_not_guessed(self):
        other = make_tenant(make_unit(make_property('Other'), name='R1'), name='Other', phone='0722000111')
        generate_invoices(date(2026, 9, 1), today=date(2026, 9, 1))
        ambiguous = MpesaSettlementEngine.process_payment(payload('X', '500', ref='R1', msisdn='254799999999'))
        self.assertFalse(ambiguous.is_settled)
        # The sender's phone settles which R1 it was.
        narrowed = MpesaSettlementEngine.process_payment(payload('Y', '500', ref='R1', msisdn='254722000111'))
        self.assertTrue(narrowed.is_settled)
        self.assertTrue(PaymentSettlement.objects.filter(transaction=narrowed, tenant=other).exists())

    def test_a_vacated_tenants_unit_is_not_matched(self):
        self.tenant.status = 'vacated'
        self.tenant.save()
        tx = MpesaSettlementEngine.process_payment(payload(ref='R1', msisdn=''))
        self.assertFalse(tx.is_settled)

    def test_invalid_payloads_are_rejected(self):
        for bad in (payload(trans_id=''), payload(amount='abc'), payload(amount='0'), payload(amount='-5')):
            with self.assertRaises(InvalidPayload):
                MpesaSettlementEngine.process_payment(bad)
        self.assertEqual(MpesaTransaction.objects.count(), 0)


@override_settings(MPESA_CALLBACK_SECRET='s3cret')
class WebhookTests(TestCase):
    def post(self, secret, body, kind='confirmation'):
        url = reverse(f'mpesa:c2b_{kind}', args=[secret])
        return self.client.post(url, data=body if isinstance(body, str) else json.dumps(body), content_type='application/json')

    def setUp(self):
        make_tenant(make_unit(name='R1', rent='10000', water='0', garbage='0'))
        generate_invoices(date(2026, 10, 1), today=date(2026, 10, 1))

    def test_wrong_secret_looks_like_an_unknown_url(self):
        self.assertEqual(self.post('wrong', payload()).status_code, 404)
        self.assertEqual(MpesaTransaction.objects.count(), 0)

    def test_correct_secret_stores_and_settles_the_payment(self):
        response = self.post('s3cret', payload())
        self.assertEqual(response.json()['ResultCode'], '0')
        self.assertTrue(MpesaTransaction.objects.get().is_settled)

    def test_validation_endpoint_accepts(self):
        self.assertEqual(self.post('s3cret', payload(), 'validation').json()['ResultCode'], '0')

    def test_malformed_body_is_rejected_without_leaking_details(self):
        response = self.post('s3cret', '{not json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['ResultDesc'], 'Rejected')

    @override_settings(MPESA_CALLBACK_SECRET='')
    def test_closed_when_no_secret_is_configured(self):
        self.assertEqual(self.post('anything', payload()).status_code, 404)


class TransactionApiTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit)
        generate_invoices(date(2026, 10, 1), today=date(2026, 10, 1))
        self.tx = MpesaSettlementEngine.process_payment(payload(ref='???', msisdn='254799999999', amount='4000'))
        self.accountant = make_user('Accountant')

    def test_unmatched_filter_and_assign(self):
        client = client_for(self.accountant)
        listed = client.get('/api/v1/mpesa/transactions/?status=unmatched').json()['results']
        self.assertEqual([t['trans_id'] for t in listed], ['SJ1'])
        self.assertEqual(listed[0]['status'], 'unmatched')

        response = client.post(f'/api/v1/mpesa/transactions/{self.tx.pk}/assign/', {'tenant': self.tenant.pk}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'settled')
        self.assertEqual(response.json()['tenant']['id'], self.tenant.pk)
        settlement = PaymentSettlement.objects.get()
        self.assertEqual(settlement.assigned_by, self.accountant)
        self.assertEqual(Invoice.objects.get().status, 'partially_paid')

    def test_assigning_twice_is_refused(self):
        client = client_for(self.accountant)
        url = f'/api/v1/mpesa/transactions/{self.tx.pk}/assign/'
        client.post(url, {'tenant': self.tenant.pk}, format='json')
        self.assertEqual(client.post(url, {'tenant': self.tenant.pk}, format='json').status_code, 400)

    def test_only_finance_roles_can_see_payments(self):
        for role in ('Secretary', 'Field Agent', None):
            self.assertEqual(client_for(make_user(role)).get('/api/v1/mpesa/transactions/').status_code, 403, role)
        self.assertEqual(client_for().get('/api/v1/mpesa/transactions/').status_code, 401)
        for role in FINANCE:
            self.assertEqual(client_for(make_user(role)).get('/api/v1/mpesa/transactions/').status_code, 200, role)
