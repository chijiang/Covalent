import test from "node:test";
import assert from "node:assert/strict";
import { emptyStream, applyStreamEvent } from "../src/stream-state.ts";
const event = (event, text, iteration = 1, agent_name) => ({ event, payload: { text, iteration, agent_name } });
test("deltas append, full assistant does not duplicate, new iteration replaces", () => {
  let state = emptyStream();
  state = applyStreamEvent(state, event("assistant_delta", "hello "));
  state = applyStreamEvent(state, event("assistant_delta", "world"));
  state = applyStreamEvent(state, event("assistant", "hello world"));
  assert.equal(state.text, "hello world");
  state = applyStreamEvent(state, event("assistant_delta", "next", 2));
  assert.equal(state.text, "next");
});
test("non-streaming provider fallback and delegated reasoning", () => {
  let state = applyStreamEvent(emptyStream(), event("assistant", "fallback"));
  assert.equal(state.text, "fallback");
  state = applyStreamEvent(state, event("delegate_reasoning_delta", "think", 1, "helper"));
  state = applyStreamEvent(state, event("reasoning_delta", "answer"));
  assert.equal(state.reasoning, "\x1e#agent:helper\x1ethink\x1e#agent:\x1eanswer");
});
test("live output is bounded", () => {
  assert.throws(() => applyStreamEvent(emptyStream(), event("assistant_delta", "x".repeat(2 * 1024 * 1024 + 1))), /limit/);
});
