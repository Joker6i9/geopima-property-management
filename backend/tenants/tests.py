from decimal import Decimal

from django.test import TestCase

from billing.models import Deposit, Invoice
from config_core.testing import client_for, make_property, make_tenant, make_unit, make_user
from tenants.models import LeaseAgreement, Tenant

MOVE_IN = {'name': 'Brian Otieno', 'phone_number': '0725 838 246', 'email': 'b@example.com', 'start_date': '2026-10-01'}


class MoveInTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R5', rent='30000', water='800', garbage='300', deposit='30000')
        self.client = client_for(make_user('Secretary'))

    def move_in(self, **extra):
        return self.client.post('/api/v1/tenants/tenants/move-in/', {**MOVE_IN, 'unit': self.unit.pk, **extra}, format='json')

    def test_creates_tenant_lease_deposit_and_first_invoice(self):
        response = self.move_in()
        self.assertEqual(response.status_code, 201, response.content)
        tenant = Tenant.objects.get(name='Brian Otieno')
        self.assertEqual(tenant.phone_number, '254725838246')
        lease = LeaseAgreement.objects.get(tenant=tenant)
        self.assertEqual((lease.is_active, lease.rent_amount), (True, Decimal('30000')))
        self.assertEqual(Deposit.objects.get(tenant=tenant).status, 'held')
        self.unit.refresh_from_db()
        self.assertTrue(self.unit.is_occupied)
        invoice = Invoice.objects.get(tenant=tenant)
        self.assertEqual(invoice.net_total, Decimal('61100'))  # rent + water + garbage + deposit

    def test_refuses_an_occupied_unit_a_bad_phone_and_a_duplicate_phone(self):
        self.assertEqual(self.move_in().status_code, 201)
        self.assertEqual(self.move_in(name='Second').status_code, 400)
        other = make_unit(make_property('P2'), name='R6')
        bad = self.client.post('/api/v1/tenants/tenants/move-in/', {**MOVE_IN, 'unit': other.pk, 'phone_number': '12345'}, format='json')
        self.assertEqual(bad.status_code, 400)
        dup = self.client.post('/api/v1/tenants/tenants/move-in/', {**MOVE_IN, 'unit': other.pk}, format='json')
        self.assertEqual(dup.status_code, 400)
        self.assertEqual(Tenant.objects.count(), 1)

    def test_lease_end_must_be_after_start(self):
        self.assertEqual(self.move_in(end_date='2026-09-01').status_code, 400)

    def test_accountants_cannot_move_tenants_in(self):
        response = client_for(make_user('Accountant')).post(
            '/api/v1/tenants/tenants/move-in/', {**MOVE_IN, 'unit': self.unit.pk}, format='json')
        self.assertEqual(response.status_code, 403)


class VacateAndRefundTests(TestCase):
    def setUp(self):
        self.unit = make_unit(name='R5', rent='10000', water='0', garbage='0', deposit='10000')
        self.secretary = client_for(make_user('Secretary'))
        self.accountant = client_for(make_user('Accountant'))
        self.secretary.post('/api/v1/tenants/tenants/move-in/', {**MOVE_IN, 'unit': self.unit.pk}, format='json')
        self.tenant = Tenant.objects.get()

    def test_vacating_frees_the_unit_ends_the_lease_and_keeps_the_balance(self):
        response = self.secretary.post(f'/api/v1/tenants/tenants/{self.tenant.pk}/vacate/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Decimal(str(response.json()['outstanding_balance'])), Decimal('20000'))
        self.tenant.refresh_from_db()
        self.unit.refresh_from_db()
        self.assertEqual((self.tenant.status, self.tenant.unit, self.unit.is_occupied), ('vacated', None, False))
        self.assertFalse(LeaseAgreement.objects.get().is_active)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(self.secretary.post(f'/api/v1/tenants/tenants/{self.tenant.pk}/vacate/').status_code, 400)

    def test_deposit_can_only_be_refunded_after_vacating_and_only_once(self):
        deposit = Deposit.objects.get()
        url = f'/api/v1/billing/deposits/{deposit.pk}/refund/'
        self.assertEqual(self.accountant.post(url).status_code, 400)
        self.secretary.post(f'/api/v1/tenants/tenants/{self.tenant.pk}/vacate/')
        self.assertEqual(self.secretary.post(url).status_code, 403)
        self.assertEqual(self.accountant.post(url).status_code, 200)
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, 'refunded')
        self.assertEqual(self.accountant.post(url).status_code, 400)

    def test_suspend_and_reactivate(self):
        self.secretary.post(f'/api/v1/tenants/tenants/{self.tenant.pk}/suspend/')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, 'suspended')
        self.secretary.post(f'/api/v1/tenants/tenants/{self.tenant.pk}/reactivate/')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.status, 'active')


