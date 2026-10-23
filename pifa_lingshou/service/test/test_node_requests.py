"""Persistent node boundaries, edit revisions, execution history and web API."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pifa_lingshou.service.node_requests import RequestStore, NODES
from pifa_lingshou.service.cases import CaseService
from pifa_lingshou.web import server


class NodeRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=RequestStore(self.root)
        self.settings=dict(customer_count=2,seed=123,target_date='2026-09-15',recommendation_days=7,scenario_count=4)
        self.records=self.store.generate(self.settings)

    def fake_solve(self,request,upstream,progress):
        if request['node']=='PACKAGE_RECOMMEND':
            packages={c['terms']['customer_id']:'F' for c in request['inputs']['customers']}
            return dict(node=request['node'],status='CERTIFIED',quality_certified=True,
                        recommended_portfolio=dict(packages=packages))
        return dict(node=request['node'],inputs=request['inputs'],status='OPTIMAL')

    def run_node(self,node,**kwargs):
        return self.store.execute(node,self.records[node]['id'],solver=self.fake_solve,**kwargs)

    def test_generate_all_nodes_separately_without_solving(self):
        with patch('pifa_lingshou.problem_solver.milp.milp',side_effect=AssertionError('must not solve')):
            another=self.store.generate(self.settings)
        self.assertEqual(set(another),set(NODES))
        for node,record in self.records.items():
            self.assertEqual(record['request']['node'],NODES[node])
            self.assertEqual(record['request']['inputs'],another[node]['request']['inputs'])
            self.assertTrue(another[node]['id'].startswith('request_02_'))
            self.assertEqual(Path(record['path']).parents[1].name,node)
        self.assertNotIn('signing_forecast',self.records['annual']['request']['inputs'])
        self.assertEqual(self.records['monthly']['request']['depends_on']['id'],self.records['annual']['id'])

    def test_edit_validation_revision_and_directory_boundary(self):
        record=self.records['retail'];request=deepcopy(record['request'])
        request['inputs']['customers'][0]['load_profile_mwh']=[1.]
        with self.assertRaises(ValueError):self.store.save('retail',record['id'],request,record['revision'])
        request=deepcopy(record['request']);request['inputs']['risk_lambda']=.8
        saved=self.store.save('retail',record['id'],request,record['revision'])
        self.assertNotEqual(saved['revision'],record['revision'])
        with self.assertRaisesRegex(ValueError,'重新加载'):self.store.save('retail',record['id'],request,record['revision'])
        with self.assertRaises(ValueError):self.store.read('retail','../../bad')
        with self.assertRaises(ValueError):self.store.read('../retail',record['id'])
        with self.assertRaisesRegex(ValueError,'节点与目录'):self.store.save('annual',self.records['annual']['id'],request,self.records['annual']['revision'])

    def test_repeated_runs_and_edits_never_overwrite_results(self):
        first=self.run_node('retail');before=(Path(first['record']['path'])/'response.json').read_bytes()
        second=self.run_node('retail')
        self.assertNotEqual(first['record']['run_id'],second['record']['run_id'])
        record=self.records['retail'];request=deepcopy(record['request']);request['inputs']['risk_lambda']=.8
        self.store.save('retail',record['id'],request,record['revision'])
        third=self.run_node('retail')
        self.assertEqual(len(self.store.history('retail',record['id'])),3)
        self.assertEqual((Path(first['record']['path'])/'response.json').read_bytes(),before)
        snapshot=json.loads((Path(third['record']['path'])/'request.json').read_text())
        self.assertEqual(snapshot['inputs']['risk_lambda'],.8)
        self.assertFalse((self.root/'solver_test').exists())

    def test_missing_failed_and_stale_upstream_cannot_be_used(self):
        with self.assertRaisesRegex(ValueError,'请先运行'):self.run_node('annual')
        self.assertEqual(self.store.history('annual',self.records['annual']['id'])[0]['status'],'ERROR')
        with patch('pifa_lingshou.service.recommendation.validate_selection') as check:
            self.run_node('retail');annual=self.run_node('annual');self.run_node('monthly')
        check.assert_not_called()
        self.assertEqual(annual['result']['test_selection']['source'],'recommended')
        retail=self.records['retail'];request=deepcopy(retail['request']);request['inputs']['risk_lambda']=.8
        self.store.save('retail',retail['id'],request,retail['revision'])
        with self.assertRaisesRegex(ValueError,'上游输入已修改'):self.run_node('ten_day')
        self.assertTrue((Path(annual['record']['path'])/'response.json').exists())

    def test_manual_packages_are_validated_and_downstream_locked(self):
        self.run_node('retail')
        record=self.records['annual'];request=deepcopy(record['request'])
        request['inputs']['customer_packages']={'C1':'L','C2':'L'}
        self.store.save('annual',record['id'],request,record['revision'])
        with patch('pifa_lingshou.service.recommendation.validate_selection',return_value={'valid':True}) as validate:
            annual=self.run_node('annual')
        validate.assert_called_once()
        self.assertEqual(annual['result']['test_selection']['source'],'manual')
        record=self.records['monthly'];request=deepcopy(record['request'])
        request['inputs']['customer_packages']={'C1':'F','C2':'F'}
        self.store.save('monthly',record['id'],request,record['revision'])
        with self.assertRaisesRegex(ValueError,'套餐与上游'):self.run_node('monthly')

    def test_new_upstream_run_requires_refresh_and_can_be_pinned(self):
        self.run_node('retail');first=self.run_node('annual');self.run_node('monthly')
        self.run_node('annual')
        with self.assertRaisesRegex(ValueError,'更新的运行'):self.run_node('ten_day')
        record=self.records['monthly'];request=deepcopy(record['request'])
        request['depends_on']['run_id']=first['record']['run_id']
        self.store.save('monthly',record['id'],request,record['revision'])
        monthly=self.run_node('monthly')
        self.assertEqual(monthly['record']['upstream_run']['run_id'],first['record']['run_id'])
        self.run_node('ten_day')

    def test_request_isolation_and_failure_record(self):
        self.run_node('retail')
        other=self.store.generate(self.settings)
        with self.assertRaisesRegex(ValueError,'请先运行'):
            self.store.execute('annual',other['annual']['id'],solver=self.fake_solve)
        def fail(*args):raise RuntimeError('solver failed')
        with self.assertRaises(RuntimeError):self.store.execute('retail',self.records['retail']['id'],solver=fail)
        history=self.store.history('retail',self.records['retail']['id'])
        self.assertEqual(history[0]['status'],'ERROR')
        self.assertIn('solver failed',history[0]['error'])
        self.assertEqual(history[1]['status'],'DONE')

    def test_web_edit_real_retail_annual_monthly_and_history(self):
        def api(path,payload=None):
            handler=server.Handler.__new__(server.Handler);handler.path=path;captured={}
            handler.send_json=lambda value,status=200:captured.update(body=value,status=status)
            if payload is None:handler.do_GET()
            else:
                body=json.dumps(payload).encode();handler.headers={'Content-Length':str(len(body))}
                handler.rfile=BytesIO(body);handler.do_POST()
            self.assertEqual(captured['status'],200,captured)
            return captured['body']
        with patch.object(server,'CASES',CaseService(self.root)),patch.object(server,'OUT',self.root/'output'),patch.object(server,'CATALOG',{}):
            self.assertEqual(len(api('/api/requests')['requests']),9)
            for node in ('retail','annual','monthly','monthly'):
                record=self.records[node]
                loaded=api(f'/api/requests/{node}/{record["id"]}')
                if node=='annual':
                    loaded['request']['inputs']['annual_price']=410.
                    loaded=api('/api/requests/save',loaded)
                job=api('/api/requests/run',dict(node=node,id=record['id'],revision=loaded['revision']))
                from pifa_lingshou.tests.test_web_jobs import WebJobTests
                status=WebJobTests().wait_job(job['job_id'])
                self.assertEqual(status['status'],'DONE',status)
            history=api(f'/api/requests/monthly/{self.records["monthly"]["id"]}/history')['runs']
            self.assertEqual(len(history),2)
            annual=self.store.latest('annual',self.records['annual']['id'])
            result=json.loads((Path(annual['path'])/'response.json').read_text())
            self.assertEqual(result['inputs']['annual_price'],410.)
            self.assertEqual(set(p.name for p in (self.root/'output').iterdir()),{'retail','annual','monthly'})


if __name__=='__main__':unittest.main()
