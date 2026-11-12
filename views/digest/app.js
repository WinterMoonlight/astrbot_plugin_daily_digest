const $ = (id) => document.getElementById(id);
const form = $('form');
const defaults = {name:'',groups:[],recipient:'',provider_id:'',persona_id:'',frequency:'daily',weekdays:[0],time:'08:00',timezone:'Asia/Shanghai',lookback_hours:24,lead_minutes:30,candidate_days:7,selection_hours:24,prompt:'提取重要通知、待办与截止时间、变更与取消，分类展示，空分类省略。合并重复事项，保留来源与日期；不明确的信息标为待确认，忽略闲聊。'};
let selected = null, tasks = [], metadata = {models:[],personas:[],groups:[]}, page = 1, pollToken = 0;
let bridge;
function message(text, error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function api(action, values={}){
  const result = await bridge.apiPost('rpc', {action,...values});
  if (!result?.ok) throw new Error(result?.message || result?.error || '操作失败');
  return result.data;
}
async function guard(fn, button){
  if(button) button.disabled=true;
  try{await fn();}catch(e){message(e.message || String(e),true);}
  finally{if(button) button.disabled=false;}
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
  selected=task;page=1;pollToken++;
  const data=task?.config||structuredClone(defaults);
  $('task-title').textContent=task?.config.name||'新建任务';
  for(const key of Object.keys(defaults)){
    if(key==='weekdays')document.querySelectorAll('[name=weekday]').forEach(x=>x.checked=data.weekdays.includes(Number(x.value)));
    else if(key==='groups')form.elements.groups.value=data.groups.join('\n');
    else form.elements[key].value=data[key];
  }
  options(form.elements.provider_id,metadata.models.map(m=>({id:m.id,label:`${m.id} · ${m.model}`})),data.provider_id);
  options(form.elements.persona_id,metadata.personas,data.persona_id);
  $('archive').disabled=!task;
  $('weekdays').hidden=data.frequency!=='weekly';
  $('preview-state').textContent='';$('preview-text').textContent='尚无预览。';$('preview-meta').textContent='';
  $('run-list').textContent='切换到执行记录查看。';
  tab('config');
}
function renderTasks(){
  $('tasks').replaceChildren();
  if(!tasks.length){const p=document.createElement('p');p.className='muted';p.textContent='还没有任务，点击新建开始。';$('tasks').append(p);}
  for(const task of tasks){const b=document.createElement('button');b.className='task';const title=document.createElement('strong');title.textContent=task.config.name||'未命名任务';const status=document.createElement('small');status.textContent=`停用 · ${task.config.frequency==='weekly'?'每周':'每日'} ${task.config.time}`;b.append(title,status);b.onclick=()=>fill(task);$('tasks').append(b);}
}
async function reload(){const data=await api('list');tasks=data.tasks;renderTasks();$('concurrency').value=data.settings.concurrency;$('retention').value=data.settings.retention_days;}
async function save(){
  const saved=await api('save',{id:selected?.id,version:selected?.version,config:readConfig()});
  selected=saved;$('task-title').textContent=saved.config.name||'未命名任务';$('archive').disabled=false;
  await reload();message('已保存，任务保持停用。');return saved;
}
function showRun(run){
  const labels={queued:'排队中',generating:'生成中',ready:'预览完成',empty:'可用消息中无重要内容',failed:'生成失败',cancelled:'已取消'};
  $('preview-state').textContent=`${labels[run.status]||run.status}${run.error?'：'+run.error:''}`;
  $('preview-text').textContent=run.result?.summary||'暂无简报正文。';
  $('preview-meta').textContent=run.result?JSON.stringify({...run.result,summary:undefined},null,2):'';
}
async function records(){
  if(!selected){$('run-list').textContent='请先保存任务。';return;}
  const runs=await api('runs',{id:selected.id,page});$('run-list').replaceChildren();
  if(!runs.length)$('run-list').textContent='当前页没有执行记录。';
  for(const run of runs){const row=document.createElement('div');row.className='run';const button=document.createElement('button');button.textContent=`${new Date(run.created_at).toLocaleString()} · ${run.status}`;button.onclick=()=>{tab('preview');showRun(run);};row.append(button);$('run-list').append(row);}
  $('more').disabled=runs.length<30;
}
function tab(name){document.querySelectorAll('.tab').forEach(x=>x.hidden=x.id!==name);document.querySelectorAll('[data-tab]').forEach(x=>{if(x.dataset.tab===name)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current');});if(name==='records')guard(records);}
document.querySelectorAll('[data-tab]').forEach(x=>x.onclick=()=>tab(x.dataset.tab));
for(const [i,name] of ['一','二','三','四','五','六','日'].entries()){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='weekday';input.value=i;label.append(input,document.createTextNode('周'+name));document.querySelector('.day-options').append(label);}
form.elements.frequency.onchange=()=>{$('weekdays').hidden=form.elements.frequency.value!=='weekly';if(!selected&&[24,168].includes(Number(form.elements.lookback_hours.value)))form.elements.lookback_hours.value=form.elements.frequency.value==='weekly'?168:24;};
$('group-picker').onchange=()=>{const value=$('group-picker').value;if(value){const groups=new Set(readConfig().groups);groups.add(value);form.elements.groups.value=[...groups].join('\n');$('group-picker').value='';}};
form.onsubmit=e=>{e.preventDefault();guard(save,e.submitter);};
$('new').onclick=()=>fill(null);
$('refresh').onclick=()=>guard(reload,$('refresh'));
$('settings-save').onclick=()=>guard(async()=>{await api('settings',{values:{concurrency:Number($('concurrency').value),retention_days:Number($('retention').value)}});message('全局设置已保存。');},$('settings-save'));
$('archive').onclick=()=>guard(async()=>{if(!selected)return;await api('archive',{id:selected.id,version:selected.version});fill(null);await reload();message('任务已归档，执行记录保留。');},$('archive'));
$('more').onclick=()=>{page++;guard(records);};
$('generate').onclick=()=>guard(async()=>{
  const task=await save();const run=await api('preview',{id:task.id});const token=++pollToken;
  message('预览已入队。');
  while(token===pollToken){const current=await api('run',{id:run.id});showRun(current);if(!['queued','generating'].includes(current.status)){message(current.status==='failed'?'预览失败，请查看执行详情。':'预览处理结束，请查看下方结果。');break;}await new Promise(resolve=>setTimeout(resolve,1000));}
},$('generate'));
await guard(async()=>{
  bridge=window.AstrBotPluginView;if(!bridge)throw new Error('请从 AstrBot 插件详情打开此页面。');
  await bridge.ready();await reload();metadata=await api('metadata');
  for(const group of metadata.groups)option($('group-picker'),group.umo,`${group.name} · ${group.id}`);
  fill(null);if(metadata.warnings.length)message(metadata.warnings.join('；'),true);
});
