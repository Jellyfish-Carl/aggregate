"""Public visualization entry: reports and the runnable layer workbench."""
from __future__ import annotations
import html
import json
from pathlib import Path
from .report.renderer import render_report as _render_financial_report

ROOT=Path(__file__).resolve().parents[1]


def render_settlement(result):
    """Offline realized settlement, separate from forecast strategy reports."""
    from ..accounting.full_accounting import COST_KEYS
    labels=('年度电能量','月度电能量','旬内电能量','D-3滚撮','D-2滚撮','日前现货','实时现货','考核与罚款','储能退化')
    esc=lambda value:html.escape(str(value))
    number=lambda value:'—' if value is None else f'{value:,.2f}'
    def table(heads,rows):
        return '<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in heads)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    metrics=[('公司成本',result['procurement_cost_yuan']),('公司售电收入',result['retail_revenue_yuan']),('公司利润',result['profit_yuan']),('客户合计节省',result['customer_saving_yuan'])]
    cards='<div class="metrics">'+''.join('<div class="metric"><small>'+title+' · 元</small><strong>'+number(v)+'</strong></div>' for title,v in metrics)+'</div>'
    costs=table(('成本组成','金额 · 元'),[(n,number(result['cost_breakdown'][k])) for k,n in zip(COST_KEYS,labels)])
    customers=table(('客户','套餐','电量MWh','分时基准账单','实际套餐账单','客户节省','公司分摊成本','分时均价分摊收入','公司实际利润','实际价差元/MWh'),[
        [c['customer_id'],c['package']]+[number(c[k]) for k in ('energy_mwh','tou_bill_yuan','actual_bill_yuan','saving_yuan','allocated_cost_yuan','allocated_revenue_yuan','company_profit_on_customer_yuan','spread_yuan_per_mwh')] for c in result['customers']])
    css=(ROOT/'utils'/'report'/'report.css').read_text(encoding='utf-8')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>标的日结算 · {esc(result['target_date'])}</title><style>{css}</style></head><body><header class="topbar"><h1>标的日结算复盘</h1><span>{esc(result['target_date'])} · {esc(result['source'])}</span></header><main><section class="visual-section"><p>案例 {esc(result['case_id'])}；采用已确认计量与储能动作核算，无事后最优调度。</p>{cards}</section><section class="visual-section"><h2>客户分时电价基准与实际账单</h2><p>分时基准总账单 {number(result['tou_baseline_bill_yuan'])} 元；公司价差 {number(result['spread_yuan_per_mwh'])} 元/MWh。</p><p>公司成本/组合收入按每点电量与组合平均购售电价分摊；客户实际应收按其已签套餐单独计算。</p>{customers}</section><section class="visual-section"><h2>成本组成</h2>{costs}<p>卖出收入为负成本，成交费用计入相应电能量；零负荷点无法分摊的成本：{number(result.get('unallocated_cost_yuan',0.))} 元。</p></section><section class="visual-section"><h2>预测策略与核对</h2><p><a href="07_STORAGE-DA.html">批发96点申报和储能计划</a> · <a href="risk_comparison.html">五λ期望价差与区间</a> · <a href="settlement.json">全部客户96点结算明细</a></p><p>{esc(result['settlement_scope'])}</p><pre>{esc(json.dumps(result['accounting_checks'],ensure_ascii=False,indent=2))}</pre></section></main></body></html>'''


def _workbench(page):
    controls=(ROOT/'web'/'controls.html').read_text(encoding='utf-8')
    css=(ROOT/'web'/'styles.css').read_text(encoding='utf-8')
    js=(ROOT/'web'/'app.js').read_text(encoding='utf-8')
    return page.replace('</head>','<style>'+css+'</style></head>').replace('<main>','<main>'+controls,1).replace('</body>','<script>'+js+'</script></body>')


def render_report(payload):
    return _workbench(_render_financial_report(payload))


def render_layer(result):
    """A node report never runs an upstream or downstream optimizer."""
    from ..service.full_evaluate import account_layer, report_defaults
    from ..service.layer_runner import config_from
    if 'raw_solution' in result:
        item=account_layer(result)
        config=config_from(result['inputs'])
        payload=dict(results=[item],packages=[result['package']],risk_lambdas=[result['risk_lambda']],cvar_alpha=.95,target_date=result['inputs'].get('target_date','目标交割日'),
            input_defaults=report_defaults(config,result['inputs']),remaining_issues=[
                '当前页面只展示本节点/当前λ的结果；请在案例页完成日前策略后点击λ区间复算，或运行 service.demo。',
                '当前曲线为事前预测或日内剩余计划；未来价格与负荷不是已实现数据。',
                '中长期费用采用日等效预测口径；是否将储能计入考核分母须按实际结算规定确认。'])
        return render_report(payload)
    esc=lambda x:html.escape(str(x))
    solver=result['solver']; curve=result['contract_curve_mwh']; peak=max(max(curve),1.)
    bars=''.join(f'<rect x="{45+i*9.5:.2f}" y="{210-v/peak*160:.2f}" width="7" height="{v/peak*160:.2f}" fill="#087f78"><title>时段{i+1}: {v:.5f} MWh</title></rect>' for i,v in enumerate(curve))
    rows=''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in (a['event'],a['side'],a['quantity_mwh'],a['resulting_daily_position_mwh'],a['solver_status']))+'</tr>' for a in result['actions'])
    timing=solver.get('valuation_solver',solver)
    body=f'''<section class="node-summary"><h2>{esc(result['node'])} · 独立计算结果</h2>
    <p>套餐 {esc(result['package'])} / λ={result['risk_lambda']:g} · 前置来源 {esc(result['input_source'])} · 状态 {esc(result['status'])}</p>
    <div class="node-kpis"><div><span>累计合同供给 · MWh/日</span><strong>{sum(curve):,.3f}</strong></div><div><span>已锁定合同成本 · 元/日</span><strong>{result['contract_cost_yuan']:,.2f}</strong></div><div><span>本节点用时 · 秒</span><strong>{result['elapsed_seconds']:.3f}</strong></div></div>
    <h3>96点中长期合同供给</h3><svg viewBox="0 0 1010 255" role="img" aria-label="合同曲线"><text x="45" y="25">MWh / 15分钟</text>{bars}<line x1="45" y1="210" x2="960" y2="210" stroke="#789"/><text x="45" y="240">00:00</text><text x="490" y="240">12:00</text><text x="925" y="240">24:00</text></svg>
    <div class="table-wrap"><table><thead><tr><th>节点</th><th>方向</th><th>成交产品电量·MWh</th><th>累计日供给·MWh</th><th>状态</th></tr></thead><tbody>{rows}</tbody></table></div>
    <p>此节点只形成合同仓位。最终成本收益需要继续计算日前现货与储能；页面上方可选择本节点JSON作为下游输入。</p>
    <details><summary>求解状态与模型规模</summary><pre>{esc(json.dumps(solver,ensure_ascii=False,indent=2))}</pre></details></section>'''
    css=(ROOT/'utils'/'report'/'report.css').read_text()
    page=f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(result["node"])} · 分层计算</title><style>{css}</style></head><body><header class="topbar"><h1>批发零售集合体</h1><a href="/" style="color:white">返回完整报告</a></header><main>{body}</main></body></html>'
    return _workbench(page)


def render_report_file(payload, output=None):
    path=Path(output or ROOT/'resource'/'output'/'visualize.html').resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(render_report(payload),encoding='utf-8')
    return path


def empty_workbench():
    css=(ROOT/'utils'/'report'/'report.css').read_text()
    return _workbench('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>分层计算</title><style>'+css+'</style></head><body><header class="topbar"><h1>批发零售集合体</h1></header><main><p>请选择节点计算，完成后打开本次曲线和成本结果。</p></main></body></html>')
