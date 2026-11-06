// A reservation is never a successful delivery. Each CAS is atomic in shared D1.
const UNKNOWN_MS = 10 * 60 * 1000;
const ready = new WeakSet();
export async function deliverySchema(env) {
  if (!env.DB) throw new Error('delivery database unavailable');
  if (ready.has(env.DB)) return;
  await env.DB.prepare(`CREATE TABLE IF NOT EXISTS cgv_delivery (
    session_key TEXT PRIMARY KEY, target_id TEXT NOT NULL, status TEXT NOT NULL,
    owner TEXT, next_attempt INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0
  )`).run();
  await env.DB.prepare(`CREATE TABLE IF NOT EXISTS cgv_delivery_limits (
    name TEXT PRIMARY KEY, until_ms INTEGER NOT NULL
  )`).run();
  ready.add(env.DB);
}
export function deliveryItemValid(item) {
  if (!item || typeof item.key !== 'string' || item.key.length > 600) return false;
  const p = item.key.split('|');
  return p.length >= 5 && p[0] === '0128' && /^\d{8}$/.test(p[1]) &&
    p[2] === item.target_id && typeof item.target_id === 'string' &&
    item.target_id.length <= 150 && /^\d{2}:\d{2}$/.test(p.at(-1));
}
export async function deliveryOperation(env, body, now = Date.now()) {
  await deliverySchema(env);
  const op = body.op;
  if (op === 'health') {
    const counts = await env.DB.prepare('SELECT status,COUNT(*) AS count FROM cgv_delivery GROUP BY status').all();
    await env.DB.prepare('UPDATE cgv_delivery SET owner=owner WHERE 0 RETURNING session_key').all();
    return {ok:true,protocol_version:1,atomic_claim_sql_ready:true,counts:counts.results || []};
  }
  if (op === 'delivered') {
    const out = await env.DB.prepare("SELECT target_id,session_key FROM cgv_delivery WHERE status='delivered'").all();
    const notified = {};
    for (const r of out.results || []) (notified[r.target_id] ||= []).push(r.session_key);
    return {ok:true,notified};
  }
  const items = body.items;
  // 20 * 4 INSERT parameters stays below D1's 100-parameter limit. Bulk SQL
  // avoids exhausting per-invocation D1 query limits on large messages.
  if (!Array.isArray(items) || items.length > 20 || !items.every(deliveryItemValid)) throw new Error('invalid delivery identities');
  if (!['observe','claim','complete','fail'].includes(op)) throw new Error('invalid delivery operation');
  if (!items.length) return {ok:true,items:[],retry_until:0};
  const owner = String(body.owner || '');
  if (['claim','complete','fail'].includes(op) && !/^[a-zA-Z0-9_-]{8,100}$/.test(owner)) throw new Error('invalid delivery owner');
  const keys=items.map(item=>item.key), slots=keys.map(()=>'?').join(',');
  let granted=new Set(),retryUntil=0;
  if (op==='observe') {
    const values=items.flatMap(item=>{
      const status=item.delivered===true?'delivered':item.bookable===true?'new':'suppressed';
      return [item.key,item.target_id,status,status==='delivered'?new Date(now).toISOString():null];
    });
    await env.DB.prepare(`INSERT INTO cgv_delivery(session_key,target_id,status,delivered_at)
      VALUES ${items.map(()=>'(?,?,?,?)').join(',')} ON CONFLICT(session_key) DO NOTHING`).bind(...values).run();
    const delivered=items.filter(item=>item.delivered===true).map(item=>item.key);
    if(delivered.length) await env.DB.prepare(`UPDATE cgv_delivery SET status='delivered',delivered_at=?
      WHERE session_key IN (${delivered.map(()=>'?').join(',')}) AND status!='delivered'`)
      .bind(new Date(now).toISOString(),...delivered).run();
  }else if(op==='claim'){
    const limit=await env.DB.prepare("SELECT until_ms FROM cgv_delivery_limits WHERE name='discord'").first();
    retryUntil=Number(limit?.until_ms || 0);
    if(retryUntil<=now){
      const change=await env.DB.prepare(`UPDATE cgv_delivery SET status='pending',owner=?,next_attempt=?,attempts=attempts+1
        WHERE session_key IN (${slots}) AND status IN ('new','retry','pending') AND next_attempt<=?
        RETURNING session_key`).bind(owner,now+UNKNOWN_MS,...keys,now).all();
      granted=new Set((change.results||[]).map(row=>row.session_key));
    }
  }else if(op==='complete'){
    await env.DB.prepare(`UPDATE cgv_delivery SET status='delivered',delivered_at=?,next_attempt=0
      WHERE session_key IN (${slots}) AND owner=? AND status='pending'`).bind(new Date(now).toISOString(),...keys,owner).run();
  }else{
    const value=Number(body.retry_seconds),seconds=Number.isFinite(value)?Math.max(0,value):0;
    const delay=body.uncertain===true?UNKNOWN_MS:Math.max(300000,seconds*1000);
    await env.DB.prepare(`UPDATE cgv_delivery SET status='retry',next_attempt=?
      WHERE session_key IN (${slots}) AND owner=? AND status='pending'`).bind(now+delay,...keys,owner).run();
    if(Number(body.http_status)===429) await env.DB.prepare(`INSERT INTO cgv_delivery_limits(name,until_ms) VALUES ('discord',?)
      ON CONFLICT(name) DO UPDATE SET until_ms=MAX(until_ms,excluded.until_ms)`).bind(now+Math.max(300000,seconds*1000)).run();
  }
  const rows=await env.DB.prepare(`SELECT session_key,status,next_attempt FROM cgv_delivery WHERE session_key IN (${slots})`).bind(...keys).all();
  const byKey=new Map((rows.results||[]).map(row=>[row.session_key,row]));
  return {ok:true,retry_until:retryUntil,items:keys.map(key=>({key,
    status:granted.has(key)?'granted':byKey.get(key)?.status||'unknown',next_attempt:byKey.get(key)?.next_attempt||0}))};
}
