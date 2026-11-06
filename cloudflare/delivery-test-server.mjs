// Loopback-only integration harness. Production SQL and Worker routes, real SQLite.
import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import worker, {performEmergencyCheck} from './worker.js';
const db = new DatabaseSync(':memory:');
export function d1(database) {
  return {prepare(sql) {
    let values=[];
    return {bind(...v){values=v;return this;},
      async run(){const r=database.prepare(sql).run(...values);return {meta:{changes:Number(r.changes)}};},
      async first(){return database.prepare(sql).get(...values)||null;},
      async all(){return {results:database.prepare(sql).all(...values)};}};
  }};
}
const env={DB:d1(db),STATUS_API_TOKEN:'isolated-local-token'};
let messages=[],responses=[], offset=0, fixture=[],target=null, failAck=false;
const RealDate=Date, realFetch=globalThis.fetch;
const capturedNow=Date.parse('2026-11-05T21:27:31.762444+09:00');
globalThis.Date=class extends RealDate {
  constructor(...args){super(...(args.length?args:[capturedNow+offset]));}
  static now(){return capturedNow+offset;}
};
const server=http.createServer(async(req,res)=>{
 try{
  let raw=''; for await(const chunk of req)raw+=chunk;
  const body=raw?JSON.parse(raw):{};
  const url=new URL(req.url,'http://127.0.0.1');
  let out;
  if(url.pathname==='/control'){
    if(body.reset){env.DB=d1(db);db.exec('DROP TABLE IF EXISTS cgv_delivery; DROP TABLE IF EXISTS cgv_delivery_limits; DROP TABLE IF EXISTS app_state;');messages=[];responses=[];offset=0;failAck=false;}
    if(body.advance) offset+=body.advance;
    if(body.responses)responses=body.responses;
    if(body.fixture)fixture=body.fixture;
    if(body.target)target=body.target;
    if('failAck' in body)failAck=body.failAck;
    out={ok:true,messages,now:Date.now(),journal:db.prepare("SELECT name FROM sqlite_master WHERE name='cgv_delivery'").get()?db.prepare("SELECT * FROM cgv_delivery").all():[]};
  }else if(url.pathname==='/webhook'){
    messages.push(body);
    const next=responses.shift()||{status:204};
    if(next.close){req.socket.destroy();return;}
    if(next.delay)await new Promise(r=>setTimeout(r,next.delay));
    res.writeHead(next.status,{'Content-Type':'application/json','Retry-After':String(next.retry_after||0)});
    res.end(next.status===204?'':JSON.stringify({retry_after:next.retry_after||0}));return;
  }else if(url.pathname==='/emergency'){
    await env.DB.prepare('CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)').run();
    const status={targets:[target],last_run_at:new Date().toISOString()};
    await env.DB.prepare("INSERT INTO app_state VALUES('status',?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(JSON.stringify(status),new Date().toISOString()).run();
    globalThis.fetch=async(input,options)=>{
      if(String(input).startsWith('https://mcp.aka.page/'))return new Response(JSON.stringify({data:fixture}),{status:200});
      if(String(input).startsWith('http://127.0.0.1:'))return realFetch(input,options);
      throw new Error('External request blocked by isolated harness');
    };
    try{out=await performEmergencyCheck(env,false);}finally{globalThis.fetch=realFetch;}
  }else{
    if(failAck&&body.op==='complete'){res.writeHead(503);res.end('{}');return;}
    const request=new Request('http://localhost'+req.url,{method:req.method,headers:req.headers,...(req.method==='POST'?{body:raw}:{})});
    const response=await worker.fetch(request,env,{});
    res.writeHead(response.status,Object.fromEntries(response.headers));res.end(await response.text());return;
  }
  res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(out));
 }catch(error){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:String(error)}));}
});
server.listen(0,'127.0.0.1',()=>{
 env.DISCORD_WEBHOOK_URL=`http://127.0.0.1:${server.address().port}/webhook`;
 console.log(JSON.stringify({url:`http://127.0.0.1:${server.address().port}`}));
});
