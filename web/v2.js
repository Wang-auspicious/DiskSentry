'use strict';
const $=id=>document.getElementById(id),el=(tag,cls,text)=>{let n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
let token=location.hash.slice(1)||sessionStorage.getItem('disksentry-token')||'';if(token){sessionStorage.setItem('disksentry-token',token);history.replaceState(null,'',location.pathname);}
let data=null,view='timeline',range={period:'today'},drive='',search='',kind='',offset=0,parent='',selected=null,details=null,busy=false,pending=false,searchTimer,toastTimer;
const names={created:'新增',discovered:'首次发现',grown:'变大',modified:'内容变化',shrunk:'变小',deleted:'已删除',moved:'移动',new:'旧版新增',removed:'旧版移除',replaced:'旧版替换'};
const risks={rebuildable:'可重建',candidate:'临时文件候选',review:'需核对',keep:'建议保留'};
const colors=['#76946b','#7b9fba','#c5a279','#a99cc0','#6e9f92','#c1989b'];
function size(v,signed=false){if(v===null||v===undefined)return '未建立基线';let n=Math.abs(v),i=0;while(n>=1000&&i<4){n/=1000;i++;}return(v<0?'−':signed&&v>0?'+':'')+(i===0?Math.round(n):n.toFixed(n>=100?0:1))+' '+['B','KB','MB','GB','TB'][i];}
function stamp(t,full=false){return new Date(t*1000).toLocaleString('zh-CN',full?{}:{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function base(p){return p.split(/[\\/]/).pop()||p;}
function localInput(d){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}T${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;}
function toast(s){$('toast').textContent=s;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),3500);}
async function api(url,body){const r=await fetch(url,{method:body?'POST':'GET',headers:{'X-DiskSentry-Token':token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});let v=await r.json();if(!r.ok)throw Error(v.error||'请求失败');return v;}
function params(){return {...range,drive,q:search,kind,offset,parent};}
async function refresh(){if(busy){pending=true;return;}busy=true;try{data=await api('/api/state?'+new URLSearchParams(params()));render();if(selected)await loadDetail(false);}catch(e){$('health').textContent='● 连接中断';$('health').classList.add('off');$('coverage-banner').textContent=e.message+'。请从桌面 DiskSentry 入口重新启动，当前没有新数据。';}finally{busy=false;if(pending){pending=false;refresh();}}}
async function action(url,body,message){try{await api(url,body);if(message)toast(message);refresh();}catch(e){toast(e.message);}}
function showView(next){view=next;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));for(const v of ['timeline','map','candidates','memory'])$(v+'-view').hidden=v!==view;$('page-name').textContent={timeline:'变更时间线',map:'空间地图',candidates:'清理线索',memory:'内存与进程'}[view];if(data)renderView();}
function render(){
 const s=data.status;const alive=data.healthy,watchers=Object.values(s.watchers||{}),allWatch=watchers.length&&watchers.every(Boolean);
 $('health').textContent=!alive?'● 后台离线':!allWatch?'● 部分监听异常':s.phase==='watching'?'● 实时监控中':'● 监听中 · 后台扫盘';$('health').className='health'+(!alive||!allWatch?' off':'');
 $('index-status').textContent=`本轮已索引 ${(s.scanned||0).toLocaleString()} 项\n跳过 ${s.skipped||0} 项`;
 $('updated').textContent='更新于 '+new Date(data.now*1000).toLocaleTimeString();$('range-label').textContent=stamp(data.range.start)+' — '+stamp(data.range.end);
 const disks=drive?data.disks.filter(d=>d.root===drive):data.disks,hasSamples=disks.some(d=>d.consumed!==null),net=hasSamples?disks.reduce((s,d)=>s+(d.consumed||0),0):null;
 $('disk-net').textContent=size(net,true);$('disk-net').className=net>0?'positive':'negative';$('disk-net-note').textContent=hasSamples?'采样期内可用空间减少 = 正数':'所选时间内没有磁盘空间采样，不能事后还原';
 $('file-net').textContent=size(data.summary.net,true);$('file-net-note').textContent=`写入 +${size(data.summary.added)} / 释放 ${size(data.summary.freed)}`;
 const safe=data.candidates.filter(c=>c.risk==='rebuildable');$('candidate-size').textContent=size(safe.reduce((s,c)=>s+c.n,0));$('candidate-note').textContent=`${safe.length} 处已扫描候选 · 初始化零 API`;
 const memory=data.memory.current;$('ram-size').textContent=memory?size(memory.used):'—';$('ram-note').textContent=memory?`${(memory.used/memory.total*100).toFixed(0)}% 使用 / 共 ${size(memory.total)}`:'等待物理内存采样';
 $('explained').textContent=size(disks.reduce((s,d)=>s+d.explained,0),true);$('unexplained').textContent=hasSamples?size(disks.reduce((s,d)=>s+(d.unexplained||0),0),true):'无采样';
 $('coverage-banner').textContent=(alive?'':'监控离线。')+(s.phase!=='watching'?`当前扫描尚未覆盖全盘，已扫描 ${(s.scanned||0).toLocaleString()} 个文件；候选容量随真实扫描更新。 `:'已完成本轮可访问范围扫描。 ')+`事件不设文件大小门槛；无法读取 ${s.skipped||0} 项。历史从 ${stamp(data.recordingSince,true)} 积累，启用前或停机期间的变化无法完整追溯。`;
 const nav=$('drive-nav');nav.replaceChildren();const all=el('button','drive-button','全部本地磁盘');all.onclick=()=>{drive='';offset=0;refresh();};nav.append(all);
 for(const d of data.disks){const b=el('button','drive-button',d.root+'   '+(drive===d.root?'· 已选择':''));b.append(el('small','',`${size(d.free)} 可用 / ${size(d.total)}`));const cap=el('div','capacity'),fill=el('i');fill.style.width=((1-d.free/d.total)*100)+'%';cap.append(fill);b.append(cap);b.onclick=()=>{drive=drive===d.root?'':d.root;offset=0;refresh();};nav.append(b);}
 $('footer-status').textContent=`全部观察事件 ${data.summary.count.toLocaleString()} 条 · 本地索引 ${size(data.indexBytes)} · 页面每 2 秒更新`;
 $('ai-enabled').checked=data.ai.enabled;$('ai-model').textContent='OpenCode Go · '+data.ai.alias;$('ai-status').textContent=(data.ai.configured?'已从本机 Pi 读取配置':'未配置，离线模式正常可用')+' · '+Object.entries(data.ai.jobs).map(([k,v])=>k+': '+v).join(' / ');
 $('settings-coverage').textContent=`进程心跳 ${stamp(s.heartbeat||data.now)}；监听 ${JSON.stringify(s.watchers||{})}；通知溢出 ${s.overflow||0}`;
 $('settings-errors').textContent=(s.errors||[]).join('\n');$('history-boundary').textContent='连续观测起点：'+stamp(data.recordingSince,true)+'。';
 renderView();
}
function svg(tag,attrs={}){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;}
function chart(container,series,field='free'){
 const host=$(container);host.replaceChildren();const count=series.reduce((s,v)=>s+v.rows.length,0);
 if(count<2){host.append(el('div','chart-empty',count?'已收到首次采样，等待下一个时间点绘制趋势':'所选时间范围没有采样。监控启动后会持续积累。'));return;}
 const width=760,height=180,left=54,right=15,top=12,bottom=28,all=series.flatMap(v=>v.rows);let min=Math.min(...all.map(r=>r[field])),max=Math.max(...all.map(r=>r[field]));if(max===min){max+=100000000;min=Math.max(0,min-100000000);}const pad=(max-min)*.15;min=Math.max(0,min-pad);max+=pad;const t0=Math.min(...all.map(r=>r.ts)),t1=Math.max(...all.map(r=>r.ts));const x=t=>left+(t-t0)/(t1-t0||1)*(width-left-right),y=v=>height-bottom-(v-min)/(max-min)*(height-top-bottom);const root=svg('svg',{viewBox:`0 0 ${width} ${height}`,preserveAspectRatio:'none',role:'img','aria-label':'所选期间的实际资源采样曲线'});
 for(let i=0;i<4;i++){const value=min+(max-min)*i/3,Y=y(value);root.append(svg('line',{x1:left,y1:Y,x2:width-right,y2:Y,stroke:'#edf1ec','stroke-dasharray':'3 4'}));const label=svg('text',{x:left-10,y:Y+3,'text-anchor':'end',fill:'#9daa9c','font-size':'9','font-family':'monospace'});label.textContent=size(value);root.append(label);}
 series.forEach((s,i)=>{const points=s.rows.map(r=>`${x(r.ts)},${y(r[field])}`).join(' ');root.append(svg('polyline',{points,fill:'none',stroke:colors[i], 'stroke-width':1.8,'stroke-linejoin':'round','stroke-linecap':'round'}));const last=s.rows.at(-1);if(last)root.append(svg('circle',{cx:x(last.ts),cy:y(last[field]),r:3,fill:colors[i]}));});
 for(const[t,anchor]of [[t0,'start'],[t1,'end']]){const label=svg('text',{x:x(t),y:height-5,'text-anchor':anchor,fill:'#a5afa2','font-size':9,'font-family':'monospace'});label.textContent=stamp(t);root.append(label);}host.append(root);
}
function renderView(){
 if(view==='timeline'){
  const disks=drive?data.disks.filter(d=>d.root===drive):data.disks;chart('disk-chart',disks.map(d=>({rows:d.trend})));$('disk-legend').replaceChildren();disks.forEach((d,i)=>{const s=el('span','',d.root);s.style.setProperty('--c',colors[i]);$('disk-legend').append(s);});
  const tbody=$('events');tbody.replaceChildren();$('event-count').textContent=data.totalEvents.toLocaleString();
  if(!data.events.length){const tr=el('tr'),td=el('td','empty','这段时间还没有匹配的文件变化。持续监听已启动，数据会自动出现。');td.colSpan=5;tr.append(td);tbody.append(tr);}
  for(const r of data.events){const tr=el('tr'),file=el('td');file.append(el('span','name',base(r.p)),el('span','path',r.p));file.title=r.p;const type=el('td');type.append(el('span','pill'+(['deleted','shrunk'].includes(r.kind)?' review':''),names[r.kind]||r.kind));const amount=el('td','mono '+(r.delta>0?'positive':r.delta<0?'negative':''),size(r.delta,true));const who=el('td','',r.attribution.agent||'未知');who.title=r.attribution.reason||'';tr.append(file,type,amount,who,el('td','event-time',stamp(r.ts).split(' ').at(-1)));tr.onclick=()=>selectFile(r.p,r.id);tbody.append(tr);}
  $('previous').disabled=offset===0;$('next').disabled=offset+150>=data.totalEvents;$('pagination-label').textContent=`${data.totalEvents?offset+1:0}–${Math.min(offset+150,data.totalEvents)} / ${data.totalEvents.toLocaleString()} 条`;
 }else if(view==='candidates'){
  const host=$('candidates');host.replaceChildren();for(const c of data.candidates){const row=el('div','candidate'),info=el('div','row-info');const label=el('div');label.append(el('span','pill '+(c.risk==='rebuildable'?'':'review'),risks[c.risk]),document.createTextNode(base(c.p)));info.append(label,el('small','',c.p),el('p','',c.reason+' · '+c.files.toLocaleString()+' 个文件'));row.append(info,el('div','row-value',size(c.n)));row.onclick=()=>selectFile(c.p);host.append(row);}if(!data.candidates.length)host.append(el('div','empty','尚未扫描到可归类候选。初次全盘扫描不会用 API，也不会虚构候选容量。'));
 }else if(view==='memory'){
  chart('ram-chart',[{rows:data.memory.history}],'used');const host=$('processes');host.replaceChildren();for(const p of data.memory.current?.processes||[]){const row=el('div','simple-row'),info=el('div','row-info');info.append(el('b','',p.name+(p.agent?' · '+p.agent:'')),el('small','',`PID ${p.pid} · 启动于 ${stamp(p.created)}`));row.append(info,el('div','row-value',size(p.rss)));host.append(row);}
 }else if(view==='map'){
  $('map-breadcrumbs').textContent=parent||'此电脑 / 全部本地磁盘';const host=$('treemap');host.replaceChildren();const nodes=data.folders.filter(f=>f.n>0),width=host.clientWidth,height=host.clientHeight;drawMap(host,nodes,0,0,width,height);
  if(!nodes.length)host.append(el('div','empty','当前目录的累计占用尚未完成。扫描期间可先查看下方已索引大文件。'));
  const list=$('large-files');list.replaceChildren();for(const r of data.largest.filter(f=>!parent||f.p.toLowerCase().startsWith(parent.toLowerCase()))){const row=el('div','simple-row'),info=el('div','row-info');info.append(el('b','',base(r.p)),el('small','',r.p));row.append(info,el('span','pill '+(r.rule.risk==='keep'?'keep':'review'),risks[r.rule.risk]),el('div','row-value',size(r.n)));row.onclick=()=>selectFile(r.p);list.append(row);}
 }
}
function drawMap(host,nodes,x,y,w,h,depth=0){
 if(!nodes.length||w<3||h<3)return;if(nodes.length===1){const n=nodes[0],tile=el('div','tile');tile.style.cssText=`left:${x+3}px;top:${y+3}px;width:${Math.max(0,w-6)}px;height:${Math.max(0,h-6)}px;--fill:${['#e7eee0','#e0e9f0','#f1e9df','#e9e3ef'][depth%4]};--stroke:#ced9c7`;tile.title=n.p+' · '+size(n.n);if(w>50&&h>30)tile.append(el('b','',base(n.p)));if(w>75&&h>60)tile.append(el('span','',size(n.n)));tile.onclick=()=>{parent=n.p;refresh();};host.append(tile);return;}
 const total=nodes.reduce((s,n)=>s+n.n,0);let split=1,sum=nodes[0].n;while(split<nodes.length-1&&sum<total/2){sum+=nodes[split].n;split++;}if(w>=h){const a=w*sum/total;drawMap(host,nodes.slice(0,split),x,y,a,h,depth+1);drawMap(host,nodes.slice(split),x+a,y,w-a,h,depth+1);}else{const a=h*sum/total;drawMap(host,nodes.slice(0,split),x,y,w,a,depth+1);drawMap(host,nodes.slice(split),x,y+a,w,h-a,depth+1);}
}
async function selectFile(path,event=null){selected={path,event};await loadDetail(true);if(innerWidth<960)$('inspector-content').scrollIntoView({behavior:'smooth',block:'nearest'});}
async function loadDetail(replace){if(!selected)return;try{const result=await api('/api/detail?'+new URLSearchParams(selected));if(!replace&&JSON.stringify(result)===JSON.stringify(details))return;details=result;renderDetail();}catch(e){if(replace)toast(e.message);}}
function section(title,text,cls=''){const s=el('section','detail-section');s.append(el('h4','',title),el('p',cls,text));return s;}
function renderDetail(){
 const d=details,host=$('inspector-content');host.replaceChildren();const box=el('div','detail');box.append(el('div','file-icon','▤'),el('h3','',base(d.file.p)),el('div','detail-path',d.file.p));const nums=el('div','detail-numbers');for(const[v,label]of [[size(d.file.n),'当前逻辑大小'],[d.file.m?stamp(d.file.m).split(' ')[0]:'—','最后修改']]){const n=el('div');n.append(el('b','',v),el('small','',label));nums.append(n);}box.append(nums);
 const rule=section('本地规则 · 不调用 API',d.rule.reason);rule.querySelector('h4').append(el('span','pill '+(d.rule.risk==='keep'?'keep':d.rule.risk==='rebuildable'?'':'review'),risks[d.rule.risk]));box.append(rule);
 const evidence=section('来源与证据',d.evidence.agent+' · '+d.evidence.reason);if(d.evidence.jsonl)evidence.append(el('p','evidence',`会话 ${d.evidence.session}\n${d.evidence.jsonl}\n${d.evidence.tool||'日志自身'} · ${stamp(d.evidence.time,true)}`));evidence.append(el('p','evidence','置信类型：'+d.evidence.confidence));box.append(evidence);
 const ai=el('section','detail-section');ai.append(el('h4','', 'AI 只读摘要 · dsv4.1f'));
 if(d.ai){if(!d.aiFresh)ai.append(el('p','', '此摘要基于旧版本文件，新的分析可重新请求。'));ai.append(el('p','',d.ai.summary||'暂未生成摘要'));ai.append(el('p','',d.ai.origin||''));ai.append(el('p','',d.ai.deletionAdvice||''));}
 else ai.append(el('p','',d.job?.status==='running'?'正在只读分析…':d.job?.status==='error'?d.job.error:data.ai.configured?'实时变更会自动排队，也可手动请求。初次扫描不自动调用 API。':'未配置 API；上方离线分类与来源证据仍然可用。'));
 const actions=el('div','detail-actions'),ask=el('button','primary','生成只读摘要');ask.disabled=!data.ai.configured||d.rule.category==='secrets';ask.onclick=()=>action('/api/analyze',{path:d.file.p},'已加入只读分析队列');const reveal=el('button','', '定位文件');reveal.onclick=()=>action('/api/reveal',{path:d.file.p},'已打开资源管理器');actions.append(ask,reveal);ai.append(actions);box.append(ai);
 const hist=el('section','detail-section');hist.append(el('h4','',`这份文件的变更 · ${d.events.length} 条`));for(const r of d.events.slice(0,15)){hist.append(el('p','evidence',`${stamp(r.ts)}  ${names[r.kind]||r.kind}  ${size(r.delta,true)}\n${size(r.b)} → ${size(r.n)}`));}if(!d.events.length)hist.append(el('p','', '这是基线中的已有文件，未记录到启用后的变化。'));box.append(hist);host.append(box);
}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));document.querySelectorAll('[data-period]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-period]').forEach(x=>x.classList.toggle('active',x===b));const custom=b.dataset.period==='custom';$('custom-range').hidden=!custom;if(custom){if(!$('start-date').value){let a=new Date();a.setDate(a.getDate()-6);a.setHours(0,0,0,0);$('start-date').value=localInput(a);$('end-date').value=localInput(new Date());}return;}range={period:b.dataset.period};offset=0;refresh();});
$('apply-range').onclick=()=>{const start=$('start-date').value,end=$('end-date').value;if(!start||!end||start>=end)return toast('请选择正确的起止时间');range={period:'custom',start,end};offset=0;refresh();};
$('search').oninput=e=>{search=e.target.value.trim();offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(refresh,250);};$('kind-filter').onchange=e=>{kind=e.target.value;offset=0;refresh();};$('previous').onclick=()=>{offset=Math.max(0,offset-150);refresh();};$('next').onclick=()=>{offset+=150;refresh();};
$('settings').onclick=()=>$('settings-dialog').showModal();$('close-settings').onclick=()=>$('settings-dialog').close();$('rescan').onclick=()=>action('/api/rescan',{},'只读补扫已排队，已有扫描会继续');$('ai-enabled').onchange=e=>action('/api/ai-enabled',{enabled:e.target.checked},'AI 自动分析设置已保存');
$('map-back').onclick=()=>{const p=parent.replace(/[\\/]+$/,'');const i=Math.max(p.lastIndexOf('\\'),p.lastIndexOf('/'));parent=i<2?'':p.slice(0,i);if(parent.length===2&&parent[1]===':')parent+='\\';refresh();};
$('export').onclick=async()=>{try{const r=await fetch('/api/export?'+new URLSearchParams(params()),{headers:{'X-DiskSentry-Token':token}});if(!r.ok)throw Error('导出失败');const blob=await r.blob(),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='DiskSentry-变更记录.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已导出所选范围记录（每次最多 100,000 条）');}catch(e){toast(e.message);}};
new ResizeObserver(()=>{if(data&&view==='map')renderView();}).observe($('treemap'));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);
