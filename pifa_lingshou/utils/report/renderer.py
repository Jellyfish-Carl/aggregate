"""Self-contained two-page report, without automatic plan selection."""
from __future__ import annotations
import html
import json
from pathlib import Path
from .charts import lambda_interval


def render_report(payload):
    directory=Path(__file__).parent
    results=[r for r in payload.get('results',[]) if r.get('feasible')]
    if not results:
        reasons='; '.join(str(r.get('reason',r.get('status'))) for r in payload.get('results',[]))
        return '<!doctype html><meta charset="utf-8"><h1>没有可行结果</h1><p>'+html.escape(reasons)+'</p>'
    customer_ids=sorted({cid for r in results for cid in r.get('customer_results',{})})
    compact=[]
    for r in results:
        item={k:v for k,v in r.items() if k not in {'wholesale_engine','scenario_results','customer_results','contracts'}}
        item['wholesale_cost_breakdown']={'expected':r.get('wholesale_cost_breakdown',{}).get('expected',{})}
        probabilities=[s['probability'] for s in r.get('scenario_results',[])]
        item['scenario_count']=len(probabilities)
        item['probabilities']=[1.0]
        # Expected series are for the separately labelled overview only.
        # Preserve pifa's execution-path breakdown for the declaration chart:
        # E[buy] and E[sell] can both be positive across different scenarios.
        item['schedule']=[dict(row) for row in r.get('schedule',[])]
        for row in item['schedule']:
            for key,value in list(row.items()):
                if isinstance(value,list) and len(value)==len(probabilities):
                    row[key]=[sum(v*p for v,p in zip(value,probabilities))]
        item['customer_results']={cid:{k:v for k,v in c.items() if k!='scenarios'} for cid,c in r.get('customer_results',{}).items()}
        source=r.get('wholesale_engine',{})
        breakdown=source.get('declaration_breakdown',{})
        item['declaration_view']={
            'rows':breakdown.get('rows',[]),
            'totals':breakdown.get('totals',{}),
            'assessment_guard':breakdown.get('assessment_guard',{}),
            'status':source.get('declaration',{}).get('status','PREDICTED'),
            'l3_objective':{key:source.get('l3_objective',{}).get(key) for key in (
                'milp_executed','objective_yuan','mip_gap'
            )},
        }
        action_keys=('event','side','quantity_mwh','equivalent_delivery_days','resulting_daily_position_mwh','solver_status')
        item['actions']=[{k:a.get(k) for k in action_keys}
            for a in source.get('portfolio',{}).get('optimized',{}).get('actions',[])]
        item['engine_status']=source.get('meta',{}).get('milp',{})
        compact.append(item)
    data={**{k:v for k,v in payload.items() if k not in {'results','feasible_results','selected','pareto_frontier'}},'results':compact}
    packages=list(dict.fromkeys(r['package'] for r in results))
    locked_packages=payload.get('input_defaults',{}).get('customer_packages',{})
    if locked_packages:
        data['package_labels']={p:'已签客户套餐组合' for p in packages}
    sections=[]
    for cid in customer_ids:
        panes=[]
        for metric in ('spread','profit'):
            series=[]
            for package in packages:
                name={'F':'固定价 F','L':'市场联动 L','S':'比例分成 S'}[package]
                locked=payload.get('input_defaults',{}).get('customer_packages',{}).get(cid)
                if locked: name='已签组合 · 本客户 '+locked
                series.append('<div class="risk-series"><h4>'+name+'</h4>'+lambda_interval(results,cid,package,metric)+'</div>')
            panes.append('<div class="risk-grid" data-risk-metric="'+metric+'"'+(' hidden' if metric=='profit' else '')+'>'+''.join(series)+'</div>')
        sections.append('<section class="customer-risk"><h3>客户 '+html.escape(cid)+'</h3>'+''.join(panes)+'</section>')
    template=(directory/'report_template.html').read_text(encoding='utf-8')
    for key,value in {
        '@@TARGET_DATE@@':html.escape(str(payload.get('target_date',payload.get('input_defaults',{}).get('target_date','目标交割日')))),
        '@@CSS@@':(directory/'report.css').read_text(encoding='utf-8'),
        '@@JS@@':(directory/'report.js').read_text(encoding='utf-8'),
        '@@DATA@@':json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c'),
        '@@RISK_CHARTS@@':''.join(sections),
    }.items():
        template=template.replace(key,value)
    return template
