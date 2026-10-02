const { test } = require('node:test');
const assert = require('node:assert/strict');
const { consumeMessageStream } = require('../dist/main/message-stream.js');
const session_id = 'a'.repeat(32);
const frame = (event, payload) => JSON.stringify({ event, payload }) + '\n';
const complete = frame('complete', { session_id });
function stream(chunks) {
  return new ReadableStream({ start(controller) {
    chunks.forEach(chunk => controller.enqueue(new TextEncoder().encode(chunk)));
    controller.close();
  }});
}
test('fragmented frames are delivered in order before terminal result', async () => {
  const first = frame('assistant_delta', { text: 'hello', iteration: 1 });
  const events = [];
  const result = await consumeMessageStream(stream([first.slice(0, 13), first.slice(13) + complete]), async event => events.push(event));
  assert.equal(events[0].payload.text, 'hello');
  assert.equal(result.session_id, session_id);
});
test('does not deliver another frame before consumer acknowledgement', async () => {
  let release;
  const gate = new Promise(resolve => release = resolve);
  const received = [];
  const run = consumeMessageStream(stream([
    frame('assistant_delta', { text: 'one' }) + frame('assistant_delta', { text: 'two' }) + complete,
  ]), async event => { received.push(event.payload.text); await gate; });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(received, ['one']);
  release();
  await run;
  assert.deepEqual(received, ['one', 'two']);
});
test('truncation, errors and oversized frames fail explicitly', async () => {
  await assert.rejects(consumeMessageStream(stream([frame('heartbeat')]), async () => {}), /before completion/);
  await assert.rejects(consumeMessageStream(stream([frame('error', { message: 'secret' })]), async () => {}), /inspect the conversation/);
  await assert.rejects(consumeMessageStream(stream(['x'.repeat(4 * 1024 * 1024 + 1)]), async () => {}), /buffer exceeded/);
});
test('consumer failure cancels the HTTP reader', async () => {
  let cancelled = false;
  const source = new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode(frame('assistant_delta', { text: 'one' })));
  }, cancel() { cancelled = true; }});
  await assert.rejects(consumeMessageStream(source, async () => { throw new Error('closed'); }), /closed/);
  assert.equal(cancelled, true);
});
