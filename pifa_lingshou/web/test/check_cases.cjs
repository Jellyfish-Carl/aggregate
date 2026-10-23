// In-memory DOM/API smoke check. This does not claim a real browser/network test.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const html=fs.readFileSync('pifa_lingshou/web/index.html','utf8');
const js=fs.readFileSync('pifa_lingshou/web/case.js','utf8');
const decode=s=>s.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
class Element {
 constructor(tag='div',attrs={}){this.tag=tag;this.attrs=attrs;this.children=[];this.value=attrs.value||'';this.hidden='hidden'in attrs;this.disabled='disabled'in attrs;this.style={};this.dataset={};this.className=attrs.class||'';this.textContent='';this.classList={add:n=>this.className+=' '+n};this.listeners={};for(const [k,v] of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=decode(v);}
 set innerHTML(s){this.children=parse(s,this);this._html=s;}
 get innerHTML(){return this._html||'';}
 get parentElement(){return this.parent;}
 setAttribute(k,v){this.attrs[k]=v;}
 addEventListener(k,fn){this.listeners[k]=fn;}
 replaceChildren(...children){this.children=children;for(const c of children)c.parent=this;}
}
function parse(s,parent){const root=new Element(),stack=[root],rx=/<\/?[a-zA-Z][^>]*>|[^<]+/g;let m;while((m=rx.exec(s))){const token=m[0];if(token.startsWith('</')){if(stack.length>1)stack.pop();continue;}if(token.startsWith('<')){const tag=token.match(/^<([\w-]+)/)[1].toLowerCase(),attrs={};const text=token.slice(tag.length+1,-1);for(const a of text.matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)')?/g))attrs[a[1]]=a[2]??a[3]??'';const el=new Element(tag,attrs);el.parent=stack.at(-1);stack.at(-1).children.push(el);if(!['input','meta','link','br','hr','img'].includes(tag))stack.push(el);}else{const el=stack.at(-1);el.textContent+=decode(token);if(el.tag==='textarea')el.value+=decode(token);}}for(const child of root.children)child.parent=parent;return root.children;}
const root=new Element();root.innerHTML=html;
const walk=n=>n.children.flatMap(c=>[c,...walk(c)]);
function matches(e,s){const attr=s.match(/^\[([^=\]]+)(?:="?([^"\]]+)"?)?\]$/);if(attr)return attr[1]in e.attrs&&(attr[2]===undefined||e.attrs[attr[1]]===attr[2]);if(s[0]==='#')return e.attrs.id===s.slice(1);if(s[0]==='.')return e.className.split(' ').includes(s.slice(1));return e.tag===s;}
function query(s){return walk(root).filter(e=>s.split(',').some(part=>{const bits=part.trim().split(/\s+/);if(!matches(e,bits.pop()))return false;let p=e.parent;while(bits.length){const want=bits.pop();while(p&&!matches(p,want))p=p.parent;if(!p)return false;p=p.parent;}return true;}));}
const document={getElementById:id=>walk(root).find(e=>e.attrs.id===id)||null,querySelectorAll:query};
const el=id=>{const x=document.getElementById(id);assert(x,'missing '+id);return x;};
let serverView=structuredClone(fixture.draft),requests=[],pendingAction=null;const jobs=[],toasts=[];
const clone=v=>JSON.parse(JSON.stringify(v));
const fetch=async(path,options)=>{let value;const body=options?.body?JSON.parse(options.body):undefined;requests.push({path,body});
 if(path==='/api/defaults')value=fixture.draft.input;
 else if(path==='/api/cases')value={cases:[serverView.state]};
 else if(path==='/api/cases/create'){value=serverView=clone(fixture.draft);}
 else if(path==='/api/cases/save'){serverView.input=body.input;serverView.state.revision++;serverView.state.status='DRAFT';delete serverView.state.recommendation;value=serverView;}
 else if(path==='/api/cases/run'){pendingAction=body.action;value={job_id:'job'};}
 else if(path==='/api/job/job'){if(pendingAction==='recommend'){serverView.state.recommendation=fixture.recommendation;serverView.state.status='RECOMMENDED';}else if(pendingAction==='stage'){serverView.state.stages=fixture.completed.state.stages.slice(0,1);}value={status:'DONE',progress:100,message:'已保存',log:[{percent:100,message:'完成'}]};}
 else if(path==='/api/cases/confirm'){serverView.state.packages=body.packages;serverView.state.status='SIGNED';value=serverView;}
 else if(path.startsWith('/api/cases/'))value=serverView;
 else throw Error(path);
 return {ok:true,json:async()=>clone(value)};
};
const storage=new Map();
const context={document,fetch,Option:function(t,v){const e=new Element('option');e.value=v;e.textContent=t;return e;},console,Date,URLSearchParams,location:{protocol:'http:',search:''},history:{replaceState(){}},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setTimeout:fn=>jobs.push(fn)};
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
(async()=>{
 vm.runInNewContext(js,context);await flush();assert(el('connection').textContent.includes('已连接'));
 await el('create-case').onclick();await flush();assert.equal(query('[data-customer-curve]').length,384);assert.equal(query('[data-market-curve]').length,192);assert.equal(document.getElementById('run-stage'),null,'unsigned case cannot trade');
 const first=query('[data-customer-curve]')[0];first.value='2.5';first.listeners.input();el('minimum-profit').value='10';
 await el('save-inputs').onclick();await flush();const saved=requests.filter(r=>r.path==='/api/cases/save').at(-1).body;assert.equal(saved.input.customers[0].load_profile_mwh[0],2.5);assert.equal(saved.input.recommendation_policy.minimum_expected_profit_yuan,10);
 await el('calculate-recommendation').onclick();await flush();assert(el('recommendation-results').innerHTML.includes('公司期望成本'));assert.equal(query('[data-package]').length,3);assert(query('[data-package]').every(e=>e.value===''),'manual choice required');
 for(const [i,e] of query('[data-package]').entries())e.value=['F','S','L'][i];await el('sign-packages').onclick();await flush();assert.equal(el('save-inputs').disabled,true);assert.equal(el('run-stage').disabled,false);assert.equal(el('forecast-json').value.includes('customer_p90_mwh'),true);
 await el('run-stage').onclick();await flush();assert(el('stage-results').innerHTML.includes('完整求解结果'));assert.equal(el('job-progress').value,100);
 serverView=clone(fixture.completed);el('case-select').value=serverView.state.case_id;await el('case-select').onchange();await flush();assert.equal(el('compare-risk').disabled,false);assert.equal(el('prepare-actuals').disabled,false);assert(el('load-chart').innerHTML.includes('左轴'));
 serverView=clone(fixture.settled);el('case-select').value=serverView.state.case_id;await el('case-select').onchange();await flush();assert(el('settlement-results').innerHTML.includes('储能退化'));assert(el('settlement-results').innerHTML.includes('实际套餐账单'));assert.equal(el('settle-case').disabled,true);
 console.log('Case UI checks passed: create/edit/save, manual signing, next stage, progress, reopening cases, costs and settlement. DOM/API are simulated; no browser/network claim.');
})().catch(e=>{console.error(e);process.exitCode=1;});
