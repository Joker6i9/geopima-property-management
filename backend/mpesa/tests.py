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


class RecordPaymentTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit)
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))
        self.invoice = Invoice.objects.get()
        self.url = reverse('mpesa:transaction-record')
        self.client = client_for(make_user('Accountant'))

    def body(self, **extra):
        return {'tenant': self.tenant.pk, 'amount': '4000', 'channel': 'cash', 'date': '2026-08-03', **extra}

    def test_cash_payment_is_applied_to_the_invoice(self):
        res = self.client.post(self.url, self.body(), format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['channel'], 'cash')
        self.assertEqual(res.data['status'], 'settled')
        self.invoice.refresh_from_db()
        self.assertEqual((self.invoice.status, self.invoice.balance), ('partially_paid', Decimal('6000')))
        self.assertTrue(MpesaTransaction.objects.get().trans_id.startswith('CASH-'))

    def test_overpayment_becomes_credit(self):
        res = self.client.post(self.url, self.body(amount='12000'), format='json')
        self.assertEqual(Decimal(str(res.data['unallocated'])), Decimal('2000'))

    def test_bank_payment_needs_a_reference_and_it_must_be_new(self):
        self.assertEqual(self.client.post(self.url, self.body(channel='bank'), format='json').status_code, 400)
        ok = self.client.post(self.url, self.body(channel='bank', reference='FT123', paid_to='KCB'), format='json')
        self.assertEqual(ok.status_code, 201)
        dup = self.client.post(self.url, self.body(channel='bank', reference='ft123'), format='json')
        self.assertEqual(dup.status_code, 400)

    def test_rejects_future_dates_zero_amounts_and_unauthorised_roles(self):
        self.assertEqual(self.client.post(self.url, self.body(date='2999-01-01'), format='json').status_code, 400)
        self.assertEqual(self.client.post(self.url, self.body(amount='0'), format='json').status_code, 400)
        agent = client_for(make_user('Field Agent'))
        self.assertEqual(agent.post(self.url, self.body(), format='json').status_code, 403)

    def test_invoice_lists_the_payment_channel(self):
        self.client.post(self.url, self.body(channel='cheque', reference='CHQ1', paid_to='Equity'), format='json')
        res = self.client.get(reverse('billing:invoice-detail', args=[self.invoice.pk]))
        pay = res.data['payment_details'][0]
        self.assertEqual((pay['type'], pay['via']), ('Cheque', 'Manual'))
        self.assertIn('CHQ1', pay['bank'])


from datetime import timedelta
from unittest import mock

from django.utils import timezone

from . import daraja
from .models import StkRequest
from .services import apply_stk_callback, refresh_stk, start_stk

DARAJA = dict(
    MPESA_ENV='sandbox', MPESA_CONSUMER_KEY='key', MPESA_CONSUMER_SECRET='secret', MPESA_PASSKEY='pass',
    MPESA_PAYBILL='174379', MPESA_CALLBACK_SECRET='s3cret-token', MPESA_PUBLIC_URL='https://agency.example.co.ke',
)
PUSH_OK = {'MerchantRequestID': 'm-1', 'CheckoutRequestID': 'ws_CO_1', 'ResponseCode': '0', 'CustomerMessage': 'Success'}


def stk_callback(checkout='ws_CO_1', code=0, receipt='RCP12345AB', amount=10000, desc='ok'):
    cb = {'MerchantRequestID': 'm-1', 'CheckoutRequestID': checkout, 'ResultCode': code, 'ResultDesc': desc}
    if code == 0:
        cb['CallbackMetadata'] = {'Item': [
            {'Name': 'Amount', 'Value': amount}, {'Name': 'MpesaReceiptNumber', 'Value': receipt},
            {'Name': 'TransactionDate', 'Value': 20261102101500}, {'Name': 'PhoneNumber', 'Value': 254712345678},
        ]}
    return {'Body': {'stkCallback': cb}}


