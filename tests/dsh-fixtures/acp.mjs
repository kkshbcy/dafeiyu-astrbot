import { createInterface } from 'node:readline';
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
const sessions=new Map();
const store='fixture-persistence.json';
const load=()=>existsSync(store)?JSON.parse(readFileSync(store,'utf8')):{};
const patchPath=process.argv[2]==='--patch' && process.argv[3];
const fixedCard=patchPath ? JSON.parse(readFileSync(patchPath,'utf8'))[0].config.personaSuffix : null;
const emit=(value)=>process.stdout.write(JSON.stringify({jsonrpc:'2.0',...value})+'\n');
for await(const line of createInterface({input:process.stdin})){
  const {id,method,params}=JSON.parse(line);
  if(id===undefined)continue;
  if(method==='initialize'){emit({id,result:{protocolVersion:1}});continue;}
  if(method==='session/new'||method==='session/resume'){
    const sid=params.sessionId||randomUUID();
    const durable=load();
    if(method==='session/resume' && !Object.hasOwn(durable,sid)){
      emit({id,error:{code:-32602,message:'session is not resumable: '+sid}});continue;
    }
    durable[sid]??=[];
    sessions.set(sid,params.mcpServers);
    emit({id,result:{sessionId:sid}});continue;
  }
  if(method==='session/close'){sessions.delete(params.sessionId);emit({id,result:{}});continue;}
  if(method!=='session/prompt'){emit({id,error:{code:-32601}});continue;}
  const sid=params.sessionId;
  const env=JSON.parse(params.prompt[0].text.split('\n').slice(1).join('\n'));
  const durable=load();
  (durable[sid]??=[]).push(...env.conversation);
  writeFileSync(store,JSON.stringify(durable));
  const update=update=>emit({method:'session/update',params:{sessionId:sid,update}});
  if(env.conversation.at(-1).content==='fail'){emit({id,result:{stopReason:'cancelled'}});continue;}
  const selected=sessions.get(sid)?.[0];
  if(selected){
    const input=JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list',params:{}})+'\n';
    const response=spawnSync(selected.command,selected.args,{input,encoding:'utf8'});
    const name=JSON.parse(response.stdout).result.tools[0].name;
    const proposed=spawnSync(selected.command,selected.args,{encoding:'utf8',input:JSON.stringify(
      {jsonrpc:'2.0',id:2,method:'tools/call',params:{name,arguments:{prompt:'云'}}})+'\n'});
    if(!JSON.parse(proposed.stdout).result.content[0].text.includes('尚未执行'))throw Error('invalid receipt');
    update({sessionUpdate:'tool_call',toolCallId:'tool-1',title:'mcp__astrbot__'+name,rawInput:{prompt:'云'}});
    update({sessionUpdate:'tool_call_update',toolCallId:'tool-1',status:'completed'});
  }
  update({sessionUpdate:'agent_thought_chunk',content:{type:'text',text:'private reasoning'}});
  update({sessionUpdate:'agent_message_chunk',content:{type:'text',text:JSON.stringify({sid,env,fixedCard,knowsFirst:durable[sid].some(m=>m.content==='第一天')})}});
  emit({id,result:{stopReason:'end_turn'}});
}
