"""Cash-flow, probability and execution invariants of the independent battery MILP."""
import unittest
from copy import deepcopy
from pifa_lingshou.problem_solver.storage_milp import dispatch, solve_storage_milp


def fixture(probabilities=(1.,), prices=None):
    prices=prices or [100.]*48+[800.]*48
    return dict(declaration_mwh=[1.]*96,contract_curve_mwh=[1.]*96,
        annual_contract_curve_mwh=[.8]*96,contract_cost_yuan=96*405.,
        day_ahead_buy_mwh=[0.]*96,day_ahead_sell_mwh=[0.]*96,
        scenarios=[dict(scenario_id=str(i),probability=p,load_mwh=[1.]*96,
            day_ahead_price=list(prices),real_time_price=[x+i for x in prices],
            retail_revenue_yuan=96*460.) for i,p in enumerate(probabilities)])


class StorageDispatchTests(unittest.TestCase):
    def test_storage_entrypoints_share_one_implementation(self):
        self.assertIs(solve_storage_milp, dispatch)

    def test_deterministic_arbitrage_cashflow_and_assessment_timing(self):
        result=dispatch(fixture(),settings={'assessment_basis':'customer_load','hard_deviation':True})
        raw=result['raw_solution']; scenario=raw['scenarios'][0]
        self.assertEqual(result['optimization']['status'],'OPTIMAL')
        self.assertAlmostEqual(sum(raw['charge_mwh']),4.8,places=6)
        self.assertAlmostEqual(sum(raw['discharge_mwh']),4.8*.92**2,places=6)
        self.assertAlmostEqual(sum(raw['charge_mwh'][48:]),0,places=6)
        self.assertAlmostEqual(sum(raw['discharge_mwh'][:48]),0,places=6)
        saving=96*405-scenario['wholesale_cost_yuan']
        self.assertAlmostEqual(saving,800*4.8*.92**2-100*4.8-2.01*4.8*(1+.92**2),places=6)
        self.assertLess(result['optimization']['objective_reconciliation_yuan'],1e-6)

    def test_full_sample_keeps_nonuniform_probabilities_and_reduced_is_unique(self):
        raw=fixture((.02,.7,.03,.25))
        full=dispatch(raw,.4,{'optimization_scenario_count':4})
        self.assertEqual([s['probability'] for s in full['optimization']['optimization_scenarios']],[.02,.7,.03,.25])
        reduced=dispatch(raw,.4,{'optimization_scenario_count':3})
        opt=reduced['optimization']; reps=opt['optimization_scenarios']
        self.assertEqual(len(set(s['scenario_id'] for s in reps)),3)
        self.assertAlmostEqual(sum(s['probability'] for s in reps),1.)
        self.assertEqual(opt['evaluation_scenario_count'],4)
        self.assertLess(opt['objective_reconciliation_yuan'],1e-6)
        self.assertEqual([s['probability'] for s in reduced['raw_solution']['scenarios']],[.02,.7,.03,.25])

    def test_negative_prices_remain_net_settled_and_reconcile(self):
        raw=fixture(prices=[-100.]*48+[30.]*48)
        result=dispatch(raw,.5,{'assessment_basis':'customer_load'})
        self.assertLess(result['optimization']['objective_reconciliation_yuan'],1e-6)
        for s in result['raw_solution']['scenarios']:
            for b,v in zip(s['rt_buy_mwh'],s['rt_sell_mwh']): self.assertLessEqual(min(b,v),1e-8)

    def test_optional_connection_limits_bound_metered_flow(self):
        result=dispatch(fixture(),settings={'grid_import_limit_mwh':1.05,'grid_export_limit_mwh':0.})
        raw=result['raw_solution']
        for s in raw['scenarios']:
            for q,c,d in zip(s['load_mwh'],raw['charge_mwh'],raw['discharge_mwh']):
                self.assertGreaterEqual(q+c-d,-1e-8)
                self.assertLessEqual(q+c-d,1.05+1e-8)

    def test_contract_assessment_is_charged_in_affected_half_hour(self):
        raw=fixture(prices=[500.]*96)
        raw['contract_curve_mwh']=[1.]*96; raw['contract_curve_mwh'][0]=2.
        raw['day_ahead_sell_mwh'][0]=1.
        result=dispatch(raw,settings={'maximum_charge_mwh':0.,'maximum_discharge_mwh':0.})
        s=result['raw_solution']['scenarios'][0]
        fees=[p['contract_assessment_penalty_yuan'] for p in s['period_costs']]
        self.assertAlmostEqual(fees[0],1.05*(500-416)*(.8)/2,places=6)
        self.assertEqual(fees[0],fees[1]); self.assertEqual(sum(fees[2:]),0.)
        self.assertAlmostEqual(sum(fees),s['contract_assessment_penalty_yuan'])

    def test_hard_band_covers_omitted_original_scenarios(self):
        from pifa_lingshou.problem_solver.milp import MilpSolveError
        raw=fixture((.99,.01));raw['scenarios'][1]['load_mwh']=[3.]*96
        with self.assertRaisesRegex(MilpSolveError,'INFEASIBLE'):
            dispatch(raw,settings={'optimization_scenario_count':1,'hard_deviation':True})

    def test_rejects_nonfinite_or_inconsistent_locked_inputs(self):
        raw=fixture();raw['day_ahead_buy_mwh'][0]=1.;raw['day_ahead_sell_mwh'][0]=1.
        with self.assertRaisesRegex(ValueError,'同时买卖'): dispatch(raw)
        with self.assertRaisesRegex(ValueError,'charge_mwh'):
            dispatch(fixture(),fixed_until=2,prior={'charge_mwh':[0.]})


if __name__=='__main__': unittest.main()
