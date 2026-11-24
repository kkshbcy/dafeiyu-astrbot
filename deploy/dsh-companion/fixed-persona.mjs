import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
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
  let db, query, slot, warned=false, closed=false;
  function readCard() {
    db??=new DatabaseSync(settings.database,{readOnly:true});
    query??=db.prepare('SELECT system_prompt FROM personas WHERE persona_id=?');
    const card=query.get(settings.id)?.system_prompt;
    // DSH renders deployment prompt templates. Leave cards with variables in AstrBot.
    return typeof card==='string' && !/\{\{|\}\}/.test(card) ? card.replace(/\r\n/g,'\n').trim() : '';
  }
  return {
    async acquire(system) {
      if (closed) return null;
      let selected;
      try {
        const card=readCard();
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
