'use strict';
const data=JSON.parse(document.getElementById('report-data').textContent);
const $=id=>document.getElementById(id);
const esc=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n=(x,d=2)=>x==null||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const time=j=>String(Math.floor(j/4)).padStart(2,'0')+':'+String(j%4*15).padStart(2,'0');
const table=(id,heads,rows)=>{$(id).innerHTML='<table><thead><tr>'+heads.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';};
const metrics=(id,rows)=>{$(id).innerHTML=rows.map(([label,value])=>'<div class="metric"><small>'+esc(label)+'</small><strong>'+esc(value)+'</strong></div>').join('');};
const mean=(values,probs)=>values?.length?values.reduce((a,v,i)=>a+v*(probs[i]??1/values.length),0):0;
const names={F:'固定价 F',L:'市场联动 L',S:'比例分成 S',...(data.package_labels||{})};
const d=data.input_defaults||{}, st=d.storage||{};
$('event-name').textContent=d.event||'D-1';
const inputs=[['年度 / 月度 / 旬内价 · 元/MWh',`${n(d.annual_price,0)} / ${n(d.monthly_price,0)} / ${n(d.ten_day_price,0)}`],['储能功率 / 容量',`${n((st.maximum_charge_mwh||0)*4,1)} MW / ${n(st.maximum_soc_mwh,1)} MWh`],['SOC 下限 / 初始 / 上限 · MWh',`${n(st.minimum_soc_mwh,1)} / ${n(st.initial_soc_mwh,1)} / ${n(st.maximum_soc_mwh,1)}`],['单程效率 / 往返效率',`${n(st.efficiency*100,1)}% / ${n(st.efficiency**2*100,2)}%`],['退化费 / 交易摩擦 · 元/MWh',`${n(st.degradation_yuan_per_mwh)} / ${n(d.transaction_friction_yuan_per_mwh)}`],['风险权重 λ',(data.risk_lambdas||[]).join(' / ')],['CVaR α / 场景数量',`${n(data.cvar_alpha,2)} / ${data.results[0].scenario_count}`],['年度覆盖 / 总覆盖范围','≥60% / 90%–110%'],['实时偏差 / 有利价差回收','10% / 1.05 × 有利价差'],['计量粒度 / 申报功率上限',`15分钟 / ${n(d.physical_peak_mw,0)} MW`]];
$('defaults').innerHTML=inputs.map(([k,v])=>'<label>'+esc(k)+'<input readonly value="'+esc(v)+'"></label>').join('');
$('all-inputs').textContent=JSON.stringify(d,null,2);
table('customer-inputs',['客户','固定价·元/MWh','联动服务费','分成基准价','上涨 / 下跌分担'],(d.customers||[]).map(c=>[c.customer_id,n(c.fixed_price?.[0]),n(c.service_fee?.[0]),n(c.share_base_price),`${n(c.share_up_ratio)} / ${n(c.share_down_ratio)}`]));
$('package-select').innerHTML=(data.packages||[...new Set(data.results.map(r=>r.package))]).map(k=>'<option value="'+esc(k)+'">'+esc(names[k])+'</option>').join('');
$('lambda-select').innerHTML=[...new Set(data.results.map(r=>r.risk_lambda))].sort((a,b)=>a-b).map(x=>'<option>'+x+'</option>').join('');
$('customer-select').innerHTML=Object.keys(data.results[0].customer_results).map(c=>'<option>'+esc(c)+'</option>').join('');
$('issues').innerHTML=(data.remaining_issues||[]).map(s=>'<li>'+esc(s)+'</li>').join('');
const comparison=data.storage_validation;
if(comparison?.cost_reduction_yuan!=null) $('storage-validation').textContent=`储能对照（${comparison.package} / λ=${comparison.risk_lambda}）：保持合同和场景一致，禁用储能成本 ${n(comparison.disabled_cost_yuan)} 元，启用成本 ${n(comparison.enabled_cost_yuan)} 元，降低 ${n(comparison.cost_reduction_yuan)} 元。`;
function declarationChart(view) {
  const rows=view?.rows||[];
  if(rows.length!==96){
    $('declaration-chart').innerHTML='<p class="formula">缺少原 pifa 的 96 点执行路径明细。</p>';
    $('declaration-totals').innerHTML='';return;
  }
  const left=58,right=1028,top=24,bottom=366,width=1100,height=420;
  const innerW=right-left,innerH=bottom-top,contractKeys=['annual_mwh','monthly_mwh','ten_day_mwh','d3_mwh','d2_mwh'];
  const val=(r,k)=>Number(r[k]??0);
  const positiveMax=Math.max(...rows.map(r=>contractKeys.reduce((a,k)=>a+Math.max(val(r,k),0),0)+Math.max(val(r,'day_ahead_buy_mwh'),0)+Math.max(val(r,'real_time_buy_mwh'),0)));
  const negativeMax=Math.max(...rows.map(r=>contractKeys.reduce((a,k)=>a+Math.max(-val(r,k),0),0)+Math.max(val(r,'day_ahead_sell_mwh'),0)+Math.max(val(r,'real_time_sell_mwh'),0)));
  const maxLoad=Math.max(...rows.flatMap(r=>[val(r,'actual_load_mwh'),val(r,'forecast_load_mwh'),val(r,'p90_mwh')]));
  const maxY=Math.max(1,Math.ceil(Math.max(positiveMax,maxLoad)*1.12));
  const minY=negativeMax>.0005?-Math.max(.1,Math.ceil(negativeMax*11.5)/10):0;
  const x=j=>left+j/95*innerW,y=v=>top+(maxY-v)/(maxY-minY)*innerH;
  const pointPath=values=>values.map((v,j)=>`${j?'L':'M'}${x(j).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
  const xml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
  const fmt=(v,d=3)=>Number.isFinite(Number(v))?Number(v).toFixed(d):'—';
  const escTip=s=>xml(s).replace(/\n/g,'&#10;');
  const tickValues=[maxY,maxY*.75,maxY*.5,maxY*.25,0];if(minY<0)tickValues.push(minY);
  let body='';
  tickValues.forEach(v=>body+=`<line x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}" class="${Math.abs(v)<1e-9?'declaration-zero-axis':'declaration-grid'}"/><text x="${left-10}" y="${y(v)+4}" text-anchor="end" class="declaration-tick">${fmt(v,Math.max(maxY,Math.abs(minY))<20?1:0)}</text>`);
  [0,6,12,18,24].forEach(h=>body+=`<text x="${left+h/24*innerW}" y="${height-11}" text-anchor="${h===0?'start':h===24?'end':'middle'}" class="declaration-tick">${String(h).padStart(2,'0')}:00</text>`);
  body+=`<text x="${left}" y="14" class="declaration-axis-label">MWh / 15分钟；买入 +，卖出 −</text>`;
  if(rows.every(r=>r.p10_mwh!=null&&r.p90_mwh!=null)){
    const upper=rows.map((r,j)=>`${x(j).toFixed(2)},${y(val(r,'p90_mwh')).toFixed(2)}`);
    const lower=rows.map((r,j)=>`${x(j).toFixed(2)},${y(val(r,'p10_mwh')).toFixed(2)}`).reverse();
    body+=`<polygon points="${upper.concat(lower).join(' ')}" class="declaration-band"/>`;
  }
  const barW=innerW/rows.length*.72,segment=(j,start,amount,cls)=>{
    if(Math.abs(amount)<.0005)return start;
    const end=start+amount,topY=Math.min(y(start),y(end)),barH=Math.max(1,Math.abs(y(end)-y(start)));
    body+=`<rect x="${x(j)-barW/2}" y="${topY}" width="${barW}" height="${barH}" class="${cls}"/>`;return end;
  };
  rows.forEach((r,j)=>{
    let pos=0,neg=0;
    contractKeys.forEach(k=>{pos=segment(j,pos,Math.max(val(r,k),0),k==='d3_mwh'||k==='d2_mwh'?'declaration-near-term':'declaration-long-term');});
    pos=segment(j,pos,Math.max(val(r,'day_ahead_buy_mwh'),0),'declaration-day-ahead-buy');
    segment(j,pos,Math.max(val(r,'real_time_buy_mwh'),0),'declaration-realtime-buy');
    contractKeys.forEach(k=>{neg=segment(j,neg,Math.min(val(r,k),0),k==='d3_mwh'||k==='d2_mwh'?'declaration-near-term':'declaration-long-term');});
    neg=segment(j,neg,-Math.max(val(r,'day_ahead_sell_mwh'),0),'declaration-day-ahead-sell');
    segment(j,neg,-Math.max(val(r,'real_time_sell_mwh'),0),'declaration-realtime-sell');
  });
  rows.forEach((r,j)=>{
    let cumulative=0;
    ['annual_mwh','monthly_mwh','ten_day_mwh'].forEach((k,i)=>{
      cumulative+=Math.max(val(r,k),0);
      if(i<2&&cumulative>.0005)body+=`<line x1="${x(j)-barW/2}" x2="${x(j)+barW/2}" y1="${y(cumulative)}" y2="${y(cumulative)}" class="declaration-layer-boundary"/>`;
    });
  });
  body+=`<path d="${pointPath(rows.map(r=>val(r,'forecast_load_mwh')))}" class="declaration-forecast"/><path d="${pointPath(rows.map(r=>val(r,'actual_load_mwh')))}" class="declaration-actual"/>`;
  rows.forEach((r,j)=>body+=`<circle cx="${x(j)}" cy="${y(val(r,'actual_load_mwh'))}" r="2.1" class="declaration-actual-point"/>`);
  body+=`<text x="${right+8}" y="${top+14}" class="declaration-axis-label">下→上</text><text x="${right+8}" y="${top+29}" class="declaration-axis-label">年/月/旬</text>`;
  rows.forEach(r=>{
    const tip=[`${r.time||time((r.period||1)-1)}`,`年度 ${fmt(r.annual_mwh)} · 月度 ${fmt(r.monthly_mwh)} · 旬 ${fmt(r.ten_day_mwh)} MWh`,`D-3 ${fmt(r.d3_mwh)} · D-2 ${fmt(r.d2_mwh)} MWh`,`年度逐点比例 ${fmt(r.annual_assessment_ratio*100,1)}% · ${r.annual_assessment_compliant?"通过":"低于60%"}`,`总体逐点比例 ${fmt(r.overall_assessment_ratio*100,1)}% · ${r.overall_assessment_compliant?"通过":"超出90%–110%"}`,`日前买入 ${fmt(r.day_ahead_buy_mwh)} · 日前卖出 ${fmt(r.day_ahead_sell_mwh)} MWh`,`实时买入 ${fmt(r.real_time_buy_mwh)} · 实时卖出 ${fmt(r.real_time_sell_mwh)} MWh`,`P10 ${fmt(r.p10_mwh)} · P50 ${fmt(r.forecast_load_mwh)} · P90 ${fmt(r.p90_mwh)} MWh`,`代表场景负荷 ${fmt(r.actual_load_mwh)} MWh`,`日前申报 ${fmt(r.declaration_mwh)} · 储能后电网负荷 ${fmt(r.net_grid_load_after_storage_mwh)} MWh`,`中长期 + 日前买 − 日前卖 + 实时买 − 实时卖 = 储能后电网负荷`].join('\n');
    body+=`<rect x="${x(r.period-1)-Math.max(6,innerW/rows.length/2)}" y="${top}" width="${Math.max(12,innerW/rows.length)}" height="${innerH}" class="declaration-hit"><title>${escTip(tip)}</title></rect>`;
  });
  $('declaration-chart').innerHTML=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" class="declaration-svg" role="img" aria-label="96个15分钟中长期、日前申报、实时买卖、P10至P90预测负荷和代表场景负荷">${body}</svg>`;
  const t=view?.totals||{};
  const guard=view?.assessment_guard||{},opt=view?.l3_objective||{};
  const totals=[['中长期',t.long_term_mwh],['日前申报',t.declaration_mwh],['日前买 / 卖',`${fmt(t.day_ahead_buy_mwh,1)} / ${fmt(t.day_ahead_sell_mwh,1)} MWh`],['实时买 / 卖',`${fmt(t.real_time_buy_mwh,1)} / ${fmt(t.real_time_sell_mwh,1)} MWh`],['预测负荷',t.forecast_load_mwh],['代表场景负荷',t.actual_load_mwh],['储能后电网负荷',t.net_grid_load_after_storage_mwh],['中长期48点考核',guard.status==='PASS'?'全部通过':guard.status==='PENDING'?'待滚撮完成':'存在不合规点'],['L3 目标',opt.objective_yuan==null?'—':`¥${n(opt.objective_yuan)} · gap ${n((opt.mip_gap||0)*100,3)}%`]];
  $('declaration-totals').innerHTML=totals.map(([k,v])=>`<span>${xml(k)} <b>${typeof v==='number'?fmt(v,1):xml(v??'—')}</b>${typeof v==='number'?' MWh':''}</span>`).join('');
}
function chart(id,series,unit,options={}) {
  const left=76,right=1012,top=36,bottom=250;
  const vals=series.flatMap(s=>s.values).concat(options.band?options.band.flat():[]).filter(Number.isFinite);
  let lo=Math.min(...vals,0),hi=Math.max(...vals,0),pad=Math.max(hi-lo,.01)*.1;
  lo-=lo<0?pad:0;hi+=pad;
  const x=j=>left+(right-left)*j/95,y=v=>bottom-(bottom-top)*(v-lo)/(hi-lo);
  const poly=(values,fn=y)=>values.map((v,j)=>`${x(j).toFixed(2)},${fn(v).toFixed(2)}`).join(' ');
  let body=`<text x="${left}" y="20" fill="#59676b" font-size="13">${esc(unit)}</text>`;
  for(let i=0;i<=4;i++) {let v=lo+(hi-lo)*i/4,yy=y(v);body+=`<line x1="${left}" x2="${right}" y1="${yy}" y2="${yy}" stroke="#e2e8e8"/><text x="${left-12}" y="${yy+4}" text-anchor="end" fill="#59676b" font-size="12">${n(v,Math.max(Math.abs(lo),Math.abs(hi))>20?0:2)}</text>`;}
  for(const j of [0,16,32,48,64,80,95]) body+=`<text x="${x(j)}" y="278" text-anchor="middle" fill="#59676b" font-size="12">${time(j)}</text>`;
  if(options.band) {let [a,b]=options.band;const pts=poly(a)+' '+b.map((v,j)=>[x(j),y(v)]).reverse().map(p=>p.join(',')).join(' ');body+=`<polygon points="${pts}" fill="#dcefed"/>`;}
  if(options.storage) {
    for(const [j,r] of options.storage.entries()) {
      for(const [v,color,label] of [[r.charge_mwh,'#087f78','充电'],[-r.discharge_mwh,'#bd7b16','放电']]) {
        body+=`<rect x="${x(j)-3}" y="${Math.min(y(0),y(v))}" width="6" height="${Math.abs(y(v)-y(0))}" fill="${color}"><title>${time(j)} ${label} ${n(Math.abs(v),4)} MWh</title></rect>`;
      }
    }
    const cap=options.socMax||20,sy=v=>bottom-(bottom-top)*v/cap;
    body+=`<polyline points="${poly(options.storage.map(r=>r.soc_mwh),sy)}" fill="none" stroke="#182126" stroke-width="2"/>`;
    for(let v=0;v<=cap;v+=cap/4) body+=`<text x="1026" y="${sy(v)+4}" font-size="12" fill="#59676b">${v}</text>`;
    body+='<text x="1090" y="20" font-size="13" text-anchor="end" fill="#59676b">SOC / MWh</text>';
  } else {
    for(const s of series) {
      body+=`<polyline points="${poly(s.values)}" fill="none" stroke="${s.color}" stroke-width="2.2"/>`;
      for(const j of [0,16,32,48,64,80,95]) body+=`<circle cx="${x(j)}" cy="${y(s.values[j])}" r="3" fill="${s.color}"><title>${time(j)} ${esc(s.name)} ${n(s.values[j],4)}</title></circle>`;
    }
  }
  $(id).innerHTML=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 300" role="img" aria-label="${esc(id)}">${body}</svg>`;
}
function selected(){return data.results.find(r=>r.package===$('package-select').value && r.risk_lambda===Number($('lambda-select').value))||data.results[0];}
function customerPeriods(){const r=selected(),c=r.customer_results[$('customer-select').value];table('customer-periods',['时间','用电量·MWh','买电均价','卖电均价','成本·元','收入·元','净收益·元','套餐账单·元'],(c?.periods||[]).map((p,j)=>[time(j),n(p.load_mwh,4),n(p.buy_price_yuan_per_mwh),n(p.sell_price_yuan_per_mwh),n(p.cost_yuan),n(p.revenue_yuan),n(p.profit_yuan),n(p.actual_bill_yuan)]));}
function render(){
 const r=selected(),rows=r.schedule,p=r.probabilities,s=r.storage_summary;
 const stages=r.layer_solvers||[];
 table('layer-solvers',['节点','求解内容','状态','秒','变量','二进制','约束','gap'],stages.map(x=>{const v=x.solver.valuation_solver||x.solver;return [x.node,x.solver.valuation_solver?'规则撮合 + 现货MILP估值':v.backend,v.status,n(v.solve_seconds,3),v.variable_count,v.binary_count,v.constraint_count,v.mip_gap==null?'—':n(v.mip_gap*100,4)+'%'];}));
 const so=stages.at(-1)?.solver||{};
 $('storage-sampling').textContent=so.optimization_scenario_count?`储能用 ${so.optimization_scenario_count} 条代表场景求解，按全部 ${so.evaluation_scenario_count} 条场景的原概率评估。gap只对应该节点的优化模型。`:'日前申报完成后，再运行独立储能节点。';
 const compare=r.storage_validation||data.storage_validation;
 $('storage-validation').textContent=compare?.cost_reduction_yuan!=null?`同合同、同申报、同场景对照：禁用储能成本 ${n(compare.disabled_cost_yuan)} 元，启用 ${n(compare.enabled_cost_yuan)} 元，成本差额 ${n(compare.cost_reduction_yuan)} 元。`:(compare?.reason||'');
 $('solve-status').textContent=(r.engine_status.executed?'L1 · L2 · L3-A · 储能已执行 · ':'')+(r.chain_status||r.status)+' · 当前节点gap '+n((r.mip_gap||0)*100,3)+'% · '+names[r.package]+' / λ='+r.risk_lambda;
 metrics('wholesale-metrics',[['合同供给 · MWh',n(rows.reduce((a,x)=>a+x.contract_supply_mwh,0),3)],['日前申报 · MWh',n(rows.reduce((a,x)=>a+x.declaration_mwh,0),3)],['储能充电 · MWh',n(s.total_charge_mwh,4)],['储能放电 · MWh',n(s.total_discharge_mwh,4)]]);
 table('action-table',['节点','方向','本次成交·产品MWh','等效交割天数','日均累计供给·MWh','状态'],(r.actions||[]).map(a=>[a.event,a.side,n(a.quantity_mwh,3),n(a.equivalent_delivery_days,2),n(a.resulting_daily_position_mwh,3),a.solver_status]));
 const load=rows.map(x=>mean(x.aggregate_load_by_scenario_mwh,p));
 declarationChart(r.declaration_view);
 chart('trade-chart',[{name:'日前净买入',values:rows.map(x=>x.day_ahead_buy_mwh-x.day_ahead_sell_mwh),color:'#2f6f9f'},{name:'实时净买入',values:rows.map(x=>mean(x.real_time_net_by_scenario_mwh,p)),color:'#087f78'}],'净交易电量 / MWh（负值为卖出）');
 chart('storage-chart',[{values:rows.map(x=>x.charge_mwh)},{values:rows.map(x=>-x.discharge_mwh)}],'充放电 / MWh每15分钟',{storage:rows,socMax:s.soc_upper_bound_mwh});
 chart('price-chart',[{name:'日前价',values:rows.map(x=>mean(x.day_ahead_price_by_scenario,p)),color:'#2f6f9f'},{name:'实时价',values:rows.map(x=>mean(x.real_time_price_by_scenario,p)),color:'#b94b45'}],'价格 / 元/MWh');
 $('storage-stats').innerHTML=[`充电均价 ${n(s.average_charge_price_yuan_per_mwh)} 元/MWh`,`放电均价 ${n(s.average_discharge_price_yuan_per_mwh)} 元/MWh`,`初始 SOC ${n(s.initial_soc_mwh,3)} MWh`,`日末 SOC ${n(s.terminal_soc_mwh,3)} MWh`].map(t=>'<span>'+esc(t)+'</span>').join('');
 table('schedule-table',['时间','负荷期望','申报','合同','日前买','日前卖','实时净买期望','充电','放电','SOC'],rows.map((x,j)=>[time(j),n(load[j],4),n(x.declaration_mwh,4),n(x.contract_supply_mwh,4),n(x.day_ahead_buy_mwh,4),n(x.day_ahead_sell_mwh,4),n(mean(x.real_time_net_by_scenario_mwh,p),4),n(x.charge_mwh,4),n(x.discharge_mwh,4),n(x.soc_mwh,4)]));
 metrics('company-metrics',[['批发成本期望 · 元',n(r.expected_wholesale_cost_yuan)],['零售收入期望 · 元',n(r.expected_retail_revenue_yuan)],['净收益期望 · 元',n(r.expected_profit_yuan)],['净收益 P10–P90 · 元',n(r.profit_p10_yuan,0)+' – '+n(r.profit_p90_yuan,0)]]);
 table('company-table',['套餐 / λ','批发成本·元','零售收入·元','净收益·元','净收益P10·元','净收益P90·元'],[[names[r.package]+' / '+r.risk_lambda,n(r.expected_wholesale_cost_yuan),n(r.expected_retail_revenue_yuan),n(r.expected_profit_yuan),n(r.profit_p10_yuan),n(r.profit_p90_yuan)]]);
 const customerRows=Object.entries(r.customer_results).map(([cid,c])=>[cid,names[c.package||r.package],n(c.load_mean_mwh,3),n(c.expected_buy_price_yuan_per_mwh),n(c.expected_sell_price_yuan_per_mwh),n(c.expected_cost_yuan),n(c.expected_revenue_yuan),n(c.expected_profit_yuan),n(c.expected_bill_yuan)]);
 table('customer-table',['客户','已定套餐','用电量·MWh','买电均价','卖电均价','分摊成本·元','分摊收入·元','净收益·元','原套餐账单·元'],customerRows);
 table('all-results',['默认套餐','λ','批发成本·元','零售收入·元','净收益·元','净收益P10–P90·元','链路状态','末节点gap'],data.results.map(x=>[names[x.package],x.risk_lambda,n(x.expected_wholesale_cost_yuan),n(x.expected_retail_revenue_yuan),n(x.expected_profit_yuan),n(x.profit_p10_yuan)+' – '+n(x.profit_p90_yuan),x.chain_status||x.status,n((x.mip_gap||0)*100,3)+'%']));
 const costs=r.wholesale_cost_breakdown?.expected||{};
 const costNames={annual_energy_cost_yuan:'年度电能量',monthly_energy_cost_yuan:'月度电能量',ten_day_energy_cost_yuan:'旬内电能量',d3_energy_cost_yuan:'D-3滚撮电能量',d2_energy_cost_yuan:'D-2滚撮电能量',day_ahead_cost_yuan:'日前现货',real_time_cost_yuan:'实时现货',deviation_penalty_yuan:'罚款/偏差考核',storage_degradation_yuan:'储能退化',total_wholesale_cost_yuan:'批发成本合计'};
 table('cost-table',['成本项','期望金额·元'],Object.entries(costNames).map(([k,v])=>[v,n(costs[k])]));
 table('customer-costs',['客户',...Object.values(costNames).slice(0,-1),'合计'],Object.entries(r.customer_results).map(([cid,c])=>[cid,...Object.keys(costNames).slice(0,-1).map(k=>n(c.cost_breakdown?.[k])),n(c.expected_cost_yuan)]));
 const auditNames={max_energy_balance_residual_mwh:'物理能量平衡最大误差·MWh',max_soc_recursion_residual_mwh:'SOC递推最大误差·MWh',terminal_soc_residual_mwh:'日末SOC误差·MWh',max_wholesale_ledger_residual_yuan:'批发账本最大误差·元',customer_cost_reconciliation_yuan:'客户成本与公司成本误差·元',customer_revenue_reconciliation_yuan:'客户收入与公司收入误差·元',expected_profit_reconciliation_yuan:'净收益恒等式误差·元'};
 table('audit-table',['核验项','差额','结果'],Object.entries(r.accounting_checks||{}).map(([k,v])=>[auditNames[k]||k,n(v,10),Math.abs(v)<1e-5?'通过':'需检查']));
 customerPeriods();
}
for(const page of ['wholesale','retail']) $('tab-'+page).onclick=()=>{for(const x of ['wholesale','retail']){$(x+'-page').hidden=x!==page;$('tab-'+x).setAttribute('aria-selected',String(x===page));}};
$('declaration-chart').onpointermove=event=>{
  const hit=event.target.closest('.declaration-hit'),tip=$('tooltip');
  if(!hit){tip.className='tooltip';return;}
  tip.textContent=hit.textContent;tip.className='tooltip visible';
  tip.style.left=Math.max(12,Math.min(window.innerWidth-tip.offsetWidth-12,event.clientX+12))+'px';
  tip.style.top=Math.max(12,Math.min(window.innerHeight-tip.offsetHeight-12,event.clientY-tip.offsetHeight/2))+'px';
};
$('declaration-chart').onpointerleave=()=>{$('tooltip').className='tooltip';};
$('package-select').onchange=render;$('lambda-select').onchange=render;$('customer-select').onchange=customerPeriods;
$('risk-metric').onchange=()=>{document.querySelectorAll('[data-risk-metric]').forEach(el=>el.hidden=el.dataset.riskMetric!==$('risk-metric').value);};
render();
