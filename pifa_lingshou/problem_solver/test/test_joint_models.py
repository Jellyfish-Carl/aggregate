"""Global package-search equivalence and L3 information/locking invariants."""
from copy import deepcopy
from itertools import product
from unittest.mock import patch
import unittest

from pifa_lingshou.service.case_data import mock_case,layer_input,validate_case
from pifa_lingshou.service.recommendation import recommend,signing_input,package_options
from pifa_lingshou.service.layer_runner import run_layer,solve_l1_input
from pifa_lingshou.problem_solver.milp import MilpSolveError


class JointPackageTests(unittest.TestCase):
    def mixed_case(self,n=3):
        data=mock_case({'customer_count':n,'recommendation_days':7,
            'recommendation_policy':{'mip_relative_gap':0.}})
        for index,c in enumerate(data['customers']):
            if index%3:
                c['terms']['fixed_price']=[1000.]*96
                c['terms']['service_fee']=[22. if index%3==1 else 0.]*96
        return data

    def test_joint_search_matches_exhaustive_fixed_annual_l1(self):
        data=self.mixed_case();inp=signing_input(data)
        _,options,_=package_options(data,inp)
        actual=recommend(data)
        self.assertTrue(actual['quality_certified'])
        packages=actual['recommended_portfolio']['packages']
        self.assertEqual(set(packages.values()),{'F','L','S'})
        feasible=[]
        for assignment in product(('F','L','S'),repeat=3):
            mix=dict(zip(options,assignment))
            if any(not options[cid][p]['eligible'] for cid,p in mix.items()):continue
            try:
                action=solve_l1_input({**inp,'customer_packages':mix})
            except MilpSolveError:continue
            feasible.append(action.solver_details['objective_yuan'])
        self.assertTrue(feasible)
        self.assertAlmostEqual(actual['recommended_portfolio']['risk_adjusted_net_loss_yuan'],min(feasible),places=5)
        fixed=run_layer('ANNUAL',{**actual['annual_inputs'],'customer_packages':packages})
        self.assertAlmostEqual(fixed['solver']['objective_yuan'],min(feasible),places=5)
        self.assertLess(fixed['solver']['objective_reconciliation_yuan'],1e-4)

    def test_more_than_eight_and_one_hundred_have_independent_choices(self):
        for count in (10,100):
            result=recommend(self.mixed_case(count))
            self.assertTrue(result['quality_certified'],result['status'])
            self.assertEqual(len(result['recommended_portfolio']['packages']),count)
            self.assertEqual(result['combination_space']['theoretical_count'],str(3**count))
            self.assertEqual(result['solver']['binary_count'],3*count+1)
            self.assertEqual(set(result['recommended_portfolio']['packages'].values()),{'F','L','S'})
            self.assertLessEqual(result['solver']['mip_gap'],.03)

    def test_timeout_without_precision_is_not_a_recommendation(self):
        data=self.mixed_case();inp=signing_input(data);_,opts,_=package_options(data,inp)
        action=solve_l1_input(inp,retail_options=opts)
        action.solver_details.update(status='LIMIT_REACHED',mip_gap=.031)
        with patch('pifa_lingshou.service.recommendation.solve_l1_input',return_value=action):
            result=recommend(data)
        self.assertIsNotNone(result['candidate_portfolio'])
        self.assertIsNone(result['recommended_portfolio'])
        self.assertFalse(result['quality_certified'])
        self.assertTrue(all(c['recommended_package'] is None for c in result['customers']))

    def test_economic_infeasibility_does_not_relax_policy(self):
        data=self.mixed_case()
        data['recommendation_policy']['minimum_total_profit_yuan']=1e15
        result=recommend(data)
        self.assertEqual(result['status'],'INFEASIBLE')
        self.assertIsNone(result['recommended_portfolio'])
        self.assertIsNone(result['candidate_portfolio'])

    def test_future_annual_forecast_is_not_used_at_signing(self):
        data=self.mixed_case()
        before=recommend(data)
        data['forecasts']['ANNUAL']['real_time_price']=[9000.]*96
        data['forecasts']['ANNUAL']['customer_load_mwh']={c['terms']['customer_id']:[9000.]*96 for c in data['customers']}
        after=recommend(data)
        self.assertEqual(before['annual_inputs'],after['annual_inputs'])
        self.assertEqual(before['recommended_portfolio']['packages'],after['recommended_portfolio']['packages'])

    def test_policy_rejects_looser_gap_and_nonfinite_budget(self):
        for key,value in (('mip_relative_gap',.031),('time_limit_seconds',float('nan'))):
            data=mock_case();data['recommendation_policy'][key]=value
            with self.assertRaises(ValueError):validate_case(data)


class JointL3Tests(unittest.TestCase):
    def test_storage_is_in_submission_and_prices_collapse_after_publication(self):
        data=mock_case({'scenario_count':4})
        packages={c['terms']['customer_id']:'F' for c in data['customers']}
        before=run_layer('L3-A',layer_input(data,'L3-A',packages))
        self.assertTrue(before['solver']['declaration_optimized'])
        self.assertFalse(before['solver']['day_ahead_price_known'])
        self.assertGreater(sum(before['raw_solution']['charge_mwh']),0.)
        self.assertGreater(sum(before['raw_solution']['discharge_mwh']),0.)
        self.assertTrue(any(before['raw_solution']['scenarios'][0]['day_ahead_price']!=s['day_ahead_price'] for s in before['raw_solution']['scenarios'][1:]))
        inp=layer_input(data,'STORAGE-DA',packages)
        inp['known_day_ahead_price']=[450.+j for j in range(96)]
        after=run_layer('STORAGE-DA',inp,before)
        self.assertEqual(after['solver']['model_structure'],before['solver']['model_structure'])
        self.assertFalse(after['solver']['declaration_optimized'])
        self.assertTrue(after['solver']['day_ahead_price_known'])
        self.assertEqual(after['raw_solution']['declaration_mwh'],before['raw_solution']['declaration_mwh'])
        self.assertEqual(after['fills'],before['fills'])
        for s in after['raw_solution']['scenarios']:
            self.assertEqual(s['day_ahead_price'],inp['known_day_ahead_price'])
        self.assertLess(after['solver']['objective_reconciliation_yuan'],1e-4)
        invalid={'known_day_ahead_price':[1.]*96}
        with self.assertRaisesRegex(ValueError,'已公布日前价格'):
            run_layer('STORAGE-RT',invalid,after)


if __name__=='__main__':unittest.main()
