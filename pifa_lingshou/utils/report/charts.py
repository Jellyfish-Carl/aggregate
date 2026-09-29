"""Dependency-free SVG charts with explicit axes, units and hover values."""
from __future__ import annotations

import html


COLORS = {"F": "#147d70", "L": "#3476a8", "S": "#bd7a20"}


def text(x, y, label, anchor="start", color="#61717b", size=12):
    return "<text x='{:.2f}' y='{:.2f}' text-anchor='{}' fill='{}' font-size='{}'>{}</text>".format(
        x, y, anchor, color, size, html.escape(str(label)),
    )


def line(x1, y1, x2, y2, color="#e2e8ec", width=1):
    return "<line x1='{:.2f}' y1='{:.2f}' x2='{:.2f}' y2='{:.2f}' stroke='{}' stroke-width='{}'/>".format(
        x1, y1, x2, y2, color, width,
    )


def svg(body, label, ident="", height=280):
    return '<svg id="{}" class="chart-svg" viewBox="0 0 700 {}" role="img" aria-label="{}">{}</svg>'.format(
        html.escape(ident), height, html.escape(label), body,
    )


def axes(ymin, ymax, unit, top=34, bottom=228, xlabels=(), right_unit=None):
    parts = [text(64, top - 16, unit)]
    for k in range(5):
        y = bottom - (bottom - top) * k / 4
        value = ymin + (ymax - ymin) * k / 4
        label = "{:,.0f}".format(value) if max(abs(ymin), abs(ymax)) > 10 else "{:.2f}".format(value)
        parts += [line(64, y, 626, y), text(54, y + 4, label, "end")]
        if right_unit is not None:
            parts.append(text(636, y + 4, "{:.2f}".format(right_unit * k / 4)))
    for x, label in xlabels:
        parts.append(text(x, bottom + 20, label, "middle"))
    return "".join(parts)


def path(values, ymin, ymax, top=34, bottom=228, xs=None, color="#147d70", dash=""):
    if not values:
        return ""
    xs = xs if xs is not None else [64 + 562 * k / max(len(values) - 1, 1) for k in range(len(values))]
    ys = [bottom - (bottom - top) * (v - ymin) / max(ymax - ymin, 1e-10) for v in values]
    coords = " ".join("{:.2f},{:.2f}".format(x, y) for x, y in zip(xs, ys))
    return "<polyline points='{}' fill='none' stroke='{}' stroke-width='2' stroke-dasharray='{}'/>".format(coords, color, dash)


def weighted(row, field, probabilities):
    values = row.get(field, [])
    if not values:
        return 0.0
    if len(values) != len(probabilities) or sum(probabilities) <= 0:
        return sum(values) / len(values)
    return sum(v * p for v, p in zip(values, probabilities)) / sum(probabilities)


