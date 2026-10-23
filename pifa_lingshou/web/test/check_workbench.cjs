const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('pifa_lingshou/web/app.js','utf8');
const html=fs.readFileSync('pifa_lingshou/web/controls.html','utf8');
function harness(protocol){
 const ids=[...html.matchAll(/id="runner-([^"]+)"/g)].map(m=>m[1]);
 const nodes=[...html.matchAll(/data-node="([^"]+)"/g)].map(m=>({dataset:{node:m[1]},disabled:false}));
 const els=Object.fromEntries(ids.map(id=>[id,{value:'',hidden:false,replaceChildren(){},add(){}}]));
 Object.assign(els.package,{value:'F'});els.risk.value='.25';els.annual.value='405';els.monthly.value='416';els.tenday.value='424';els.basis.value='grid_net_load';els.source.value='default';
 const requests=[],timers=[];let polls=0,failed=false;
 const entry={id:'up',node:'L3-A',package:'S',risk_lambda:.5,status:'OPTIMAL',result_url:'/outputs/up.json'};
 const fetch=async(url,options)=>{requests.push({url,options});let value;
  if(url==='/api/catalog')value={results:[entry]};
  else if(url===entry.result_url)value={inputs:{package:'S',risk_lambda:.5,annual_price:411,monthly_price:422,ten_day_price:433,storage:{assessment_basis:'customer_load'}}};
  else if(url==='/api/run')value={job_id:'job'};
  else if(url==='/api/job/job')value=failed?{status:'ERROR',error:'实绩缺失',message:'计算未完成',progress:5}:polls++===0?{status:'RUNNING',progress:45,message:'储能MILP',log:[{percent:45,message:'储能MILP'}]}:{status:'DONE',progress:100,message:'OPTIMAL',result_id:'job',result_url:'/outputs/job/result.json',report_url:'/outputs/job/visualize.html'};
  else throw Error(url);
  return {ok:true,json:async()=>value};
 };
 vm.runInNewContext(source,{document:{getElementById:id=>els[id.slice(7)],querySelectorAll:()=>nodes},location:{protocol},fetch,Option:function(t,v){this.text=t;this.value=v},setTimeout:fn=>timers.push(fn),Date,console});
 return {els,nodes,requests,timers,setFailed:()=>{failed=true}};
}
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve()};
(async()=>{
 const offline=harness('file:');assert(offline.nodes.every(n=>n.disabled));assert.equal(offline.requests.length,0);assert(offline.els.connection.textContent.includes('离线'));
 const h=harness('http:');await flush();assert(h.els.connection.textContent.includes('已连接'));
 h.els.upstream.value='up';await h.els.upstream.onchange();assert.equal(h.els.annual.value,411);assert.equal(h.els.package.value,'S');assert.equal(h.els.basis.value,'customer_load');
 await h.nodes.find(n=>n.dataset.node==='STORAGE-DA').onclick();await flush();
 const payload=JSON.parse(h.requests.find(r=>r.url==='/api/run').options.body);assert.equal(payload.upstream_id,'up');assert.equal(payload.inputs.annual_price,411);assert.equal(h.els.progress.value,45);assert(h.nodes.every(n=>n.disabled));
 await h.timers.shift()();await flush();assert.equal(h.els.progress.value,100);assert.equal(h.els.view.href,'/outputs/job/visualize.html');assert.equal(h.els.download.hidden,false);assert(h.nodes.every(n=>!n.disabled));
 h.setFailed();await h.nodes.find(n=>n.dataset.node==='STORAGE-RT').onclick();await flush();assert.equal(h.els.message.textContent,'实绩缺失');assert(h.nodes.every(n=>!n.disabled));
 console.log('Workbench checks passed: offline state; upstream parameter inheritance; node submission; progress polling; result links; recoverable errors.');
})().catch(e=>{console.error(e);process.exitCode=1});
