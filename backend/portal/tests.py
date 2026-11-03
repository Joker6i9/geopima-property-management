from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings

from billing.models import Deposit, Invoice
from billing.services import generate_invoices
from config_core.testing import client_for, make_property, make_tenant, make_unit, make_user
from mpesa.services import MpesaSettlementEngine

# The portal reports the current month, so the fixture bills the current month.
OCT = date.today().replace(day=1)
PREV = (OCT - timedelta(days=1)).replace(day=1)


def pay(tenant, amount, trans_id):
    MpesaSettlementEngine.process_payment({
        'TransID': trans_id, 'TransTime': OCT.strftime('%Y%m') + '10120000', 'TransAmount': str(amount),
        'BusinessShortCode': '123456', 'BillRefNumber': tenant.unit.unit_name, 'MSISDN': tenant.phone_number,
        'FirstName': 'X', 'LastName': 'Y',
    })


class PortalFixture(TestCase):
    def setUp(self):
        self.prop = make_property('Riverside', commission_percentage=Decimal('10'))
        self.unit = make_unit(self.prop, name='A1', rent='20000', water='500', garbage='300', deposit='20000')
        self.tenant = make_tenant(self.unit, deposit='20000')
        self.tenant.user = make_user()
        self.tenant.save()
        other_unit = make_unit(make_property('Elsewhere'), name='B1')
        self.other = make_tenant(other_unit, name='Other Person', phone='0722000111')
        self.other.user = make_user()
        self.other.save()
        Deposit.objects.create(deposit_code='D1', tenant=self.tenant, unit=self.unit, amount=20000)
        generate_invoices(OCT, today=OCT)
        self.landlord = self.prop.landlord
        self.landlord.user = make_user()
        self.landlord.save()


class AccessTests(PortalFixture):
    URLS = [
        '/api/v1/portal/tenant/home/', '/api/v1/portal/tenant/invoices/', '/api/v1/portal/tenant/payments/',
        '/api/v1/portal/tenant/lease/',
        '/api/v1/portal/landlord/overview/', '/api/v1/portal/landlord/properties/',
        '/api/v1/portal/landlord/statements/',
    ]

    def test_anonymous_is_rejected_everywhere(self):
        for url in self.URLS:
            self.assertEqual(client_for().get(url).status_code, 401, url)

    def test_each_portal_is_closed_to_the_other_kinds_of_user(self):
        staff = client_for(make_user('Admin'))
        tenant = client_for(self.tenant.user)
        landlord = client_for(self.landlord.user)
        for url in self.URLS:
            tenant_url = '/tenant/' in url
            self.assertEqual(staff.get(url).status_code, 403, url)
            self.assertEqual(tenant.get(url).status_code, 200 if tenant_url else 403, url)
            self.assertEqual(landlord.get(url).status_code, 403 if tenant_url else 200, url)


class TenantPortalTests(PortalFixture):
    def setUp(self):
        super().setUp()
        self.client = client_for(self.tenant.user)

    def test_home_shows_balance_next_due_and_how_to_pay(self):
        with override_settings(MPESA_PAYBILL='400200'):
            data = self.client.get('/api/v1/portal/tenant/home/').json()
        self.assertEqual(Decimal(data['balance']), Decimal("40800"))
        self.assertEqual(data['tenant']['unit'], 'A1')
        self.assertEqual(data['payment'], {'paybill': '400200', 'account_number': 'A1'})
        self.assertEqual(data['next_due']['invoice_no'], Invoice.objects.get(tenant=self.tenant).invoice_number)
        self.assertEqual(len(data['recent_invoices']), 1)

    def test_home_balance_drops_after_a_payment(self):
        pay(self.tenant, 10000, 'QJ1')
        data = self.client.get('/api/v1/portal/tenant/home/').json()
        self.assertEqual(Decimal(data['balance']), Decimal("30800"))
        self.assertEqual(len(data['recent_payments']), 1)

    def test_tenant_sees_only_their_own_invoices_and_payments(self):
        pay(self.tenant, 5000, 'QJ1')
        pay(self.other, 700, 'QJ2')
        invoices = self.client.get('/api/v1/portal/tenant/invoices/').json()
        self.assertEqual(len(invoices), 1)
        payments = self.client.get('/api/v1/portal/tenant/payments/').json()
        self.assertEqual([p['trans_id'] for p in payments], ['QJ1'])

    def test_another_tenants_invoice_is_a_404(self):
        theirs = Invoice.objects.get(tenant=self.other)
        mine = Invoice.objects.get(tenant=self.tenant)
        self.assertEqual(self.client.get(f'/api/v1/portal/tenant/invoices/{theirs.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/portal/tenant/invoices/{mine.pk}/').status_code, 200)

    def test_status_filter(self):
        self.assertEqual(len(self.client.get('/api/v1/portal/tenant/invoices/?status=paid').json()), 0)
        self.assertEqual(len(self.client.get('/api/v1/portal/tenant/invoices/?status=unpaid').json()), 1)

    def test_lease_shows_deposit_and_landlord_name_but_no_landlord_contacts(self):
        data = self.client.get('/api/v1/portal/tenant/lease/').json()
        self.assertEqual(Decimal(data['lease']['rent']), Decimal('20000'))
        self.assertEqual(data['deposit'], {'amount': '20000.00', 'status': 'held'})
        self.assertEqual(data['landlord'], self.landlord.name)
        self.assertNotIn('bank', str(data).lower())

    def test_a_vacated_tenant_can_still_see_their_history(self):
        self.tenant.status = 'vacated'
        self.tenant.save()
        self.assertEqual(self.client.get('/api/v1/portal/tenant/invoices/').status_code, 200)


