'use strict';
const $ = id => document.getElementById(id);
const state = {project: null, projects: [], status: {}, busy: false, clipIndex: -1, export: null, preview: null, agentJob: null, watchId: 0};
document.body.dataset.tab = 'media';
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<span class="icon" aria-hidden="true" style="--icon:url(/assets/icons/${name}.svg)"></span>`;
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
function renderAgentTask() {
 const j=state.agentJob;
 $('agentTaskPanel').hidden=!j;
 if(!j)return;
 const failed=j.status==='failed', disconnected=j.status==='disconnected';
 $('agentTaskPanel').dataset.status=j.status;
 $('agentTaskTitle').textContent=failed?'这次处理没有完成':(disconnected?'正在等待重新连接':(j.kind==='analyze'?'正在理解你的素材':'正在整理这条故事'));
 $('agentTaskMessage').textContent=j.error||j.message||'正在准备';
 $('agentTaskProgress').value=Number(j.progress)||0;
 $('agentTaskHint').textContent=failed?(j.lost?'任务记录已失效，请刷新项目核对结果后再提交。':'原时间线已保留，可以重试或修改剪辑要求。'):(disconnected?'后台任务可能仍在运行。重新连接会继续查询，不会重复提交。':'显示实际处理阶段；模型等待期间进度不会虚增。刷新后可继续查看。');
 $('agentTaskRetry').hidden=j.lost||!(failed||disconnected);
 $('agentTaskRetry').textContent=disconnected?'重新连接':'重试';
 $('agentTaskDismiss').hidden=!failed;
 const elapsed=Math.max(0,Math.floor((failed?(j.updated||j.created):Date.now()/1000)-(j.created||Date.now()/1000)));
 $('agentTaskElapsed').textContent=(failed?'用时 ':'已等待 ')+(elapsed<60?`${elapsed} 秒`:`${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`);
}
setInterval(()=>{if(state.agentJob)renderAgentTask();},1000);
async function watchAgentJob(job) {
 const watch=++state.watchId;
 state.agentJob=job;setBusy(true,job.message||'剪辑搭子正在处理…');renderAgentTask();
 let failures=0;
 while(watch===state.watchId && state.project?.id===job.project_id) {
  if(job.status==='done'||job.status==='failed')break;
  await new Promise(r=>setTimeout(r,800));
  try {job=await api('/agent-jobs/'+job.id);failures=0;}
  catch(e) {
   if(e.status===404){state.agentJob={...job,status:'failed',lost:true,error:e.message};setBusy(false);renderAgentTask();return;}
   failures++;
   if(failures<3)continue;
   state.agentJob={...job,status:'disconnected',error:'暂时连接不到工作室，请恢复连接后继续查看。'};renderAgentTask();return;
  }
  state.agentJob=job;renderAgentTask();
  if($('analysisDialog').open)$('analysisNotice').textContent=job.message||'正在处理…';
 }
 if(watch!==state.watchId)return;
 if(job.status==='failed') {state.agentJob=job;setBusy(false);renderAgentTask();return;}
 try {
  const p=await api('/projects/'+job.project_id);
  state.agentJob=null;renderProject(p);await refreshProjects();
  if(job.kind==='edit' && $('chatInput').value===job.request_text){$('chatInput').value='';saveDraft();}
  toast(job.kind==='analyze'?'素材理解已完成，点击时间或画面可以核对原片。':'剪辑方案已更新，可以生成预览。');
 } catch(e) {state.agentJob={...job,status:'disconnected',error:'任务可能已完成，重新连接后会读取结果。'};renderAgentTask();return;}
 setBusy(false);renderAgentTask();
}
async function submitAgentJob(kind,text='') {
 if(state.busy||!state.project)return;
 setBusy(true,'正在提交…');
 try {
  const job=await api('/projects/'+state.project.id+'/agent-jobs',{method:'POST',body:JSON.stringify({kind,text,revision:state.project.revision})});
  await watchAgentJob(job);
 } catch(e) {state.agentJob={kind,request_text:text,status:'failed',error:e.message,created:Date.now()/1000,progress:0};setBusy(false);renderAgentTask();}
}
$('agentTaskRetry').onclick=async()=>{
 const j=state.agentJob;if(!j)return;
 if(j.status==='disconnected') {
  try {await watchAgentJob(await api('/agent-jobs/'+j.id));}catch(e){if(e.status===404){state.agentJob={...j,status:'failed',lost:true,error:e.message};setBusy(false);renderAgentTask();}else toast('仍未连接到工作室，请稍后重试。');}
 } else {if(j.kind==='edit'&&j.request_text)$('chatInput').value=j.request_text;submitAgentJob(j.kind,j.request_text||'');}
};
$('agentTaskDismiss').onclick=()=>{state.agentJob=null;renderAgentTask();};
async function api(path, options = {}) {
 const headers = options.body instanceof FormData ? {} : {'Content-Type':'application/json'};
 const response = await fetch('/api' + path, {...options, headers, credentials:'same-origin'});
 const data = await response.json();
 if (response.status === 401) { $('app').hidden = true; $('pairScreen').hidden = false; }
 if (!response.ok) {const error=new Error(typeof data.detail === 'string' ? data.detail : '请求参数有误，请检查输入');error.status=response.status;throw error;}
 return data;
}
function fileUrl(relative) { return '/api/projects/' + state.project.id + '/files/' + relative.split('/').map(encodeURIComponent).join('/'); }
const transitionNames={cut:'直接切换',dissolve:'短叠化',fadeblack:'淡黑',wipeleft:'左擦除',wiperight:'右擦除',slideleft:'左平移',slideright:'右平移',circleopen:'圆形展开'};
const lookNames={natural:'自然',warm:'轻暖',soft:'柔和',bw:'黑白'};
function incoming(clips,index) { const t=clips[index]?.transition;return index>0 && t && t.type!=='cut' ? Number(t.duration)||0 : 0; }
function clipSpan(clips,index) { return Number(clips[index].duration)-incoming(clips,index+1); }
function total() { return state.project ? state.project.timeline.reduce((n,c,i,clips) => n + Number(c.duration)-incoming(clips,i), 0) : 0; }
function visualModelAvailable() { return state.status.mode==='model' && (state.status.provider==='codex'||state.status.send_frames); }
function analysisControls() {
 const visual=state.project?.media.some(m=>m.kind!=='audio');
 $('analyzeBtn').disabled=state.busy||!visual;
 $('analysisStatus').textContent=state.project?.understanding?'内容、联系与故事依据':'先逐份看素材';
 $('analysisRefresh').disabled=state.busy||!visual||!visualModelAvailable();
 $('analysisRefresh').textContent=state.project?.understanding?'检查素材理解':'开始素材理解';
 $('analysisSettings').hidden=visualModelAvailable();
}
function setBusy(value, hint = '') {
 state.busy = value;
 for (const id of ['sendBtn','renderBtn','exportBtn','addMediaBtn','importEmpty','undoBtn','musicBtn','newProjectBtn','aspectSelect']) $(id).disabled = value || (!state.project && !['newProjectBtn','addMediaBtn','importEmpty'].includes(id));
 $('renderBtn').disabled ||= !state.project?.timeline.length;
 $('exportBtn').disabled ||= !state.project?.timeline.length;
 $('undoBtn').disabled ||= !state.project || state.project.revision === 0;
 document.querySelectorAll('.project-item,.clip-card').forEach(el => el.disabled = value);
 document.querySelectorAll('[data-prompt]').forEach(el => el.disabled = value);
 $('composerHint').textContent = hint || 'Enter 发送 · Shift+Enter 换行';
 $('projectStatus').textContent = value ? '正在处理…' : (state.project ? '已自动保存' : '准备开始');
 $('saveStatus').innerHTML = '<i></i>' + (value ? '正在处理你的创作' : (state.project ? '本地自动保存' : '本地工作空间'));
 analysisControls();
}
function renderProjects() {
 $('projectCount').textContent = state.projects.length;
 $('projectList').innerHTML = state.projects.map(p => `<button class="project-item ${p.id === state.project?.id ? 'active' : ''}" data-project="${p.id}" title="${escapeHtml(p.name)}">${icon('film')}<span class="project-copy"><strong>${escapeHtml(p.name)}</strong><small>${p.count} 份素材 · ${p.duration.toFixed(1)} 秒</small></span></button>`).join('');
 document.querySelectorAll('[data-project]').forEach(el => el.onclick = () => openProject(el.dataset.project));
}
async function refreshProjects() { state.projects = await api('/projects'); renderProjects(); }
function updateMode() {
 const model = state.status.mode === 'model';
 $('modeBadge').textContent = model ? (state.status.provider==='codex'?'Codex 视觉':(state.status.send_frames?'API 视觉':'API 文字')) : '基础模式';
 $('agentNote').textContent = model && (state.status.provider==='codex'||state.status.send_frames) ? '看懂画面，再一起找到故事的节奏。' : '先说说，你想怎样组织这些片段？';
 if(state.status.provider==='codex') $('modeDisclosure').textContent='先理解时间采样，再规划剪辑；使用本机 Codex 账号额度。';
 else $('modeDisclosure').textContent = model ? (state.status.send_frames ? '视觉模型理解采样与素材联系；结果仍需你核对。' : '当前模型仅接收文字信息，未启用图片分析。') : '基础模式按规则剪辑，不分析画面内容。';
 analysisControls();
}
const listHtml=items=>(Array.isArray(items)?items:[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
const secondsLabel=value=>Number.isFinite(Number(value))?Number(value).toFixed(2)+' 秒':'未知';
function renderAnalysis() {
 const p=state.project, a=p?.understanding, plan=p?.editorial_plan;
 const names=new Map((p?.media||[]).map(m=>[m.id,m.name]));
 const name=id=>names.get(id)||'未知素材';
 const stale=plan && p.plan_revision!==p.revision;
 $('planSummary').hidden=!plan;
 if(plan) $('planSummary').innerHTML=`<span>${icon('wand-sparkles')} <strong>剪辑方案</strong><small>${stale?'时间线已手调 · 可查看原方案':'查看叙事与衔接依据'}</small></span>${icon('chevron-right')}`;
 const planHtml=plan?`<section class="analysis-section editorial-section"><p class="eyebrow">EDITORIAL PLAN</p><div class="analysis-section-heading"><h3>这次，怎样讲这个故事</h3><span class="analysis-tag ${stale?'is-stale':''}">${stale?'时间线已手调':'已应用到时间线'}</span></div><p class="analysis-theme">${escapeHtml(plan.theme||'')}</p>${stale?'<p class="analysis-warning">这是上一次 Agent 的方案。时间线已手动修改或撤销，请按当前时间线核对，或再次发送剪辑要求。</p>':''}<ol class="story-beats">${listHtml(plan.structure)}</ol><div class="plan-facts">${[['节奏',plan.pace],['画面',plan.style],['转场',plan.transition_strategy],['声音',plan.audio_strategy]].map(([label,value])=>`<div><dt>${label}</dt><dd>${escapeHtml(value||'未说明')}</dd></div>`).join('')}</div>${plan.uncertainties?.length?`<details class="analysis-details"><summary>方案仍有哪些未知</summary><ul>${listHtml(plan.uncertainties)}</ul></details>`:''}</section>`:'';
 if(!a) {
  $('analysisNotice').textContent=visualModelAvailable()?'理解后会保留带时间的画面证据，首次分析可能需要数分钟。':'请先在模型设置启用本机 Codex，或允许发送图片的视觉模型 API。';
  $('analysisContent').innerHTML=planHtml+'<div class="analysis-empty"><span>✳</span><h3>先看看每一段里发生了什么。</h3><p>逐份描述素材，再找出它们的联系，最后规划开场、发展、重点与收尾。</p><small>采样不等于完整观看；本版不分析对白或音乐。</small></div>';
  analysisControls();return;
 }
 const coverage=Array.isArray(a.coverage)?a.coverage:[];
 const sampleCount=coverage.reduce((n,c)=>n+(Number(c.model_sample_count)||0),0);
 $('analysisNotice').textContent=`${a.media?.length||0} 份素材 · ${sampleCount} 张模型采样。判断仅依据这些画面，未分析音轨。相似画面不证明身份、拍摄顺序或因果。`;
 const confidence={high:'较高',medium:'中等',low:'较低'};
 const sources=(a.media||[]).map(m=>{
  const c=coverage.find(c=>c.media_id===m.media_id);
  const evidence=(a.evidence||[]).filter(e=>e.media_id===m.media_id);
  const events=(m.events||[]).map(v=>`<li><div><button class="evidence-jump" data-source="${escapeHtml(m.media_id)}" data-time="${Number(v.start)}" type="button" aria-label="查看原素材 ${secondsLabel(v.start)}">${icon('play')}${secondsLabel(v.start)} — ${secondsLabel(v.end)}</button><span class="confidence-label">可信度：${escapeHtml(confidence[v.confidence]||'未知')}</span></div><p>${escapeHtml(v.description||'')}</p><small>采样依据 ${(v.panels||[]).map(n=>'P'+escapeHtml(n)).join('、')}</small></li>`).join('');
  const frames=evidence.map(e=>`<figure><button class="evidence-frame" data-source="${escapeHtml(m.media_id)}" data-time="${Number(e.time)}" type="button" aria-label="查看原素材 P${Number(e.panel)}，${secondsLabel(e.time)}"><img src="${fileUrl(e.frame)}" alt="${escapeHtml(name(m.media_id))}，P${escapeHtml(e.panel)}，${secondsLabel(e.time)}" loading="lazy"><figcaption>P${escapeHtml(e.panel)} · ${secondsLabel(e.time)} · 查看原片 ↗</figcaption></button></figure>`).join('');
  return `<article class="source-analysis"><div class="source-title"><h4>${escapeHtml(name(m.media_id))}</h4><span class="analysis-tag">${c?Number(c.model_sample_count)||0:evidence.length} 张采样</span></div><p class="source-summary">${escapeHtml(m.summary||'未说明')}</p><div class="source-meta"><p><b>主体</b> ${escapeHtml((m.subjects||[]).join('、'))}</p><p><b>场景</b> ${escapeHtml(m.setting||'未知')}</p><p><b>画面</b> ${escapeHtml(m.camera_and_light||'未知')}</p></div>${c?`<p class="sampling-note">最大采样间隔 ${secondsLabel(c.model_max_gap)} · 未观察的间隔仍可能包含变化</p>`:''}<ol class="observed-events">${events||'<li>没有足够证据描述事件。</li>'}</ol>${frames?`<details class="analysis-details frame-evidence"><summary>查看 ${evidence.length} 张带时间的画面证据</summary><div class="analysis-frames">${frames}</div></details>`:''}${m.editorial_roles?.length?`<details class="analysis-details"><summary>适合放在故事哪里</summary><ul>${listHtml(m.editorial_roles)}</ul></details>`:''}${m.uncertainties?.length?`<details class="analysis-details"><summary>这份素材的未知与局限</summary><ul>${listHtml(m.uncertainties)}</ul></details>`:''}</article>`;
 }).join('');
 const kinds={same_topic:'同主题',possible_continuation:'可能延续',detail_of:'细节补充',contrast:'对比',visual_match:'视觉匹配',uncertain:'不确定'};
 const relations=(a.relations||[]).map(r=>`<article class="relation-card"><div><span class="analysis-tag">${escapeHtml(kinds[r.kind]||'不确定')}</span><small>可信度：${escapeHtml(confidence[r.confidence]||'未知')}</small></div><h4>${escapeHtml(name(r.from_id))} <span>→</span> ${escapeHtml(name(r.to_id))}</h4><p>${escapeHtml(r.basis||'未说明依据')}</p></article>`).join('');
 const outline=(a.story_outline||[]).map(b=>`<li><strong>${escapeHtml(b.beat||'')}</strong><p>${escapeHtml(b.purpose||'')}</p><small>${escapeHtml((b.media_ids||[]).map(name).join(' · '))}</small></li>`).join('');
 $('analysisContent').innerHTML=planHtml+`<section class="analysis-section"><p class="eyebrow">SOURCE EVIDENCE</p><h3>每一份素材，先看清楚</h3>${sources}</section><section class="analysis-section"><p class="eyebrow">RELATIONSHIPS</p><h3>它们之间，有什么联系</h3><div class="relation-grid">${relations||'<p class="muted">目前没有足够依据建立素材之间的联系。</p>'}</div></section>${outline?`<section class="analysis-section"><p class="eyebrow">STORY CANDIDATES</p><h3>从素材出发的故事候选</h3><ol class="story-beats">${outline}</ol><p class="small muted">这是素材分析的候选结构，实际剪辑以你的要求和已应用方案为准。</p></section>`:''}<section class="analysis-section analysis-limits"><h3>判断的边界</h3><ul>${listHtml(a.limitations)}</ul><p>时域 CV 提供变化候选和亮度、清晰度等测量，不等于审美评分或完整动作理解。模型描述仍需你核对。</p></section>`;
 analysisControls();
}
function openAnalysis() {
 if(state.busy||!state.project)return;
 renderAnalysis();$('analysisDialog').showModal();
 const view=state.analysisReturn;
 if(view?.projectId===state.project.id){const details=$('analysisContent').querySelectorAll('details');for(const i of view.expanded)if(details[i])details[i].open=true;requestAnimationFrame(()=>$('analysisDialog').scrollTop=view.scroll);}
}
async function analyzeMaterials() {
 if(state.busy||!state.project)return;
 await submitAgentJob('analyze');
}
$('analyzeBtn').onclick=$('planSummary').onclick=openAnalysis;
$('analysisRefresh').onclick=analyzeMaterials;
$('analysisSettings').onclick=()=>{$('analysisDialog').close();$('settingsBtn').click();};
function previewControls() {
 $('returnToAnalysisBtn').hidden=!state.preview?.fromEvidence;
 $('viewFilmBtn').hidden=state.preview?.mode!=='source'||!state.export;
}
function showSource(media,seconds=0,fromEvidence=false) {
 const time=media.kind==='video'?Math.max(0,Math.min(Number(seconds)||0,Math.max(0,media.duration-.01))):0;
 state.preview={mode:'source',mediaId:media.id,time,fromEvidence};
 document.querySelectorAll('[data-media]').forEach(el=>el.classList.toggle('is-selected',el.dataset.media===media.id));
 const video=$('previewVideo');video.pause();video.onloadedmetadata=null;
 video.hidden=true;$('previewImage').hidden=true;$('stageEmpty').hidden=true;
 if(media.kind==='image') {$('previewImage').src=fileUrl(media.file);$('previewImage').hidden=false;}
 else {
  const source=fileUrl(media.file);
  const seek=()=>{if(state.preview?.mode==='source'&&state.preview.mediaId===media.id)video.currentTime=time;};
  if(video.getAttribute('src')!==source){video.onloadedmetadata=seek;video.src=source;video.load();}
  else if(video.readyState>=1)seek();else video.onloadedmetadata=seek;
  video.hidden=false;
 }
 $('previewLabel').textContent='原素材 · '+media.name+(fromEvidence&&media.kind==='video'?' · '+secondsLabel(time):'');
 previewControls();
}
function showExport(item) {
 state.export=item;state.preview={mode:'export'};
 $('stageEmpty').hidden=true;$('previewImage').hidden=true;
 const video=$('previewVideo'),source=fileUrl(item.file);video.onloadedmetadata=null;
 if(video.getAttribute('src')!==source){video.pause();video.src=source;video.load();}
 video.hidden=false;
 document.querySelectorAll('[data-media]').forEach(el=>el.classList.remove('is-selected'));
 $('previewLabel').textContent=`${item.quality==='preview'?'剪辑预览':'导出成片'} · ${item.info.duration.toFixed(1)} 秒${item.revision!==state.project.revision?' · 修改前版本，请重新生成':''}`;
 previewControls();
}
$('analysisContent').onclick=e=>{
 const target=e.target.closest('[data-source][data-time]');if(!target)return;
 const media=state.project?.media.find(m=>m.id===target.dataset.source);if(!media)return;
 state.analysisReturn={projectId:state.project.id,scroll:$('analysisDialog').scrollTop,expanded:[...$('analysisContent').querySelectorAll('details')].map((d,i)=>d.open?i:-1).filter(i=>i>=0)};
 $('analysisDialog').close();showSource(media,Number(target.dataset.time),true);
 if(innerWidth<=700){document.body.dataset.tab='media';document.querySelectorAll('button[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab==='media'));}
 $('previewStage').scrollIntoView({block:'nearest',behavior:'smooth'});
};
$('returnToAnalysisBtn').onclick=openAnalysis;
$('viewFilmBtn').onclick=()=>{if(state.export)showExport(state.export);};
function renderProject(p) {
 state.project = p;
 document.body.dataset.hasMedia = p.media.some(m=>m.kind!=='audio') ? 'true' : 'false';
 $('promptIdeas').open=p.timeline.length===0;
 $('projectTitle').textContent = p.name;
 $('projectSubtitle').textContent = `${p.media.filter(m => m.kind !== 'audio').length} 份生活素材 · ${total().toFixed(1)} 秒故事`;
 $('aspectSelect').value = p.aspect;
 const visual = p.media.filter(m => m.kind !== 'audio');
 $('mediaCount').textContent = String(visual.length).padStart(2,'0');
 $('importEmpty').hidden = visual.length > 0;
 $('mediaList').innerHTML = visual.map(m => `<button class="media-tile" data-media="${m.id}" aria-label="预览 ${escapeHtml(m.name)}"><img src="${fileUrl(m.thumbnail)}" alt="${escapeHtml(m.name)}"><span class="file-name">${escapeHtml(m.name)}</span><span class="duration">${m.kind === 'image' ? '照片' : m.duration.toFixed(1)+'s'}</span></button>`).join('');
 document.querySelectorAll('[data-media]').forEach(el => el.onclick = () => showSource(p.media.find(m => m.id === el.dataset.media)));
 $('messages').innerHTML = p.messages.map((m,i) => `<div class="message ${m.role === 'user' ? 'user' : ''} ${i===0&&m.role==='assistant'?'system-note':''}">${m.role === 'assistant' ? `<small>${m.mode === 'model' ? '随剪 · 剪辑搭子' : (i===0?'从这里开始':'随剪 · 操作记录')}</small>` : ''}${escapeHtml(m.text)}</div>`).join('');
 $('messages').scrollTop = $('messages').scrollHeight;
 const overlap=p.timeline.reduce((n,c,i,clips)=>n+incoming(clips,i),0);
 $('timelineStats').textContent = `${p.timeline.length} 个镜头 · ${total().toFixed(1)} 秒${overlap>0?' · 含 '+overlap.toFixed(1)+' 秒转场重叠':''}`;
 const timelineCards = p.timeline.length ? p.timeline.map((c,i) => {
  const m = p.media.find(m => m.id === c.media_id);
  const transition=incoming(p.timeline,i);
  const effect=transition>0?`<span class="clip-transition">${escapeHtml(transitionNames[c.transition.type]||'转场')} ${transition.toFixed(1)}s</span>`:'';
  return `<button class="clip-card" style="--duration:${clipSpan(p.timeline,i)}" title="${escapeHtml(c.reason)}" data-clip="${i}" aria-label="调整第${i+1}段"><div class="clip-image" style="background-image:url('${fileUrl(m.thumbnail)}')"><span>${String(i+1).padStart(2,'0')}</span>${effect}</div><p>${escapeHtml(c.caption || m.name)}</p><small>${c.duration.toFixed(1)} 秒 · ${escapeHtml(lookNames[c.look]||'自然')} · ${c.volume === 0 ? '静音' : '原声'}</small></button>`;
 }).join('') : `<div class="timeline-empty">${icon('film')}<p>你的镜头，会在这里排成故事。</p><span>让搭子帮你生成第一版剪辑。</span></div>`;
 const seconds=total();
 const timeLabel=n=>{const t=Math.round(n*10);return `${String(Math.floor(t/600)).padStart(2,'0')}:${String(Math.floor(t/10)%60).padStart(2,'0')}${t%10?'.'+t%10:''}`;};
 const ticks=seconds>0?Array.from({length:5},(_,i)=>`<span>${timeLabel(seconds*i/4)}</span>`).join(''):'<span>00:00</span><span>时间线等待第一段素材</span>';
 const available=Math.max(200,$('timelineList').clientWidth-36);
 const width=seconds>0?Math.max(available,seconds*26,seconds*36/Math.min(...p.timeline.map((c,i)=>clipSpan(p.timeline,i)))):available;
 $('timelineRuler')?.remove();
 $('timelineList').innerHTML=`<div class="timeline-track" style="width:${Math.round(width)}px"><div id="timelineRuler" class="timeline-ruler" aria-hidden="true">${ticks}</div><div class="timeline-clips">${timelineCards}</div></div>`;
 document.querySelectorAll('[data-clip]').forEach(el => el.onclick = () => openClip(Number(el.dataset.clip)));
 const music = p.media.find(m => m.id === p.music_id);
 $('musicLabel').textContent = music ? '♫ ' + music.name + ' · ' + Math.round(p.music_volume * 100) + '%' : '♫ 原声轨道';
 renderAnalysis();
 renderProjects();
 setBusy(state.busy);
 if (state.export && state.preview?.mode==='export') showExport(state.export);
}
async function openProject(id) {
 if (state.busy) return;
 saveDraft();
 state.watchId++;
 try {
  $('previewVideo').pause(); $('previewVideo').removeAttribute('src'); $('previewVideo').load();
  $('previewVideo').hidden = true; $('previewImage').hidden = true; $('stageEmpty').hidden = false;
  $('previewLabel').textContent = '选择素材或生成预览';
  state.export = null;state.preview=null;state.analysisReturn=null;state.agentJob=null;renderAgentTask();previewControls();
  const p = await api('/projects/' + id); renderProject(p);
  $('chatInput').value=localStorage.getItem('suijian-draft-'+id)||'';
  localStorage.setItem('suijian-project',id);
  if (p.exports.length) showExport(p.exports[p.exports.length-1]);
  if(p.active_agent_job){if(p.active_agent_job.request_text&&!$('chatInput').value)$('chatInput').value=p.active_agent_job.request_text;watchAgentJob(p.active_agent_job);}
 } catch(e) { toast(e.message); }
}
async function init() {
 state.watchId++;state.busy=false;state.agentJob=null;renderAgentTask();
 try {
  state.status = await api('/status'); updateMode();
  $('pairScreen').hidden = true; $('app').hidden = false;
  await refreshProjects();
  const previous = localStorage.getItem('suijian-project');
  const first = state.projects.find(p => p.id === previous) || state.projects[0];
  if (first) await openProject(first.id); else {
   $('messages').innerHTML='<div class="message system-note"><small>创作从一点想法开始</small>先把照片和视频放进素材匣。<br>然后告诉我，你想留下什么感受。</div>';
   setBusy(false);
  }
 } catch(e) { if (!$('pairScreen').hidden) return; toast('连接失败：' + e.message); }
}
$('pairForm').onsubmit = async e => { e.preventDefault(); try { await api('/session',{method:'POST',body:JSON.stringify({code:$('pairCode').value.trim()})}); $('pairCode').value=''; await init(); } catch(e) { $('pairError').textContent=e.message; } };
function showNewProject() { if(state.busy)return; $('storiesDialog').close(); $('newName').value=''; $('newDialog').showModal(); $('newName').focus(); }
$('newProjectBtn').onclick = $('newStoryBtn').onclick = showNewProject;
$('storiesBtn').onclick = () => {
 if(state.busy) return toast('请等待当前处理完成');
 $('mobileProjectList').innerHTML=state.projects.length?state.projects.map(p=>`<button class="project-item" data-mobile-project="${p.id}">${icon('film')}<span class="project-copy"><strong>${escapeHtml(p.name)}</strong><small>${p.count} 份素材 · ${p.duration.toFixed(1)} 秒</small></span></button>`).join(''):'<p class="muted small">还没有故事，从第一份素材开始吧。</p>';
 document.querySelectorAll('[data-mobile-project]').forEach(b=>b.onclick=()=>{$('storiesDialog').close();openProject(b.dataset.mobileProject);});
 $('storiesDialog').showModal();
};
$('newForm').onsubmit = async e => { e.preventDefault(); try { const p = await api('/projects',{method:'POST',body:JSON.stringify({name:$('newName').value})}); $('newDialog').close(); await refreshProjects(); await openProject(p.id); } catch(e) { toast(e.message); } };
async function ensureProject() {
 if (state.project) return;
 const p = await api('/projects',{method:'POST',body:JSON.stringify({name:'我的生活片段'})});
 renderProject(p);localStorage.setItem('suijian-project',p.id); await refreshProjects();
}
async function uploadFiles(files, kind='visual') {
 if (!files.length || state.busy) return;
 setBusy(true,'正在导入素材…');
 try {
  await ensureProject();
  for (let i=0;i<files.length;i++) {
   $('composerHint').textContent=`正在导入 ${i+1}/${files.length}`;
   const form = new FormData(); form.append('file',files[i]); form.append('kind',kind);
   const p = await api('/projects/'+state.project.id+'/media',{method:'POST',body:form}); renderProject(p);
  }
  await refreshProjects(); toast('素材已导入，告诉助手你想怎么剪');
  if (kind === 'audio') populateMusic();
 } catch(e) { toast(e.message); } finally { setBusy(false); $('mediaInput').value=''; $('musicInput').value=''; }
}
// An empty workspace must still allow importing without requiring a separate project setup.
$('addMediaBtn').onclick = $('importEmpty').onclick = () => $('mediaInput').click();
$('mediaInput').onchange = e => uploadFiles(Array.from(e.target.files));
$('musicInput').onchange = e => uploadFiles(Array.from(e.target.files),'audio');
async function sendChat(text) {
 if(state.busy||!text.trim())return;
 if(!state.project?.media.some(m=>m.kind!=='audio'))return toast('先拖入照片或视频，搭子才能开始剪辑');
 $('chatInput').value=text;saveDraft();await submitAgentJob('edit',text);
}
function saveDraft(){if(state.project)localStorage.setItem('suijian-draft-'+state.project.id,$('chatInput').value);}
$('chatInput').oninput=saveDraft;
$('chatForm').onsubmit = e => { e.preventDefault(); sendChat($('chatInput').value); };
$('chatInput').onkeydown = e => { if(e.key==='Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat($('chatInput').value); } };
document.querySelectorAll('[data-prompt]').forEach(b => b.onclick = () => sendChat(b.dataset.prompt));
async function saveTimeline(changes={}) {
 const p=state.project;
 return api('/projects/'+p.id+'/timeline',{method:'PUT',body:JSON.stringify({timeline:p.timeline,aspect:p.aspect,revision:p.revision,music_id:p.music_id,music_volume:p.music_volume,...changes})});
}
$('aspectSelect').onchange = async e => { try { renderProject(await saveTimeline({aspect:e.target.value})); } catch(e) { toast(e.message); $('aspectSelect').value=state.project.aspect; } };
$('undoBtn').onclick = async () => { try { renderProject(await api('/projects/'+state.project.id+'/undo',{method:'POST',body:JSON.stringify({revision:state.project.revision})})); } catch(e) { toast(e.message); } };
function openClip(index) {
 if(state.busy) return;
 state.clipIndex=index; const c=state.project.timeline[index]; const m=state.project.media.find(m=>m.id===c.media_id);
 $('clipTitle').textContent='调整第 '+(index+1)+' 段'; $('clipSource').textContent=m.name+' · '+(m.kind==='image'?'照片':m.duration.toFixed(1)+' 秒原素材');
 $('clipStart').value=c.start; $('clipStart').disabled=m.kind==='image'; $('clipDuration').value=c.duration; $('clipCaption').value=c.caption; $('clipVolume').value=c.volume;
 $('clipTransition').value=index===0?'cut':(c.transition?.type||'cut');
 $('clipTransitionDuration').value=index===0?0:(c.transition?.duration||0);
 $('clipLook').value=c.look||'natural';$('clipMotion').value=c.motion||'none';
 $('clipMotion').disabled=m.kind!=='image';
 $('clipMotionHint').textContent=m.kind==='image'?'照片轻推拉会中心裁切最多约 6%，请核对主体是否完整。':'轻推拉仅支持照片，视频保持原有运动，不自动裁切主体。';
 $('clipSaveError').textContent='';updateClipTransition();
 $('moveLeftBtn').disabled=index===0; $('moveRightBtn').disabled=index===state.project.timeline.length-1; $('deleteClipBtn').disabled=state.project.timeline.length===1;
 if(!$('clipDialog').open)$('clipDialog').showModal();
}
function updateClipTransition(chooseDefault=false) {
 const first=state.clipIndex===0,select=$('clipTransition'),input=$('clipTransitionDuration');
 select.disabled=first;
 if(first)select.value='cut';
 input.disabled=select.value==='cut';
 if(input.disabled) {
  input.value=0;input.min=0;input.max=0;
  $('clipTransitionHint').textContent=first?'第一段没有进入转场，从画面直接开始。':'直接切换不重叠时间；转场属于进入本镜头的边界。';return;
 }
 const previous=state.project.timeline[state.clipIndex-1];
 const max=Math.min(1.2,Number(previous.duration)/2,Number($('clipDuration').value)/2);
 input.min=.1;input.max=Number.isFinite(max)?max:1.2;
 if(chooseDefault && !(Number(input.value)>0))input.value=Math.min(.4,max);
 $('clipTransitionHint').textContent=`允许 0.1—${Number.isFinite(max)?max.toFixed(3).replace(/0+$/,'').replace(/\.$/,''):'1.2'} 秒，不超过前后镜头各自的一半；重叠会缩短成片。`;
}
$('clipTransition').onchange=()=>updateClipTransition(true);
$('clipDuration').oninput=()=>updateClipTransition();
function validateClipTransitions(timeline) {
 for(let i=1;i<timeline.length;i++) {
  const c=timeline[i],t=c.transition;if(!t||t.type==='cut')continue;
  const d=Number(t.duration),max=Math.min(1.2,Number(c.duration)/2,Number(timeline[i-1].duration)/2);
  if(!Number.isFinite(d)||d<.1||d>max+1e-9)throw new Error(`第 ${i+1} 段进入转场须在 0.1—${max.toFixed(3).replace(/0+$/,'').replace(/\.$/,'')} 秒内，请同时核对相邻镜头时长。`);
 }
}
$('clipForm').onsubmit = async e => {
 e.preventDefault();const timeline=structuredClone(state.project.timeline),index=state.clipIndex;
 const type=index===0?'cut':$('clipTransition').value;
 Object.assign(timeline[index],{start:Number($('clipStart').value),duration:Number($('clipDuration').value),caption:$('clipCaption').value,volume:Number($('clipVolume').value),reason:'用户手动调整',transition:{type,duration:type==='cut'?0:Number($('clipTransitionDuration').value)},look:$('clipLook').value,motion:$('clipMotion').disabled?'none':$('clipMotion').value});
 try { validateClipTransitions(timeline);renderProject(await saveTimeline({timeline}));$('clipDialog').close(); }
 catch(e) { $('clipSaveError').textContent=e.message;toast(e.message); }
};
async function moveClip(delta) { const timeline=structuredClone(state.project.timeline); const i=state.clipIndex; [timeline[i],timeline[i+delta]]=[timeline[i+delta],timeline[i]]; try { renderProject(await saveTimeline({timeline})); openClip(i+delta); } catch(e) { toast(e.message); } }
$('moveLeftBtn').onclick=()=>moveClip(-1); $('moveRightBtn').onclick=()=>moveClip(1);
$('deleteClipBtn').onclick=async()=>{const timeline=state.project.timeline.filter((_,i)=>i!==state.clipIndex);try{renderProject(await saveTimeline({timeline}));$('clipDialog').close();}catch(e){toast(e.message);}};
function populateMusic() { $('musicSelect').innerHTML='<option value="">仅使用原声</option>'+state.project.media.filter(m=>m.kind==='audio').map(m=>`<option value="${m.id}">${escapeHtml(m.name)}</option>`).join(''); $('musicSelect').value=state.project.music_id||''; $('musicVolume').value=state.project.music_volume; }
$('musicBtn').onclick=()=>{populateMusic();$('musicDialog').showModal();};
$('uploadMusicBtn').onclick=()=>$('musicInput').click();
$('musicForm').onsubmit=async e=>{e.preventDefault();try{renderProject(await saveTimeline({music_id:$('musicSelect').value||null,music_volume:Number($('musicVolume').value)}));$('musicDialog').close();}catch(e){toast(e.message);}};
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function runRender(quality) {
 if(state.busy || !state.project?.timeline.length) return;
 setBusy(true,'正在剪辑，请稍候…'); $('renderProgress').hidden=false;
 try {
  let job=await api('/projects/'+state.project.id+'/render',{method:'POST',body:JSON.stringify({revision:state.project.revision,quality})});
  while(job.status==='running') {
   $('progressMessage').textContent=job.message; $('progressPercent').textContent=job.progress+'%'; $('progressBar').value=job.progress;
   await delay(1200); job=await api('/jobs/'+job.id);
  }
  if(job.status==='failed') throw new Error(job.message);
  $('progressBar').value=100; $('progressPercent').textContent='100%'; $('progressMessage').textContent='已完成';
  state.project=await api('/projects/'+state.project.id); showExport(job.export);
  if(quality==='export') {
   const info=job.export.info; $('exportInfo').textContent=`${info.duration.toFixed(1)} 秒 · ${info.width} × ${info.height} · MP4 / H.264 / 30 fps`;
   $('downloadLink').href='/api/projects/'+state.project.id+'/export/'+job.id; $('exportDialog').showModal();
  } else toast('预览已生成，可以继续修改');
 } catch(e) { toast(e.message); } finally { setBusy(false); setTimeout(()=>$('renderProgress').hidden=true,2000); }
}
$('renderBtn').onclick=()=>runRender('preview'); $('exportBtn').onclick=()=>runRender('export');
function updateSettingsFields(){
 const provider=$('providerSelect').value;
 $('apiSettingsFields').hidden=provider!=='api';
 $('providerDescription').textContent=provider==='codex'?'使用本机已登录的 Codex，先理解带时间的采样与素材联系，再规划剪辑；无需填写 Key。需要联网，使用当前账号额度。单视频最多 24 张、项目最多 160 张及 20 份画面素材，分析可缓存；无法保证完整动作或语音理解。':(provider==='api'?'连接支持图片输入的模型服务。启用图片后先理解再规划；首次两次请求，理解可复用缓存。单视频最多 24 张采样、项目最多 160 张及 20 份画面素材，不发送音轨。':'按导入顺序生成初剪，支持常见修改指令；此模式不理解画面内容，也不调用模型。');
}
$('providerSelect').onchange=updateSettingsFields;
$('settingsBtn').onclick=()=>{ $('providerSelect').value=state.status.provider||'api'; $('modelBase').value=state.status.base_url||''; $('modelName').value=state.status.model||''; $('modelKey').value=''; $('sendFrames').checked=!!state.status.send_frames; $('settingsMessage').textContent=''; updateSettingsFields();$('settingsDialog').showModal(); };
$('settingsForm').onsubmit=async e=>{ e.preventDefault(); try { await api('/settings',{method:'PUT',body:JSON.stringify({provider:$('providerSelect').value,base_url:$('modelBase').value.trim(),model:$('modelName').value.trim(),api_key:$('modelKey').value.trim(),send_frames:$('sendFrames').checked})}); state.status=await api('/status'); updateMode(); $('modelKey').value=''; $('settingsDialog').close(); toast('设置已保存'); } catch(e) {$('settingsMessage').textContent=e.message;} };
$('clearModelBtn').onclick=async()=>{try{await api('/settings',{method:'PUT',body:JSON.stringify({clear_key:true})});state.status=await api('/status');updateMode();$('settingsDialog').close();toast('已切换到基础模式');}catch(e){toast(e.message);}};
document.querySelectorAll('.close-dialog').forEach(b=>b.onclick=()=>b.closest('dialog').close());
document.querySelectorAll('button[data-tab]').forEach(b=>b.onclick=()=>{document.body.dataset.tab=b.dataset.tab;document.querySelectorAll('button[data-tab]').forEach(x=>x.classList.toggle('active',x===b));});
window.addEventListener('offline',()=>toast('网络已断开，云端模型请求可能无法完成。'));
let dragDepth=0;
new ResizeObserver(()=>{
 const track=$('timelineList').querySelector('.timeline-track');
 if(!track||!state.project?.timeline.length||!$('timelineList').clientWidth)return;
 const seconds=total();
 const style=getComputedStyle($('timelineList'));
 const available=$('timelineList').clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight);
 const width=Math.max(available,seconds*26,seconds*36/Math.min(...state.project.timeline.map((c,i,clips)=>clipSpan(clips,i))));
 if(Math.abs(track.getBoundingClientRect().width-width)>1)track.style.width=Math.round(width)+'px';
}).observe($('timelineList'));
window.addEventListener('dragenter',e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();dragDepth++;$('dropOverlay').hidden=false;}});
window.addEventListener('dragover',e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();e.dataTransfer.dropEffect='copy';}});
window.addEventListener('dragleave',e=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)$('dropOverlay').hidden=true;});
window.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;$('dropOverlay').hidden=true;const files=[...e.dataTransfer.files];if(files.length)uploadFiles(files);});
init();
