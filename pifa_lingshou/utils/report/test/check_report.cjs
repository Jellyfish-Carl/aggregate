const fs=require('fs');const vm=require('vm');const assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
const raw=html.match(/<script id="report-data" type="application\/json">([\s\S]*?)<\/script>/)[1];
const scripts=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
const elements=new Map();
for(const match of html.matchAll(/\bid="([^"]+)"/g)){assert(!elements.has(match[1]),'duplicate id '+match[1]);elements.set(match[1],{value:'',textContent:'',innerHTML:'',hidden:false,attributes:{},setAttribute(k,v){this.attributes[k]=v;}});}
elements.get('report-data').textContent=raw;
elements.get('package-select').value='F';elements.get('lambda-select').value='0';elements.get('customer-select').value='C1';
const riskEls=[...html.matchAll(/data-risk-metric="([^"]+)"/g)].map(m=>({dataset:{riskMetric:m[1]},hidden:m[1]==='profit'}));
const document={getElementById:id=>{assert(elements.has(id),'missing binding '+id);return elements.get(id);},querySelectorAll:s=>{assert.equal(s,'[data-risk-metric]');return riskEls;}};
vm.runInNewContext(scripts[0][1],{document,console,window:{innerWidth:1100,innerHeight:800}});
assert(elements.get('solve-status').textContent.includes('OPTIMAL'));
const declaration=elements.get('declaration-chart').innerHTML;
assert(declaration.includes('viewBox="0 0 1100 420"'),'pifa-sized 96-point plot');
assert.equal((declaration.match(/class="declaration-hit"/g)||[]).length,96,'96 hoverable periods');
for(const klass of ['declaration-long-term','declaration-forecast','declaration-actual','declaration-band','declaration-layer-boundary']) assert(declaration.includes(klass),'missing declaration layer '+klass);
assert(declaration.includes('P10')&&declaration.includes('储能后电网负荷'),'period tooltip details');
assert(elements.get('declaration-totals').innerHTML.includes('实时买 / 卖'),'declaration totals');
const tip=elements.get('tooltip');tip.style={};tip.offsetWidth=360;tip.offsetHeight=230;
elements.get('declaration-chart').onpointermove({clientX:1000,clientY:750,target:{closest:()=>({textContent:'23:45 实时买入 0.100 · 实时卖出 0.000 MWh'})}});
assert.equal(tip.className,'tooltip visible');assert(tip.textContent.includes('实时卖出'));
assert(parseFloat(tip.style.left)>=0&&parseFloat(tip.style.top)>=0);
elements.get('declaration-chart').onpointerleave();assert.equal(tip.className,'tooltip');
assert(elements.get('customer-table').innerHTML.includes('C1'));
const first=elements.get('company-table').innerHTML;
elements.get('lambda-select').value='1';elements.get('lambda-select').onchange();
assert.notEqual(elements.get('company-table').innerHTML,first);
if(JSON.parse(raw).results.some(r=>r.package==='S')){
  elements.get('package-select').value='S';elements.get('package-select').onchange();
  assert(elements.get('company-table').innerHTML.includes('比例分成'));
} else assert(JSON.parse(raw).input_defaults.customer_packages,'single-package report must identify locked mix');
elements.get('tab-retail').onclick();assert.equal(elements.get('retail-page').hidden,false);assert.equal(elements.get('wholesale-page').hidden,true);
elements.get('tab-wholesale').onclick();assert.equal(elements.get('retail-page').hidden,true);
elements.get('risk-metric').value='profit';elements.get('risk-metric').onchange();
assert(riskEls.filter(e=>e.dataset.riskMetric==='profit').every(e=>!e.hidden));
elements.get('customer-select').value='C2';elements.get('customer-select').onchange();
assert.equal((elements.get('customer-periods').innerHTML.match(/<tr>/g)||[]).length,97);
for(const result of JSON.parse(raw).results){
  elements.get('package-select').value=result.package;elements.get('lambda-select').value=String(result.risk_lambda);elements.get('lambda-select').onchange();
  const plot=elements.get('declaration-chart').innerHTML;
  assert.equal((plot.match(/class="declaration-hit"/g)||[]).length,96);
  assert(!/NaN|Infinity|undefined/.test(plot));
  for(const key of ['day_ahead_buy','day_ahead_sell','real_time_buy','real_time_sell']){
    if(result.declaration_view.rows.some(row=>row[key+'_mwh']>=.0005)){
      const klass='declaration-'+key.replace('day_ahead','day-ahead').replace('real_time','realtime').replaceAll('_','-');
      assert(plot.includes('class="'+klass+'"'),'missing nonzero trade '+key);
    }
  }
  for(const row of result.declaration_view.rows)assert(Math.min(row.real_time_buy_mwh,row.real_time_sell_mwh)<1e-8,'execution-path round trip');
}
for(const [id,el] of elements) if(el.innerHTML){assert(!/NaN|Infinity|undefined/.test(el.innerHTML),id);const rows=[...el.innerHTML.matchAll(/<tr>([\s\S]*?)<\/tr>/g)];if(rows.length){const counts=rows.map(r=>(r[1].match(/<(th|td)>/g)||[]).length);assert(counts.every(n=>n===counts[0]),'table columns '+id);}}
console.log('Report script checks passed: both tabs; F/S and lambda switching; customer C2; 96 period rows; finite charts; table widths.');
