(() => {
  let tasks=[],settings={concurrency:1,retention_days:30},runs={};
  window.fixtureActions=[];
  window.AstrBotPluginView={ready:async()=>({}),apiPost:async(endpoint,payload)=>{
    window.fixtureActions.push(payload.action);let data;
    switch(payload.action){
      case 'list':data={tasks,settings};break;
      case 'metadata':data={models:[{id:'fixture',model:'offline'}],personas:[{id:'default'}],groups:[{umo:'test:GroupMessage:123',name:'测试源群',id:'123'}],warnings:[]};break;
      case 'save':{
        const old=tasks.find(t=>t.id===payload.id);
        data={id:old?.id||'task1',version:(old?.version||0)+1,config:payload.config,enabled:false};
        tasks=tasks.filter(t=>t.id!==data.id);tasks.push(data);break;
      }
      case 'settings':Object.assign(settings,payload.values);data=settings;break;
      case 'archive':tasks=tasks.filter(t=>t.id!==payload.id);data={};break;
      case 'preview':{
        data={id:'preview1'};runs.preview1={id:'preview1',kind:'preview',status:'ready',created_at:new Date().toISOString(),result:{summary:'重要通知\n今天 15:00 课程说明会。\n\n<img src=x onerror=alert(1)>\n\n[覆盖说明] 测试源群：覆盖完整性未知',candidates:[],coverage:[{group:'test:GroupMessage:123',count:80,note:'覆盖未知'}],logical_calls:1}};break;
      }
      case 'run':data=runs[payload.id];break;
      case 'runs':data=Object.values(runs);break;
      default:throw new Error('Unexpected fixture operation');
    }
    return {ok:true,data:structuredClone(data)};
  }};
})();
