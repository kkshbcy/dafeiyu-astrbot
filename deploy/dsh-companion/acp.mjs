import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

export class Acp {
  constructor(command, args, cwd) {
    this.pending = new Map();
    this.turns = new Map();
    this.sequence = 0;
    this.proc = spawn(command, args, {cwd, stdio:['pipe','pipe','pipe']});
    this.dead = false;
    this.proc.stdin.on('error', () => this.stop());
    // DSH can log prompts and account details on stderr. Keep them out of service logs.
    this.proc.stderr.resume();
    this.proc.once('error', () => this.stop());
    this.proc.once('exit', () => this.stop());
    createInterface({input:this.proc.stdout}).on('line', line => {
      let packet;
      try { packet=JSON.parse(line); } catch { return; }
      if (packet.method === 'session/update') {
        this.turns.get(packet.params?.sessionId)?.(packet.params.update);
      } else if (packet.method && packet.id !== undefined) {
        const reply = packet.method === 'session/request_permission'
          ? {result:{outcome:{outcome:'cancelled'}}}
          : {error:{code:-32601,message:'unsupported client capability'}};
        this.send({id:packet.id,...reply});
      } else if (this.pending.has(packet.id)) {
        const {resolve,reject,timer}=this.pending.get(packet.id);
        clearTimeout(timer); this.pending.delete(packet.id);
        if (packet.error) reject(new Error('DSH RPC '+String(packet.error.code)));
        else resolve(packet.result);
      }
    });
  }
  send(obj) {
    if (!this.dead && this.proc.stdin.writable) this.proc.stdin.write(JSON.stringify({jsonrpc:'2.0',...obj})+'\n');
  }
  request(method,params,ms=25000) {
    if (this.dead) return Promise.reject(new Error('DSH offline'));
    const id=++this.sequence;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('DSH deadline'));},ms);
      this.pending.set(id,{resolve,reject,timer});
      this.send({id,method,params});
    });
  }
  stop() {
    if (this.dead) return;
    this.dead=true;
    for(const item of this.pending.values()){clearTimeout(item.timer);item.reject(new Error('DSH offline'));}
    this.pending.clear(); this.turns.clear();
    this.proc.kill();
  }
}
