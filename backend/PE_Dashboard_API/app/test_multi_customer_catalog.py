import unittest
from services.azure_monitor import (
    get_known_catalog,
    get_known_resource_groups,
    discover_vms,
    search_vms_with_fallback,
    _synthesize_customer_estate,
)


class MultiCustomerCatalogTests(unittest.TestCase):
    def test_catalog_all_customers(self):
        subs, vms = get_known_catalog('')
        self.assertGreaterEqual(len(subs), 6)
        self.assertGreaterEqual(len(vms), 30)
        customers = {s.get('customer') for s in subs}
        self.assertIn('Target Corp', customers)
        self.assertIn('Walmart Global', customers)
        self.assertIn('Nebraska Furniture Mart', customers)

    def test_catalog_target_customer(self):
        subs, vms = get_known_catalog('target')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('customer') == 'Target Corp' for v in vms))
        roles = {v.get('type') for v in vms}
        self.assertIn('APP', roles)
        self.assertIn('DB', roles)
        self.assertIn('SRE', roles)

    def test_catalog_walmart_customer(self):
        subs, vms = get_known_catalog('walmart')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('customer') == 'Walmart Global' for v in vms))

    def test_catalog_kroger_customer(self):
        subs, vms = get_known_catalog('kroger')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('customer') == 'Kroger Supply Chain' for v in vms))

    def test_catalog_dhl_customer(self):
        subs, vms = get_known_catalog('dhl')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('customer') == 'DHL Supply Chain' for v in vms))

    def test_catalog_pepsi_customer(self):
        subs, vms = get_known_catalog('pepsi')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('customer') == 'PepsiCo Global' for v in vms))

    def test_dynamic_synthesis_arbitrary_customer(self):
        subs, vms = get_known_catalog('Costco')
        self.assertEqual(len(vms), 8)
        self.assertEqual(vms[0].get('customer'), 'Costco')
        roles = {v.get('type') for v in vms}
        self.assertEqual(roles, {'APP', 'DB', 'SRE'})

    def test_resource_groups_lookup(self):
        rgs = get_known_resource_groups('4a1e9b23-7c10-4f32-bb19-d830182410a1')
        self.assertTrue(rgs)
        rg_names = [r['name'] for r in rgs]
        self.assertIn('rg-tgt-scpo-prod-eastus2', rg_names)

    def test_discover_vms_fallback(self):
        cfg = {'azure_subscription_id': '4a1e9b23-7c10-4f32-bb19-d830182410a1'}
        vms = discover_vms(cfg)
        self.assertTrue(vms)
        self.assertEqual(vms[0]['customer'], 'Target Corp')

    def test_search_vms_fallback_never_throws_denied(self):
        vms, expanded = search_vms_with_fallback(None, 'Target')
        self.assertTrue(vms)
        self.assertEqual(vms[0]['customer'], 'Target Corp')


if __name__ == '__main__':
    unittest.main()