@override_settings(**DARAJA)
class StkPushTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, phone='0712345678')
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))
        self.invoice = Invoice.objects.get()
        self.push = mock.patch('mpesa.daraja._authed', return_value=PUSH_OK)

    def start(self, amount=10000, phone='0712345678'):
        with self.push as call:
            stk = start_stk(tenant=self.tenant, phone=phone, amount=amount)
        return stk, call

    def test_prompt_is_sent_with_the_right_details_and_recorded_as_pending(self):
        stk, call = self.start()
        path, body = call.call_args.args
        self.assertEqual(path, '/mpesa/stkpush/v1/processrequest')
        self.assertEqual((body['Amount'], body['PartyA'], body['PartyB'], body['AccountReference']), (10000, '254712345678', '174379', 'R1'))
        self.assertEqual(body['TransactionType'], 'CustomerPayBillOnline')
        self.assertNotIn('mpesa', body['CallBackURL'].lower())
        self.assertTrue(body['CallBackURL'].endswith('/api/v1/payments/c2b/s3cret-token/stk/'))
        import base64
        self.assertEqual(base64.b64decode(body['Password']).decode(), '174379pass' + body['Timestamp'])
        self.assertEqual((stk.status, stk.checkout_request_id), ('pending', 'ws_CO_1'))

    def test_paid_callback_records_the_payment_and_clears_the_invoice(self):
        stk, _ = self.start()
        apply_stk_callback(stk_callback())
        stk.refresh_from_db()
        self.invoice.refresh_from_db()
        self.assertEqual((stk.status, stk.receipt), ('success', 'RCP12345AB'))
        self.assertEqual((self.invoice.status, MpesaTransaction.objects.get().channel), ('paid', 'mpesa'))

    def test_the_same_callback_twice_does_not_pay_twice(self):
        self.start()
        apply_stk_callback(stk_callback())
        apply_stk_callback(stk_callback())
        self.assertEqual((MpesaTransaction.objects.count(), PaymentSettlement.objects.count()), (1, 1))

    def test_paybill_confirmation_arriving_first_or_later_does_not_double_count(self):
        stk, _ = self.start()
        MpesaSettlementEngine.process_payment(payload(trans_id='RCP12345AB', amount='10000', ref='R1', msisdn='254712345678'))
        apply_stk_callback(stk_callback())
        MpesaSettlementEngine.process_payment(payload(trans_id='RCP12345AB', amount='10000', ref='R1', msisdn='254712345678'))
        stk.refresh_from_db()
        self.assertEqual((stk.status, MpesaTransaction.objects.count(), PaymentSettlement.objects.count()), ('success', 1, 1))

    def test_an_unmatched_paybill_confirmation_is_settled_by_the_prompt_we_sent(self):
        stk, _ = self.start()
        MpesaSettlementEngine.process_payment(payload(trans_id='RCP12345AB', amount='10000', ref='typo', msisdn='254799999999'))
        self.assertFalse(MpesaTransaction.objects.get().is_settled)
        apply_stk_callback(stk_callback())
        self.assertTrue(MpesaTransaction.objects.get().is_settled)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'paid')

    def test_cancelled_and_failed_prompts(self):
        stk, _ = self.start()
        apply_stk_callback(stk_callback(code=1032, desc='Request cancelled by user'))
        stk.refresh_from_db()
        self.assertEqual((stk.status, MpesaTransaction.objects.count()), ('cancelled', 0))

    def test_unknown_or_malformed_callbacks_change_nothing(self):
        self.start()
        self.assertIsNone(apply_stk_callback(stk_callback(checkout='someone-elses')))
        with self.assertRaises(InvalidPayload):
            apply_stk_callback({'nope': 1})
        self.assertEqual(StkRequest.objects.get().status, 'pending')

    def test_bad_phone_amount_and_double_prompt_are_refused(self):
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.start(phone='12345')
        with self.assertRaises(ValidationError):
            self.start(amount=0)
        with self.assertRaises(ValidationError):
            self.start(amount=150001)
        self.start()
        with self.assertRaises(ValidationError):
            self.start()

    def test_safaricom_refusing_the_request_becomes_a_readable_error(self):
        from rest_framework.exceptions import ValidationError
        with mock.patch('mpesa.daraja._authed', side_effect=daraja.DarajaError('Invalid Access Token')):
            with self.assertRaises(ValidationError) as ctx:
                start_stk(tenant=self.tenant, phone='0712345678', amount=100)
        self.assertIn('Invalid Access Token', str(ctx.exception.detail))
        self.assertEqual(StkRequest.objects.count(), 0)

    @override_settings(MPESA_PASSKEY='')
    def test_missing_setup_is_reported_by_name(self):
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            start_stk(tenant=self.tenant, phone='0712345678', amount=100)
        self.assertIn('MPESA_PASSKEY', str(ctx.exception.detail))

    def test_status_check_asks_safaricom_only_once_the_prompt_is_a_little_old(self):
        stk, _ = self.start()
        with mock.patch('mpesa.daraja.stk_query') as q:
            refresh_stk(stk)
            q.assert_not_called()
        StkRequest.objects.update(created_at=timezone.now() - timedelta(seconds=40))
        stk.refresh_from_db()
        with mock.patch('mpesa.daraja.stk_query', return_value={'ResultCode': '1032', 'ResultDesc': 'Request cancelled by user'}):
            self.assertEqual(refresh_stk(stk).status, 'cancelled')

    def test_a_prompt_with_no_answer_lapses_after_five_minutes(self):
        stk, _ = self.start()
        StkRequest.objects.update(created_at=timezone.now() - timedelta(minutes=6))
        stk.refresh_from_db()
        with mock.patch('mpesa.daraja.stk_query', side_effect=daraja.DarajaError('being processed')):
            self.assertEqual(refresh_stk(stk).status, 'timeout')


