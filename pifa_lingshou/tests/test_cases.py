"""Lifecycle invariants: dated forecasts, signing, real solvers and settlement."""
import copy
import json
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
from pifa_lingshou.inputs.case_data import mock_case,validate_case,NODES,layer_input
from pifa_lingshou.inputs.full_model import retail_prices
from pifa_lingshou.service.cases import CaseService
from pifa_lingshou.service.layer_runner import config_from
from pifa_lingshou.service.recommendation import recommend


class CaseTests(unittest.TestCase):
    def test_calendar_edges_and_signing_period(self):
        for day in ('2027-01-01','2028-02-29','2026-10-11','2026-10-21','2026-12-31'):
            data=mock_case({'target_date':day,'recommendation_days':30})
            self.assertLessEqual(data['signing_forecast']['start_date'],day)
            self.assertGreaterEqual(data['signing_forecast']['end_date'],day)
            validate_case(data)
        data=mock_case();data['forecasts']['D-2']['as_of']=data['target_date']
        with self.assertRaises(ValueError):validate_case(data)

    def test_recommendation_all_days_and_economic_floor(self):
        data=mock_case({'recommendation_days':7})
        initial=recommend(data)
        self.assertEqual([c['recommended_package'] for c in initial['customers']],['F','S','L'])
        self.assertEqual(initial['lambda_values'],[0.,.25,.5,.75,1.])
        self.assertEqual(set(initial['company_curve']['packages']),{'C1','C2','C3'})
        self.assertEqual(len(initial['company_curve']['lambda_curve']),5)
        for customer in initial['customers']:
            for candidate in customer['candidates']:
                self.assertEqual(len(candidate['lambda_curve']),5)
                self.assertLessEqual(candidate['saving_p10_yuan'],candidate['saving_p90_yuan'])
        data['signing_forecast']['days'][-1]['load_factors']['C1']=3.
        changed=recommend(data)
        self.assertGreater(changed['customers'][0]['daily_energy_cv'],initial['customers'][0]['daily_energy_cv'])
        self.assertGreater(changed['customers'][0]['candidates'][0]['expected_cost_yuan'],initial['customers'][0]['candidates'][0]['expected_cost_yuan'])
        data['recommendation_policy']['minimum_expected_profit_yuan']=1e15
        self.assertTrue(all(c['recommended_package'] is None for c in recommend(data)['customers']))
        data['market']['annual_reference_price']+=100
        self.assertGreater(recommend(data)['customers'][1]['candidates'][1]['expected_revenue_yuan'],changed['customers'][1]['candidates'][1]['expected_revenue_yuan'])

    def test_case_isolation_revisions_validation(self):
        with tempfile.TemporaryDirectory() as root:
            svc=CaseService(root);a=svc.create();b=svc.create();cid=a['state']['case_id']
            self.assertNotEqual(cid,b['state']['case_id'])
            a['input']['market']['monthly_reference_price']=499
            svc.save(cid,a['input'],1)
            self.assertNotEqual(svc.view(b['state']['case_id'])['input']['market']['monthly_reference_price'],499)
            self.assertTrue(svc.store.path('input',cid,'revision_0001.json').exists())
            with self.assertRaises(ValueError):svc.save(cid,a['input'],1)
            with self.assertRaises(ValueError):svc.run(cid,'ANNUAL',2)
            with self.assertRaises(ValueError):svc.store.path('input',cid,'../../output/other.json')
            bad=copy.deepcopy(a['input']);bad['customers'][0]['tou_price'][0]=float('nan')
            with self.assertRaises(ValueError):svc.save(cid,bad,2)

    def test_real_chain_intraday_fixed_history_and_settlement(self):
        with tempfile.TemporaryDirectory() as root:
            svc=CaseService(root);view=svc.create({'scenario_count':4,'target_date':'2026-10-28'})
            cid=view['state']['case_id'];view=svc.recommend(cid,1)
            packages={c['customer_id']:c['recommended_package'] for c in view['state']['recommendation']['customers']}
            view=svc.confirm(cid,packages,1)
            with self.assertRaises(ValueError):svc.save(cid,view['input'],1)
            with self.assertRaises(ValueError):svc.run(cid,'MONTHLY',1)
            for node in NODES[:7]:
                view=svc.run(cid,node,1)
                r=svc.store.read('output',cid,view['state']['stages'][-1]['result_file'])
                self.assertIn(r['status'],('OPTIMAL','EXECUTED','LIMIT_REACHED'))
                if node in ('ANNUAL','MONTHLY','TEN_DAY','L3-A','STORAGE-DA'):self.assertTrue(r['solver']['milp_executed'])
            raw=r['raw_solution'];self.assertGreater(sum(raw['charge_mwh']),0)
            self.assertGreater(sum(raw['discharge_mwh']),0)
            annual=svc.store.read('output',cid,'01_ANNUAL.json')
            self.assertEqual(annual['fills'][0]['equivalent_delivery_days'],365)
            with self.assertRaises(ValueError):svc.update_forecast(cid,'ANNUAL',view['input']['forecasts']['ANNUAL'],1)
            # Recompute two lambdas from archived node inputs; do not post new orders.
            view=svc.compare_risk(cid,1,lambdas=(0.,1.))
            self.assertEqual(len(view['state']['stages']),7)
            compare=svc.store.read('output',cid,'risk_comparison.json')
            self.assertEqual(len(compare['results']),2)
            actual=svc.actual_template(cid);fixed=4
            execution=dict(fixed_until=fixed,actual_load_mwh=[sum(v[j] for v in actual['customer_load_mwh'].values()) for j in range(fixed)],actual_rt_price=actual['real_time_price'][:fixed],executed_storage={k:actual[k][:fixed] for k in ('charge_mwh','discharge_mwh','soc_mwh')})
            view=svc.run(cid,'STORAGE-RT',1,execution)
            actual=svc.actual_template(cid)
            tampered=copy.deepcopy(actual);tampered['real_time_price'][0]+=1
            with self.assertRaises(ValueError):svc.settle(cid,tampered,1)
            result=svc.settle(cid,actual,1)['state']['settlement']
            self.assertFalse(result['dispatch_reoptimized'])
            self.assertAlmostEqual(result['profit_yuan'],result['retail_revenue_yuan']-result['procurement_cost_yuan'],6)
            self.assertAlmostEqual(sum(c['allocated_cost_yuan'] for c in result['customers']),result['procurement_cost_yuan'],6)
            data=view['input'];cfg=config_from(layer_input(data,'STORAGE-RT',packages))
            prices=retail_prices(cfg,'F',actual['real_time_price'])
            for customer in data['customers']:
                key=customer['terms']['customer_id'];entry=next(c for c in result['customers'] if c['customer_id']==key)
                self.assertAlmostEqual(entry['tou_bill_yuan'],sum(a*b for a,b in zip(actual['customer_load_mwh'][key],customer['tou_price'])),6)
                self.assertAlmostEqual(entry['actual_bill_yuan'],sum(a*b for a,b in zip(actual['customer_load_mwh'][key],prices[key])),6)
            self.assertTrue(svc.store.path('input',cid,'settlement_actuals.json').is_file())

if __name__=='__main__':unittest.main()
