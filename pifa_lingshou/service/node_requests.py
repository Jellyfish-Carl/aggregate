"""Decision-node requests and immutable, timestamped execution records."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from zoneinfo import ZoneInfo

from .case_data import mock_case, validate_case, layer_input, curve
from ..utils.resource_store import RESOURCE, write_json
from .request_runner import solve_request

NODES = {'retail': 'PACKAGE_RECOMMEND', 'annual': 'ANNUAL', 'monthly': 'MONTHLY',
         'ten_day': 'TEN_DAY', 'd3': 'D-3', 'd2': 'D-2', 'day_ahead': 'L3-A',
         'storage_day_ahead': 'STORAGE-DA', 'storage_intraday': 'STORAGE-RT'}
FOLDERS = {v: k for k, v in NODES.items()}
PREVIOUS = dict(zip(list(NODES.values())[1:], list(NODES.values())[:-1]))
NAME = re.compile(r'request_\d{2,}_\d{8}_\d{6}_\d{6}')
RUN = re.compile(r'run_\d{8}_\d{6}_\d{6}')
LOCK = RLock()


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S_%f')


def validate_request(request):
    if not isinstance(request, dict) or request.get('node') not in FOLDERS:
        raise ValueError('未知决策节点')
    inputs = request.get('inputs')
    if not isinstance(inputs, dict):
        raise ValueError('inputs 必须为对象')
    json.dumps(request, allow_nan=False)
    if request['node'] == 'PACKAGE_RECOMMEND':
        validate_case(inputs)
    else:
        for key, value in inputs.items():
            if key in ('load_forecast_mwh', 'load_p10_mwh', 'load_p90_mwh',
                       'day_ahead_price_forecast', 'rt_price_forecast'):
                curve(value, key, key.startswith('load'))
        packages = inputs.get('customer_packages')
        if packages is not None and (not isinstance(packages, dict) or
                any(p not in ('F', 'L', 'S') for p in packages.values())):
            raise ValueError('customer_packages 须为客户 F/L/S 字典或 null（沿用上游）')
        if packages is not None and 'customers' in inputs and set(packages) != {c['customer_id'] for c in inputs['customers']}:
            raise ValueError('customer_packages 客户集合不匹配')
    return request


class RequestStore:
    def __init__(self, root=None):
        self.root = Path(root or RESOURCE).resolve()

    def path(self, node, request_id):
        if node not in NODES or not isinstance(request_id, str) or not NAME.fullmatch(request_id):
            raise ValueError('非法节点或请求编号')
        path = (self.root / 'input' / node / request_id / 'request.json').resolve()
        if not path.is_relative_to(self.root / 'input' / node):
            raise ValueError('非法请求路径')
        return path

    def read(self, node, request_id):
        path = self.path(node, request_id)
        raw = path.read_bytes()
        return dict(node=node, id=request_id, request=json.loads(raw),
                    revision=sha256(raw).hexdigest(), path=str(path))

    def listing(self, node=None):
        if node is not None and node not in NODES:
            raise ValueError('未知决策节点')
        return [dict(node=n, id=p.parent.name, path=str(p)) for n in ([node] if node else NODES)
                for p in sorted((self.root / 'input' / n).glob('request_*/request.json'), reverse=True)
                if NAME.fullmatch(p.parent.name)]

    def create(self, request, generation=None):
        validate_request(request)
        node = FOLDERS[request['node']]
        with LOCK:
            number = max((int(r['id'].split('_')[1]) for r in self.listing(node)), default=0) + 1
            ident = f'request_{number:02d}_{stamp()}'
            write_json(self.path(node, ident), request)
            if generation is not None:
                write_json(self.path(node, ident).with_name('generation.json'), generation)
            return self.read(node, ident)

    def save(self, node, request_id, request, revision):
        with LOCK:
            current = self.read(node, request_id)
            if current['revision'] != revision:
                raise ValueError('请求已被修改，请重新加载后编辑')
            validate_request(request)
            if FOLDERS[request['node']] != node:
                raise ValueError('request 节点与目录不一致')
            write_json(self.path(node, request_id), request)
            return self.read(node, request_id)

    def generate(self, settings):
        return self.generate_from_data(mock_case(settings), settings)

    def generate_from_data(self, data, settings=None, retail=None):
        validate_case(data)
        records = {}
        previous = None
        for folder, node in NODES.items():
            request = dict(node=node, inputs=deepcopy(data if folder == 'retail' else layer_input(data, node, None)))
            if previous:
                request['depends_on'] = dict(node=previous['node'], id=previous['id'])
            record = retail if folder == 'retail' and retail else self.create(request, dict(source='MOCK', settings=settings,
                note='每个文件仅对应一个决策节点。customer_packages=null 表示运行时沿用上游已确认套餐；日内实绩须另行填写。'))
            records[folder] = record
            previous = record
        return records

    def run_dir(self, node, request_id, run_id):
        self.path(node, request_id)
        if not isinstance(run_id, str) or not RUN.fullmatch(run_id):
            raise ValueError('非法运行编号')
        path = (self.root / 'output' / node / request_id / run_id).resolve()
        if not path.is_relative_to(self.root / 'output' / node):
            raise ValueError('非法运行路径')
        return path

    def history(self, node, request_id):
        self.path(node, request_id)
        rows = []
        for path in sorted((self.root / 'output' / node / request_id).glob('run_*/run.json'), reverse=True):
            rows.append(json.loads(path.read_text(encoding='utf-8')))
        return rows

    def latest(self, node, request_id):
        revision = self.read(node, request_id)['revision']
        for row in self.history(node, request_id):
            if row['status'] == 'DONE' and row['revision'] == revision:
                self.check_lineage(row)
                return row
        raise ValueError(f'请先运行 {node}/{request_id} 的当前输入版本')

    def check_lineage(self, row):
        if self.read(row['node'], row['id'])['revision'] != row['revision']:
            raise ValueError('上游输入已修改，请重跑上游节点')
        if row.get('upstream_run'):
            previous = row['upstream_run']
            self.check_lineage(previous)
            if not row.get('upstream_pinned'):
                current_revision = self.read(previous['node'], previous['id'])['revision']
                newest = next((r for r in self.history(previous['node'], previous['id'])
                               if r['status'] == 'DONE' and r['revision'] == current_revision), None)
                if newest and newest['run_id'] != previous['run_id']:
                    raise ValueError('上游已有更新的运行结果，请重跑中间节点或明确指定 depends_on.run_id')

    def execute(self, node, request_id, revision=None, upstream=None, progress=None, solver=None, allow_standalone=False):
        record = self.read(node, request_id)
        if revision is not None and revision != record['revision']:
            raise ValueError('请求已被修改，请重新加载')
        request = deepcopy(validate_request(record['request']))
        if FOLDERS[request['node']] != node:
            raise ValueError('request 节点与目录不一致')
        with LOCK:
            run_id = 'run_' + stamp()
            directory = self.run_dir(node, request_id, run_id)
            directory.mkdir(parents=True)
        row = dict(node=node, id=request_id, run_id=run_id, revision=record['revision'],
                   status='RUNNING', path=str(directory))
        write_json(directory / 'source_request.json', request)
        write_json(directory / 'run.json', row)
        try:
            dependency = request.get('depends_on')
            if upstream is not None and upstream.get('request_run'):
                reference = upstream['request_run']
                upstream_path = self.run_dir(reference['node'], reference['id'], reference['run_id']) / 'run.json'
                if upstream_path.exists():
                    row['upstream_run'] = json.loads(upstream_path.read_text())
                    row['upstream_pinned'] = True
            predecessor = None
            if dependency:
                if upstream is not None:
                    raise ValueError('depends_on 与显式 upstream 不能同时指定')
                expected = FOLDERS.get(PREVIOUS.get(request['node']))
                if dependency['node'] != expected and not (node == 'storage_intraday' and dependency['node'] == node):
                    raise ValueError('上游节点顺序错误')
                if dependency.get('run_id'):
                    path = self.run_dir(dependency['node'], dependency['id'], dependency['run_id'])
                    predecessor = json.loads((path / 'run.json').read_text())
                    if predecessor['status'] != 'DONE':
                        raise ValueError('指定上游运行未成功')
                    self.check_lineage(predecessor)
                    row['upstream_pinned'] = True
                else:
                    predecessor = self.latest(dependency['node'], dependency['id'])
                row['upstream_run'] = predecessor
                upstream = json.loads((Path(predecessor['path']) / 'response.json').read_text())
            inputs = request['inputs']
            selection = None
            if node == 'annual' and dependency:
                from .recommendation import validate_selection
                if not upstream.get('quality_certified') or not upstream.get('recommended_portfolio'):
                    raise ValueError('零售推荐尚未通过精度认证')
                recommended = upstream['recommended_portfolio']['packages']
                packages = recommended if inputs.get('customer_packages') is None else inputs['customer_packages']
                retail_data = json.loads((Path(predecessor['path']) / 'request.json').read_text())['inputs']
                if inputs.get('customers') != [c['terms'] for c in retail_data['customers']]:
                    raise ValueError('年度客户条款与零售推荐不同，请确认输入并重跑零售推荐')
                if packages != recommended:
                    selection = validate_selection(retail_data, upstream, packages)
                inputs['customer_packages'] = packages
                upstream = None
            elif node not in ('retail', 'annual') and dependency:
                packages = upstream['inputs']['customer_packages']
                if inputs.get('customer_packages') not in (None, packages):
                    raise ValueError('套餐与上游确认结果不一致')
                inputs['customer_packages'] = packages
            if node != 'retail' and 'customer_packages' in inputs and inputs['customer_packages'] is None:
                raise ValueError('须指定客户套餐或有效上游请求')
            if node not in ('retail', 'annual') and upstream is None and not allow_standalone:
                raise ValueError('非年度节点须指定上游结果，不能用默认持仓替代')
            if node == 'storage_intraday' and inputs.get('fixed_until', 0) <= 0:
                raise ValueError('日内阶段须填写 fixed_until 和已执行实绩')
            write_json(directory / 'request.json', request)
            if upstream is not None:
                write_json(directory / 'upstream.json', upstream)
            print('请求文件:', record['path'], '\n本次运行:', directory, flush=True)
            result = (solver or solve_request)(request, upstream, progress)
            if node == 'annual' and dependency:
                result['test_selection'] = dict(packages=packages, source='manual' if selection else 'recommended', signing_validation=selection)
            result['request_run'] = {k: row[k] for k in ('node', 'id', 'run_id', 'revision')}
            write_json(directory / 'response.json', result)
            row['status'] = 'DONE'
        except Exception as exc:
            row.update(status='ERROR', error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            write_json(directory / 'run.json', row)
        return dict(record=row, result=result)
