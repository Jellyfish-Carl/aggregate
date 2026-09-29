/* Case workflow. Model calculations run on the local Python service. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), all=s=>[...document.querySelectorAll(s)];
  const nodes=['ANNUAL','MONTHLY','TEN_DAY','D-3','D-2','L3-A','STORAGE-DA','STORAGE-RT'];
  const names=['年度中长期','月度中长期','旬内中长期','D-3滚撮','D-2滚撮','日前现货申报','日前储能','日内储能'];
  const name=n=>names[nodes.indexOf(n)]||n;
  const costNames={annual_energy_cost_yuan:'年度电能量',monthly_energy_cost_yuan:'月度电能量',ten_day_energy_cost_yuan:'旬内电能量',d3_energy_cost_yuan:'D-3滚撮电能量',d2_energy_cost_yuan:'D-2滚撮电能量',day_ahead_cost_yuan:'日前现货',real_time_cost_yuan:'实时现货',deviation_penalty_yuan:'考核与罚款',storage_degradation_yuan:'储能退化'};
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=n=>n===null||n===undefined?'—':Number(n).toLocaleString('zh-CN',{maximumFractionDigits:2});
  const input=(value,attrs='')=>`<input type="number" step="any" value="${esc(value)}" ${attrs}>`;
  const metric=(title,value)=>`<div class="metric"><small>${title}</small><strong>${typeof value==='string'?esc(value):num(value)}</strong></div>`;
  const table=(headers,rows)=>`<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  const chartColors=['#087f78','#326c9a','#b77224','#9b5f91','#ae4a42'];
  function lambdaChart(title,series,metricKey='expected_profit_yuan',lowerKey='profit_p10_yuan',upperKey='profit_p90_yuan'){
    series=(series||[]).filter(s=>Array.isArray(s.points)&&s.points.length);
    if(!series.length)return '';
    const points=series.flatMap(s=>s.points||[]), lows=points.map(p=>Number(p[lowerKey]??p[metricKey])), highs=points.map(p=>Number(p[upperKey]??p[metricKey])), values=points.map(p=>Number(p[metricKey]));
    const min=Math.min(...lows,...values),max=Math.max(...highs,...values),span=Math.max(max-min,1),w=760,h=250,pad={l:58,r:22,t:30,b:40};
    const x=i=>pad.l+(w-pad.l-pad.r)*(i/Math.max((series[0].points||[]).length-1,1));
    const y=v=>h-pad.b-(h-pad.t-pad.b)*(v-min)/span;
    const path=(items,key,reverse=false)=>items.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(Number(p[key]??p[metricKey])).toFixed(1)}`).join(' ');
    const area=(items)=>`${path(items,upperKey)} ${items.slice().reverse().map((p,i)=>`L${x(items.length-1-i).toFixed(1)},${y(Number(p[lowerKey]??p[metricKey])).toFixed(1)}`).join(' ')} Z`;
    const ticks=[0,.25,.5,.75,1].map(t=>{const value=min+span*t;return `<line x1="${pad.l}" x2="${w-pad.r}" y1="${y(value)}" y2="${y(value)}" stroke="#e2eae8"/><text x="${pad.l-8}" y="${y(value)+4}" text-anchor="end">${num(value)}</text>`;}).join('');
    const lambdaPoints=(series[0].points||[]).map((p,i)=>`<text x="${x(i)}" y="${h-14}" text-anchor="middle">λ=${Number(p.lambda_value).toFixed(2)}</text>`).join('');
    const plots=series.map((s,i)=>{const color=chartColors[i%chartColors.length];return `<path d="${area(s.points)}" fill="${color}" opacity=".09"/><path d="${path(s.points,metricKey)}" fill="none" stroke="${color}" stroke-width="2.5"/>`;}).join('');
    const legend=series.map((s,i)=>`<span><i style="background:${chartColors[i%chartColors.length]}"></i>${esc(s.name)}</span>`).join('');
    return `<div class="lambda-chart"><div class="lambda-chart-title"><strong>${esc(title)}</strong><span>阴影：随 λ 收缩的 P10–P90 风险带</span></div><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(title)}">${ticks}${plots}${lambdaPoints}<text x="${pad.l}" y="16">风险调整收益 · 元</text></svg><div class="chart-legend">${legend}</div></div>`;
  }
  let current=null,customerId='',busy=false,activeJob=null,dirty=false,started=0,packageSelections={};
  let selectedNode=null;
  function notify(text,error=false){$('toast').textContent=text;$('toast').className='show';$('toast').style.background=error?'#953e37':'#203638';setTimeout(()=>$('toast').className='',5000);}
  async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j;}
  const guard=fn=>async()=>{try{await fn();}catch(e){notify(e.message,true);}};
  function tab(id){all('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===id)));all('.tab-panel').forEach(p=>p.hidden=p.id!==`tab-${id}`);}
  all('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
  const signed=()=>!!current&&Object.keys(current.state.packages).length>0;
  const settled=()=>current?.state.status==='SETTLED';
  const hasDA=()=>!!current?.state.stages.some(s=>s.node==='STORAGE-DA');
  const base=()=>({case_id:current.state.case_id,revision:current.state.revision});
  const url=(kind,file)=>`/resource/${kind}/${current.state.case_id}/${file}`;
  function settings(){const market={};all('[data-market]').forEach(e=>market[e.dataset.market]=Number(e.value));all('[data-market-array]').forEach(e=>market[e.dataset.marketArray]=e.value.split(',').map(Number));return {name:$('case-name').value,customer_count:Number($('customer-count').value),target_date:$('target-date').value,seed:Number($('seed').value),recommendation_days:Number($('recommendation-days').value),risk_lambda:Number($('risk-lambda').value),scenario_count:Number($('scenario-count').value),market,recommendation_policy:{stable_threshold:Number($('stable-threshold').value),variable_threshold:Number($('variable-threshold').value),minimum_expected_profit_yuan:Number($('minimum-profit').value),minimum_customer_saving_yuan:Number($('minimum-saving').value)}};}
  function fillSettings(d){for(const [id,key] of Object.entries({'case-name':'name','customer-count':'customer_count','target-date':'target_date','seed':'seed','recommendation-days':'recommendation_days','risk-lambda':'risk_lambda','scenario-count':'scenario_count'}))$(id).value=d[key];all('[data-market]').forEach(e=>e.value=d.market[e.dataset.market]);all('[data-market-array]').forEach(e=>e.value=d.market[e.dataset.marketArray].join(', '));const p=d.recommendation_policy;$('stable-threshold').value=p.stable_threshold;$('variable-threshold').value=p.variable_threshold;$('minimum-profit').value=p.minimum_expected_profit_yuan;$('minimum-saving').value=p.minimum_customer_saving_yuan;}
  function updateControls(){
    $('create-case').disabled=busy;$('case-select').disabled=busy;
    $('save-inputs').disabled=busy||!current||signed();$('calculate-recommendation').disabled=busy||!current||signed();
    $('sign-packages').disabled=busy||signed();$('prepare-actuals').disabled=busy||!hasDA()||settled();
    $('settle-case').disabled=busy||!$('actual-json')||settled();$('compare-risk').disabled=busy||!hasDA();
    for(const id of ['apply-customer','apply-json','edit-inputs'])$(id).disabled=busy||!current||signed();
    all('#tab-inputs input,#tab-inputs textarea,#tab-inputs select').forEach(e=>e.disabled=busy||signed());
    if($('run-stage'))$('run-stage').disabled=busy||!signed()||settled()||selectedNode!==nextNode();
    if($('save-forecast'))$('save-forecast').disabled=busy||!signed()||settled()||current.state.stages.some(s=>s.node===selectedNode&&selectedNode!=='STORAGE-RT');
    const reasons={
      'save-inputs':!current?'请先点击“新建案例”':signed()?'案例已签约，输入已锁定':'保存当前输入后才能计算推荐',
      'calculate-recommendation':!current?'请先新建案例':signed()?'案例已签约，不能重复推荐':'先保存输入',
      'sign-packages':!current?'先新建案例':!current.state.recommendation?'先计算套餐推荐':'请选择每个客户的套餐',
      'prepare-actuals':!hasDA()?'先完成“日前储能”节点':settled()?'案例已结算':'',
      'compare-risk':!hasDA()?'先完成“日前储能”节点':'',
      'settle-case':!$('actual-json')?'先点击“生成可编辑模拟实绩”':settled()?'案例已结算':''
    };
    Object.entries(reasons).forEach(([id,reason])=>{const e=$(id);if(e)e.title=e.disabled&&reason?reason:'';});
    const help=$('workflow-help');
    if(help){
      if(!current)help.textContent='当前还没有案例。先点击“新建案例”，页面才会加载客户、96点预测和后续计算按钮。';
      else if(!signed())help.textContent=current.state.recommendation?'下一步：在“套餐推荐”页为每个客户选择套餐并点击“确认套餐并进入购电阶段”。':'下一步：在“输入与校验”页点击“保存并验证输入”，再点击“计算套餐收益与推荐”。';
      else if(!hasDA())help.textContent='已签约。请在“逐阶段购电”页按左侧高亮的“下一节点”依次运行；储能曲线会在完成“日前储能”后出现在页面底部结果卡片。';
      else help.textContent='日前储能已完成。请在页面底部点击储能节点结果中的“查看储能充放电与 SOC 曲线”；完成后可生成实绩并结算。';
    }
  }
  function markDirty(){if(!signed()){dirty=true;$('input-status').textContent='有未保存修改';}}
  all('.setup input').forEach(e=>e.addEventListener('input',markDirty));
  function captureCustomer(){if(!current||!$('curve-table'))return;const c=current.input.customers.find(c=>c.terms.customer_id===customerId);all('[data-customer-scalar]').forEach(e=>{const [scope,key]=e.dataset.customerScalar.split('.');(scope==='terms'?c.terms:c)[key]=Number(e.value);});all('[data-customer-curve]').forEach(e=>{const [key,index]=e.dataset.customerCurve.split(':');(key==='fixed_price'||key==='service_fee'?c.terms[key]:c[key])[Number(index)]=Number(e.value);});}
  function renderCustomer(){
    const c=current.input.customers.find(c=>c.terms.customer_id===customerId);if(!c)return;
    $('customer-editor').className='customer-editor';
    const fields=[['分成基准价','terms.share_base_price',c.terms.share_base_price],['上行分成比例','terms.share_up_ratio',c.terms.share_up_ratio],['下行分成比例','terms.share_down_ratio',c.terms.share_down_ratio],['预测相对半区间','forecast_uncertainty',c.forecast_uncertainty]];
    $('customer-editor').innerHTML=`<div class="customer-fields">${fields.map(([label,key,v])=>`<label>${label}${input(v,`data-customer-scalar="${key.includes('.')?key:'customer.'+key}"`)}</label>`).join('')}</div><p>电量：MWh / 15分钟；电价：元/MWh。每个点可直接编辑。</p><div id="curve-table" class="table-wrap">${table(['点 / 时间','负荷','分时基准价','固定套餐价','联动服务费'],c.load_profile_mwh.map((q,j)=>[`${j+1} / ${String(Math.floor(j/4)).padStart(2,'0')}:${String(j%4*15).padStart(2,'0')}`,...['load_profile_mwh','tou_price','fixed_price','service_fee'].map(k=>input((c[k]||c.terms[k])[j],`data-customer-curve="${k}:${j}"`))]))}</div>`;
    all('#customer-editor input').forEach(e=>e.addEventListener('input',markDirty));drawChart();updateControls();
  }
  function drawChart(){
    const c=current.input.customers.find(c=>c.terms.customer_id===customerId);if(!c)return;
    const width=1000,height=275,pad=55,maxQ=Math.max(1,...c.load_profile_mwh),loP=Math.min(0,...c.tou_price),hiP=Math.max(1,...c.tou_price),x=i=>pad+(width-2*pad)*i/95;
    const y=(v,lo,hi)=>height-36-(height-76)*(v-lo)/(hi-lo);
    const path=(arr,lo,hi)=>arr.map((v,i)=>`${i?'L':'M'}${x(i).toFixed(2)},${y(v,lo,hi).toFixed(2)}`).join(' ');
    const grid=[0,.25,.5,.75,1].map(t=>`<line x1="${pad}" x2="${width-pad}" y1="${y(t,0,1)}" y2="${y(t,0,1)}" stroke="#e2eae8"/><text x="5" y="${y(t,0,1)+4}">${num(t*maxQ)}</text><text x="${width-pad+8}" y="${y(t,0,1)+4}">${num(loP+t*(hiP-loP))}</text>`).join('');
    $('load-chart').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="客户96点负荷左轴和分时电价右轴"><text x="5" y="18">负荷 · MWh（左轴，蓝）</text><text x="${width-260}" y="18">分时基准 · 元/MWh（右轴，橙）</text>${grid}<path class="load-line" d="${path(c.load_profile_mwh,0,maxQ)}"/><path class="tou-line" d="${path(c.tou_price,loP,hiP)}"/>${[0,24,48,72,95].map(j=>`<text x="${x(j)-12}" y="${height-8}">${Math.floor(j/4)}:${String(j%4*15).padStart(2,'0')}</text>`).join('')}</svg>`;
  }
  function renderInputs(){
    if(!current)return;const d=current.input;fillSettings(d);if(!d.customers.some(c=>c.terms.customer_id===customerId))customerId=d.customers[0].terms.customer_id;
    $('customer-select').replaceChildren(...d.customers.map(c=>new Option(c.terms.customer_id,c.terms.customer_id)));$('customer-select').value=customerId;
    $('input-status').textContent=`${dirty?'有未保存修改':current.state.status} · v${current.state.revision}`;
    $('case-meta').innerHTML=`${esc(current.state.case_id)} · 标的 ${d.target_date} · <a href="${url('input',`revision_${String(current.state.revision).padStart(4,'0')}.json`)}" target="_blank">输入文件</a> · <a href="${url('output','state.json')}" target="_blank">结果文件</a>`;
    $('input-json').value=JSON.stringify(d,null,2);
    $('forecast-summary').innerHTML=`<p>签约预测：${esc(d.signing_forecast.start_date)} 至 ${esc(d.signing_forecast.end_date)}，共 ${d.signing_forecast.days.length} 天。</p><p>发布：${esc(d.signing_forecast.issued_at)}</p><p>年 / 月 / 旬节点使用各自交割范围的日均96点曲线，按日历天数折算。尚未构建逐日全年情景树。</p><p>数据来源：${esc(d.source)}</p>`;
    $('market-curves').innerHTML=table(['15分钟点','日前预测价','实时预测价'],Array.from({length:96},(_,j)=>[j+1,...['day_ahead_price','real_time_price'].map(k=>input(d.market[k][j],`data-market-curve="${k}:${j}"`))]));
    $('signing-days').innerHTML=table(['日期',...d.customers.map(c=>esc(c.terms.customer_id)+'负荷系数'),'价格系数'],d.signing_forecast.days.map((day,i)=>[day.date,...d.customers.map(c=>input(day.load_factors[c.terms.customer_id],`data-day="${i}" data-day-customer="${esc(c.terms.customer_id)}"`)),input(day.price_factor,`data-day-price="${i}"`)]));
    all('#market-curves input,#signing-days input').forEach(e=>e.addEventListener('input',markDirty));renderCustomer();
  }
  function captureInputs(){
    captureCustomer();const s=settings(),d=current.input;
    if(s.customer_count!==d.customers.length||s.target_date!==d.target_date||s.recommendation_days!==d.signing_forecast.days.length||s.seed!==d.seed)throw Error('客户数量、标的日、预测天数或随机种子已改变，请点击“新建案例”重新生成，或载入完整JSON');
    d.name=s.name;d.risk_lambda=s.risk_lambda;d.scenario_count=s.scenario_count;d.market={...d.market,...s.market};d.recommendation_policy=s.recommendation_policy;
    all('[data-market-curve]').forEach(e=>{const [k,j]=e.dataset.marketCurve.split(':');d.market[k][j]=Number(e.value);});
    all('[data-day-customer]').forEach(e=>d.signing_forecast.days[e.dataset.day].load_factors[e.dataset.dayCustomer]=Number(e.value));
    all('[data-day-price]').forEach(e=>d.signing_forecast.days[e.dataset.dayPrice].price_factor=Number(e.value));
  }
  async function save(){if(!current)throw Error('请先新建案例');captureInputs();current=await api('/api/cases/save',{...base(),input:current.input});dirty=false;renderAll();await listCases();}
  function renderRecommendation(){
    const r=current?.state.recommendation,box=$('recommendation-results');$('confirm-packages').hidden=!r;
    if(!r){box.className='empty-state';box.textContent='先保存并确认输入，再计算推荐。推荐不会自动签约。';return;}
    r.customers.forEach(c=>{packageSelections[c.customer_id]=packageSelections[c.customer_id]||current.state.packages[c.customer_id]||c.recommended_package||'F';});
    const company=r.company_curve;
    const selectedCustomers=r.customers.map(c=>c.candidates.find(x=>x.package===packageSelections[c.customer_id])||c.candidates[0]);
    const usesRecommendedMix=!!company&&r.customers.every(c=>packageSelections[c.customer_id]===(c.recommended_package||'F'));
    const displayCompany=company&&usesRecommendedMix?company:company?{...company,
      packages:Object.fromEntries(r.customers.map(c=>[c.customer_id,packageSelections[c.customer_id]])),
      expected_cost_yuan:selectedCustomers.reduce((a,x)=>a+x.expected_cost_yuan,0),
      expected_revenue_yuan:selectedCustomers.reduce((a,x)=>a+x.expected_revenue_yuan,0),
      profit_p10_yuan:selectedCustomers.reduce((a,x)=>a+x.profit_p10_yuan,0),
      profit_p90_yuan:selectedCustomers.reduce((a,x)=>a+x.profit_p90_yuan,0),
      lambda_curve:(company.lambda_curve||[]).map((_,i)=>({
        lambda_value:company.lambda_curve[i].lambda_value,
        expected_profit_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].expected_profit_yuan,0),
        expected_saving_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].expected_saving_yuan,0),
        expected_spread_yuan_per_mwh:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].expected_spread_yuan_per_mwh,0),
        profit_p10_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].profit_p10_yuan,0),
        profit_p90_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].profit_p90_yuan,0),
        profit_interval_low_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].profit_interval_low_yuan,0),
        profit_interval_high_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].profit_interval_high_yuan,0),
        saving_interval_low_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].saving_interval_low_yuan,0),
        saving_interval_high_yuan:selectedCustomers.reduce((a,x)=>a+x.lambda_curve[i].saving_interval_high_yuan,0),
      }))}:null;
    const companyChart=displayCompany?lambdaChart('售电公司：λ–风险调整利润与预测区间',[{name:'当前客户套餐组合',points:displayCompany.lambda_curve}],'expected_profit_yuan','profit_interval_low_yuan','profit_interval_high_yuan'):'';
    const companyBlock=displayCompany?`<section class="recommend-portfolio"><div class="panel-heading"><div><h3>售电公司整体 λ 曲线</h3><p>组合：${esc(Object.entries(displayCompany.packages).map(([id,p])=>`${id}=${p}`).join('，'))}；客户修改套餐后曲线即时更新。</p></div></div><div class="metric-row">${metric('期望成本 · 元',displayCompany.expected_cost_yuan)+metric('期望收入 · 元',displayCompany.expected_revenue_yuan)+metric('λ=0期望利润 · 元',displayCompany.lambda_curve[0]?.expected_profit_yuan)+metric('λ=1风险调整利润 · 元',displayCompany.lambda_curve[displayCompany.lambda_curve.length-1]?.expected_profit_yuan)+metric('利润预测区间 · P10–P90',`${num(displayCompany.profit_p10_yuan)} ~ ${num(displayCompany.profit_p90_yuan)}`)}</div>${companyChart}<p class="eyebrow">公司曲线聚合当前客户套餐候选结果；保持程序推荐组合时使用联合情景区间，手动更换套餐后为客户区间的加总近似。</p></section>`:'';
    box.className='';box.innerHTML=`<p>${esc(r.formula)}</p><p>${esc(r.cost_basis)}</p><p class="eyebrow">${esc(r.risk_note)}；金额为整个 ${r.day_count} 天预测期的元，价差为元/MWh。</p><p class="lambda-note">${esc(r.lambda_curve_definition||'λ=0 使用情景均值，λ=1 向情景 P10 下行分位数靠拢。')}</p>${companyBlock}`+r.customers.map(c=>{const selected=packageSelections[c.customer_id]||c.recommended_package||'F';const customerSeries=c.candidates.map(x=>({name:`套餐 ${x.package}`,points:x.lambda_curve}));return `<article class="recommend-card"><h3>${esc(c.customer_id)}　<span class="tag">建议 ${esc(c.recommended_package||'调整报价')}</span></h3><p>${esc(c.reason)}。不稳定度 ${c.instability_score.toFixed(3)}；逐日电量CV ${c.daily_energy_cv.toFixed(3)}</p><div class="metric-row">${metric('客户期望节省 · 元',c.candidates.find(x=>x.package===selected)?.expected_customer_saving_yuan)+metric('客户节省 P10 · 元',c.candidates.find(x=>x.package===selected)?.saving_p10_yuan)+metric('客户节省 P90 · 元',c.candidates.find(x=>x.package===selected)?.saving_p90_yuan)+metric('推荐套餐',selected)+metric('推荐期望价差 · 元/MWh',c.candidates.find(x=>x.package===selected)?.expected_spread_yuan_per_mwh)}</div>${lambdaChart(`${c.customer_id}：λ–客户风险调整收益与预测区间`,customerSeries,'expected_saving_yuan','saving_interval_low_yuan','saving_interval_high_yuan')}<div class="table-wrap">${table(['套餐','公司期望成本','公司期望收入','公司期望利润','利润P10–P90','客户期望节省','节省P10–P90','期望价差','通过底线'],c.candidates.map(x=>[x.package,num(x.expected_cost_yuan),num(x.expected_revenue_yuan),num(x.expected_profit_yuan),`${num(x.profit_p10_yuan)} ~ ${num(x.profit_p90_yuan)}`,num(x.expected_customer_saving_yuan),`${num(x.saving_p10_yuan)} ~ ${num(x.saving_p90_yuan)}`,num(x.expected_spread_yuan_per_mwh),x.eligible?'是':'否']))}</div></article>`;}).join('');
    $('package-choices').innerHTML=table(['客户','程序建议','最终选择（可修改）','选择说明'],r.customers.map(c=>[esc(c.customer_id),`<span class="tag">${esc(c.recommended_package||'无可行建议')}</span>`,`<select class="package-select" data-package="${esc(c.customer_id)}" ${signed()?'disabled':''}>${['F','L','S'].map(p=>`<option value="${p}" ${packageSelections[c.customer_id]===p?'selected':''}>${{F:'固定价 F',L:'市场联动 L',S:'比例分成 S'}[p]}</option>`).join('')}</select>`,esc(c.reason)]));
    all('.package-select').forEach(e=>e.onchange=()=>{packageSelections[e.dataset.package]=e.value;renderRecommendation();});
  }
  function nextNode(){const done=current.state.stages.filter(s=>s.node!=='STORAGE-RT').length;return nodes[Math.min(done,7)];}
  function renderStageEditor(){
    if(!signed()){$('stage-editor').className='empty-state';$('stage-editor').textContent='先在第2页逐客户确认套餐，再开始采购。';return;}
    const n=selectedNode||nextNode(),f=current.input.forecasts[n];selectedNode=n;
    $('stage-editor').className='';$('stage-editor').innerHTML=`<h3>${name(n)}</h3><p>发布：${f.as_of}　交割：${f.scope_start} — ${f.scope_end}</p><p>已签套餐 ${esc(Object.entries(current.state.packages).map(([c,p])=>c+'：'+p).join('，'))}。年/月/旬固定已成交仓位；储能固定日前申报。</p><div class="table-wrap">${table(['预测指标','范围'],[['客户总预测电量 · MWh/代表日',num(Object.values(f.customer_load_mwh).flat().reduce((a,b)=>a+b,0))],['日前价格 · 元/MWh',`${num(Math.min(...f.day_ahead_price))} ~ ${num(Math.max(...f.day_ahead_price))}`],['实时价格 · 元/MWh',`${num(Math.min(...f.real_time_price))} ~ ${num(Math.max(...f.real_time_price))}`]])}</div><details class="json-panel"><summary>查看 / 更新本节点96点预测与报价</summary><p>客户P10/P50/P90、日前与实时价格均可编辑。保存后输入版本递增，已成交节点不可重写。</p><textarea id="forecast-json" rows="14">${esc(JSON.stringify(f,null,2))}</textarea><button id="save-forecast">保存节点预测</button></details>${n==='STORAGE-RT'?'<div class="json-panel"><h4>已执行实绩</h4><p>输入已执行点数 fixed_until、等长 actual_load_mwh、actual_rt_price，以及 executed_storage 内的 charge_mwh / discharge_mwh / soc_mwh。历史前缀不可改写。</p><textarea id="rt-prefix" rows="7" placeholder="粘贴实绩 JSON"></textarea></div>':''}<div class="stage-controls"><button id="run-stage" class="primary">运行 ${name(n)}</button><span>只计算本节点；下游由你确认后运行。</span></div>`;
    $('save-forecast').onclick=guard(async()=>{current=await api('/api/cases/forecast',{...base(),node:n,forecast:JSON.parse($('forecast-json').value)});renderAll();notify('节点预测已验证并保存');});
    $('run-stage').onclick=guard(async()=>{let execution;if(n==='STORAGE-RT')execution=JSON.parse($('rt-prefix').value);await startJob('stage',{node:n,execution});});updateControls();
  }
  function renderStages(){
    if(!current)return;const done=current.state.stages,expected=nextNode();if(!selectedNode)selectedNode=expected;
    $('clock-tag').textContent=`案例时间 ${current.state.clock} · ${settled()?'已结算':signed()?'已签约':'待签约'}`;
    $('stage-list').innerHTML=nodes.map(n=>`<li class="${done.some(r=>r.node===n)?'done ':''}${n===selectedNode?'current':''}"><button data-stage="${n}">${name(n)}</button><small>${done.some(r=>r.node===n)?'已完成':n===expected?'下一节点':'等待前置'} · ${current.input.forecasts[n].as_of}</small></li>`).join('');
    all('[data-stage]').forEach(b=>b.onclick=()=>{selectedNode=b.dataset.stage;renderStages();});renderStageEditor();
    $('stage-results').innerHTML=done.map(r=>`<article class="stage-result"><strong>${name(r.node)}</strong>　<span class="tag">${esc(r.status)}</span><p>交割 ${r.forecast.scope_start} — ${r.forecast.scope_end} · ${r.elapsed_seconds.toFixed(3)} 秒 · 输入版本 ${r.input_revision}</p><a href="${url('output',r.report_file)}" target="_blank">${r.node.startsWith('STORAGE')?'查看储能充放电与 SOC 曲线':'查看96点曲线 / 成本与收益'}</a><a href="${url('output',r.result_file)}" target="_blank">完整求解结果</a></article>`).join('');
    $('risk-report').hidden=!current.state.risk_comparison;if(current.state.risk_comparison)$('risk-report').href=url('output',current.state.risk_comparison.report_file);
  }
  function renderSettlement(){
    const r=current?.state.settlement;if(!r){$('settlement-results').innerHTML='';return;}
    $('settlement-results').innerHTML=`<div class="settlement-box"><h3>售电公司与客户 · ${r.target_date} · ${esc(r.source)}</h3><div class="summary-grid">${metric('公司售电收入 · 元',r.retail_revenue_yuan)+metric('公司全部成本 · 元',r.procurement_cost_yuan)+metric('公司利润 · 元',r.profit_yuan)+metric('公司价差 · 元/MWh',r.spread_yuan_per_mwh)+metric('分时电价基准总账单 · 元',r.tou_baseline_bill_yuan)+metric('客户总节省 · 元',r.customer_saving_yuan)}</div><h3>公司成本组成 · 元</h3><div class="table-wrap">${table(['成本项','金额'],Object.entries(costNames).map(([k,n])=>[n,num(r.cost_breakdown[k])]))}</div><h3>逐客户结果</h3><p>公司成本按每点客户电量 × 组合平均购电成本分摊；同时呈现按组合平均售价分摊的收入和按该客户套餐实际收费的收入。</p><div class="table-wrap">${table(['客户','套餐','电量MWh','分时基准账单','实际套餐账单','客户节省','公司分摊成本','平均售价分摊收入','公司实际利润','实际价差'],r.customers.map(c=>[esc(c.customer_id),c.package,num(c.energy_mwh),num(c.tou_bill_yuan),num(c.actual_bill_yuan),num(c.saving_yuan),num(c.allocated_cost_yuan),num(c.allocated_revenue_yuan),num(c.company_profit_on_customer_yuan),num(c.spread_yuan_per_mwh)]))}</div><p>${esc(r.settlement_scope)}。按已执行动作核算，不做事后最优调度；物理校验 ${esc(r.physical_validation)}。</p><a target="_blank" href="${url('output','settlement.json')}">完整结算结果与96点客户明细</a></div>`;
  }
  function renderAll(){renderInputs();renderRecommendation();renderStages();renderSettlement();updateControls();}
  async function listCases(){const r=await api('/api/cases');$('case-select').replaceChildren(new Option('选择已有案例',''),...r.cases.map(c=>new Option(`${c.name} · ${c.target_date} · ${c.status} · ${c.case_id.slice(-8)}`,c.case_id)));if(current)$('case-select').value=current.state.case_id;}
  async function loadCase(cid){current=await api('/api/cases/'+encodeURIComponent(cid));dirty=false;customerId='';selectedNode=null;packageSelections={};$('actual-editor').className='empty-state';$('actual-editor').textContent='完成日前储能后生成模拟实绩，或粘贴计量数据进行结算。';history.replaceState(null,'','?case='+cid);renderAll();await listCases();}
  async function startJob(action,extra={}){if(busy)throw Error('已有计算正在进行');const r=await api('/api/cases/run',{...base(),action,...extra});activeJob=r.job_id;sessionStorage.setItem('case-job',JSON.stringify({id:activeJob,cid:current.state.case_id,started:Date.now()}));started=Date.now();return poll();}
  async function poll(){
    busy=true;updateControls();$('job-box').hidden=false;
    try{const j=await api('/api/job/'+activeJob);$('job-status').textContent={QUEUED:'等待求解器',RUNNING:'正在计算',DONE:'计算完成',ERROR:'计算失败'}[j.status]||j.status;$('job-progress').value=j.progress;$('job-time').textContent=((Date.now()-started)/1000).toFixed(1)+' 秒';$('job-message').textContent=j.error||j.message;$('job-log').textContent=(j.log||[]).map(x=>`${Number(x.percent).toFixed(0)}% · ${x.message}`).join('\n');
      if(['QUEUED','RUNNING'].includes(j.status)){setTimeout(()=>poll(),600);return;}
      busy=false;sessionStorage.removeItem('case-job');if(j.status==='ERROR')throw Error(j.error);selectedNode=null;current=await api('/api/cases/'+current.state.case_id);renderAll();await listCases();notify('计算完成，输入与结果已保存');
    }catch(e){busy=false;sessionStorage.removeItem('case-job');notify(e.message,true);}finally{updateControls();}
  }
  $('create-case').onclick=guard(async()=>{current=await api('/api/cases/create',{settings:settings()});dirty=false;customerId='';selectedNode=null;await loadCase(current.state.case_id);tab('inputs');notify('已创建独立案例；Mock数据可编辑');});
  $('case-select').onchange=guard(async()=>{if($('case-select').value)await loadCase($('case-select').value);});
  $('save-inputs').onclick=guard(async()=>{await save();notify('输入已验证并保存');});
  $('customer-select').onchange=()=>{captureCustomer();customerId=$('customer-select').value;renderCustomer();};
  $('apply-customer').onclick=guard(async()=>{captureCustomer();markDirty();drawChart();$('input-json').value=JSON.stringify(current.input,null,2);notify('当前客户已暂存；点击“保存并验证输入”写入文件');});
  const showJson=()=>{if(!current)throw Error('请先新建案例');captureInputs();$('input-json').value=JSON.stringify(current.input,null,2);$('input-json-panel').open=true;tab('inputs');};
  $('show-json').onclick=guard(showJson);$('edit-inputs').onclick=guard(showJson);
  $('apply-json').onclick=guard(async()=>{const d=JSON.parse($('input-json').value);if(!Array.isArray(d.customers)||!d.signing_forecast?.days)throw Error('缺少客户或逐日预测');current.input=d;dirty=true;customerId='';renderInputs();notify('JSON已载入；请保存并验证输入');});
  $('calculate-recommendation').onclick=guard(async()=>{await save();await startJob('recommend');tab('recommendation');});
  $('sign-packages').onclick=guard(async()=>{if(dirty)throw Error('请先保存修改并重新计算推荐');const packages={};all('[data-package]').forEach(e=>packages[e.dataset.package]=e.value);if(Object.values(packages).some(v=>!v))throw Error('请逐客户人工选择套餐');current=await api('/api/cases/confirm',{...base(),packages});selectedNode=null;renderAll();tab('procurement');notify('套餐已确认，已锁定签约条款');});
  $('compare-risk').onclick=guard(()=>startJob('risk'));
  $('prepare-actuals').onclick=guard(async()=>{const a=await api('/api/cases/actual-template',{case_id:current.state.case_id});$('actual-editor').className='json-panel';$('actual-editor').innerHTML=`<p>当前为 <strong>模拟回放</strong>。采用最新计划动作生成待确认实绩；你可修改负荷、价格和实际动作，或替换为 source=METERED 的计量数据。结算不会重新优化储能。</p><textarea id="actual-json" rows="18">${esc(JSON.stringify(a,null,2))}</textarea>`;updateControls();});
  $('settle-case').onclick=guard(()=>startJob('settle',{actual:JSON.parse($('actual-json').value)}));
  async function init(){
    if(location.protocol==='file:'){$('connection').textContent='请启动项目 web 入口后访问本地地址';$('create-case').disabled=true;return;}
    try{fillSettings(await api('/api/defaults'));$('connection').textContent='本地计算服务已连接';$('connection').parentElement.classList.add('ok');await listCases();const cid=new URLSearchParams(location.search).get('case');if(cid)await loadCase(cid);const saved=JSON.parse(sessionStorage.getItem('case-job')||'null');if(saved){await loadCase(saved.cid);activeJob=saved.id;started=saved.started;await poll();}updateControls();}catch(e){$('connection').textContent='连接或加载失败';notify(e.message,true);}
  }
  init();
})();
