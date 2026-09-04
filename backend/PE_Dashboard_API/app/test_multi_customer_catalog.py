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
        subs, vms = get_known_catalog('FedEx Logistics')
        self.assertEqual(len(vms), 8)
        self.assertEqual(vms[0].get('customer'), 'Fedex Logistics')
        roles = {v.get('type') for v in vms}
        self.assertEqual(roles, {'APP', 'DB', 'SRE'})

    def test_catalog_region_search(self):
        subs, vms = get_known_catalog('westeurope')
        self.assertTrue(vms)
        self.assertTrue(all(v.get('location') == 'westeurope' for v in vms))
        customers = {v.get('customer') for v in vms}
        self.assertIn('DHL Supply Chain', customers)

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

    def test_search_vms_unscoped_fallback_returns_catalog(self):
        vms, expanded = search_vms_with_fallback(None, 'nfm', subscription_ids=[])
        self.assertTrue(vms)
        self.assertIn('Nebraska Furniture Mart', vms[0]['customer'])

    def test_build_server_records_and_payload_preserves_server_details(self):
        from services.azure_monitor import _build_server_records
        from services.resource_calculator import build_resource_payload
        subs, vms = get_known_catalog('dhl')
        self.assertTrue(vms)
        records = _build_server_records(None, vms, 24)
        self.assertTrue(records)
        s0 = records[0]
        self.assertEqual(s0['customer'], 'DHL Supply Chain')
        self.assertEqual(s0['application'], 'SCPO')
        self.assertEqual(s0['environment'], 'PROD')
        self.assertEqual(s0['location'], 'westeurope')
        self.assertGreater(s0['cpu_used'], 0.0)
        self.assertGreater(s0['mem_used'], 0.0)
        self.assertGreater(s0['mem_total_gb'], 0.0)

        payload = build_resource_payload(records)
        p_servers = payload.get('servers', [])
        self.assertTrue(p_servers)
        p0 = p_servers[0]
        self.assertEqual(p0['customer'], 'DHL Supply Chain')
        self.assertEqual(p0['application'], 'SCPO')
        self.assertEqual(p0['location'], 'westeurope')
        self.assertGreater(p0['cpu_used'], 0.0)
        self.assertGreater(p0['mem_used'], 0.0)


if __name__ == '__main__':
    unittest.main()