class TenantListTests(TestCase):
    def test_filters_by_status_property_and_cluster(self):
        prop = make_property('Riverside', cluster='Cluster B')
        make_tenant(make_unit(prop, name='A1'), name='In Riverside')
        make_tenant(make_unit(make_property('Other'), name='B1'), name='Elsewhere', phone='0722000111')
        client = client_for(make_user('Field Agent'))

        def names(query):
            response = client.get(f'/api/v1/tenants/tenants/?{query}')
            self.assertEqual(response.status_code, 200, query)
            return {t['name'] for t in response.json()['results']}

        self.assertEqual(names('property=Riverside'), {'In Riverside'})
        self.assertEqual(names('cluster=Cluster B'), {'In Riverside'})
        self.assertEqual(names('status=active'), {'In Riverside', 'Elsewhere'})
        self.assertEqual(names('status=vacated'), set())

    def test_unit_tenants_endpoint_lists_current_tenants_only(self):
        unit = make_unit(name='A1')
        make_tenant(unit, name='Current')
        Tenant.objects.create(name='Past', phone_number='0722000999', unit=unit, status='vacated')
        response = client_for(make_user('Admin')).get(f'/api/v1/properties/units/{unit.pk}/tenants/')
        self.assertEqual([t['name'] for t in response.json()], ['Current'])


class InvoiceApiTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant(make_unit(name='R1', rent='10000', water='0', garbage='0'))
        self.accountant = client_for(make_user('Accountant'))

    def test_generate_monthly_is_idempotent(self):
        first = self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')
        again = self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')
        self.assertEqual((first.json()['created'], again.json()['created'], again.json()['skipped']), (1, 0, 1))

    def test_bad_period_is_a_clear_error_not_a_server_error(self):
        response = self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': 'October'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_raise_for_one_tenant_then_refuse_a_duplicate(self):
        body = {'tenant': self.tenant.pk, 'period': '2026-09'}
        first = self.accountant.post('/api/v1/billing/invoices/raise/', body, format='json')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()['status'], 'overdue')
        self.assertEqual(self.accountant.post('/api/v1/billing/invoices/raise/', body, format='json').status_code, 400)

    def test_invoice_detail_shows_agency_items_and_payments(self):
        self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')
        invoice = Invoice.objects.get()
        data = self.accountant.get(f'/api/v1/billing/invoices/{invoice.pk}/').json()
        self.assertEqual((data['invoice_no'], data['balance'], data['status']), (invoice.invoice_number, '10000.00', 'overdue'))
        self.assertEqual(data['payment_details'], [])

    def test_list_filters(self):
        self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')
        count = lambda q: len(self.accountant.get(f'/api/v1/billing/invoices/?{q}').json()['results'])  # noqa: E731
        self.assertEqual((count('period=2026-10'), count('period=2026-09'), count(f'tenant={self.tenant.pk}')), (1, 0, 1))
        self.assertEqual(self.accountant.get('/api/v1/billing/invoices/?period=bad').status_code, 400)

    def test_invoices_cannot_be_edited_directly(self):
        self.accountant.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')
        invoice = Invoice.objects.get()
        self.assertEqual(self.accountant.delete(f'/api/v1/billing/invoices/{invoice.pk}/').status_code, 405)
        self.assertEqual(self.accountant.patch(f'/api/v1/billing/invoices/{invoice.pk}/', {}, format='json').status_code, 405)
