import assert from 'node:assert/strict';
import test from 'node:test';
import { createMonitor, readAccount } from '../deploy/dsh-companion/qq-autologin.mjs';

const account = '10001';
const online = { uin: account, online: true };
const offline = { uin: account, online: false };
const waiting = { uin: '' };

function fixture(storedGuard = null) {
  let info = online, now = 1_000_000, restarts = 0, fail = false;
  const monitor = createMonitor({
    read: async () => ({ account, info }),
    load: async () => storedGuard,
    save: async record => { storedGuard = record; },
    restart: async () => { if (fail) throw Error('restart failed'); restarts++; },
    clock: () => now
  });
  return {
    step(next) { info = next; now += 30_000; return monitor(); },
    skip(ms) { now += ms; }, fail(value) { fail = value; },
    count: () => restarts, guard: () => storedGuard
  };
}

test('quiet group, missing status, another account and one transient drop never restart QQ', async () => {
  const f = fixture();
  for (const info of [online, offline, online, offline, waiting, offline, {uin:'10002',online:false}, offline, undefined, online]) {
    await f.step(info);
  }
  assert.equal(f.count(),0);
});

test('QR wait survives monitor restart, then an offline session can recover again', async () => {
  const f = fixture();
  await f.step(online); await f.step(offline); await f.step(offline); assert.equal(f.count(),1);
  const g = fixture(f.guard());
  g.skip(900_000);
  for (const info of [waiting, waiting, waiting, waiting]) await g.step(info);
  assert.equal(g.count(),0);
  for (const info of [online, offline, offline]) await g.step(info);
  assert.equal(g.count(),1);
});

test('rapid flapping is rate limited; a failed systemd command is retried after cooldown', async () => {
  const f = fixture();
  await f.step(online); await f.step(offline); f.fail(true); await assert.rejects(f.step(offline));
  f.fail(false); await f.step(offline); assert.equal(f.count(),0);
  f.skip(610_000); await f.step(offline); assert.equal(f.count(),1);
  await f.step(online); await f.step(offline); await f.step(offline);
  assert.equal(f.count(),1);
  f.skip(610_000); await f.step(offline); assert.equal(f.count(),2);
});

test('WebUI reader uses the kernel online flag after a kick, caches its credential, and skips a disabled account', async () => {
  const paths = [];
  const config = {port:6099, token:'fixture-only', autoLoginAccount:account};
  const read = readAccount(async () => config, async (url, opts) => {
    paths.push(new URL(url).pathname);
    assert.equal(new URL(url).hostname,'127.0.0.1');
    const login = url.endsWith('/auth/login');
    if (!login) assert.equal(opts.headers.authorization,'Bearer fixture');
    return Response.json({code:0, data:login?{Credential:'fixture'}:offline});
  });
  assert.deepEqual(await read(),{account,info:offline}); await read();
  assert.deepEqual(paths,['/api/auth/login','/api/QQLogin/GetQQLoginInfo','/api/QQLogin/GetQQLoginInfo']);
  config.autoLoginAccount=''; assert.equal((await read()).account,''); assert.equal(paths.length,3);
});

test('persistently offline after an acknowledged command retries after the cooldown', async () => {
  const f = fixture();
  await f.step(online); await f.step(offline); await f.step(offline);
  await f.step(offline); assert.equal(f.count(), 1);
  f.skip(610_000); await f.step(offline); assert.equal(f.count(), 2);
});

test('a saved pre-dispatch guard survives a crash without locking recovery forever', async () => {
  const f = fixture({account, at:1_000_000, awaitingLogin:true});
  f.skip(610_000); await f.step(offline); await f.step(offline);
  assert.equal(f.count(), 1);
});

test('a fresh monitor gives startup a grace period and leaves pending verification alone', async () => {
  const f = fixture();
  await f.step(offline); await f.step(offline); assert.equal(f.count(), 0);
  await f.step(waiting); f.skip(3_600_000); await f.step(waiting); assert.equal(f.count(), 0);
  await f.step(offline); await f.step(offline); assert.equal(f.count(), 1);
});
