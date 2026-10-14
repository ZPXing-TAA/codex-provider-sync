import {spawn, spawnSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import http from 'node:http';
import {randomUUID, createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

// Isolated protocol probe: dummy credentials and a loopback-only endpoint.
const binary = process.env.CODEX_BINARY;
if (!binary) throw new Error('Set CODEX_BINARY to the installed Codex CLI binary');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-route-probe-'));
const id = randomUUID();
const captured = [];
const sockets = new Set();
let capture;
const capturedRequest = new Promise(resolve => {capture = resolve});
const httpServer = http.createServer((req, res) => {
  let raw = '';
  req.on('data', chunk => {raw += chunk});
  req.on('end', () => {
    if (req.method === 'POST') captured.push({path: req.url, auth: req.headers.authorization === 'Bearer probe-only', body: raw});
    res.writeHead(400, {'content-type': 'application/json'});
    res.end(JSON.stringify({error: {message: 'isolated probe complete', type: 'invalid_request_error'}}));
    if (req.method === 'POST') capture();
  });
});
httpServer.on('connection', socket => {sockets.add(socket); socket.on('close', () => sockets.delete(socket))});
function sendJson(socket, value) {
  const payload = Buffer.from(JSON.stringify(value));
  const header = Buffer.alloc(payload.length < 126 ? 2 : 4);
  header[0] = 129;
  if (payload.length < 126) header[1] = payload.length;
  else {header[1] = 126; header.writeUInt16BE(payload.length, 2)}
  socket.write(Buffer.concat([header, payload]));
}
httpServer.on('upgrade', (req, socket) => {
  const accept = createHash('sha1').update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let buffer = Buffer.alloc(0);
  socket.on('data', data => {
    buffer = Buffer.concat([buffer, data]);
    while (buffer.length >= 2) {
      let length = buffer[1] & 127, offset = 2;
      if (length === 126) {if (buffer.length < 4) return; length = buffer.readUInt16BE(2); offset = 4}
      else if (length === 127) {if (buffer.length < 10) return; length = Number(buffer.readBigUInt64BE(2)); offset = 10}
      const masked = !!(buffer[1] & 128);
      if (buffer.length < offset + (masked ? 4 : 0) + length) return;
      const mask = masked ? buffer.subarray(offset, offset + 4) : null;
      if (masked) offset += 4;
      const payload = Buffer.from(buffer.subarray(offset, offset + length));
      if (mask) for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
      buffer = buffer.subarray(offset + length);
      if (payload[0] === 123) {
        captured.push({path: req.url, auth: req.headers.authorization === 'Bearer probe-only', body: payload.toString()});
        if (payload.toString().includes('loopback probe')) capture();
        else sendJson(socket, {type: 'response.completed', response: {id: 'resp_probe', status: 'completed', output: [], usage: {input_tokens: 0, output_tokens: 0, total_tokens: 0}}});
      }
    }
  });
  socket.on('error', () => {});
});
await new Promise(resolve => httpServer.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${httpServer.address().port}/v1`;
fs.writeFileSync(path.join(home, 'config.toml'), `model_provider = "custom"\nmodel = "gpt-6.1-sol"\n[model_providers.custom]\nname = "Probe"\nbase_url = "${base}"\nrequires_openai_auth = true\nsupports_websockets = false\n[analytics]\nenabled = false\n`);
fs.writeFileSync(path.join(home, 'auth.json'), JSON.stringify({auth_mode: 'apikey', OPENAI_API_KEY: 'probe-only'}));
const sessionDir = path.join(home, 'sessions/2026/10/14');
fs.mkdirSync(sessionDir, {recursive: true});
const rollout = path.join(sessionDir, `rollout-2026-10-14T00-00-00-${id}.jsonl`);
const timestamp = '2026-10-14T00:00:00.000Z';
const records = [
  {timestamp, type: 'session_meta', payload: {id, timestamp, cwd: home, originator: 'codex_cli_rs', cli_version: '0.162.0', source: 'cli', model_provider: 'openai'}},
  {timestamp, type: 'response_item', payload: {type: 'message', role: 'user', content: [{type: 'input_text', text: 'retained original message'}]}},
  {timestamp, type: 'response_item', payload: {type: 'reasoning', id: 'rs_probe', summary: [], encrypted_content: 'opaque-probe-history'}},
];
const original = records.map(record => JSON.stringify(record) + '\n').join('');
fs.writeFileSync(rollout, original);
const sync = spawnSync(process.env.PYTHON || 'python3', ['-m', 'codex_provider_sync', '--codex-home', home, 'sync', '--no-quit', '--no-open'], {
  env: {...process.env, PYTHONPATH: path.join(root, 'src')}, encoding: 'utf8',
});
if (sync.status !== 0) throw new Error(`sync failed: ${sync.stderr}`);
if (fs.readFileSync(rollout, 'utf8') !== original) throw new Error('sync changed native history');
const p = spawn(binary, ['app-server'], {env: {PATH: process.env.PATH, HOME: home, CODEX_HOME: home, NO_PROXY: '127.0.0.1,localhost'}, stdio: ['pipe', 'pipe', 'pipe']});
let stderr = '';
p.stderr.on('data', data => {stderr += data});
const pending = new Map();
let nextId = 0;
readline.createInterface({input: p.stdout}).on('line', line => {
  let message;
  try {message = JSON.parse(line)} catch {return}
  if (message.id != null && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
});
function request(method, params) {
  const requestId = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(requestId); reject(new Error(`timeout ${method}: ${stderr.slice(-1500)}`))}, 12000);
    pending.set(requestId, message => {
      clearTimeout(timer);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
    });
    p.stdin.write(JSON.stringify({id: requestId, method, params}) + '\n');
  });
}
try {
  await request('initialize', {clientInfo: {name: 'route_probe', version: '0'}, capabilities: {experimentalApi: true}});
  p.stdin.write(JSON.stringify({method: 'initialized'}) + '\n');
  const resumed = await request('thread/resume', {threadId: id, path: rollout, modelProvider: 'openai', excludeTurns: true});
  await request('turn/start', {threadId: id, input: [{type: 'text', text: 'loopback probe'}]});
  let captureTimer;
  try {
    await Promise.race([capturedRequest, new Promise((_, reject) => {captureTimer = setTimeout(() => reject(new Error(`no loopback request: ${stderr.slice(-1500)}`)), 12000)})]);
  } finally {clearTimeout(captureTimer)}
  const capturedTurn = captured.find(request => request.body.includes('loopback probe'));
  const body = JSON.parse(capturedTurn.body);
  const result = {provider: resumed.modelProvider, path: capturedTurn.path, correctDummyAuth: capturedTurn.auth, originalPrefixPreserved: fs.readFileSync(rollout, 'utf8').startsWith(original), originalMessageSent: JSON.stringify(body).includes('retained original message'), opaqueReasoningSent: JSON.stringify(body).includes('opaque-probe-history')};
  console.log(JSON.stringify(result));
  if (!result.correctDummyAuth || !result.originalPrefixPreserved || !result.originalMessageSent || !result.opaqueReasoningSent) throw new Error('route/history probe failed');
} finally {
  p.stdin.end();
  if (p.exitCode === null) await new Promise(resolve => {p.once('exit', resolve); setTimeout(() => {p.kill(); resolve()}, 1000)});
  httpServer.closeAllConnections();
  for (const socket of sockets) socket.destroy();
  await new Promise(resolve => httpServer.close(resolve));
  fs.rmSync(home, {recursive: true, force: true});
}
