/* A session-scoped MCP tool catalogue. The real tool runs inside AstrBot with the original event. */
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

const offered = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const tools = offered.map(({name, description, parameters}) => ({
  name, description: description || name, inputSchema: parameters || {type: 'object', properties: {}},
}));
for await (const line of createInterface({input: process.stdin})) {
  let req;
  try { req = JSON.parse(line); } catch { continue; }
  if (req.id === undefined) continue;
  let result;
  switch (req.method) {
    case 'initialize':
      result = {protocolVersion: req.params.protocolVersion, capabilities: {tools: {}},
                serverInfo: {name: 'dafeiyu-astrbot', version: '1.0.0'}};
      break;
    case 'ping': result = {}; break;
    case 'tools/list': result = {tools}; break;
    case 'tools/call':
      result = tools.some(tool => tool.name === req.params?.name)
        ? {content:[{type:'text',text:'已提交给 AstrBot，尚未执行。结束本轮，等工具返回实际结果再答复。'}]}
        : {isError:true,content:[{type:'text',text:'此会话没有这个工具。'}]};
      break;
    default:
      process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:req.id,error:{code:-32601,message:'unknown method'}})+'\n');
      continue;
  }
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:req.id,result})+'\n');
}
