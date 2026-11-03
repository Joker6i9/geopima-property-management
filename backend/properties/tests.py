from django.test import TestCase

# Create your tests here.


class PropertyEditTests(TestCase):
    def test_editing_a_property_keeps_its_code_and_saves_default_charges(self):
        from config_core.testing import client_for, make_property, make_user
        prop = make_property(name='Old name')
        code = prop.property_id
        client = client_for(make_user('Secretary'))
        res = client.patch(f'/api/v1/properties/{code}/', {'name': 'New name', 'default_water_rate': 300, 'commission_percentage': 12}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        prop.refresh_from_db()
        self.assertEqual((prop.property_id, prop.name, int(prop.default_water_rate), int(prop.commission_percentage)), (code, 'New name', 300, 12))
