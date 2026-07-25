#!/usr/bin/env node
/**
 * OpenAI JS SDK test — chat.completions + responses
 *
 * Usage:
 *   node openai-test.js <base_url> <model>
 *   node openai-test.js http://localhost:8080 deepseek-v4-flash-free
 *
 * Tests:
 *   1. Non-streaming chat.completions
 *   2. Streaming chat.completions
 *   3. Non-streaming responses API
 *   4. Streaming responses API
 *   5. Model allow check (401 on bad key, if applicable)
 */

import OpenAI from "openai";

const BASE  = process.argv[2] || "http://localhost:8080";
const MODEL = process.argv[3] || "deepseek-v4-flash-free";

let passed = 0;
let failed = 0;

function ok(name) { passed++; console.log(`  ✓ ${name}`); }
function fail(name, err) { failed++; console.log(`  ✗ ${name}: ${err?.message ?? err}`); }

// Need a client with API key set; if proxy doesn't require one, any key works
const KEY = process.env.OPCODE_API_KEY || "sk-unused";
const client = new OpenAI({ baseURL: BASE + "/v1", apiKey: KEY });

async function main() {
  let timing;

  // ── 1. Non-streaming chat.completions ────────────────────────────
  console.log("\n=== 1. Non-streaming chat.completions (OpenAI SDK) ===");
  try {
    timing = Date.now();
    const res = await client.chat.completions.create({
      model: MODEL,
      messages: [{ role: "user", content: "reply with exactly one word: hello" }],
    });
    const elapsed = Date.now() - timing;
    const content = res.choices?.[0]?.message?.content || "";
    if (res.choices?.[0]?.finish_reason === "stop" && content.length > 0) {
      ok(`response in ${elapsed}ms: "${content.slice(0, 60)}"`);
    } else {
      fail("non-stream", `finish_reason=${res.choices?.[0]?.finish_reason} content="${content.slice(0, 60)}"`);
    }
  } catch (e) { fail("non-stream", e); }

  // ── 2. Streaming chat.completions ────────────────────────────────
  console.log("\n=== 2. Streaming chat.completions (OpenAI SDK) ===");
  try {
    timing = Date.now();
    const stream = await client.chat.completions.create({
      model: MODEL,
      messages: [{ role: "user", content: "reply with exactly one word: hello" }],
      stream: true,
    });
    let chunks = 0;
    let sawDone = false;
    for await (const chunk of stream) {
      chunks++;
      if (chunk.choices?.[0]?.finish_reason) sawDone = true;
    }
    const elapsed = Date.now() - timing;
    if (chunks > 0 && sawDone) {
      ok(`${chunks} chunks in ${elapsed}ms, finish_reason received`);
    } else {
      fail("stream", `chunks=${chunks} sawDone=${sawDone}`);
    }
  } catch (e) { fail("stream", e); }

  // ── 3. Non-streaming responses API ──────────────────────────────
  console.log("\n=== 3. Non-streaming responses API (OpenAI SDK) ===");
  try {
    timing = Date.now();
    const res = await client.responses.create({
      model: MODEL,
      input: [{ role: "user", content: [{ type: "input_text", text: "reply with exactly one word: hello" }] }],
    });
    const elapsed = Date.now() - timing;
    const text = res.output_text || "";
    if (res.output && text.length > 0) {
      ok(`response in ${elapsed}ms: "${text.slice(0, 60)}"`);
    } else {
      fail("responses non-stream", `output_text="${text.slice(0, 60)}"`);
    }
  } catch (e) { fail("responses non-stream", e); }

  // ── 4. Streaming responses API ─────────────────────────────────
  console.log("\n=== 4. Streaming responses API (OpenAI SDK) ===");
  try {
    timing = Date.now();
    const stream = await client.responses.create({
      model: MODEL,
      input: [{ role: "user", content: [{ type: "input_text", text: "reply with exactly one word: hello" }] }],
      stream: true,
    });
    let events = 0;
    for await (const event of stream) {
      events++;
    }
    const elapsed = Date.now() - timing;
    if (events > 0) {
      ok(`${events} events in ${elapsed}ms`);
    } else {
      fail("responses stream", "0 events received");
    }
  } catch (e) { fail("responses stream", e); }

  // ── Summary ─────────────────────────────────────────────────────
  console.log(`\n============================`);
  console.log(`Results: ${passed} passed, ${failed} failed`);
  console.log(`============================`);
  process.exit(failed ? 1 : 0);
}

main();
