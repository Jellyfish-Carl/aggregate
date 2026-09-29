import unittest

from pifa_lingshou.service.layer_runner import run_layer


class IndependentLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain=run_layer('CHAIN', {'package':'F','risk_lambda':0.0,'scenario_count':8})

    def test_complete_chain_runs_each_layer_and_storage_is_fast_milp(self):
        result = self.chain
        self.assertEqual(result['node'], 'STORAGE-DA')
        self.assertEqual(result['solver']['backend'], 'scipy.optimize.milp / HiGHS')
        self.assertTrue(result['solver']['milp_executed'])
        self.assertEqual(result['solver']['binary_count'], 96)
        self.assertLess(result['solver']['solve_seconds'], 2.0)
        self.assertGreater(sum(result['raw_solution']['charge_mwh']), 0)
        self.assertGreater(sum(result['raw_solution']['discharge_mwh']), 0)
        self.assertIn('L3-A', [item['node'] for item in result['upstream_runs']])

    def test_intraday_requires_actual_prefix_and_preserves_it(self):
        upstream = self.chain
        inputs = {'package':'F','risk_lambda':0.0,'scenario_count':8,'fixed_until':2,
            'actual_load_mwh':[1.0,1.0],'actual_rt_price':[300.0,500.0]}
        with self.assertRaisesRegex(ValueError, 'executed_storage'):
            run_layer('STORAGE-RT', inputs, upstream)
        prior=upstream['raw_solution']
        inputs['executed_storage']={key:prior[key][:2] for key in ('charge_mwh','discharge_mwh','soc_mwh')}
        result=run_layer('STORAGE-RT',inputs,upstream)
        self.assertEqual(result['solver']['fixed_until'],2)
        for key in ('charge_mwh','discharge_mwh','soc_mwh'):
            self.assertEqual(result['raw_solution'][key][:2],prior[key][:2])
        for scenario in result['raw_solution']['scenarios']:
            for buy,sell in zip(scenario['rt_buy_mwh'],scenario['rt_sell_mwh']):
                self.assertLessEqual(min(buy,sell),1e-10)
        invalid={**inputs,'executed_storage':{**inputs['executed_storage'],'soc_mwh':[9.,9.]}}
        with self.assertRaisesRegex(ValueError,'实绩不可改写'):
            run_layer('STORAGE-RT',invalid,result)
        with self.assertRaisesRegex(ValueError,'不能回退'):
            run_layer('STORAGE-RT',{'fixed_until':1},result)

    def test_signed_terms_are_locked_and_json_upstream_is_accepted(self):
        import json
        upstream=json.loads(json.dumps(self.chain))
        result=run_layer('STORAGE-DA',{},upstream)
        self.assertEqual(result['raw_solution']['declaration_mwh'],upstream['raw_solution']['declaration_mwh'])
        with self.assertRaisesRegex(ValueError,'已签套餐'):
            run_layer('STORAGE-DA',{'package':'S'},upstream)
        with self.assertRaisesRegex(ValueError,'申报已锁定'):
            run_layer('STORAGE-DA',{'declaration_mwh':[1.]*96},upstream)

    def test_each_customers_signed_package_enters_scenario_revenue(self):
        from pifa_lingshou.inputs.full_model import retail_prices,FullModelInput
        from pifa_lingshou.service.full_evaluate import account_layer
        assignment={'C1':'F','C2':'L'}
        result=run_layer('STORAGE-DA',{'scenario_count':8,'risk_lambda':0.,'customer_packages':assignment})
        config=FullModelInput(customer_packages=assignment)
        for s in result['raw_solution']['scenarios']:
            prices=retail_prices(config,'S',s['real_time_price'])
            expected=sum(s['load_mwh'][j]*config.customer_shares[i][j]*prices[c.customer_id][j]
                for i,c in enumerate(config.customers) for j in range(96))
            self.assertAlmostEqual(expected,s['retail_revenue_yuan'],places=6)
        accounted=account_layer(result)
        self.assertEqual(accounted['customer_results']['C1']['package'],'F')
        self.assertEqual(accounted['customer_results']['C2']['package'],'L')
        with self.assertRaisesRegex(ValueError,'已签套餐'):
            run_layer('STORAGE-DA',{'customer_packages':{'C1':'S','C2':'L'}},result)

    def test_intraday_forecast_update_preserves_actual_and_refreshes_band(self):
        from pifa_lingshou.service.full_evaluate import declaration_for
        upstream=self.chain
        inputs={'fixed_until':1,'actual_load_mwh':[1.5],'actual_rt_price':[333.],
            'executed_storage':{'charge_mwh':[0.],'discharge_mwh':[0.],'soc_mwh':[10.]},
            'load_forecast_mwh':[2.]*96,'load_p10_mwh':[1.9]*96,'load_p90_mwh':[2.1]*96,
            'rt_price_forecast':[700.]*96}
        result=run_layer('STORAGE-RT',inputs,upstream)
        self.assertEqual(result['raw_solution']['declaration_mwh'],upstream['raw_solution']['declaration_mwh'])
        self.assertEqual(declaration_for(result)['rows'][5]['p90_mwh'],2.1)
        for s in result['raw_solution']['scenarios']:
            self.assertEqual(s['load_mwh'][0],1.5);self.assertEqual(s['real_time_price'][0],333.)
        self.assertNotEqual(result['raw_solution']['scenarios'][0]['load_mwh'][1:],upstream['raw_solution']['scenarios'][0]['load_mwh'][1:])


if __name__ == '__main__':
    unittest.main()