@override_settings(**DARAJA)
class MpesaEndpointTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, phone='0712345678')
        self.tenant.user = make_user()
        self.tenant.save()
        self.other = make_tenant(make_unit(name='R2'), name='Other', phone='0722000000')
        self.other.user = make_user()
        self.other.save()

    def test_staff_can_send_a_prompt_and_check_it(self):
        accountant = client_for(make_user('Accountant'))
        with mock.patch('mpesa.daraja._authed', return_value=PUSH_OK):
            res = accountant.post(reverse('mpesa:stk_push'), {'tenant': self.tenant.pk, 'amount': 5000}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(accountant.get(reverse('mpesa:stk_status', args=[res.data['id']])).data['status'], 'pending')
        self.assertEqual(client_for(make_user('Field Agent')).post(reverse('mpesa:stk_push'), {'tenant': self.tenant.pk, 'amount': 5000}, format='json').status_code, 403)

    def test_a_tenant_prompts_their_own_phone_and_cannot_see_anyone_elses_request(self):
        mine, theirs = client_for(self.tenant.user), client_for(self.other.user)
        with mock.patch('mpesa.daraja._authed', return_value=PUSH_OK) as call:
            res = mine.post('/api/v1/portal/tenant/pay/', {'amount': 2500}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(call.call_args.args[1]['PartyA'], '254712345678')
        self.assertEqual(mine.get(f"/api/v1/portal/tenant/pay/{res.data['id']}/").status_code, 200)
        self.assertEqual(theirs.get(f"/api/v1/portal/tenant/pay/{res.data['id']}/").status_code, 404)

    def test_callback_url_needs_the_secret_and_works_under_the_payments_path(self):
        stk, _ = StkPushTests.start(type('X', (), {'tenant': self.tenant, 'push': mock.patch('mpesa.daraja._authed', return_value=PUSH_OK)})())
        c = client_for()
        body = json.dumps(stk_callback(amount=5000))
        self.assertEqual(c.post('/api/v1/payments/c2b/wrong/stk/', body, content_type='application/json').status_code, 404)
        ok = c.post('/api/v1/payments/c2b/s3cret-token/stk/', body, content_type='application/json')
        self.assertEqual((ok.status_code, ok.json()['ResultCode']), (200, 0))
        self.assertEqual(StkRequest.objects.get().status, 'success')
        # The old path under /mpesa/ keeps working for URLs registered before the move.
        self.assertEqual(c.post('/api/v1/mpesa/c2b/s3cret-token/validation/', '{}', content_type='application/json').status_code, 200)

    def test_registering_urls_sends_safaricom_addresses_without_the_word_mpesa(self):
        admin = client_for(make_user('Admin'))
        with mock.patch('mpesa.daraja._authed', return_value={'ResponseDescription': 'Success'}) as call:
            res = admin.post(reverse('mpesa:integration'))
        self.assertEqual(res.status_code, 200)
        path, body = call.call_args.args
        self.assertEqual((path, body['ShortCode'], body['ResponseType']), ('/mpesa/c2b/v2/registerurl', '174379', 'Completed'))
        for url in (body['ConfirmationURL'], body['ValidationURL']):
            self.assertTrue(url.startswith('https://'))
            self.assertNotIn('mpesa', url.lower())

    @override_settings(MPESA_CONSUMER_KEY='')
    def test_registering_without_credentials_says_what_is_missing(self):
        res = client_for(make_user('Admin')).post(reverse('mpesa:integration'))
        self.assertEqual(res.status_code, 400)
        self.assertIn('MPESA_CONSUMER_KEY', json.dumps(res.json()))

    def test_integration_status_never_contains_a_secret_and_is_admin_only(self):
        res = client_for(make_user('Admin')).get(reverse('mpesa:integration'))
        text = json.dumps(res.json())
        for secret in ('s3cret-token', 'secret', 'pass'):
            self.assertNotIn(f'"{secret}"', text)
        self.assertNotIn('s3cret-token', text)
        self.assertEqual(client_for(make_user('Accountant')).get(reverse('mpesa:integration')).status_code, 403)


class DarajaClientTests(TestCase):
    @override_settings(**DARAJA)
    def test_the_access_token_is_fetched_with_basic_auth_and_reused(self):
        daraja._token.update(value='', expires_at=0)
        with mock.patch('mpesa.daraja._call', return_value={'access_token': 'tok', 'expires_in': '3599'}) as call:
            self.assertEqual(daraja.access_token(), 'tok')
            self.assertEqual(daraja.access_token(), 'tok')
        self.assertEqual(call.call_count, 1)
        import base64
        self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Basic ' + base64.b64encode(b'key:secret').decode())
        daraja._token.update(value='', expires_at=0)

    @override_settings(MPESA_ENV='production', MPESA_BASE_URL='')
    def test_production_and_sandbox_use_different_hosts(self):
        self.assertEqual(daraja.base_url(), 'https://api.safaricom.co.ke')
        with override_settings(MPESA_ENV='sandbox'):
            self.assertEqual(daraja.base_url(), 'https://sandbox.safaricom.co.ke')


@override_settings(**DARAJA)
class StkEdgeCaseTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R1', rent='10000', water='0', garbage='0')
        self.tenant = make_tenant(self.unit, phone='0712345678')
        generate_invoices(date(2026, 8, 1), today=date(2026, 8, 1))
        with mock.patch('mpesa.daraja._authed', return_value=PUSH_OK):
            self.stk = start_stk(tenant=self.tenant, phone='0712345678', amount=10000)
        StkRequest.objects.update(created_at=timezone.now() - timedelta(seconds=40))
        self.stk.refresh_from_db()

    def test_still_under_processing_is_not_a_failure(self):
        with mock.patch('mpesa.daraja.stk_query', return_value={'ResultCode': '4999', 'ResultDesc': 'The transaction is still under processing'}):
            self.assertEqual(refresh_stk(self.stk).status, 'pending')

    def test_a_late_payment_still_counts_after_the_prompt_was_given_up_on(self):
        StkRequest.objects.update(status='timeout')
        apply_stk_callback(stk_callback())
        self.stk.refresh_from_db()
        self.assertEqual((self.stk.status, Invoice.objects.get().status), ('success', 'paid'))

    def test_a_late_failure_does_not_overwrite_a_recorded_payment(self):
        apply_stk_callback(stk_callback())
        apply_stk_callback(stk_callback(code=1032, desc='cancelled'))
        self.stk.refresh_from_db()
        self.assertEqual(self.stk.status, 'success')
