import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, rename, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

const interval = 30_000;
const cooldown = 10 * 60_000;
const startupGrace = 90_000;

export function readAccount(config, request = fetch) {
  let credential;
  async function post(port, path, body, auth) {
    const res = await request('http://127.0.0.1:' + port + '/api/' + path, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { 'content-type': 'application/json', ...(auth && { authorization: 'Bearer ' + auth }) },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw Error('NapCat WebUI HTTP ' + res.status);
    const json = await res.json();
    if (json.code !== 0) throw Error('NapCat WebUI code ' + json.code);
    return json.data;
  }
  return async () => {
    const { token, port, autoLoginAccount } = await config();
    const account = String(autoLoginAccount || '');
    if (!/^\d+$/.test(account)) return { account: '', info: null };
    try {
      if (!credential) {
        const hash = createHash('sha256').update(token + '.napcat').digest('hex');
        const login = await post(port, 'auth/login', { hash });
        if (!login?.Credential) throw Error('missing WebUI credential');
        credential = login.Credential;
      }
      const info = await post(port, 'QQLogin/GetQQLoginInfo', {}, credential);
      return { account, info };
    } catch (error) {
      credential = undefined;
      throw error;
    }
  };
}

export function createMonitor({ read, load, save, restart, clock = Date.now }) {
  const started = clock();
  let seenOnline = false;
  let previousOfflineAccount = '';
  return async () => {
    const previous = previousOfflineAccount;
    previousOfflineAccount = '';
    const { account, info } = await read();
    if (!account) return 'automatic login disabled';
    if (!info || String(info.uin) !== account || typeof info.online !== 'boolean') {
      return 'waiting for the configured QQ session';
    }
    if (info.online) {
      seenOnline = true;
      return 'QQ online';
    }
    previousOfflineAccount = account;
    if (previous !== account) return 'first offline sample';
    const now = clock();
    if (!seenOnline && now - started < startupGrace) return 'waiting for startup login';
    const guard = await load();
    if (guard?.account === account && now - guard.at < cooldown) return 'offline; recovery cooldown';
    // A crash on either side of dispatch must rate-limit, but never lock out, the next attempt.
    await save({ account, at: now });
    await restart();
    return 'confirmed offline; requested NapCat restart for automatic login';
  };
}

async function main() {
  const config = join(homedir(), 'dafeiyu-live/napcat-program/resources/app/app_launcher/napcat/config/webui.json');
  const state = join(process.env.STATE_DIRECTORY, 'recovery.json');
  const check = createMonitor({
    read: readAccount(async () => JSON.parse(await readFile(config, 'utf8'))),
    load: async () => {
      try { return JSON.parse(await readFile(state, 'utf8')); }
      catch (error) { if (error.code === 'ENOENT') return null; throw error; }
    },
    save: async (record) => {
      await writeFile(state + '.new', JSON.stringify(record), { mode: 0o600 });
      await rename(state + '.new', state);
    },
    restart: () => promisify(execFile)('/usr/bin/systemctl',
      ['--user', 'try-restart', 'dafeiyu-napcat.service'], { timeout: 90_000 })
  });
  let lastStatus;
  while (true) {
    let status;
    try { status = await check(); }
    catch (error) { status = 'recovery check failed: ' + error.message; }
    if (status !== lastStatus) console.log(status);
    lastStatus = status;
    await sleep(interval);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
