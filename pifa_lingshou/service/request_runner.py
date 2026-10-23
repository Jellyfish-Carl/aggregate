"""把 {node, inputs} 请求交给现有业务求解入口，不另建一套模型。"""
import argparse
import json
from pathlib import Path

from .layer_runner import run_layer
from ..utils.resource_store import write_json


def solve_request(request, upstream=None, progress=None):
    if not isinstance(request, dict):
        raise ValueError('request 必须是包含 node 和 inputs 的 JSON 对象')
    if not isinstance(request.get('node'), str) or not isinstance(request.get('inputs'), dict):
        raise ValueError('request 必须包含字符串 node 和对象 inputs')
    if request['node'] == 'PACKAGE_RECOMMEND':
        from .case_data import validate_case
        from .recommendation import recommend
        if upstream is not None:
            raise ValueError('套餐选择是签约前阶段，不接收已成交的 upstream')
        data = request['inputs']
        if not {'customers', 'signing_forecast', 'forecasts', 'recommendation_policy', 'market'} <= data.keys():
            raise ValueError('PACKAGE_RECOMMEND 的 inputs 必须是完整案例数据，不能使用年度采购的扁平 inputs')
        validate_case(data)
        result = recommend(data, progress)
        result.update(node='PACKAGE_RECOMMEND',
                      elapsed_seconds=result['performance']['end_to_end_seconds'])
        if 'input_revision' in request:
            result['input_revision'] = request['input_revision']
        return result
    # 网页的历史请求没有保存上游完整结果，不能用默认模拟持仓冒充历史重放。
    if 'input_revision' in request and request['node'] != 'ANNUAL' and upstream is None:
        raise ValueError('重跑历史非年度 request 须提供上一节点结果 upstream；request 本身不包含上游成交')
    result = run_layer(request['node'], request['inputs'], upstream, progress)
    if 'input_revision' in request:
        result['input_revision'] = request['input_revision']
    return result


def main():
    from .node_requests import RequestStore, FOLDERS
    parser = argparse.ArgumentParser(description='读取节点 request，每次求解保存独立运行记录')
    parser.add_argument('request', type=Path)
    parser.add_argument('--upstream', type=Path, help='显式提供上一节点结果；depends_on 请求自动读取其有效结果')
    parser.add_argument('--output', type=Path, help='可选额外导出文件；正式历史记录始终独立保留')
    args = parser.parse_args()
    path = args.request.resolve()
    request = json.loads(path.read_text(encoding='utf-8'))
    folder = FOLDERS[request['node']]
    if path.name == 'request.json' and path.parent.parent.name == folder and path.parents[2].name == 'input':
        store = RequestStore(path.parents[3]); record = store.read(folder, path.parent.name)
    else:
        store = RequestStore(); record = store.create(request, dict(source='CLI_IMPORT', file=str(path)))
    upstream = json.loads(args.upstream.read_text(encoding='utf-8')) if args.upstream else None
    saved = store.execute(folder, record['id'], upstream=upstream,
        progress=lambda p, m: print(f'{p:5.1f}% {m}', flush=True))
    if args.output:
        write_json(args.output, saved['result'])
    print('结果:', Path(saved['record']['path']) / 'response.json')
    print('状态:', saved['result']['status'], 'gap:', saved['result'].get('solver', {}).get('mip_gap'))


if __name__ == '__main__':
    main()
