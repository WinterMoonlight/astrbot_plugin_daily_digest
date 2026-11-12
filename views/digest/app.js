const $ = (id) => document.getElementById(id);
const form = $('form');
const defaults = {name:'',groups:[],recipient:'',provider_id:'',persona_id:'',frequency:'daily',weekdays:[0],time:'08:00',timezone:'Asia/Shanghai',lookback_hours:24,lead_minutes:30,candidate_days:7,selection_hours:24,prompt:'提取重要通知、待办与截止时间、变更与取消，分类展示，空分类省略。合并重复事项，保留来源与日期；不明确的信息标为待确认，忽略闲聊。'};
let selected = null, selectedRun = null, tasks = [], metadata = {models:[],personas:[],groups:[]}, page = 1, pollToken = 0;
let bridge, initialized = false, deletionPreview = null, deleting = false;
const stateNames={native_paused:'原生任务已停用',native_absent:'原生任务已结束或删除（送达未确认）',pending:'待处理',scheduled:'等待提醒',retry_wait:'等待重试',sending:'发送中',acknowledged:'已确认发送',failed:'失败',unknown:'结果未知',held:'已停止',active:'可选择',awaiting_delivery:'等待询问成功',awaiting_choice:'等待指定时间',expired:'已过期',superseded:'已被新清单替换',cancelled:'已取消',missed:'已错过',confirmed:'已确认',rejected:'已拒绝'};
const stateName=state=>stateNames[state]||state;
function message(text, error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function api(action, values={}){
  // AstrBot's host unwraps response.data and rejects failed HTTP requests.
  return await bridge.apiPost('rpc', {action,...values});
}
async function guard(fn, button){
  if(button) button.disabled=true;
  try{await fn();}catch(e){message(e.message || String(e),true);}
  finally{if(button){button.disabled=false;if(['archive','toggle'].includes(button.id))button.disabled=!selected||!!selected.archived;if(['save','generate'].includes(button.id))button.disabled=!!selected?.archived;if(button.id==='delete-task')button.disabled=!selected?.archived;}}
}
function option(select,value,label){const node=document.createElement('option');node.value=value;node.textContent=label;select.append(node);}
function options(select,values,current){select.replaceChildren();option(select,'','请选择');for(const v of values)option(select,v.id,v.label||v.id);if(current&&!values.some(v=>v.id===current))option(select,current,`${current}（当前不可用）`);select.value=current||'';}
function readConfig(){
  const data={};
  for(const key of Object.keys(defaults)){
    if(key==='weekdays')data[key]=[...document.querySelectorAll('[name=weekday]:checked')].map(x=>Number(x.value));
    else if(key==='groups')data[key]=form.elements.groups.value.split('\n').map(x=>x.trim()).filter(Boolean);
    else data[key]=typeof defaults[key]==='number'?Number(form.elements[key].value):form.elements[key].value;
  }
  return data;
}
function fill(task){
  selected=task;selectedRun=null;page=1;pollToken++;
  const data=task?.config||structuredClone(defaults);
  $('task-title').textContent=task?.config.name||'新建任务';
  for(const key of Object.keys(defaults)){
    if(key==='weekdays')document.querySelectorAll('[name=weekday]').forEach(x=>x.checked=data.weekdays.includes(Number(x.value)));
    else if(key==='groups')form.elements.groups.value=data.groups.join('\n');
    else form.elements[key].value=data[key];
  }
  options(form.elements.provider_id,metadata.models.map(m=>({id:m.id,label:`${m.id} · ${m.model}`})),data.provider_id);
  options(form.elements.persona_id,metadata.personas,data.persona_id);
  taskState();$('delivery-panel').hidden=true;
  $('weekdays').hidden=data.frequency!=='weekly';
  $('preview-state').textContent='';$('preview-text').textContent='尚无预览。';$('preview-meta').textContent='';
  $('run-list').textContent='切换到执行记录查看。';
  tab('config');
}
function renderTasks(){
  $('tasks').replaceChildren();
  if(!tasks.length){const p=document.createElement('p');p.className='muted';p.textContent='还没有任务，点击新建开始。';$('tasks').append(p);}
  for(const task of tasks.filter(t=>!t.archived||$('show-archived').checked)){const b=document.createElement('button');b.className='task';const title=document.createElement('strong');title.textContent=task.config.name||'未命名任务';const status=document.createElement('small');status.textContent=`${task.archived?'已归档':task.enabled?'已启用':'停用'} · ${task.config.frequency==='weekly'?'每周':'每日'} ${task.config.time}`;b.append(title,status);b.onclick=()=>fill(task);$('tasks').append(b);}
}
function taskState(){
  $('delete-task').disabled=!selected?.archived;
  $('archive').disabled=!selected||selected.archived;$('save').disabled=!!selected?.archived;
  $('toggle').disabled=!selected||selected.archived;$('generate').disabled=!!selected?.archived;
  $('toggle').textContent=selected?.enabled?'停用任务':'启用任务';
  $('task-status').textContent=selected?.archived?'已归档':selected?.enabled?'已启用':'停用';
}
async function reload(){const data=await api('list');tasks=data.tasks;renderTasks();for(const [id,key] of Object.entries(settingFields))$(id).value=data.settings[key];if(data.runtime_error)message('投递执行器异常：'+data.runtime_error,true);return data;}
async function save(){
  const saved=await api('save',{id:selected?.id,version:selected?.version,config:readConfig()});
  selected=saved;$('task-title').textContent=saved.config.name||'未命名任务';$('archive').disabled=false;
  await reload();taskState();message(saved.enabled?'已保存，新配置用于后续计划。':'已保存，任务保持停用。');return saved;
}
function showRun(run){
  selectedRun=run;$('delivery-panel').hidden=run.kind!=='scheduled';
  const labels={queued:'排队中',generating:'生成中',ready:'预览完成',prepared:'正文已准备',sent:'简报已发送',partial:'部分投递失败',unknown:'投递结果未知',missed:'遗漏计划，需手动补发',empty:'可用消息中无重要内容',failed:'生成失败',cancelled:'已取消'};
  $('preview-state').textContent=`${labels[run.status]||run.status}${run.error?'：'+run.error:''}`;
  $('preview-text').textContent=run.result?.summary||'暂无简报正文。';
  $('preview-meta').textContent=run.result?JSON.stringify({...run.result,summary:undefined},null,2):'';
}
async function records(){
  if(!selected){$('run-list').textContent='请先保存任务。';return;}
  const runs=await api('runs',{id:selected.id,page});$('run-list').replaceChildren();
  if(!runs.length)$('run-list').textContent='当前页没有执行记录。';
  for(const run of runs){const row=document.createElement('div');row.className='run';const button=document.createElement('button');button.textContent=`${new Date(run.snapshot?.planned_at||run.created_at).toLocaleString()} · ${run.kind==='scheduled'?'正式计划':'预览'} · ${run.status}`;button.onclick=()=>guard(async()=>{tab('preview');showRun(run);if(run.kind==='scheduled')await delivery();});row.append(button);$('run-list').append(row);}
  $('more').disabled=runs.length<30;
}
function tab(name){document.querySelectorAll('.tab').forEach(x=>x.hidden=x.id!==name);document.querySelectorAll('[data-tab]').forEach(x=>{if(x.dataset.tab===name)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current');});if(name==='records')guard(records);if(name==='reminders')guard(related);}
document.querySelectorAll('[data-tab]').forEach(x=>x.onclick=()=>tab(x.dataset.tab));
for(const [i,name] of ['一','二','三','四','五','六','日'].entries()){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='weekday';input.value=i;label.append(input,document.createTextNode('周'+name));document.querySelector('.day-options').append(label);}
form.elements.frequency.onchange=()=>{$('weekdays').hidden=form.elements.frequency.value!=='weekly';if(!selected&&[24,168].includes(Number(form.elements.lookback_hours.value)))form.elements.lookback_hours.value=form.elements.frequency.value==='weekly'?168:24;};
$('group-picker').onchange=()=>{const value=$('group-picker').value;if(value){const groups=new Set(readConfig().groups);groups.add(value);form.elements.groups.value=[...groups].join('\n');$('group-picker').value='';}};
form.onsubmit=e=>{e.preventDefault();guard(save,e.submitter);};
$('new').onclick=()=>fill(null);
$('refresh').onclick=()=>initialized?guard(reload,$('refresh')):initializeView();
const settingFields={concurrency:'concurrency',retention:'retention_days','text-limit':'text_limit','send-interval':'send_interval','generation-timeout':'generation_timeout','call-budget':'call_budget'};
$('settings-save').onclick=()=>guard(async()=>{const values={};for(const [id,key] of Object.entries(settingFields))values[key]=Number($(id).value);await api('settings',{values});message('全局设置已保存。');},$('settings-save'));
$('archive').onclick=()=>guard(async()=>{if(!selected)return;await api('archive',{id:selected.id,version:selected.version});fill(null);await reload();message('任务已归档，执行记录保留。');},$('archive'));
function deleteReady(){ $('delete-confirm').disabled=deleting||!deletionPreview||deletionPreview.busy||!deletionPreview.archived||$('delete-confirmation').value!=='彻底删除'; }
$('delete-task').onclick=()=>guard(async()=>{
  if(!selected)return;
  deletionPreview=await api('delete_preview',{id:selected.id});
  const p=deletionPreview;
  $('delete-name').textContent=`任务：${p.name||'未命名任务'}`;
  $('delete-counts').textContent=`将清除 ${p.runs} 条执行记录、${p.lists} 份候选清单；关联 ${p.unique_reminders} 条独有提醒、${p.shared_reminders} 条共享提醒。`;
  $('delete-legacy').textContent=p.legacy_reply_texts?`同时清空该收件人 ${p.legacy_reply_texts} 条无法归属任务的旧操作反馈文本，保留防重 ID。其他任务的配置、简报和提醒不受影响。`:'';
  $('delete-reminders').checked=p.cancel_unique;
  $('delete-reminders').disabled=p.resuming||!p.unique_reminders;
  $('delete-shared').textContent=p.resuming?'此前删除尚未完成，将沿用原提醒处理方式继续；已取消的提醒不会重建。':`共享提醒始终保留，仅解除与本任务的关联。保留的独有提醒也会解除关联，继续独立存在。`;
  $('delete-confirmation').value='';
  $('delete-error').textContent=p.busy?'仍有生成、发送或私聊操作正在进行，请稍后重新打开确认页。':'';
  deleteReady();$('delete-dialog').showModal();
},$('delete-task'));
$('delete-confirmation').oninput=deleteReady;
$('delete-cancel').onclick=()=>{if(!deleting)$('delete-dialog').close();};
$('delete-dialog').oncancel=e=>{if(deleting)e.preventDefault();};
$('delete-confirm').onclick=async()=>{
  if($('delete-confirm').disabled)return;
  deleting=true;deleteReady();$('delete-cancel').disabled=true;$('delete-reminders').disabled=true;
  try{
    await api('delete',{id:deletionPreview.id,version:deletionPreview.version,token:deletionPreview.token,
      cancel_unique:$('delete-reminders').checked,confirmation:$('delete-confirmation').value});
    $('delete-dialog').close();deletionPreview=null;fill(null);await reload();
    message('任务及其内容已彻底删除。已保留最小防重凭据；提醒按确认的方式处理。');
  }catch(e){$('delete-error').textContent=(e.message||String(e))+' 请关闭并重新打开确认页后重试。';deletionPreview=null;}
  finally{deleting=false;$('delete-cancel').disabled=false;deleteReady();}
};
$('more').onclick=()=>{page++;guard(records);};
$('show-archived').onchange=renderTasks;
$('toggle').onclick=()=>guard(async()=>{if(!selected)return;selected=await api('enable',{id:selected.id,version:selected.version,enabled:!selected.enabled});taskState();await reload();message(selected.enabled?'已启用，从下一计划时点开始执行。':'已停用，既有提醒保持不变。');},$('toggle'));
function textNode(tag,text){const node=document.createElement(tag);node.textContent=text;return node;}
function actionButton(label,fn){const button=textNode('button',label);button.onclick=()=>guard(fn,button);return button;}
function receiptCard(part,refresh){
  const card=document.createElement('div');card.className='card';card.append(textNode('strong',`${({digest:'简报',inquiry:'日程询问',reminder:'提醒',change:'变更询问',reply:'回复'})[part.channel]||part.channel} #${part.ordinal+1} · ${stateName(part.state)}`));
  if(part.body)card.append(textNode('pre',part.body));
  if(part.receipt)card.append(textNode('p','回执：'+part.receipt));
  if(part.error)card.append(textNode('p',part.error));
  if(part.state==='unknown')for(const [outcome,label] of [['acknowledged','已在 QQ 核实发送成功'],['failed','已在 QQ 核实未发送']])card.append(actionButton(label,async()=>{await api('resolve',{id:part.id,outcome});await refresh();message('已记录核对结果；未发送部分仍需明确补发。');}));
  return card;
}
async function delivery(){if(!selectedRun)return;const data=await api('delivery',{id:selectedRun.id});showRun(data.run);$('delivery-parts').replaceChildren();for(const part of data.parts)$('delivery-parts').append(receiptCard(part,delivery));}
$('delivery-refresh').onclick=()=>guard(delivery,$('delivery-refresh'));
$('catchup-preview').onclick=()=>guard(async()=>{if(!selectedRun)return;const id=selectedRun.id;await api('catchup_preview',{id});const token=++pollToken;message('正在准备补发预览，尚未提交发送。');while(token===pollToken){const run=await api('run',{id});showRun(run);if(!['queued','generating','ready'].includes(run.status)){await delivery();break;}await new Promise(r=>setTimeout(r,1000));}},$('catchup-preview'));
for(const [id,channel] of [['resend-digest','digest'],['resend-inquiry','inquiry']])$(id).onclick=()=>guard(async()=>{if(!selectedRun)return;await api('resend',{id:selectedRun.id,channel});message('已提交未成功部分，请刷新查看回执。');await delivery();},$(id));
function field(card,label,value=''){const node=document.createElement('label'),input=document.createElement('input');node.append(document.createTextNode(label),input);input.value=value;card.append(node);return input;}
async function related(){
  const container=$('related-list');container.replaceChildren();if(!selected){container.textContent='请先选择任务。';return;}
  const data=await api('related',{id:selected.id});
  for(const list of data.list){const card=document.createElement('div');card.className='card';card.append(textNode('h3',`候选清单 ${list.id.slice(0,8)} · ${stateName(list.state)}`),textNode('p',`有效至：${list.expires_at||'尚未成功询问'}`));for(const [i,item] of list.items.entries())card.append(textNode('p',`${i+1}. ${item.title} · ${item.event_at}`));container.append(card);}
  for(const reminder of data.reminder){const card=document.createElement('div');card.className='card';card.append(textNode('h3',`${reminder.title} · ${stateName(reminder.state)}`),textNode('p',`提醒 ${reminder.id.slice(0,8)} · ${reminder.recipient} · 关联 ${reminder.task_ids.length} 个任务`));if(reminder.native_owned){card.append(textNode('p',`原生未来任务：${reminder.native_name||reminder.title} · ID ${reminder.native_job_id||'尚未确认创建'}`),textNode('p',`原生计划：${reminder.native_schedule||reminder.remind_at} · ${reminder.native_enabled===false?'停用':'由原生执行'}`));if(reminder.native_error)card.append(textNode('p',reminder.native_error));}const eventAt=field(card,'事项时间（带时区）',reminder.event_at),remindAt=field(card,'提醒时间（带时区）',reminder.remind_at);if(!['cancelled','native_absent'].includes(reminder.state)){card.append(actionButton('保存本次修改',async()=>{const values={};if(eventAt.value!==reminder.event_at)values.event_at=eventAt.value;if(remindAt.value!==reminder.remind_at)values.remind_at=remindAt.value;if(!Object.keys(values).length){message('时间未变化。');return;}const result=await api('reminder',{id:reminder.id,version:reminder.version,operation:'modify',values});message(result.message);await related();}),actionButton('取消该提醒',async()=>{const result=await api('reminder',{id:reminder.id,version:reminder.version,operation:'cancel'});message(result.message);await related();}));}container.append(card);}
  for(const change of data.change){const card=document.createElement('div');card.className='card';const proposal=change.proposal;const target=data.reminder.find(x=>x.id===proposal.reminder_id)?.title||data.list.find(x=>x.id===proposal.list_id)?.items[proposal.index-1]?.title||'关联事项';const operation={select:'添加所选日程',modify:'调整事项或提醒时间',cancel:'取消已有提醒'}[proposal.action]||'调整关联事项';card.append(textNode('h3',`确认 ${change.id.slice(0,8)} · ${stateName(change.state)}`),textNode('p',`${target}：${change.reason}`),textNode('p',operation+(proposal.event_at?'，事项时间 '+proposal.event_at:'')));if(change.state==='pending'){const when=field(card,'本次指定提醒时间（可选，带时区）');for(const accept of [true,false])card.append(actionButton(accept?'确认此项操作':'拒绝并保留原提醒',async()=>{const result=await api('change',{id:change.id,accept,values:when.value?{remind_at:when.value}:{}});message(result.message);await related();}));}container.append(card);}
  for(const part of data.outbox.filter(x=>['failed','unknown'].includes(x.state)))container.append(receiptCard(part,related));
  if(!container.childNodes.length)container.textContent='暂无关联清单或提醒。';
}
$('related-refresh').onclick=()=>guard(related,$('related-refresh'));
$('generate').onclick=()=>guard(async()=>{
  const task=await save();const run=await api('preview',{id:task.id});const token=++pollToken;
  message('预览已入队。');
  while(token===pollToken){const current=await api('run',{id:run.id});showRun(current);if(!['queued','generating'].includes(current.status)){message(current.status==='failed'?'预览失败，请查看执行详情。':'预览处理结束，请查看下方结果。');break;}await new Promise(resolve=>setTimeout(resolve,1000));}
},$('generate'));
async function initializeView(){await guard(async()=>{
  message('正在加载任务与可选配置…');
  bridge=window.AstrBotPluginView;if(!bridge)throw new Error('请从 AstrBot 插件详情打开此页面。');
  await bridge.ready();const initial=await reload();metadata=await api('metadata');
  $('group-picker').replaceChildren();option($('group-picker'),'','选择群后添加');
  for(const group of metadata.groups)option($('group-picker'),group.umo,`${group.name} · ${group.id}`);
  fill(null);initialized=true;document.querySelector('.layout').inert=false;
  const warnings=[...metadata.warnings];if(initial.runtime_error)warnings.push('投递执行器异常：'+initial.runtime_error);
  message(warnings.join('；'),warnings.length>0);
},$('refresh'));}
await initializeView();
