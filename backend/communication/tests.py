from datetime import date

from django.test import TestCase, override_settings
from django.urls import reverse

from billing.services import generate_invoices
from config_core.testing import client_for, make_tenant, make_unit, make_user
from .models import SmsMessage


@override_settings(MPESA_PAYBILL='522522')
class SendSmsTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, name='Grace Wanjiku', phone='0712345678')
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))
        self.client = client_for(make_user('Secretary'))
        self.url = reverse('communication:message-send')

    def test_message_is_personalised_and_logged(self):
        res = self.client.post(self.url, {'message': 'Hi {first_name}, {unit} owes {balance}. Pay to {paybill}.', 'tenants': [self.tenant.pk]}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['test'], 1)
        msg = SmsMessage.objects.get()
        self.assertEqual(msg.body, 'Hi Grace, R1 owes KES 10,000. Pay to 522522.')
        self.assertEqual((msg.phone, msg.status), ('254712345678', 'test'))

    def test_unknown_placeholder_and_no_recipients_are_rejected(self):
        self.assertEqual(self.client.post(self.url, {'message': 'Hi {nope}', 'tenants': [self.tenant.pk]}, format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url, {'message': 'Hi', 'tenants': []}, format='json').status_code, 400)

    def test_tenant_without_a_phone_is_skipped(self):
        self.tenant.phone_number = ''
        self.tenant.save()
        res = self.client.post(self.url, {'message': 'Hi', 'tenants': [self.tenant.pk]}, format='json')
        self.assertEqual((res.data['skipped'], SmsMessage.objects.count()), (1, 0))

    def test_field_agents_cannot_send(self):
        agent = client_for(make_user('Field Agent'))
        self.assertEqual(agent.post(self.url, {'message': 'Hi', 'tenants': [self.tenant.pk]}, format='json').status_code, 403)

    def test_log_and_provider_endpoints(self):
        self.client.post(self.url, {'message': 'Hi', 'tenants': [self.tenant.pk]}, format='json')
        self.assertEqual(len(self.client.get(reverse('communication:message-list')).data['results']), 1)
        self.assertEqual(self.client.get(reverse('communication:message-provider')).data, {'provider': 'console'})


from config_core.models import SMSTemplate
from mpesa.models import MpesaTransaction
from mpesa.services import MpesaSettlementEngine


@override_settings(MPESA_PAYBILL='522522', SMS_BACKGROUND=False)
class PaymentReceiptTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, name='Grace Wanjiku', phone='0712345678')
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))

    def pay(self, trans_id='RCP1', amount='4000'):
        with self.captureOnCommitCallbacks(execute=True):
            return MpesaSettlementEngine.process_payment({
                'TransID': trans_id, 'TransAmount': amount, 'TransTime': '20261102101500', 'BusinessShortCode': '522522',
                'BillRefNumber': 'R1', 'MSISDN': '254712345678', 'FirstName': 'G', 'LastName': 'W',
            })

    def test_tenant_gets_a_receipt_with_amount_reference_and_new_balance(self):
        self.pay()
        msg = SmsMessage.objects.get()
        self.assertEqual(msg.phone, '254712345678')
        self.assertEqual(msg.payment_ref, 'RCP1')
        self.assertIn('KES 4,000', msg.body)
        self.assertIn('RCP1', msg.body)
        self.assertIn('2 Nov 2026', msg.body)
        self.assertIn('KES 6,000', msg.body)  # 10,000 - 4,000

    def test_a_payment_is_only_texted_about_once(self):
        self.pay()
        self.pay()  # Safaricom re-delivering the same confirmation
        self.assertEqual(SmsMessage.objects.count(), 1)

    def test_unmatched_payments_send_nothing_until_assigned(self):
        with self.captureOnCommitCallbacks(execute=True):
            MpesaSettlementEngine.process_payment({
                'TransID': 'RCP9', 'TransAmount': '500', 'TransTime': '20261102101500', 'BusinessShortCode': '522522',
                'BillRefNumber': 'nope', 'MSISDN': '254799999999',
            })
        self.assertEqual(SmsMessage.objects.count(), 0)

    def test_uses_the_agency_template_and_can_be_switched_off(self):
        SMSTemplate.objects.create(category='PAYMENT_NOTIF', template_text='Thanks {first_name}: {amount} received, ref {receipt}.')
        self.pay('RCP2')
        self.assertEqual(SmsMessage.objects.get().body, 'Thanks Grace: KES 4,000 received, ref RCP2.')
        SMSTemplate.objects.update(enabled=False)
        self.pay('RCP3', '1000')
        self.assertEqual(SmsMessage.objects.count(), 1)

    def test_a_template_with_an_unknown_word_falls_back_to_the_default_text(self):
        SMSTemplate.objects.create(category='PAYMENT_NOTIF', template_text='Hi {nonsense}')
        self.pay()
        self.assertIn('we received KES 4,000', SmsMessage.objects.get().body)

    def test_a_failing_sms_provider_never_breaks_the_payment(self):
        from unittest import mock
        with mock.patch('communication.services.deliver', side_effect=RuntimeError('boom')):
            tx = self.pay()
        tx.refresh_from_db()
        self.assertTrue(tx.is_settled)
        self.assertEqual(SmsMessage.objects.get().status, 'failed')

    def test_manual_payments_also_get_a_receipt_and_the_log_says_automatic(self):
        accountant = client_for(make_user('Accountant'))
        with self.captureOnCommitCallbacks(execute=True):
            accountant.post(reverse('mpesa:transaction-record'), {'tenant': self.tenant.pk, 'amount': '2000', 'channel': 'cash', 'date': '2026-11-01'}, format='json')
        self.assertEqual(SmsMessage.objects.count(), 1)
        row = accountant.get(reverse('communication:message-list')).data['results'][0]
        self.assertEqual(row['sent_by_name'], 'Automatic')
