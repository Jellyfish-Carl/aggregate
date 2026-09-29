"""Exercise the real job worker and artifact API without opening a network socket."""
import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from pifa_lingshou.web import server
from pifa_lingshou.service.cases import CaseService


class WebJobTests(unittest.TestCase):
    def test_case_routes_and_real_recommendation_worker_without_socket(self):
        def request(path,payload=None):
            handler=server.Handler.__new__(server.Handler);handler.path=path
            capture={}
            handler.send_json=lambda obj,status=200:capture.update(body=obj,status=status)
            if payload is None:handler.do_GET()
            else:
                body=json.dumps(payload).encode();handler.headers={'Content-Length':str(len(body))};handler.rfile=BytesIO(body)
                handler.do_POST()
            return capture
        with TemporaryDirectory() as directory,patch.object(server,'CASES',CaseService(directory)):
            created=request('/api/cases/create',{'settings':{'customer_count':2,'scenario_count':4}})
            self.assertEqual(created['status'],200)
            cid=created['body']['state']['case_id']
            bad=request('/api/cases/confirm',{'case_id':cid,'revision':1,'packages':{'C1':'F','C2':'L'}})
            self.assertEqual(bad['status'],400)
            queued=request('/api/cases/run',{'case_id':cid,'revision':1,'action':'recommend'})
            result=self.wait_job(queued['body']['job_id'])
            self.assertEqual(result['status'],'DONE',result.get('error'))
            loaded=request('/api/cases/'+cid)['body']
            self.assertEqual(loaded['state']['status'],'RECOMMENDED')
            signed=request('/api/cases/confirm',{'case_id':cid,'revision':1,'packages':{'C1':'F','C2':'L'}})
            self.assertEqual(signed['body']['state']['status'],'SIGNED')
            self.assertEqual(len(request('/api/cases')['body']['cases']),1)

    def wait_job(self,jid):
        deadline=monotonic()+30
        while monotonic()<deadline:
            with server.LOCK: row=dict(server.JOBS[jid])
            if row['status'] in ('DONE','ERROR'): return row
            sleep(.02)
        self.fail('本地任务未在30秒内完成')

    def test_independent_job_artifacts_catalog_and_upstream(self):
        with TemporaryDirectory() as directory, patch.object(server,'OUT',Path(directory)), patch.object(server,'CATALOG',{}):
            jid=server.start_job({'node':'STORAGE-DA','inputs':{'risk_lambda':0.,'annual_price':411.,'scenario_count':8}})
            row=self.wait_job(jid)
            self.assertEqual(row['status'],'DONE',row.get('error'))
            self.assertEqual(row['progress'],100.)
            path=Path(directory)/'jobs'/jid
            result=json.loads((path/'result.json').read_text())
            self.assertTrue(result['solver']['milp_executed'])
            self.assertEqual(result['upstream_runs'],[])
            page=(path/'visualize.html').read_text()
            self.assertIn('runner-progress',page);self.assertNotIn('@@DATA@@',page)
            self.assertEqual(server.catalog()[0]['result_url'],row['result_url'])
            jid2=server.start_job({'node':'STORAGE-RT','upstream_id':row['result_id'],'inputs':{
                'fixed_until':2,'actual_load_mwh':[2.,2.],'actual_rt_price':[300.,800.],
                'executed_storage':{'charge_mwh':[0.,0.],'discharge_mwh':[0.,0.],'soc_mwh':[10.,10.]}}})
            row2=self.wait_job(jid2)
            self.assertEqual(row2['status'],'DONE',row2.get('error'))
            result2=json.loads((Path(directory)/'jobs'/jid2/'result.json').read_text())
            self.assertEqual(result2['inputs']['annual_price'],411.)
            self.assertEqual(result2['raw_solution']['declaration_mwh'],result['raw_solution']['declaration_mwh'])
            self.assertEqual(result2['solver']['fixed_until'],2)
            jid3=server.start_job({'node':'STORAGE-RT','inputs':{'fixed_until':2}})
            self.assertEqual(self.wait_job(jid3)['status'],'ERROR')

if __name__=='__main__': unittest.main()
