import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createConnection } from 'node:net';
import { mkdtemp, rm, stat, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
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