class LandlordPortalTests(PortalFixture):
    def setUp(self):
        super().setUp()
        self.client = client_for(self.landlord.user)

    def test_overview_excludes_the_deposit_and_computes_commission(self):
        pay(self.tenant, 21100, 'QJ1')  # 20000 rent + 500 water + 300 garbage = 20800, plus 300 toward the deposit
        data = self.client.get('/api/v1/portal/landlord/overview/').json()
        totals = data['totals']
        # Billed is 20,800: the 20,000 deposit is the tenant's money, held for them.
        self.assertEqual(Decimal(totals['billed']), Decimal('20800'))
        self.assertEqual(Decimal(totals['collected']), Decimal('20800'))
        self.assertEqual(Decimal(totals['commission']), Decimal('2080'))
        self.assertEqual(Decimal(totals['net']), Decimal('18720'))
        self.assertEqual((totals['units'], totals['occupied'], totals['occupancy']), (1, 1, 100))

    def test_only_own_properties_are_listed(self):
        names = [p['name'] for p in self.client.get('/api/v1/portal/landlord/properties/').json()]
        self.assertEqual(names, ['Riverside'])

    def test_another_landlords_property_is_a_404(self):
        theirs = self.other.unit.property
        self.assertEqual(self.client.get(f'/api/v1/portal/landlord/properties/{theirs.pk}/').status_code, 404)

    def test_property_detail_names_tenants_without_contact_details(self):
        data = self.client.get(f'/api/v1/portal/landlord/properties/{self.prop.pk}/').json()
        self.assertEqual(data['units'][0]['tenant_name'], self.tenant.name)
        body = str(data)
        self.assertNotIn(self.tenant.phone_number, body)
        self.assertNotIn(self.tenant.tenant_id, body)

    def test_vacant_units_are_reported_vacant(self):
        make_unit(self.prop, name='A2')
        data = self.client.get(f'/api/v1/portal/landlord/properties/{self.prop.pk}/').json()
        self.assertEqual({u['name']: u['status'] for u in data['units']}, {'A1': 'occupied', 'A2': 'vacant'})
        self.assertEqual(data['property']['occupancy'], 50)

    def test_statements_group_by_month_and_property(self):
        generate_invoices(PREV, today=OCT)
        pay(self.tenant, 1000, 'QJ1')
        rows = self.client.get('/api/v1/portal/landlord/statements/?months=60').json()['rows']
        self.assertEqual([r['period'] for r in rows], [OCT.strftime('%Y-%m'), PREV.strftime('%Y-%m')])
        self.assertTrue(all(r['property'] == 'Riverside' for r in rows))
        self.assertEqual(sum(Decimal(r['commission']) for r in rows), Decimal('100.00'))

    def test_months_is_validated(self):
        self.assertEqual(self.client.get('/api/v1/portal/landlord/statements/?months=0').status_code, 400)
        self.assertEqual(self.client.get('/api/v1/portal/landlord/statements/?months=abc').status_code, 400)

    def test_landlord_with_no_properties_gets_empty_totals(self):
        lonely = make_property('Empty').landlord
        lonely.user = make_user()
        lonely.save()
        lonely.properties.all().delete()
        data = client_for(lonely.user).get('/api/v1/portal/landlord/overview/').json()
        self.assertEqual(data['totals']['units'], 0)
        self.assertEqual(data['totals']['occupancy'], 0)
