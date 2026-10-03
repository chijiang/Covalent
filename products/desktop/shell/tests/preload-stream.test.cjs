const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
function bridge() {
  let api, resolveRun;
  const ipc = new EventEmitter();
  const sends = [], calls = [];
  ipc.send = (...args) => sends.push(args);
  ipc.invoke = (channel, ...args) => {
    calls.push([channel, ...args]);
    return channel === 'desktop:send-message'
      ? new Promise(resolve => resolveRun = resolve) : Promise.resolve();
  };
  vm.runInNewContext(readFileSync(require.resolve('../dist/preload/index.js'), 'utf8'), {
    exports: {}, crypto: webcrypto,
    require: () => ({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_, value) => api = value } }),
  });
  return { api, ipc, sends, calls, finish: value => resolveRun(value) };
}
test('subscription precedes invoke, ignores other streams, acknowledges and cleans up', async () => {
  const b = bridge();
  const chunks = [];
  const run = b.api.streamMessage({ agent_name: 'helper', message: 'hi' }, chunk => chunks.push(chunk));
  assert.equal(b.ipc.listenerCount('desktop:message-event'), 1);
  const id = b.calls[0][2];
  b.ipc.emit('desktop:message-event', {}, 'different', 1, { event: 'assistant_delta' });
  assert.equal(chunks.length, 0);
  const chunk = { event: 'assistant_delta', payload: { text: 'hi' } };
  b.ipc.emit('desktop:message-event', {}, id, 1, chunk);
  assert.deepEqual(chunks, [chunk]);
  assert.deepEqual(b.sends, [['desktop:stream-ack', id, 1]]);
  b.finish({ session_id: 'a'.repeat(32) });
  await run;
  assert.equal(b.ipc.listenerCount('desktop:message-event'), 0);
});
test('consumer overflow cancels rather than acknowledging dropped output', async () => {
  const b = bridge();
  const run = b.api.streamMessage({ agent_name: 'helper', message: 'hi' }, () => { throw Error('limit'); });
  b.ipc.emit('desktop:message-event', {}, b.calls[0][2], 1, {});
  assert.equal(b.sends.length, 0);
  assert.equal(b.calls[1][0], 'desktop:cancel-message');
  b.finish({});
  await run;
});
