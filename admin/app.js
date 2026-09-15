'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const paths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  sliders:'M4 21v-7 M4 10V3 M12 21V12 M12 8V3 M20 21v-5 M20 12V3 M1 10h6 M9 12h6 M17 16h6',
  settings:'M9 3l-1 3-3 1-2 4 2 3 1 4 4 3 3-1 4-1 3-4-1-4-2-4-4-1z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  layers:'M12 3L2 8l10 5 10-5-10-5z M2 12l10 5 10-5 M2 16l10 5 10-5',
  terminal:'M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z M7 9l3 3-3 3 M13 15h4',
  activity:'M2 12h5l3-8 4 16 3-8h5',
  logout:'M9 4H4v16h5 M9 12h12 M17 8l4 4-4 4',
  refresh:'M20 8a8 8 0 0 0-14-3L3 8 M3 3v5h5 M4 16a8 8 0 0 0 14 3l3-3 M21 21v-5h-5',
  arrow:'M4 12h16 M14 6l6 6-6 6',
  chevron:'M9 5l7 7-7 7',
  scan:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M16 21h5v-5 M7 12s2-4 5-4 5 4 5 4-2 4-5 4-5-4-5-4z M12 11v2',
  globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c5 5 5 13 0 18-5-5-5-13 0-18',
  smile:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M8 9h.1 M16 9h.1 M8 14s1 3 4 3 4-3 4-3',
  image:'M4 3h16v18H4z M4 16l5-5 4 4 3-3 4 4 M14 7h2v2h-2z',
  film:'M3 3h18v18H3z M7 3v18 M17 3v18 M3 8h4 M3 16h4 M17 8h4 M17 16h4',
  mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M8 22h8',
  music:'M9 18V5l12-2v13 M9 9l12-2 M9 18a3 3 0 1 1-3-3c2 0 3 1 3 3 M21 16a3 3 0 1 1-3-3c2 0 3 1 3 3',
  sparkles:'M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3z M20 2v4 M18 4h4',
  shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3z M8 12l3 3 5-6',
  info:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 11v6 M12 7v1',
  pause:'M8 5v14 M16 5v14',
  play:'M7 4l14 8-14 8V4z',
  stop:'M5 5h14v14H5z',
  copy:'M8 8h13v13H8z M16 8V3H3v13h5',
  download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
  search:'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M15 15l6 6',
  check:'M4 12l5 5L20 6',
  menu:'M3 6h18 M3 12h18 M3 18h18',
  clock:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 7v5l4 2'
};
const icon = name => `<i data-icon="${name}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.settings}"/></svg></i>`;
function hydrateIcons(root = document) { $$('i[data-icon]', root).forEach(el => { if (!el.firstChild) el.innerHTML = icon(el.dataset.icon).match(/<svg.*<\/svg>/)[0]; }); }
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {csrf:'', config:null, draft:{}, status:null, page:'overview', group:'chat', paused:false, logs:[], logRequest:0, loading:false, saving:false};
const titles = {
  overview:['运行概览', 'WORKSPACE OVERVIEW', '一眼了解灯里的状态，轻松安排每一项能力。'],
  features:['功能管理', 'CAPABILITIES', '按需开启灯里的能力，让互动保持刚刚好的节奏。'],
  settings:['参数配置', 'PREFERENCES', '从回复风格到创作细节，找到适合你的设置。'],
  tasks:['任务记录', 'TASK HISTORY', '追踪执行进度，回看最近的创作与工作任务。'],
  logs:['运行日志', 'LOG EXPLORER', '从每一条运行记录中，找到问题发生的线索。'],
  system:['系统诊断', 'SYSTEM HEALTH', '检查 bot、依赖服务和 GPU 资源的运行情况。']
};
const statusLabels = {succeeded:'已完成',running:'执行中',queued:'排队中',failed:'失败',timed_out:'已超时',cancelled:'已取消',interrupted:'已中断'};
const fmtTime = value => value ? new Date(value * 1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—';
function duration(seconds) { if (seconds < 60) return `${Math.max(0, Math.floor(seconds))} 秒`; if (seconds < 3600) return `${Math.floor(seconds/60)} 分钟`; return `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`; }
function notify(message, error = false) { const el = $('#toast'); el.textContent = message; el.classList.toggle('error',error); el.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => el.hidden = true, error ? 6500 : 3500); }
async function api(path, body) {
  const response = await fetch('/api/' + path, {method:body === undefined ? 'GET' : 'POST', headers:body === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':state.csrf}, body:body === undefined ? undefined : JSON.stringify(body), credentials:'same-origin', signal:AbortSignal.timeout(path === 'service' ? 45000 : 15000)});
  const data = await response.json();
  if (!response.ok) { if (response.status === 401 && path !== 'login') showLogin(); const e = new Error(data.error || '请求失败'); e.status = response.status; throw e; }
  return data;
}
function showLogin() { state.csrf=''; $('#app').hidden=true; $('#login').hidden=false; $('#access-token').value=''; }
function showError(error, source='general') { $('#global-error').dataset.source=source; $('#global-error').hidden=false; $('#global-error').textContent=error.message || String(error); }
function dirty() { if (!state.config) return []; return state.config.fields.filter(f => state.config.config[f.scope].available && JSON.stringify(state.draft[f.scope][f.key]) !== JSON.stringify(state.config.config[f.scope].values[f.key])); }
function fieldID(f) { return `field-${f.scope}-${f.key}`; }
function switchHTML(f) { return `<label class="switch"><input type="checkbox" aria-label="${esc(f.label)}" data-key="${f.key}" data-scope="${f.scope}" ${state.draft[f.scope][f.key] ? 'checked' : ''}><span class="switch-track"></span></label>`; }
function renderFeatures() {
  const features = state.config.fields.filter(f=>f.feature);
  const card = (f,idx) => `<article class="feature-card" data-feature="${f.key}"><div class="feature-top"><span class="feature-icon ${['violet','blue','mint','peach'][idx%4]}">${icon(f.feature)}</span>${switchHTML(f)}</div><h3>${esc(f.label)}</h3><p>${esc(f.help)}</p><div class="feature-footer"><span class="feature-status"><span class="tiny-dot"></span><span data-state-label></span></span><a href="#settings/${f.group}">参数设置 ↗</a></div></article>`;
  $('#all-features').innerHTML=features.map(card).join('');
  $('#quick-features').innerHTML=['agent_enabled','image_enabled','voice_enabled','music_enabled'].map((k,i)=>card(features.find(f=>f.key===k),i)).join('');
  syncDraft();
}
function syncDraft() {
  const edits=dirty();
  $$('#app [data-key]').forEach(input=>{
    const value=state.draft[input.dataset.scope]?.[input.dataset.key];
    if(input.type==='checkbox') input.checked=!!value;
    const changed=edits.some(f=>f.key===input.dataset.key && f.scope===input.dataset.scope);
    input.closest('.field, .feature-card')?.classList.toggle('changed',changed);
  });
  $$('[data-feature]').forEach(card=>{
    const on=state.draft.bot[card.dataset.feature];
    $('.feature-status',card).classList.toggle('on',on);
    $('[data-state-label]',card).textContent=on ? '已开启' : '已关闭';
  });
  const count=state.config.fields.filter(f=>f.feature && state.draft.bot[f.key]).length;
  $('#nav-feature-count').textContent=count;
  $('#features-summary').textContent=`${count} / 8 项已开启（当前编辑）`;
  $('#save-bar').hidden=edits.length===0;
  $('#change-count').textContent=`${edits.length} 项未保存的修改`;
  document.body.classList.toggle('has-changes',edits.length>0);
}
function renderSettings() {
  const group=state.config.groups.find(g=>g.id===state.group) || state.config.groups[0];
  state.group=group.id;
  $('#settings-nav').innerHTML=state.config.groups.map(g=>`<a href="#settings/${g.id}" class="${g.id===group.id?'active':''}" ${g.id===group.id?'aria-current="page"':''}>${esc(g.name)}</a>`).join('');
  $('#settings-title').textContent=group.name;
  $('#settings-description').textContent=group.description;
  $('#settings-form').innerHTML=state.config.fields.filter(f=>f.group===group.id && !f.feature).map(f=>{
    const value=state.draft[f.scope]?.[f.key] ?? f.default;
    const attrs=`id="${fieldID(f)}" data-key="${f.key}" data-scope="${f.scope}" aria-describedby="help-${fieldID(f)}"`;
    let input;
    if(f.type==='bool') input=switchHTML(f);
    else if(f.type==='select') input=`<select ${attrs}>${f.options.map(([v,l])=>`<option value="${esc(v)}" ${v===value?'selected':''}>${esc(l)}</option>`).join('')}</select>`;
    else if(f.type==='list') input=`<textarea ${attrs} rows="5" spellcheck="false" placeholder="每行一个 QQ OpenID">${esc(value.join('\n'))}</textarea>`;
    else if(f.type==='text') input=`<textarea ${attrs} minlength="${f.min}" maxlength="${f.max}" required>${esc(value)}</textarea>`;
    else if(f.type==='str') input=`<input ${attrs} type="text" value="${esc(value)}" minlength="${f.min}" maxlength="${f.max}" required>`;
    else input=`<input ${attrs} type="number" value="${esc(value)}" min="${f.min}" max="${f.max}" step="${f.type==='int'?'1':'0.01'}" required>`;
    return `<div class="field">${f.type==='bool'?`<span class="field-label">${esc(f.label)}</span>`:`<label for="${fieldID(f)}">${esc(f.label)}</label>`}${input}<p id="help-${fieldID(f)}">${esc(f.help || (f.type==='bool'?'保存并重启 bot 后生效。':f.scope==='music'?'新任务生效。':f.type==='select'?'选择灯里参与群聊的方式。':`允许范围：${f.min} – ${f.max}${f.type==='str'?' 字符':''}`))}</p>${!state.config.config[f.scope].available?'<p class="field-error">该配置文件不存在，暂不可编辑。</p>':''}</div>`;
  }).join('');
  if(group.id==='agent') {
    const ids=state.draft.bot.agent_allowed_openids || [];
    $('#settings-form').insertAdjacentHTML('beforeend',`<section class="account-list"><h3>近期发言账号</h3><p class="muted">来自最近收到的 QQ 消息。昵称仅供识别，授权按 OpenID；点击后还需保存并重启。</p>${(state.config.accounts || []).map(a=>`<div class="account-row"><div><strong>${esc(a.nick)}</strong><span class="muted"> · ${esc(a.scene)}</span><code>${esc(a.openid)}</code></div><button type="button" class="button secondary" data-account="${esc(a.openid)}">${ids.includes(a.openid)?'移出白名单':'加入白名单'}</button></div>`).join('') || '<p class="muted">暂无记录，可在上方手动填写 OpenID。</p>'}</section>`);
  }
  $$('#settings-form [data-key]').forEach(el=>el.disabled=!state.config.config[el.dataset.scope].available);
  syncDraft();
}
function empty(iconName,title,description='') {return `<div class="empty-state">${icon(iconName)}<strong>${esc(title)}</strong><p>${esc(description)}</p></div>`;}
function taskHTML(task,details=false) {
  const music=String(task.operation || '').includes('sing') || /翻唱|唱歌|音乐/.test(task.prompt);
  return `<div class="task-row"><span class="task-icon">${icon(music?'music':'terminal')}</span><div class="task-body"><strong title="${esc(task.prompt)}">${esc(task.prompt || '未命名任务')}</strong><small>${esc(task.id)} · ${fmtTime(task.created)}</small></div><span class="status-pill ${esc(task.status)}">${esc(statusLabels[task.status] || task.status)}</span></div>${details?`<details class="task-detail" data-task-id="${esc(task.id)}"><summary>查看任务详情</summary><pre>${esc(task.prompt)}\n\n创建：${fmtTime(task.created)}\n开始：${fmtTime(task.started)}\n结束：${fmtTime(task.finished)}${task.error ? '\n\n错误：'+esc(task.error) : ''}</pre></details>`:''}`;
}
function renderTasks() {
  const openTasks=new Set($$('#task-list details[open]').map(el=>el.dataset.taskId));
  const jobs=state.status.jobs;
  const items=jobs.items || [];
  $('#recent-tasks').innerHTML=items.length?items.slice(0,4).map(t=>taskHTML(t)).join(''):empty('layers','还没有任务记录','在 QQ 中发送任务指令，进度就会出现在这里。');
  const filter=$('#task-filter').value;
  const selected=items.filter(t=>filter==='all' || t.status===filter || (filter==='failed' && ['timed_out','interrupted'].includes(t.status)));
  $('#task-list').innerHTML=selected.length?selected.map(t=>taskHTML(t,true)).join(''):empty('layers','没有匹配的任务','切换筛选条件，或稍后刷新查看。');
  $$('#task-list details').forEach(el=>el.open=openTasks.has(el.dataset.taskId));
}
function renderStatus() {
  const s=state.status, service=s.service, runtime=s.runtime, counts=s.jobs.counts;
  const running=service.active==='active', ready=s.telemetry && runtime.gateway==='ready';
  const savedFeatures=state.config.fields.filter(f=>f.feature && state.config.config.bot.values[f.key]).length;
  const completed=counts.succeeded || 0;
  const featureNote=s.pending_restart===true?'已保存，待重启应用':s.pending_restart===false?'当前配置已生效':'已保存的功能开关';
  $('#stats').innerHTML=[
    ['Bot 服务',running?'运行中':service.active==='unknown'?'未知':'已停止','',ready?'QQ 网关已连接':s.telemetry?'QQ 网关未就绪':'网关状态尚未确认','activity',running],
    ['已开启功能',savedFeatures,'/ 8',featureNote,'sliders',false],
    ['执行中任务',counts.running || 0,'项',`${counts.queued || 0} 项排队等待`,'layers',false],
    ['累计完成',completed,'项',`${(counts.failed||0)+(counts.timed_out||0)+(counts.interrupted||0)} 项失败 / 超时 / 中断`,'check',false]
  ].map(([label,value,unit,note,ico,green])=>`<div class="stat-card"><div class="stat-top"><span>${label}</span>${icon(ico)}</div><div class="stat-value ${green?'green':''}">${value}<small>${unit}</small></div><p class="stat-note">${note}</p></div>`).join('');
  $('#hero-status').textContent=ready?'QQ 已连接 · 陪伴进行中':running?'Bot 已启动 · 等待网关就绪':'Bot 尚未运行';
  $('#hero-title').textContent=ready?'灯里，随时准备回应。':'灯里的日常，尽在掌握。';
  $('#pending-banner').hidden=s.pending_restart!==true;
  const busy=(counts.running||0)+(counts.queued||0)>0;
  $$('[data-action]').forEach(b=>{
    b.disabled=state.serviceBusy || (busy && b.dataset.action!=='start');
    b.title=busy && b.dataset.action!=='start'?'等待当前任务结束后可操作':'';
  });
  $('#system-status').innerHTML=[
    ['进程状态',`${service.active} / ${service.sub}`],['进程 PID',service.pid || '—'],
    ['QQ 网关',!s.telemetry?'尚未获得实时状态':ready?'已连接':runtime.gateway==='connecting'?'连接中':'未连接'],
    ['本次运行时长',runtime.started?duration(s.time-runtime.started):'—'],
    ['最近网关心跳',fmtTime(runtime.last_ack)],['配置状态',s.pending_restart===null?'需要启动状态记录后确认':s.pending_restart?'有配置待重启生效':'已生效']
  ].map(([k,v])=>`<div class="info-row"><span>${k}</span><span>${esc(v)}</span></div>`).join('');
  $('#sidebar-name').textContent=state.config.config.bot.values.bot_name;
  $('#last-update').textContent='最近更新 '+fmtTime(s.time);
  $('#connection').innerHTML='<span class="tiny-dot"></span>控制台已连接';
  $('#connection').classList.remove('offline');
  renderTasks();
}
async function refreshStatus(silent=false) {
  if(state.loading || !state.csrf) return;
  state.loading=true;
  try {if(!state.config) await loadConfig();state.status=await api('status');renderStatus();if($('#global-error').dataset.source==='connection')$('#global-error').hidden=true;if(!silent) notify('运行状态已更新');}
  catch(e) {$('#connection').textContent='连接已中断';$('#connection').classList.add('offline');showError(e,'connection');}
  finally {state.loading=false;}
}
async function loadConfig() {
  state.config=await api('config');
  state.draft=Object.fromEntries(Object.entries(state.config.config).map(([scope,c])=>[scope,{...c.values}]));
  renderFeatures(); renderSettings();
}
async function enter() {
  $('#login').hidden=true;$('#app').hidden=false;
  try {await loadConfig();navigate();await refreshStatus(true);} catch(e){showError(e);}
}
function navigate() {
  const [page,group]=location.hash.slice(1).split('/');
  state.page=titles[page]?page:'overview';
  const [title,eyebrow,description]=titles[state.page];
  $('#page-title').textContent=title;$('#breadcrumb-current').textContent=title;$('#page-eyebrow').textContent=eyebrow;$('#page-description').textContent=description;
  $$('.page').forEach(el=>el.hidden=el.id!==`page-${state.page}`);
  $$('[data-page]').forEach(el=>{el.classList.toggle('active',el.dataset.page===state.page);if(el.dataset.page===state.page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  if(state.page==='settings' && state.config) {if(group)state.group=group;renderSettings();}
  if(state.page==='logs' && state.csrf) refreshLogs();
  $('#sidebar').classList.remove('open');$('#menu-toggle').setAttribute('aria-expanded','false');
  document.title=`${title} · 灯里控制台`;
}
async function refreshLogs() {
  if(!state.csrf || state.paused) return;
  const request=++state.logRequest;
  const params=new URLSearchParams({source:$('#log-source').value,level:$('#log-level').value,q:$('#log-search').value});
  try {
    const result=await api('logs?'+params);
    if(request!==state.logRequest || state.paused)return;
    state.logs=result.records;
    const el=$('#log-content'),oldTop=el.scrollTop;
    el.innerHTML=result.records.length?result.records.map((r,i)=>`<div class="log-entry ${r.level}"><span class="log-number">${i+1}</span><span class="log-level">${{info:'INFO',warning:'WARN',error:'ERROR'}[r.level]}</span><pre>${esc(r.text)}</pre></div>`).join(''):empty('search','没有匹配的日志','试试其他级别、关键词或日志来源。');
    el.scrollTop=$('#log-follow').checked?el.scrollHeight:oldTop;
    $('#log-meta').textContent=`显示 ${result.records.length} 条 · 文件尾部：${result.counts.error} 错误 / ${result.counts.warning} 警告${result.truncated?' · 已截取最近 512 KiB':''}`;
    $('#log-updated').textContent=fmtTime(result.time);
    $('#log-dot').hidden=!result.counts.error;
  } catch(e) {notify(e.message,true);}
}
function confirmAction(title,description,label='确认操作') {
  $('#confirm-title').textContent=title;$('#confirm-description').textContent=description;$('#confirm-submit').textContent=label;
  const dialog=$('#confirm-dialog');dialog.returnValue='cancel';dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true}));
}
async function save() {
  if(state.saving) return;
  const fields=dirty();if(!fields.length)return;
  const invalid=fields.find(f=>{
    const v=state.draft[f.scope][f.key];
    return (f.type==='list' && (!Array.isArray(v) || v.length>100 || v.some(x=>!/^[-A-Za-z0-9_]{1,128}$/.test(x)))) || (f.type==='int' && !Number.isInteger(v)) || (['int','float'].includes(f.type) && (!Number.isFinite(v) || v<f.min || v>f.max)) || (['str','text'].includes(f.type) && (v.length<f.min || v.length>f.max));
  });
  if(invalid) {location.hash=`settings/${invalid.group}`;notify(`${invalid.label}：请输入范围内的有效值`,true);return;}
  state.saving=true;$('#save').disabled=true;
  try {
    for(const scope of ['bot','music']) {
      const changes=Object.fromEntries(fields.filter(f=>f.scope===scope).map(f=>[f.key,state.draft[scope][f.key]]));
      if(!Object.keys(changes).length)continue;
      const result=await api('config',{scope,revision:state.config.config[scope].revision,changes});
      state.config.config[scope]=result.config[scope];
      // Keep any edits made while the request was running in the draft.
      syncDraft();
    }
    $('#global-error').hidden=true;
    notify(fields.some(f=>f.scope==='bot')?'配置已保存。点击“应用并重启”使 bot 配置生效。':'音乐配置已保存，将用于下一次翻唱任务。');
    await refreshStatus(true);
  } catch(e) {notify(e.message,true);showError(e);}
  finally {state.saving=false;$('#save').disabled=false;syncDraft();}
}
async function serviceAction(action) {
  if(state.serviceBusy)return;
  if(dirty().length) {notify('请先保存或撤销未保存的修改。',true);return;}
  const names={start:'启动',restart:'重启',stop:'停止'};
  if(!await confirmAction(`${names[action]} bot？`,action==='stop'?'停止后灯里将暂停接收和回复 QQ 消息，可随时从控制台重新启动。':action==='restart'?'将加载已保存的配置，并短暂重新连接 QQ。若有排队或执行任务，此操作会被阻止。':'使用已保存的配置启动 bot，并连接 QQ 网关。',`确认${names[action]}`))return;
  state.serviceBusy=true;renderStatus();
  try {const data=await api('service',{action});notify(data.message);await refreshStatus(true);setTimeout(()=>refreshStatus(true),3000);}
  catch(e) {notify(e.message,true);}
  finally {state.serviceBusy=false;if(state.status)renderStatus();}
}
async function diagnose() {
  $('#diagnose').disabled=true;$('#diagnostics').innerHTML=empty('activity','正在检查本机服务…','通常只需几秒钟。');
  try {
    const result=await api('diagnostics',{});
    $('#diagnostics').innerHTML=result.checks.map(c=>`<div class="diagnostic-row"><span class="diagnostic-dot ${c.ok===true?'ok':c.ok===false?'fail':''}"></span><div><strong>${esc(c.name)}</strong><p>${esc(c.detail)}</p></div><small>${c.ms!==undefined?c.ms+' ms':'—'}</small></div>`).join('')+`<p class="footnote">${esc(result.note)}<br>检查时间：${fmtTime(result.time)}</p>`;
    $('#gpu-list').innerHTML=result.gpus.length?result.gpus.map(g=>`<div class="gpu-card"><strong>GPU ${esc(g.index)}</strong><small>${esc(g.name)}</small><meter min="0" max="${g.total}" value="${g.used}" aria-label="GPU ${esc(g.index)} 显存使用量"></meter><p>${(g.used/1024).toFixed(1)} / ${(g.total/1024).toFixed(1)} GiB · 利用率 ${g.utilization}%</p></div>`).join(''):'<p class="muted">未获得 GPU 数据，请检查 NVIDIA 驱动或 nvidia-smi。</p>';
  } catch(e){$('#diagnostics').innerHTML=empty('info','检查未完成',e.message);notify(e.message,true);}
  finally{$('#diagnose').disabled=false;}
}
$('#login-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('button',event.target);button.disabled=true;$('#login-error').textContent='';
  try {const data=await api('login',{token:$('#access-token').value.trim()});state.csrf=data.csrf;$('#access-token').value='';await enter();}
  catch(e){$('#login-error').textContent=e.message;}finally{button.disabled=false;}
});
$('#app').addEventListener('input',event=>{
  const el=event.target;if(!el.dataset.key)return;
  const field=state.config.fields.find(f=>f.scope===el.dataset.scope && f.key===el.dataset.key);
  state.draft[field.scope][field.key]=field.type==='list'?[...new Set(el.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))]:el.type==='checkbox'?el.checked:['int','float'].includes(field.type)?(el.value===''?NaN:Number(el.value)):el.value;
  syncDraft();
});
$('#settings-form').addEventListener('click',event=>{
  const button=event.target.closest('[data-account]');if(!button)return;
  const uid=button.dataset.account,ids=state.draft.bot.agent_allowed_openids || [];
  state.draft.bot.agent_allowed_openids=ids.includes(uid)?ids.filter(x=>x!==uid):[...ids,uid];
  renderSettings();
});
$('#settings-form').addEventListener('submit',e=>e.preventDefault());
$('#app').addEventListener('click',event=>{const button=event.target.closest('[data-action]');if(button && !button.disabled)serviceAction(button.dataset.action);});
$('#save').addEventListener('click',save);
$('#discard').addEventListener('click',async()=>{if(await confirmAction('撤销未保存的修改？','将重新载入磁盘上的最新配置。','撤销修改')){try{await loadConfig();$('#global-error').hidden=true;notify('已重新载入配置');}catch(e){notify(e.message,true);}}});
$('#refresh').addEventListener('click',()=>refreshStatus());
$('#task-filter').addEventListener('change',()=>state.status && renderTasks());
$('#menu-toggle').addEventListener('click',()=>{$('#sidebar').classList.toggle('open');$('#menu-toggle').setAttribute('aria-expanded',$('#sidebar').classList.contains('open'));});
$('#logout').addEventListener('click',async()=>{if(dirty().length && !await confirmAction('退出控制台？','未保存的修改将丢失。','退出登录'))return;try{await api('logout',{});showLogin();}catch(e){notify(e.message,true);}});
$('#log-pause').addEventListener('click',()=>{state.paused=!state.paused;$('#log-pause').innerHTML=icon(state.paused?'play':'pause')+(state.paused?'继续':'暂停');$('#log-live').textContent=state.paused?'已暂停':'实时刷新';if(!state.paused)refreshLogs();});
for(const id of ['log-source','log-level'])$('#'+id).addEventListener('change',()=>{if(state.paused){notify('日志已暂停，点击“继续”后应用筛选。');return;}refreshLogs();});
$('#log-search').addEventListener('input',()=>{clearTimeout(state.searchTimer);state.searchTimer=setTimeout(refreshLogs,300);});
$('#log-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.logs.map(r=>r.text).join('\n'));notify('已复制当前显示的日志');}catch(e){notify('浏览器未允许复制，可使用下载按钮。',true);}});
$('#log-download').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([state.logs.map(r=>r.text).join('\n')],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`akari-${$('#log-source').value}-${new Date().toISOString().slice(0,10)}.log`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('#diagnose').addEventListener('click',diagnose);
window.addEventListener('hashchange',navigate);
window.addEventListener('beforeunload',event=>{if(state.csrf && dirty().length){event.preventDefault();event.returnValue='';}});
setInterval(()=>{if(state.csrf && !document.hidden)refreshStatus(true);},5000);
setInterval(()=>{if(state.csrf && state.page==='logs' && !document.hidden)refreshLogs();},4000);
hydrateIcons();
(async()=>{try{const data=await api('session');state.csrf=data.csrf;await enter();}catch(e){showLogin();}})();
