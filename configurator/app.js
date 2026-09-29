const CHANNELS = [
  ['电网代购电',1,0,'不参与市场化交易，以分时电价为基准的兜底购电方式。按代理购电价格、分时电价及输配电价结算；售电公司代理用户，通常代收代付。'],
  ['中长期协商',1,1,'按年、月、旬组织的双边协商交易，由售电公司与发电企业通过电力交易中心签订中长期合同。'],
  ['集中竞价',1,1,'交易中心平台集中申报、集中出清，形成批发侧中长期成本或发电侧收益。'],
  ['D-3/D-2滚撮',1,1,'交易中心滚动撮合，临近运行日调整仓位，形成交易成本或卖出收益。'],
  ['挂牌交易',1,1,'通过电力交易中心挂牌、摘牌，形成中长期交易的买入成本或卖出收益。'],
  ['现货日前',1,1,'参与现货市场买电形成日前出清电费；发电资源也可形成日前市场收益。'],
  ['现货实时',1,1,'现货市场实时偏差结算，可能包括不平衡费用、阻塞费用或发电侧实时收益。'],
  ['绿电交易',1,1,'通过电力交易中心绿电专场或双边协商交易，价格通常包含电能量价格与环境溢价。'],
  ['绿证交易',1,1,'绿证平台挂牌或协议转让。购买方承担证书价格，新能源发电方可出售绿证获得环境权益收益。'],
  ['零售合同',1,1,'售电公司与电力用户签订零售合同，可按固定价格、比例分成或市场联动方式结算。'],
  ['EMC合同',1,1,'合同能源管理通道，涉及第三方投资建设、客户支付电费或收益分成；收益归属按合同约定。'],
  ['分布式光伏结算',1,1,'覆盖用户侧分布式光伏自用、余电上网及市场化交易，收益通常归投资建设主体。'],
  ['充电运营平台',1,1,'售电公司自营时购电为进项、充电电费和服务费为出项；外部运营商是售电公司的零售客户。'],
  ['虚拟电厂平台',1,1,'聚合可调负荷、分布式资源与储能参与调节，形成聚合和服务收益。'],
  ['需求响应平台',0,1,'参加邀约型或竞价型需求响应并获得补偿；统一归入虚拟电厂收益。'],
  ['辅助服务市场',0,1,'参与调峰、调频、备用等辅助服务市场并获得补偿；统一归入虚拟电厂收益。'],
  ['机制电量结算',0,1,'新能源发电按照机制电价或差价结算形成的电量收益。'],
  ['用户侧储能结算',1,1,'用户侧储能充放电结算。储能收益仅计低充高放与自消纳价差，其他服务收益按协议分配。'],
  ['分时/峰谷电价',1,1,'按峰谷时段形成储能低充高放或自消纳价差，可同时体现购电成本和节省收益。'],
  ['独立储能市场',1,1,'独立储能参与现货或独立储能市场，按市场规则形成充电成本和放电收益。'],
];
const DEFAULT_MAP = {
  '工业负荷':['电网代购电','中长期协商','现货日前','现货实时','零售合同','绿电交易','绿证交易','虚拟电厂平台','需求响应平台','辅助服务市场','分时/峰谷电价'],
  '光伏':['中长期协商','现货日前','现货实时','绿电交易','绿证交易','分布式光伏结算','EMC合同','机制电量结算','零售合同'],
  '储能':['分时/峰谷电价','用户侧储能结算','现货日前','现货实时','独立储能市场','EMC合同','零售合同'],
  '充电桩':['充电运营平台','零售合同','分时/峰谷电价','绿电交易','虚拟电厂平台','需求响应平台','辅助服务市场','现货日前','现货实时'],
};
const DEFAULT_SUBJECTS = [
  {name:'售电公司', active:[1,1,1,1]}, {name:'用电客户',active:[1,1,1,1]}, {name:'EMC/第三方',active:[false,true,true,true]},
];
const STORAGE_KEY='aggregate-configurator-v1';
const DEFAULT_KEY='aggregate-configurator-default-v1';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function defaults(){
  const channels=CHANNELS.map((c,i)=>({id:`c${i}`,name:c[0],in:!!c[1],out:!!c[2],description:c[3]}));
  return {channels,assets:Object.entries(DEFAULT_MAP).map(([name,mapped],i)=>({id:`a${i}`,name,mapped:mapped.map(label=>channels.find(c=>c.name===label)?.id).filter(Boolean)})),subjects:DEFAULT_SUBJECTS.map((s,i)=>({id:`s${i}`,name:s.name,active:[...s.active]})),selectedPairs:[{subjectId:'s0',assetId:'a0'},{subjectId:'s0',assetId:'a2'}],selectedChannels:channels.filter(c=>DEFAULT_MAP['工业负荷'].some(n=>n===c.name)).map(c=>c.id),selectedDirections:{}};
}
function normalize(saved){if(!saved||!saved.channels||!saved.assets||!saved.subjects)return null;saved.subjects.forEach(s=>{s.active=(s.active||[]).map(Boolean)});if(!saved.selectedPairs){const subjects=saved.selectedSubjects||[saved.selectedSubject||saved.subjects[0]?.id];const assets=saved.selectedAssets||[saved.selectedAsset||saved.assets[0]?.id];saved.selectedPairs=subjects.flatMap(subjectId=>assets.filter(assetId=>{const s=saved.subjects.find(x=>x.id===subjectId),i=saved.assets.findIndex(x=>x.id===assetId);return s&&s.active[i]!==false}).map(assetId=>({subjectId,assetId})));}saved.selectedChannels=saved.selectedChannels||((saved.selectedChannel)?[saved.selectedChannel]:[]);saved.selectedDirections=Array.isArray(saved.selectedDirections)?{}:(saved.selectedDirections||{});return saved}
function load(){try{return normalize(JSON.parse(localStorage.getItem(STORAGE_KEY)))||normalize(JSON.parse(localStorage.getItem(DEFAULT_KEY)))||defaults()}catch{return defaults()}}
let state=load(); let toastTimer;
const save=()=>{localStorage.setItem(STORAGE_KEY,JSON.stringify(state));$('save-state').textContent='已保存到此浏览器';};
const touch=()=>{save();renderAll()};
const assetIndex=id=>state.assets.findIndex(a=>a.id===id);
const subjectIndex=id=>state.subjects.findIndex(s=>s.id===id);
const channelById=id=>state.channels.find(c=>c.id===id);
const assetById=id=>state.assets.find(a=>a.id===id);
const subjectById=id=>state.subjects.find(s=>s.id===id);
const applicable=asset=>asset?state.channels.filter(c=>asset.mapped.includes(c.id)&&(c.in||c.out)):[];
function showToast(message){const node=$('toast');node.textContent=message;node.classList.add('is-visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('is-visible'),1900)}
function renderChannels(){
  $('channel-table-body').innerHTML=state.channels.map(c=>`<tr><td><div class="channel-name-editor"><input class="editable-input channel-name-input" data-channel-name="${c.id}" value="${esc(c.name)}" aria-label="编辑交易渠道名称"><button class="channel-open-button" data-open-channel="${c.id}" aria-label="查看并编辑${esc(c.name)}说明">↗</button></div></td><td><button class="direction-toggle ${c.in?'is-on-in':''}" data-direction="in" data-id="${c.id}" aria-label="切换${esc(c.name)}进">${c.in?'✓':'—'}</button></td><td><button class="direction-toggle ${c.out?'is-on-out':''}" data-direction="out" data-id="${c.id}" aria-label="切换${esc(c.name)}出">${c.out?'✓':'—'}</button></td><td><button class="row-delete" data-delete-channel="${c.id}" aria-label="删除渠道">×</button></td></tr>`).join('');
  $('in-flow-list').innerHTML=state.channels.filter(c=>c.in).map(c=>`<button class="flow-chip" data-open-channel="${c.id}">${esc(c.name)}</button>`).join('');$('out-flow-list').innerHTML=state.channels.filter(c=>c.out).map(c=>`<button class="flow-chip" data-open-channel="${c.id}">${esc(c.name)}</button>`).join('');$('channel-count').textContent=`${state.channels.length} 个渠道 · 进 ${state.channels.filter(c=>c.in).length} · 出 ${state.channels.filter(c=>c.out).length}`;
}
function renderMapping(){
  $('mapping-head').innerHTML=`<tr><th>资产</th>${state.channels.map(c=>`<th class="mapping-channel-head"><span>${esc(c.name)}</span><button class="row-delete" data-delete-mapping-column="${c.id}" aria-label="删除映射列">×</button></th>`).join('')}<th>操作</th></tr>`;
  $('mapping-body').innerHTML=state.assets.map(a=>`<tr><td><input class="editable-input" data-asset-name="${a.id}" value="${esc(a.name)}" aria-label="资产名称"></td>${state.channels.map(c=>`<td class="mapping-check"><input type="checkbox" data-map="${a.id}" data-channel="${c.id}" ${a.mapped.includes(c.id)?'checked':''} aria-label="${esc(a.name)}适用${esc(c.name)}"></td>`).join('')}<td><button class="button button-secondary row-delete-text" data-delete-asset="${a.id}" aria-label="删除资产">删除资产</button></td></tr>`).join('');
}
function renderSubjects(){
  $('subject-head').innerHTML=`<tr><th>主体 \\ 资产</th>${state.assets.map(a=>`<th>${esc(a.name)}</th>`).join('')}<th>操作</th></tr>`;
  $('subject-body').innerHTML=state.subjects.map(s=>`<tr><td><input class="editable-input" data-subject-name="${s.id}" value="${esc(s.name)}" aria-label="主体名称"></td>${state.assets.map((a,i)=>{const active=s.active[i]!==false;return `<td><button class="subject-status ${active?'':'is-off'}" data-pair="${s.id}" data-asset-index="${i}">${active?'有效':'不适用'}</button></td>`}).join('')}<td><button class="row-delete" data-delete-subject="${s.id}" aria-label="删除主体">×</button></td></tr>`).join('');
}
function renderSelectors(){
  state.selectedPairs=state.selectedPairs.filter(pair=>subjectById(pair.subjectId)&&assetById(pair.assetId)&&subjectById(pair.subjectId).active[assetIndex(pair.assetId)]!==false);
  const selectedAssetObjects=state.selectedPairs.map(pair=>assetById(pair.assetId)).filter(Boolean); const channels=state.channels.filter(c=>selectedAssetObjects.some(a=>a.mapped.includes(c.id))&&(c.in||c.out));
  state.selectedChannels=state.selectedChannels.filter(id=>channels.some(c=>c.id===id));
  $('pair-list').innerHTML=state.selectedPairs.map((pair,index)=>{const s=subjectById(pair.subjectId);return `<div class="pair-row"><span class="pair-index">${index+1}</span><select data-pair-subject="${index}">${state.subjects.map(x=>`<option value="${x.id}" ${x.id===pair.subjectId?'selected':''}>${esc(x.name)}</option>`).join('')}</select><span class="pair-times">×</span><select data-pair-asset="${index}">${state.assets.filter((x,i)=>!s||s.active[i]!==false).map(x=>`<option value="${x.id}" ${x.id===pair.assetId?'selected':''}>${esc(x.name)}</option>`).join('')}</select><button class="row-delete" data-delete-pair="${index}" aria-label="删除配对">×</button></div>`}).join('')||'<div class="pair-empty">尚未建立主体与资产配对，请新增一行。</div>';
  $('channel-options').innerHTML=channels.map(c=>`<div class="channel-option"><label class="channel-option-name"><input type="checkbox" data-select-channel="${c.id}" ${state.selectedChannels.includes(c.id)?'checked':''}><span>${esc(c.name)}</span></label><div class="channel-directions">${c.in?`<label class="direction-check"><input type="checkbox" data-select-flow="${c.id}:in" ${state.selectedDirections[c.id]?.includes('in')?'checked':''}>进</label>`:''}${c.out?`<label class="direction-check"><input type="checkbox" data-select-flow="${c.id}:out" ${state.selectedDirections[c.id]?.includes('out')?'checked':''}>出</label>`:''}</div></div>`).join('')||'<span>请先选择资产</span>';
  $('pair-hint').textContent=`已建立 ${state.selectedPairs.length} 个明确配对；同一主体可以拥有多个资产，同一资产也可以归属于不同主体。`; $('channel-hint').textContent=`已选 ${state.selectedChannels.length} 个渠道；每个渠道可独立选择进、出`;
  renderCounts();
}
function renderCounts(){
  const allPairs=state.subjects.length*state.assets.length;
  const pairCount=state.selectedPairs.length;
  const selectedChannels=state.selectedChannels.map(channelById).filter(Boolean);
  const bitsForPair=(pair, channelIds)=>{const asset=assetById(pair.assetId);return channelIds.map(channelById).filter(Boolean).filter(c=>asset?.mapped.includes(c.id)).reduce((sum,c)=>sum+Number(c.in)+Number(c.out),0)};
  const statesForPair=(pair, channelIds)=>2n**BigInt(bitsForPair(pair,channelIds));
  const directionStates=selectedChannels.reduce((sum,c)=>sum+Number(c.in)+Number(c.out),0);
  const selectedBits=state.selectedPairs.reduce((sum,pair)=>sum+bitsForPair(pair,state.selectedChannels),0);
  const unrestricted=2n**BigInt(selectedBits);
  const required=unrestricted-1n;
  const breakdown=state.assets.map((asset,index)=>{
    const subjects=state.subjects.filter(s=>s.active[index]!==false);
    const channels=state.channels.filter(c=>asset.mapped.includes(c.id)&&(c.in||c.out));
    const bits=channels.reduce((sum,c)=>sum+Number(c.in)+Number(c.out),0);
    const perPair=2n**BigInt(bits)-1n;
    return {asset,subjects,channels,bits,perPair,subtotal:BigInt(subjects.length)*perPair};
  }).filter(item=>item.subjects.length);
  const validPairs=breakdown.reduce((sum,item)=>sum+item.subjects.length,0);
  const theoreticalBits=breakdown.reduce((sum,item)=>sum+item.subjects.length*item.bits,0);
  const theoretical=2n**BigInt(theoreticalBits);
  const theoreticalRequired=theoretical-1n;
  $('combination-count').textContent=unrestricted.toLocaleString('zh-CN'); $('large-combination-count').textContent=`${validPairs} / ${allPairs}`; $('selected-pair-count').textContent=pairCount; $('selected-channel-count').textContent=selectedChannels.length; $('selected-direction-count').textContent=directionStates; $('theoretical-count-footer').textContent=theoreticalRequired.toLocaleString('zh-CN');
  $('theoretical-calculation').textContent=breakdown.length?`2^(${breakdown.map(item=>`${item.subjects.length} × ${item.bits}`).join(' + ')}) − 1 = 2^${theoreticalBits} − 1`:'0 = 0';
  $('theoretical-explanation').innerHTML=breakdown.length?breakdown.map(item=>`<p><strong>${esc(item.asset.name)}</strong>：${item.subjects.length} 个有效主体（${item.subjects.map(s=>esc(s.name)).join('、')}）× ${item.bits} 个进/出方向位 = ${item.subjects.length*item.bits} 个独立方向位；该资产单独的非空状态为 ${item.subtotal.toLocaleString('zh-CN')} 个。</p>`).join('')+'<p>不同资产之间可以同时出现，因此各资产的方向位不能把非空小计直接相加，而要合并为一次幂运算。总方向位为 '+theoreticalBits+'，所以全量有效状态为 2^'+theoreticalBits+' − 1；减去的 1 是所有资产、所有方向均未选择的全局空状态。</p>':'<p>表3中暂无有效主体与资产配对，当前总数为 0。</p>';
  const result=$('definition-result'); result.classList.remove('empty'); result.innerHTML=`当前选中 ${pairCount} 个明确主体 × 资产配对，按每个配对实际适用的渠道计算。<strong>当前状态组合 ${unrestricted.toLocaleString('zh-CN')} 个</strong>；若排除“所有方向均未选”的空状态，则为 <strong>${required.toLocaleString('zh-CN')} 个有效组合</strong>。全量有效主体 × 资产配对、按表2渠道映射展开后为 <strong>${theoretical.toLocaleString('zh-CN')} 个理论状态</strong>（排除空状态为 ${theoreticalRequired.toLocaleString('zh-CN')} 个）。`;
}
function renderAll(){renderChannels();renderMapping();renderSubjects();renderSelectors()}
function openDrawer(id){const c=channelById(id);if(!c)return;$('drawer-title').textContent=c.name;$('drawer-direction-badge').textContent=c.in&&c.out?'进 / 出':c.in?'仅进 · 成本':c.out?'仅出 · 收益':'当前无方向';$('drawer-direction-text').textContent=c.in&&c.out?'支持成本与收益，方向独立配置':c.in?'当前仅支持成本进入':c.out?'当前仅支持收益流出':'请先在表1中启用方向';$('drawer-description-input').value=c.description||'';$('drawer-subjects').textContent='售电公司、用电客户、EMC/第三方（依主体资产表确定）';$('drawer-assets').textContent=state.assets.filter(a=>a.mapped.includes(id)).map(a=>a.name).join('、')||'尚未映射';$('channel-drawer').dataset.channelId=id;$('channel-drawer').classList.add('is-open');$('drawer-backdrop').classList.add('is-visible');$('channel-drawer').setAttribute('aria-hidden','false')}
function closeDrawer(){$('channel-drawer').classList.remove('is-open');$('drawer-backdrop').classList.remove('is-visible');$('channel-drawer').setAttribute('aria-hidden','true')}
function addItem(promptText){const value=prompt(promptText);return value?.trim()||''}
function addChannel(){const name=addItem('请输入交易渠道名称');if(!name)return;const description=addItem('请输入渠道业务说明')||'自定义渠道，请补充业务说明。';state.channels.push({id:`c${Date.now()}`,name,in:true,out:true,description});touch();showToast('已新增交易渠道')}
function addAsset(){const name=addItem('请输入资产名称');if(!name)return;state.assets.push({id:`a${Date.now()}`,name,mapped:[]});state.subjects.forEach(s=>s.active.push(true));touch();showToast('已新增资产')}
function addSubject(){const name=addItem('请输入主体名称');if(!name)return;state.subjects.push({id:`s${Date.now()}`,name,active:state.assets.map(()=>true)});touch();showToast('已新增主体')}
function addSubjectAsset(){const name=addItem('请输入新增资产列的名称');if(!name)return;state.assets.push({id:`a${Date.now()}`,name,mapped:[]});state.subjects.forEach(s=>s.active.push(true));touch()}
function addPair(){const subjectId=state.subjects[0]?.id;const assetId=state.assets.find((a,i)=>{const s=subjectById(subjectId);return s&&s.active[i]!==false})?.id;if(!subjectId||!assetId){showToast('请先在表3中建立有效主体和资产');return}state.selectedPairs.push({subjectId,assetId});touch();showToast('已新增主体 × 资产配对')}
function deleteChannel(id){const c=channelById(id);if(!c||!confirm(`删除交易渠道“${c.name}”？`))return;state.channels=state.channels.filter(x=>x.id!==id);state.assets.forEach(a=>a.mapped=a.mapped.filter(cid=>cid!==id));touch()}
function deleteAsset(id){const a=assetById(id);if(!a||!confirm(`删除资产“${a.name}”？`))return;const i=assetIndex(id);state.assets.splice(i,1);state.subjects.forEach(s=>s.active.splice(i,1));touch()}
function deleteSubject(id){const s=subjectById(id);if(!s||!confirm(`删除主体“${s.name}”？`))return;state.subjects=state.subjects.filter(x=>x.id!==id);touch()}
function setAsDefault(){localStorage.setItem(DEFAULT_KEY,JSON.stringify(state));$('save-state').textContent='已设为默认值';showToast('当前状态已设为默认值')}
function restoreDefault(){const saved=normalize(JSON.parse(localStorage.getItem(DEFAULT_KEY)||'null'));state=saved||defaults();save();renderAll();showToast(saved?'已恢复到自定义默认值':'已恢复系统默认值')}
document.addEventListener('click',e=>{const t=e.target;const open=t.closest('[data-open-channel]');if(open){openDrawer(open.dataset.openChannel);return}if(t.id==='close-drawer'||t.id==='drawer-backdrop'){closeDrawer();return}if(t.id==='add-pair'){state.selectedPairs.push({subjectId:state.subjects[0]?.id||'',assetId:state.assets[0]?.id||''});touch();return}const deletePair=t.closest('[data-delete-pair]');if(deletePair){state.selectedPairs.splice(Number(deletePair.dataset.deletePair),1);touch();return}const d=t.closest('[data-direction]');if(d){const c=channelById(d.dataset.id);c[d.dataset.direction]=!c[d.dataset.direction];touch();return}const dc=t.closest('[data-delete-channel]');if(dc){deleteChannel(dc.dataset.deleteChannel);return}const da=t.closest('[data-delete-asset]');if(da){deleteAsset(da.dataset.deleteAsset);return}const ds=t.closest('[data-delete-subject]');if(ds){deleteSubject(ds.dataset.deleteSubject);return}const col=t.closest('[data-delete-mapping-column]');if(col){deleteChannel(col.dataset.deleteMappingColumn);return}const pair=t.closest('[data-pair]');if(pair){const s=subjectById(pair.dataset.pair);const i=Number(pair.dataset.assetIndex);s.active[i]=s.active[i]===false;touch();return}});
document.addEventListener('change',e=>{const t=e.target;if(t.matches('[data-map]')){const a=assetById(t.dataset.map);if(t.checked&&!a.mapped.includes(t.dataset.channel))a.mapped.push(t.dataset.channel);if(!t.checked)a.mapped=a.mapped.filter(id=>id!==t.dataset.channel);touch();return}if(t.matches('[data-pair-subject]')){const pair=state.selectedPairs[Number(t.dataset.pairSubject)];pair.subjectId=t.value;const s=subjectById(pair.subjectId);if(!s||s.active[assetIndex(pair.assetId)]===false)pair.assetId=state.assets.find((a,i)=>s&&s.active[i]!==false)?.id||state.assets[0]?.id;touch();return}if(t.matches('[data-pair-asset]')){state.selectedPairs[Number(t.dataset.pairAsset)].assetId=t.value;touch();return}if(t.matches('[data-select-channel]')){const id=t.dataset.selectChannel;state.selectedChannels=t.checked?[...new Set([...state.selectedChannels,id])]:state.selectedChannels.filter(x=>x!==id);if(!t.checked)delete state.selectedDirections[id];save();renderSelectors();return}if(t.matches('[data-select-flow]')){const [id,direction]=t.dataset.selectFlow.split(':');const current=state.selectedDirections[id]||[];if(t.checked&&!state.selectedChannels.includes(id))state.selectedChannels.push(id);state.selectedDirections[id]=t.checked?[...new Set([...current,direction])]:current.filter(x=>x!==direction);if(!state.selectedDirections[id].length)delete state.selectedDirections[id];save();renderSelectors()}});
document.addEventListener('input',e=>{const t=e.target;if(t.dataset.channelName){const c=channelById(t.dataset.channelName);if(c){c.name=t.value;save();if($('channel-drawer').dataset.channelId===c.id)$('drawer-title').textContent=t.value}return}if(t.id==='drawer-description-input'){const c=channelById($('channel-drawer').dataset.channelId);if(c){c.description=t.value;save()}return}if(t.dataset.assetName){assetById(t.dataset.assetName).name=t.value;save();renderSubjects();renderSelectors()}if(t.dataset.subjectName){subjectById(t.dataset.subjectName).name=t.value;save();renderSelectors()}});
 $('add-channel').addEventListener('click',addChannel);$('add-mapping-channel').addEventListener('click',addChannel);$('add-asset').addEventListener('click',addAsset);$('add-subject').addEventListener('click',addSubject);$('add-subject-column').addEventListener('click',addSubjectAsset);$('set-default').addEventListener('click',()=>{if(confirm('将当前状态设为默认值？'))setAsDefault()});$('reset-all').addEventListener('click',()=>{if(confirm('恢复默认表格定义？'))restoreDefault()});$('close-drawer').addEventListener('click',closeDrawer);$('drawer-backdrop').addEventListener('click',closeDrawer);
if($('add-pair'))$('add-pair').addEventListener('click',e=>{e.stopImmediatePropagation();addPair()});
renderAll();