def dispatch(selected):
    schedule = selected.get("schedule", [])
    if not schedule:
        return "<p class='note'>没有调度计划</p>"
    probabilities = [s["probability"] for s in selected.get("scenario_results", [])]
    load = [weighted(r, "aggregate_load_by_scenario_mwh", probabilities) for r in schedule]
    declaration = [r.get("declaration_mwh", 0) for r in schedule]
    contract = [r.get("contract_supply_mwh", 0) for r in schedule]
    charge = [max(0, r.get("charge_mwh", 0)) for r in schedule]
    discharge = [max(0, r.get("discharge_mwh", 0)) for r in schedule]
    soc = [r.get("soc_mwh", 0) for r in schedule]
    ticks = [(64 + 562 * i / 95, "{:02d}:{:02d}".format(i // 4, i % 4 * 15)) for i in (0, 24, 48, 72, 95)]
    ymin, ymax = min(contract + [0]), max(load + declaration + contract + [0.01]) * 1.15
    body = axes(ymin, ymax, "每 15 分钟电量 / MWh", top=34, bottom=204, xlabels=ticks)
    for values, color in ((load, "#172b4d"), (declaration, "#3476a8"), (contract, "#bd7a20")):
        body += path(values, ymin, ymax, top=34, bottom=204, color=color)
    top, bottom = 294, 462
    flow_max = max(charge + discharge + [0.02]) * 1.15
    soc_max = max(selected.get("storage_summary", {}).get("soc_upper_bound_mwh", 1), max(soc + [0.01]))
    body += axes(0, flow_max, "每 15 分钟充放电 / MWh", top, bottom, ticks, soc_max)
    body += text(626, top - 16, "SOC / MWh（右轴）", "end")
    bw = min(4, 500 / len(schedule))
    for i, (c, d) in enumerate(zip(charge, discharge)):
        x = 64 + 562 * i / max(len(schedule) - 1, 1)
        for dx, v, color, label in ((-bw, c, "#c07a13", "充电"), (0, d, "#65798a", "放电")):
            height = (bottom - top) * v / flow_max
            body += "<rect x='{:.2f}' y='{:.2f}' width='{:.2f}' height='{:.2f}' fill='{}'><title>时段 {} {} {:.4f} MWh</title></rect>".format(
                x + dx, bottom - height, bw, height, color, i + 1, label, v,
            )
    body += path(soc, 0, soc_max, top, bottom, color="#374151")
    return svg(body, "96 点合同、日前、负荷及储能双图", "schedule", 500)


def risk_scatter(results):
    points = [r for r in results if r.get("feasible")]
    if not points:
        return "<p class='note'>没有可行方案</p>"
    risks = [r["profit_loss_cvar_yuan"] for r in points]
    profits = [r["expected_profit_yuan"] for r in points]
    dx, dy = max(max(risks) - min(risks), 1), max(max(profits) - min(profits), 1)
    xmin, xmax = min(risks) - dx * .12, max(risks) + dx * .12
    ymin, ymax = min(profits) - dy * .12, max(profits) + dy * .12
    ticks = [(64 + 562 * i / 4, "{:,.0f}".format(xmin + (xmax - xmin) * i / 4)) for i in range(5)]
    body = axes(ymin, ymax, "期望利润 / 元", xlabels=ticks)
    for r in points:
        x = 64 + 562 * (r["profit_loss_cvar_yuan"] - xmin) / (xmax - xmin)
        y = 228 - 194 * (r["expected_profit_yuan"] - ymin) / (ymax - ymin)
        color = COLORS.get(r["package"], "#666")
        title = "{} / λ={}：利润 {:,.2f}，利润损失 CVaR {:,.2f}".format(r["package"], r["risk_lambda"], r["expected_profit_yuan"], r["profit_loss_cvar_yuan"])
        body += "<circle cx='{:.2f}' cy='{:.2f}' r='5' fill='{}' stroke='#fff'><title>{}</title></circle>".format(x, y, color, html.escape(title))
    for package in COLORS:
        subset = [r for r in points if r["package"] == package]
        if subset:
            r = max(subset, key=lambda item: item["expected_profit_yuan"])
            x = 64 + 562 * (r["profit_loss_cvar_yuan"] - xmin) / (xmax - xmin)
            y = 228 - 194 * (r["expected_profit_yuan"] - ymin) / (ymax - ymin)
            body += text(x, y - 12, package, "middle", COLORS[package])
    body += text(345, 274, "利润损失 CVaR / 元（越小越好；负值表示尾部仍盈利）", "middle")
    return svg(body, "方案利润与尾部风险", "tradeoff", 292)


def financial_comparison(results, risk_lambda):
    subset = [r for r in results if r.get("risk_lambda") == risk_lambda]
    if not subset:
        return ""
    metrics = [("批发成本", "expected_wholesale_cost_yuan", "#3476a8"), ("零售收入", "expected_retail_revenue_yuan", "#147d70"), ("利润", "expected_profit_yuan", "#bd7a20")]
    values = [r[key] for r in subset for _, key, _ in metrics]
    ymin, ymax = min(values + [0]) * 1.15, max(values + [1]) * 1.18
    xs = [64 + 562 * (i + .5) / len(subset) for i in range(len(subset))]
    body = axes(ymin, ymax, "金额 / 元", xlabels=list(zip(xs, [r["package"] for r in subset])))
    y0 = 228 - 194 * (0 - ymin) / (ymax - ymin)
    for r, x in zip(subset, xs):
        for j, (label, key, color) in enumerate(metrics):
            y = 228 - 194 * (r[key] - ymin) / (ymax - ymin)
            body += "<rect x='{:.2f}' y='{:.2f}' width='24' height='{:.2f}' fill='{}'><title>{} {} {:,.2f} 元</title></rect>".format(x + (j - 1) * 28 - 12, min(y, y0), abs(y - y0), color, r["package"], label, r[key])
    for i, (label, _, color) in enumerate(metrics):
        body += text(180 + i * 130, 278, label, "middle", color)
    return svg(body, "三种套餐批发成本零售收入利润对比", "financial-comparison", 295)


def lambda_lines(results, metric, label, ident):
    subset = [r for r in results if metric in r]
    if not subset:
        return ""
    values = [r[metric] for r in subset]
    pad = max(max(values) - min(values), abs(max(values)) * .02, 1) * .15
    ymin, ymax = min(values) - pad, max(values) + pad
    lambdas = sorted(set(r["risk_lambda"] for r in subset))
    body = axes(ymin, ymax, label + " / 元", xlabels=[(64 + 562 * v, "λ={:g}".format(v)) for v in lambdas])
    for package, color in COLORS.items():
        series = sorted((r for r in subset if r["package"] == package), key=lambda r: r["risk_lambda"])
        xs = [64 + 562 * r["risk_lambda"] for r in series]
        body += path([r[metric] for r in series], ymin, ymax, xs=xs, color=color)
        for r, x in zip(series, xs):
            y = 228 - 194 * (r[metric] - ymin) / (ymax - ymin)
            body += "<circle cx='{:.2f}' cy='{:.2f}' r='4' fill='{}'><title>{} λ={}：{:,.2f} 元</title></circle>".format(x, y, color, package, r["risk_lambda"], r[metric])
    return svg(body, label + "随风险权重变化", ident, 265)


def lambda_price_spread(results, customer_id):
    """Plot expected sell-minus-buy spread against lambda with P10/P90 bands."""
    entries = [
        (result, result.get("customer_results", {}).get(customer_id))
        for result in results
        if result.get("feasible") and customer_id in result.get("customer_results", {})
    ]
    entries = [(result, item) for result, item in entries if item and "expected_price_spread_yuan_per_mwh" in item]
    if not entries:
        return "<p class='note'>没有客户价差数据</p>"
    values = [
        float(item[key])
        for _, item in entries
        for key in ("price_spread_p10_yuan_per_mwh", "expected_price_spread_yuan_per_mwh", "price_spread_p90_yuan_per_mwh")
    ]
    pad = max(max(values) - min(values), 1.0) * 0.12
    ymin, ymax = min(values) - pad, max(values) + pad
    lambdas = sorted({float(result["risk_lambda"]) for result, _ in entries})
    ticks = [(64 + 562 * value, "λ={:g}".format(value)) for value in lambdas]
    body = axes(ymin, ymax, "卖电均价 - 分摊买电均价 / 元/MWh", xlabels=ticks)
    for package, color in COLORS.items():
        series = sorted(
            ((result, item) for result, item in entries if result.get("package") == package),
            key=lambda pair: float(pair[0]["risk_lambda"]),
        )
        if not series:
            continue
        points = []
        for result, item in series:
            x = 64 + 562 * float(result["risk_lambda"])
            lo = float(item["price_spread_p10_yuan_per_mwh"])
            mean = float(item["expected_price_spread_yuan_per_mwh"])
            hi = float(item["price_spread_p90_yuan_per_mwh"])
            y_lo = 228 - 194 * (lo - ymin) / max(ymax - ymin, 1e-10)
            y_mean = 228 - 194 * (mean - ymin) / max(ymax - ymin, 1e-10)
            y_hi = 228 - 194 * (hi - ymin) / max(ymax - ymin, 1e-10)
            body += line(x, y_lo, x, y_hi, color, 4)
            body += line(x - 5, y_lo, x + 5, y_lo, color, 1)
            body += line(x - 5, y_hi, x + 5, y_hi, color, 1)
            body += "<circle cx='{:.2f}' cy='{:.2f}' r='4' fill='{}'><title>{} λ={}：期望价差 {:,.2f}，P10 {:,.2f}，P90 {:,.2f} 元/MWh</title></circle>".format(
                x, y_mean, color, package, result["risk_lambda"], mean, lo, hi
            )
            points.append((x, y_mean))
        if len(points) > 1:
            coords = " ".join("{:.2f},{:.2f}".format(x, y) for x, y in points)
            body += "<polyline points='{}' fill='none' stroke='{}' stroke-width='2'/>".format(coords, color)
    return svg(body, "客户 {} lambda-期望价差与预测区间".format(customer_id), "lambda-spread-" + customer_id, 285)


def lambda_price_spread_all(results, customer_ids):
    return "".join(
        "<div class='spread-chart'><h4>客户 {}：lambda-期望价差与预测区间</h4>{}</div>".format(
            html.escape(str(customer_id)), lambda_price_spread(results, customer_id)
        )
        for customer_id in customer_ids
    )


def customer_intervals(results, risk_lambda, saving=False):
    entries = [(r, cid, c) for r in results if r.get("risk_lambda") == risk_lambda for cid, c in r.get("customer_results", {}).items()]
    entries.sort(key=lambda item: (item[1], item[0]["package"]))
    stem, expected = ("saving", "expected_saving_yuan") if saving else ("bill", "expected_bill_yuan")
    if not entries:
        return ""
    lower = min(c[stem + "_p10_yuan"] for _, _, c in entries)
    upper = max(c[stem + "_p90_yuan"] for _, _, c in entries)
    pad = max(upper - lower, 1) * .1
    lo, hi = lower - pad, upper + pad
    height = 90 + 38 * len(entries)
    body = text(100, 20, "节省额 / 元（负值为比基准贵）" if saving else "客户账单 / 元")
    for i in range(5):
        x = 145 + 450 * i / 4
        body += line(x, 30, x, height - 48)
        body += text(x, height - 25, "{:,.0f}".format(lo + (hi - lo) * i / 4), "middle")
    for i, (r, cid, c) in enumerate(entries):
        y = 48 + 38 * i
        a, mean, b = [145 + 450 * (c[k] - lo) / (hi - lo) for k in (stem + "_p10_yuan", expected, stem + "_p90_yuan")]
        color = COLORS.get(r["package"], "#666")
        body += text(125, y + 4, cid + " / " + c.get("package", r["package"]), "end")
        body += line(a, y, b, y, color, 3) + line(a, y-6, a, y+6, color) + line(b, y-6, b, y+6, color)
        body += "<circle cx='{:.2f}' cy='{:.2f}' r='5' fill='{}'><title>均值 {:,.2f}；P10 {:,.2f}；P90 {:,.2f}</title></circle>".format(mean, y, color, c[expected], c[stem + "_p10_yuan"], c[stem + "_p90_yuan"])
    return svg(body, "客户节省区间" if saving else "客户账单区间", "customer-saving" if saving else "customer-bills", height)


def lambda_interval(results, customer_id, package, metric='spread'):
    entries=sorted([(r,r['customer_results'][customer_id]) for r in results if r.get('package')==package and customer_id in r.get('customer_results',{})],key=lambda pair:pair[0]['risk_lambda'])
    stem='price_spread' if metric=='spread' else 'profit'
    unit='yuan_per_mwh' if metric=='spread' else 'yuan'
    keys=[stem+'_p10_'+unit,'expected_'+stem+'_'+unit,stem+'_p90_'+unit]
    entries=[(r,c) for r,c in entries if all(k in c for k in keys)]
    if not entries:
        return '<p>没有区间数据</p>'
    values=[c[k] for r,c in entries for k in keys]
    pad=max(max(values)-min(values),1)*.12
    lo,hi=min(values)-pad,max(values)+pad
    color=COLORS[package]
    xs=[64+562*r['risk_lambda'] for r,c in entries]
    y=lambda value:228-194*(value-lo)/(hi-lo)
    lower=[(x,y(c[keys[0]])) for x,(r,c) in zip(xs,entries)]
    upper=[(x,y(c[keys[2]])) for x,(r,c) in zip(xs,entries)]
    coords=' '.join('{:.2f},{:.2f}'.format(x,v) for x,v in lower+list(reversed(upper)))
    ticks=[(64+562*r['risk_lambda'],'{:g}'.format(r['risk_lambda'])) for r,c in entries]
    body=axes(lo,hi,'价差 / 元/MWh' if metric=='spread' else '净收益 / 元',xlabels=ticks)
    body+="<polygon points='{}' fill='{}' fill-opacity='.14'/>".format(coords,color)
    for key,dash in ((keys[0],'4 4'),(keys[2],'4 4'),(keys[1],'')):
        body+=path([c[key] for r,c in entries],lo,hi,xs=xs,color=color,dash=dash)
    for x,(r,c) in zip(xs,entries):
        label='λ={}：期望 {:.2f}，P10 {:.2f}，P90 {:.2f}'.format(r['risk_lambda'],c[keys[1]],c[keys[0]],c[keys[2]])
        body+="<circle cx='{:.2f}' cy='{:.2f}' r='4' fill='{}'><title>{}</title></circle>".format(x,y(c[keys[1]]),color,html.escape(label))
    body+=text(626,273,'风险权重 λ','end')
    return svg(body,'客户 '+customer_id+' '+package+' λ-期望值与P10-P90预测区间','risk-'+customer_id+'-'+package+'-'+metric,285)
