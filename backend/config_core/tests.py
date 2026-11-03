from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from billing.services import generate_invoices
from config_core.models import UserRole
from config_core.testing import client_for, make_property, make_tenant, make_unit, make_user
from properties.models import Landlord

# Every endpoint that returns agency data. None may answer an anonymous request.
PROTECTED = [
    '/api/v1/tenants/tenants/', '/api/v1/tenants/agreements/', '/api/v1/properties/', '/api/v1/properties/units/',
    '/api/v1/properties/landlords/', '/api/v1/billing/invoices/', '/api/v1/billing/deposits/',
    '/api/v1/billing/vote-heads/', '/api/v1/mpesa/transactions/', '/api/v1/dashboard/metrics/',
    '/api/v1/dashboard/payment-summaries/', '/api/v1/agency-profile/1/', '/api/v1/user-roles/', '/api/v1/users/',
    '/api/v1/payment-banks/', '/api/v1/sms-templates/', '/api/v1/agreement-templates/', '/api/v1/me/',
]


class AnonymousAccessTests(TestCase):
    def test_nothing_is_readable_without_signing_in(self):
        client = client_for()
        for url in PROTECTED:
            self.assertEqual(client.get(url).status_code, 401, url)

    def test_nothing_is_writable_without_signing_in(self):
        client = client_for()
        for url in ('/api/v1/properties/', '/api/v1/properties/landlords/', '/api/v1/billing/deposits/',
                    '/api/v1/billing/invoices/generate-monthly/', '/api/v1/billing/vote-heads/'):
            self.assertEqual(client.post(url, {}, format='json').status_code, 401, url)


class RoleMatrixTests(TestCase):
    def test_each_role_reaches_only_what_it_needs(self):
        expected = {
            # role: (can read invoices, can generate invoices, can create a landlord, can manage users)
            'Super Admin': (True, True, True, True),
            'Admin': (True, True, True, True),
            'Accountant': (True, True, False, False),
            'Secretary': (True, False, True, False),
            'Field Agent': (True, False, False, False),
        }
        for role, (read_invoices, generate, create_landlord, manage_users) in expected.items():
            client = client_for(make_user(role))
            ok = lambda response: response.status_code < 400  # noqa: E731
            self.assertEqual(ok(client.get('/api/v1/billing/invoices/')), read_invoices, role)
            self.assertEqual(ok(client.post('/api/v1/billing/invoices/generate-monthly/', {'period': '2026-10'}, format='json')), generate, role)
            self.assertEqual(ok(client.post('/api/v1/properties/landlords/', {'landlord_id': f'L-{role}', 'name': 'X'}, format='json')), create_landlord, role)
            self.assertEqual(ok(client.get('/api/v1/users/')), manage_users, role)

    def test_a_user_with_no_role_gets_nothing(self):
        client = client_for(make_user())
        for url in PROTECTED:
            if url != '/api/v1/me/':
                self.assertEqual(client.get(url).status_code, 403, url)

    def test_tenant_and_landlord_accounts_cannot_use_staff_endpoints(self):
        tenant_user = make_user()
        make_tenant(user=tenant_user)
        landlord_user = make_user()
        Landlord.objects.create(landlord_id='L1', name='L', user=landlord_user)
        for user in (tenant_user, landlord_user):
            client = client_for(user)
            for url in PROTECTED:
                if url != '/api/v1/me/':
                    self.assertEqual(client.get(url).status_code, 403, url)

    def test_superuser_passes_everywhere(self):
        admin = make_user(is_superuser=True)
        self.assertEqual(client_for(admin).get('/api/v1/users/').status_code, 200)

    def test_roles_from_django_groups_still_count(self):
        user = make_user()
        user.groups.add(Group.objects.create(name='Accountant'))
        self.assertEqual(client_for(user).get('/api/v1/billing/invoices/').status_code, 200)


