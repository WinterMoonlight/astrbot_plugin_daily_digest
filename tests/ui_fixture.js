(() => {
  let tasks=[],settings={concurrency:1,retention_days:30,text_limit:1500,send_interval:1,generation_timeout:1200,call_budget:64,reply_message_limit:3},runs={};
  let parts=[{id:'part1',owner:'formal1',channel:'digest',ordinal:0,body:'第一段已发送',state:'acknowledged',receipt:'001'},
    {id:'part2',owner:'formal1',channel:'digest',ordinal:1,body:'第二段结果未知',state:'unknown'}];
  let reminder={id:'reminder123',task_id:'task1',task_ids:['task1'],list_ids:['formal1'],title:'课程说明会',recipient:'test:FriendMessage:456',event_at:'2026-12-01T15:00:00+08:00',remind_at:'2026-12-01T14:30:00+08:00',version:1,state:'scheduled'};
  window.fixtureActions=[];
  window.AstrBotPluginView={ready:async()=>({}),apiPost:async(endpoint,payload)=>{
    window.fixtureActions.push(payload.action);let data;
    switch(payload.action){
      case 'list':data={tasks,settings};break;
      case 'metadata':data={models:[{id:'fixture',model:'offline'}],personas:[{id:'default'}],groups:[{umo:'test:GroupMessage:123',name:'测试源群',id:'123'}],warnings:[]};break;
      case 'save':{
        const old=tasks.find(t=>t.id===payload.id);
        data={id:old?.id||'task1',version:(old?.version||0)+1,config:payload.config,enabled:old?.enabled||false,archived:false};
        tasks=tasks.filter(t=>t.id!==data.id);tasks.push(data);break;
      }
      case 'settings':Object.assign(settings,payload.values);data=settings;break;
      case 'archive':data=tasks.find(t=>t.id===payload.id);data.archived=true;data.enabled=false;data.version++;break;
      case 'delete_preview':{const t=tasks.find(t=>t.id===payload.id);data={id:t.id,name:t.config.name,version:t.version,archived:t.archived,busy:false,token:'fixture-token',runs:Object.keys(runs).length,lists:1,unique_reminders:1,shared_reminders:0,legacy_reply_texts:0,resuming:false,cancel_unique:false};break;}
      case 'delete':if(payload.confirmation!=='彻底删除'||payload.token!=='fixture-token')throw new Error('删除确认无效');tasks=tasks.filter(t=>t.id!==payload.id);runs={};data={deleted:true};break;
      case 'enable':data=tasks.find(t=>t.id===payload.id);data.enabled=payload.enabled;data.version++;runs.formal1={id:'formal1',task_id:data.id,kind:'scheduled',status:'unknown',snapshot:{planned_at:new Date().toISOString()},created_at:new Date().toISOString(),result:{summary:'第一段已发送\n第二段结果未知',candidates:[]}};break;
      case 'preview':{
        data={id:'preview1'};runs.preview1={id:'preview1',kind:'preview',status:'ready',created_at:new Date().toISOString(),result:{summary:'重要通知\n今天 15:00 课程说明会。\n\n<img src=x onerror=alert(1)>\n\n[覆盖说明] 测试源群：覆盖完整性未知',candidates:[],coverage:[{group:'test:GroupMessage:123',count:80,note:'覆盖未知'}],logical_calls:1}};break;
      }
      case 'run':data=runs[payload.id];break;
      case 'runs':data=Object.values(runs);break;
      case 'delivery':data={run:runs[payload.id],parts};break;
      case 'catchup_preview':data={id:payload.id};break;
      case 'resolve':parts.find(p=>p.id===payload.id).state=payload.outcome;data={resolved:true};break;
      case 'resend':if(parts.some(p=>p.state==='unknown'))throw new Error('请先核对未知结果');parts.forEach(p=>p.state='acknowledged');runs.formal1.status='sent';data={submitted:true};break;
      case 'related':data={list:[],reminder:[reminder],change:[],outbox:[]};break;
      case 'reminder':if(payload.operation==='cancel')reminder.state='cancelled';else Object.assign(reminder,payload.values);reminder.version++;data={message:'模拟提醒已更新'};break;
      default:throw new Error('Unexpected fixture operation');
    }
    // Match the native Plugin View bridge: the host already unwraps data.
    return structuredClone(data);
  }};
})();
