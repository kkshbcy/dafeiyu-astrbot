import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createConnection } from 'node:net';
import { mkdtemp, rm, stat, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { start } from '../deploy/dsh-companion/server.mjs';

const fixture=fileURLToPath(new URL('./dsh-fixtures/acp.mjs',import.meta.url));
const base={binding:'a'.repeat(64),system:['心境 A'],history:[{role:'user',content:'第一天'}],
            recent:[{role:'user',content:'接着说'}],images:[],tools:[],event:'m1'};
function ask(socket,input){
  return new Promise((resolve,reject)=>{
    const s=createConnection(socket),parts=[];
    s.on('connect',()=>s.write(JSON.stringify(input)+'\n'));s.on('data',c=>parts.push(c));
    s.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(parts)))}catch(e){reject(e)}});s.on('error',reject);
  });
}
test('one official-protocol connection, isolated durable and auxiliary turns, scoped tools',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'dfy-')),socket=join(dir,'s');
  const options={socket,state:dir,command:process.execPath,args:[fixture],deadline:2500};
  let app=await start(options);
  try{
    assert.equal((await stat(socket)).mode&0o777,0o600);
    const first=JSON.parse((await ask(socket,base)).text);
    assert.deepEqual(first.env.conversation,base.history);
    const answers=await Promise.all([1,2,3].map(i=>ask(socket,{...base,system:['今日心境'+i]})));
    assert(answers.every(a=>a.ok&&!a.text.includes('private reasoning')));
    answers.forEach((a,i)=>{const parsed=JSON.parse(a.text);assert.equal(parsed.sid,first.sid);
      assert.equal(parsed.env.instructions[0],'今日心境'+(i+1));assert.deepEqual(parsed.env.conversation,base.recent);});
    const group=JSON.parse((await ask(socket,{...base,binding:'b'.repeat(64)})).text);
    assert.notEqual(group.sid,first.sid);
    const aux=JSON.parse((await ask(socket,{...base,binding:null})).text);
    assert.notEqual(aux.sid,first.sid);
    const tools=[{name:'generate_image',description:'画图',parameters:{type:'object',properties:{prompt:{type:'string'}}}}];
    assert.equal((await ask(socket,{...base,binding:null,tools})).ok,false);
    const tool=await ask(socket,{...base,tools});assert.deepEqual(tool.call,{id:'tool-1',name:'generate_image',args:{prompt:'云'}});
    const fail=await ask(socket,{...base,recent:[{role:'user',content:'fail'}]});assert.equal(fail.ok,false);
    await app.close();app=await start(options);
    const reopened=JSON.parse((await ask(socket,base)).text);assert.equal(reopened.sid,first.sid);
    assert(reopened.knowsFirst);
    await app.close();await unlink(join(dir,'workspace','fixture-persistence.json'));app=await start(options);
    const rebuilt=JSON.parse((await ask(socket,base)).text);
    assert.notEqual(rebuilt.sid,first.sid);assert.deepEqual(rebuilt.env.conversation,base.history);assert(rebuilt.knowsFirst);
    const invalid=await ask(socket,{...base,binding:'/tmp/foreign'});assert.equal(invalid.ok,false);
  }finally{await app.close();await rm(dir,{recursive:true,force:true});}
});

test('fixed persona follows AstrBot edits while existing conversations and auxiliary prompts stay isolated',async()=>{
  const state=await mkdtemp(join(tmpdir(),'dfy-persona-')),socket=join(state,'s');
  const database=join(state,'astrbot.db');
  const db=new DatabaseSync(database);
  db.exec('CREATE TABLE personas (persona_id TEXT PRIMARY KEY, system_prompt TEXT)');
  const card='鲸鱼娘｜说话干脆，偶尔幽默。';
  const edited='鲸鱼娘｜说话温和，偶尔幽默。';
  const setCard=db.prepare('INSERT OR REPLACE INTO personas VALUES (?, ?)');
  setCard.run('whale',card);
  const common={socket,state,command:process.execPath,args:[fixture],deadline:2500};
  const options={...common,fixedPersona:{database,id:'whale'}};
  let app=await start(common);
  const run=async(input)=>{
    const reply=await ask(socket,input);
    assert.equal(reply.ok,true,reply.error);
    return JSON.parse(reply.text);
  };
  try{
    const old=await run({...base,system:[card]});
    await app.close();
    app=await start(options);
    const turns=await Promise.all([1,2].map(i=>run({...base,system:[`群规则\n\n${card}\n\n心境 ${i}`]})));
    for (const [index,turn] of turns.entries()){
      assert.equal(turn.sid,old.sid);
      assert.equal(turn.fixedCard,card);
      assert.deepEqual(turn.env.instructions,[`群规则\n\n心境 ${index+1}`]);
      assert.deepEqual(turn.env.conversation,base.recent);
      assert(turn.knowsFirst);
    }
    const aux=await run({...base,binding:null,system:[card,'只描述图片']});
    assert.equal(aux.fixedCard,null);
    assert.deepEqual(aux.env.instructions,[card,'只描述图片']);
    const unrelated=await run({...base,binding:'b'.repeat(64),system:[`引述「${card}」`,'其他角色']});
    assert.equal(unrelated.fixedCard,null);
    assert.deepEqual(unrelated.env.instructions,[`引述「${card}」`,'其他角色']);
    assert.notEqual(unrelated.sid,old.sid);
    setCard.run('whale',edited);
    const next=await run({...base,system:[edited,'新近记忆']});
    assert.equal(next.sid,old.sid);
    assert.equal(next.fixedCard,edited);
    assert.deepEqual(next.env.instructions,['新近记忆']);
    await app.close();
    app=await start(options);
    const reopened=await run({...base,system:[edited]});
    assert.equal(reopened.sid,old.sid);
    assert.equal(reopened.fixedCard,edited);
    assert.deepEqual(reopened.env.instructions,[]);
    db.prepare('DELETE FROM personas').run();
    const fallback=await run({...base,system:[edited,'继续聊']});
    assert.equal(fallback.sid,old.sid);
    assert.equal(fallback.fixedCard,null);
    assert.deepEqual(fallback.env.instructions,[edited,'继续聊']);
  }finally{db.close();await app.close();await rm(state,{recursive:true,force:true});}
});