class CurrentUserTests(TestCase):
    def test_staff_get_the_admin_portal(self):
        me = client_for(make_user('Accountant')).get('/api/v1/me/').json()
        self.assertEqual((me['role'], me['roles']), ('admin', ['Accountant']))

    def test_tenant_account_gets_the_tenant_portal_and_profile_id(self):
        user = make_user()
        tenant = make_tenant(user=user)
        me = client_for(user).get('/api/v1/me/').json()
        self.assertEqual((me['role'], me['tenant_id'], me['landlord_id']), ('tenant', tenant.pk, None))

    def test_landlord_account_gets_the_landlord_portal(self):
        user = make_user()
        landlord = Landlord.objects.create(landlord_id='L1', name='L', user=user)
        me = client_for(user).get('/api/v1/me/').json()
        self.assertEqual((me['role'], me['landlord_id']), ('landlord', landlord.pk))

    def test_role_permissions_are_listed(self):
        user = make_user()
        user.roles.add(UserRole.objects.create(name='Custom', permissions=['invoices.view']))
        self.assertEqual(client_for(user).get('/api/v1/me/').json()['permissions'], ['invoices.view'])

    def test_login_response_includes_the_portal_role(self):
        user = make_user('Admin')
        user.set_password('secret-pass')
        user.save()
        response = client_for().post('/api/v1/token/', {'username': user.username, 'password': 'secret-pass'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['user']['role'], 'admin')


class SeedRolesTests(TestCase):
    def test_creates_roles_and_carries_group_membership_across(self):
        from io import StringIO
        from django.core.management import call_command
        user = make_user()
        user.groups.add(Group.objects.create(name='Accountant'))
        call_command('seed_roles', stdout=StringIO())
        call_command('seed_roles', stdout=StringIO())  # running it again changes nothing
        self.assertEqual(UserRole.objects.count(), 5)
        self.assertEqual(list(user.roles.values_list('name', flat=True)), ['Accountant'])


class UserAccountTests(TestCase):
    def test_creating_a_user_assigns_a_user_role_and_phone(self):
        admin = client_for(make_user('Admin'))
        UserRole.objects.get_or_create(name='Accountant')
        response = admin.post('/api/v1/users/', {
            'username': 'newbie', 'email': 'n@example.com', 'password': 'a-long-pass-123',
            'full_name': 'New Bie', 'role': 'Accountant', 'phone': '0712345678',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.get(username='newbie')
        self.assertEqual(list(user.roles.values_list('name', flat=True)), ['Accountant'])
        self.assertEqual(user.phone, '254712345678')
        self.assertEqual(client_for(user).get('/api/v1/billing/invoices/').status_code, 200)


class DashboardTests(TestCase):
    def test_metrics_count_real_units_and_current_tenants(self):
        prop = make_property('Riverside')
        occupied = make_unit(prop, name='A1')
        make_unit(prop, name='A2')
        make_tenant(occupied)
        make_tenant(make_unit(prop, name='A3'), name='Gone', phone='0722000111', status='vacated')
        data = client_for(make_user('Field Agent')).get('/api/v1/dashboard/metrics/').json()
        self.assertEqual(data, {'properties': ['Riverside'], 'total_units': [3], 'occupied_units': [1]})

    def test_payment_summaries_are_computed_from_invoices(self):
        make_tenant(make_unit(rent='10000', water='1000', garbage='0'))
        today = date.today()
        generate_invoices(today, today=today)
        data = client_for(make_user('Accountant')).get('/api/v1/dashboard/payment-summaries/').json()
        self.assertEqual({d['name']: (d['collected'], d['total']) for d in data}, {'Rent': (0, 10000), 'Water': (0, 1000)})
        self.assertEqual(client_for(make_user('Secretary')).get('/api/v1/dashboard/payment-summaries/').status_code, 403)


class StaffAccountTests(TestCase):
    def setUp(self):
        self.admin = make_user('Admin')
        self.client = client_for(self.admin)
        for name in ('Accountant', 'Secretary'):
            UserRole.objects.get_or_create(name=name)

    def create(self, **extra):
        body = {'username': 'jane', 'email': 'jane@x.co', 'password': 'Sup3rSecret!', 'full_name': 'Jane Mwangi',
                'phone': '0712000111', 'role': 'Accountant', **extra}
        return self.client.post('/api/v1/users/', body, format='json')

    def test_create_then_edit_role_name_and_active(self):
        res = self.create()
        self.assertEqual(res.status_code, 201, res.data)
        pk = res.data['id']
        res = self.client.patch(f'/api/v1/users/{pk}/', {'role': 'Secretary', 'full_name': 'Jane W Mwangi', 'is_active': False}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        user = get_user_model().objects.get(pk=pk)
        self.assertEqual(list(user.roles.values_list('name', flat=True)), ['Secretary'])
        self.assertEqual((user.first_name, user.last_name, user.is_active), ('Jane', 'W Mwangi', False))

    def test_unknown_role_and_weak_password_are_rejected(self):
        self.assertEqual(self.create(role='Wizard').status_code, 400)
        self.assertEqual(self.create(password='1234').status_code, 400)

    def test_password_can_be_reset_without_resending_other_fields(self):
        pk = self.create().data['id']
        self.assertEqual(self.client.patch(f'/api/v1/users/{pk}/', {'password': 'An0therSecret!'}, format='json').status_code, 200)
        self.assertTrue(get_user_model().objects.get(pk=pk).check_password('An0therSecret!'))

    def test_you_cannot_deactivate_or_demote_yourself_and_accounts_are_not_deleted(self):
        url = f'/api/v1/users/{self.admin.pk}/'
        self.assertEqual(self.client.patch(url, {'is_active': False}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'role': 'Accountant'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 400)

    def test_only_admins_manage_staff(self):
        self.assertEqual(client_for(make_user('Accountant')).get('/api/v1/users/').status_code, 403)
