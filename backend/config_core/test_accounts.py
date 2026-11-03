from django.contrib.auth import get_user_model
from django.test import TestCase

from config_core.testing import client_for, make_property, make_tenant, make_unit, make_user


class PortalLoginTests(TestCase):
    def setUp(self):
        self.admin = client_for(make_user('Admin'))
        self.tenant = make_tenant(make_unit(make_property()), phone='0712345678')

    def url(self):
        return f'/api/v1/tenants/tenants/{self.tenant.pk}/login/'

    def test_creates_and_links_a_tenant_login_that_can_sign_in(self):
        data = self.admin.post(self.url()).json()
        self.assertTrue(data['created'])
        self.assertEqual(data['username'], '254712345678')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.user.username, '254712345678')
        token = client_for().post('/api/v1/token/', {'username': data['username'], 'password': data['password']})
        self.assertEqual(token.status_code, 200)

    def test_second_call_resets_the_password_on_the_same_account(self):
        first = self.admin.post(self.url()).json()
        second = self.admin.post(self.url()).json()
        self.assertFalse(second['created'])
        self.assertEqual(first['username'], second['username'])
        self.assertNotEqual(first['password'], second['password'])
        self.assertEqual(get_user_model().objects.filter(username=first['username']).count(), 1)
        bad = client_for().post('/api/v1/token/', {'username': first['username'], 'password': first['password']})
        self.assertEqual(bad.status_code, 401)

    def test_username_clash_gets_a_number(self):
        get_user_model().objects.create_user(username='254712345678')
        self.assertEqual(self.admin.post(self.url()).json()['username'], '2547123456782')

    def test_moved_out_tenant_cannot_get_a_login(self):
        self.tenant.status = 'vacated'
        self.tenant.save()
        self.assertEqual(self.admin.post(self.url()).status_code, 400)

    def test_only_portfolio_roles_can_do_it(self):
        self.assertEqual(client_for(make_user('Accountant')).post(self.url()).status_code, 403)
        self.assertEqual(client_for(make_user()).post(self.url()).status_code, 403)
        self.assertEqual(client_for().post(self.url()).status_code, 401)

    def test_landlord_login(self):
        landlord = self.tenant.unit.property.landlord
        data = self.admin.post(f'/api/v1/properties/landlords/{landlord.pk}/login/').json()
        self.assertEqual(data['username'], landlord.landlord_id.lower())
        landlord.refresh_from_db()
        self.assertEqual(landlord.user.username, data['username'])
