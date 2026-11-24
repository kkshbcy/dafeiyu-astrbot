import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

function removeCard(instructions, card) {
  for (const [index, value] of instructions.entries()) {
    if (typeof value !== 'string') continue;
    const text=value.replace(/\r\n/g,'\n').trim();
    const start=text.indexOf(card);
    const end=start+card.length;
    if (start<0 || (start>0 && text[start-1]!=='\n') ||
        (end<text.length && text[end]!=='\n')) continue;
    const remaining=[text.slice(0,start).trimEnd(),text.slice(end).trimStart()].filter(Boolean).join('\n\n');
    return [...instructions.slice(0,index),...(remaining ? [remaining] : []),...instructions.slice(index+1)];
  }
  return null;
}

export function createFixedPersona(settings,state,connection) {
  if (!settings) return null;
  const profile=join(state,'fixed-persona.patch.json');
  const settingsFile=join(dirname(settings.database),'cmd_config.json');
  let db, queries, slot, warned=false, closed=false;
  function readCard(binding) {
    db??=new DatabaseSync(settings.database,{readOnly:true});
    queries??={
      profile:db.prepare("SELECT value FROM preferences WHERE key='abconf_mapping' LIMIT 1"),
      override:db.prepare("SELECT value FROM preferences WHERE scope='umo' AND scope_id=? AND key='session_service_config' LIMIT 1"),
      conversations:db.prepare('SELECT user_id, conversation_id, persona_id FROM conversations WHERE platform_id=?'),
      card:db.prepare('SELECT system_prompt FROM personas WHERE persona_id=?')
    };
    const config=JSON.parse(readFileSync(settingsFile,'utf8').replace(/^\uFEFF/,''));
    const runner=config.agent_runner;
    if (runner?.runner_type!=='local' || runner.config?.persona?.persona_id!==settings.id) return '';
    // Let AstrBot supply the full prompt when it owns per-session configuration.
    const profiles=queries.profile.get();
    if (profiles && Object.keys(JSON.parse(profiles.value) ?? {}).length) return '';
    for (const qq of config.platform ?? []) {
      if (!qq.enable || qq.type!=='aiocqhttp' || !qq.id) continue;
      for (const row of queries.conversations.iterate(qq.id)) {
        const scope=row.user_id;
        if (!scope.startsWith(qq.id+':FriendMessage:') && !scope.startsWith(qq.id+':GroupMessage:')) continue;
        // Match AstrBot bridge's Python json.dumps default separators.
        const key=createHash('sha256').update(`[${JSON.stringify(scope)}, ${JSON.stringify(row.conversation_id)}]`).digest('hex');
        if (key!==binding) continue;
        if (row.persona_id!=null && row.persona_id!==settings.id) return '';
        const override=queries.override.get(scope);
        const persona=override && JSON.parse(override.value)?.persona_id;
        if (persona && persona!==settings.id) return '';
        return plainCard();
      }
    }
    return '';
  }
  function plainCard() {
    const card=queries.card.get(settings.id)?.system_prompt;
    // DSH renders deployment prompt templates. Leave cards with variables in AstrBot.
    return typeof card==='string' && !/\{\{|\}\}/.test(card) ? card.replace(/\r\n/g,'\n').trim() : '';
  }
  return {
    async acquire(binding,system) {
      if (closed) return null;
      let selected;
      try {
        const card=readCard(binding);
        const instructions=card && removeCard(system,card);
        if (!instructions) return null;
        if (slot?.card!==card) {
          if (slot?.users) return null;
          writeFileSync(profile,JSON.stringify([{id:'system-prompt',config:{personaSuffix:card}}]),{mode:0o600});
          slot?.connection.stop();
          slot={card,users:0,connection:connection(['--patch',profile])};
        }
        selected=slot;
        selected.users++;
        const client=await selected.connection.connect();
        if (!selected.ready) {
          selected.ready=true;
          console.info('Official DSH fixed persona ready for matched chat turns');
        }
        return {client,instructions,release:()=>{selected.users--;}};
      } catch {
        if (selected) selected.users--;
        if (!warned) {
          console.warn('DSH fixed persona unavailable; using AstrBot instructions');
          warned=true;
        }
        return null;
      }
    },
    close() {
      closed=true;
      slot?.connection.stop();
      db?.close();
    }
  };
}
