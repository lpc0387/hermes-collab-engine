#!/usr/bin/env node
/**
 * Anthropic JS SDK test — /v1/messages
 *
 * Usage:
 *   node anthropic-test.js <base_url> <model>
 *
 * Examples:
 *   node anthropic-test.js http://localhost:8080 deepseek-v4-flash-free
 *   node anthropic-test.js http://localhost:8080 minimax-m2.5
 *
 * If the model doesn't support the Claude format upstream (free models),
 * the test verifies the proxy routes the request correctly (upstream
 * returns a meaningful error, not a 502/404 from the proxy).
 * Set OPCODE_UPSTREAM_TOKEN for paid models that need auth.
 * Set OPCODE_API_KEY if the proxy requires auth.
 */

import Anthropic from "@anthropic-ai/sdk";

const BASE  = process.argv[2] || "http://localhost:8080";
const MODEL = process.argv[3] || "deepseek-v4-flash-free";

let passed = 0;
let failed = 0;
let skipped = 0;

function ok(name) { passed++; console.log(`  ✓ ${name}`); }
function fail(name, err) { failed++; console.log(`  ✗ ${name}: ${err?.message ?? err}`); }
function skip(name, reason) { skipped++; console.log(`  - ${name}: SKIP (${reason})`); }

// The Anthropic SDK sends x-api-key by default. For opencode upstream
// the token goes in x-api-key for /v1/messages.
const KEY = process.env.OPCODE_API_KEY || "public";
const client = new Anthropic({ baseURL: BASE, apiKey: KEY });

async function main() {
  let timing;

  // ── 1. Non-streaming ────────────────────────────────────────────
  console.log("\n=== 1. Non-streaming messages (Anthropic SDK) ===");
  try {
    timing = Date.now();
    const res = await client.messages.create({
      model: MODEL,
      max_tokens: 256,
      messages: [{ role: "user", content: "reply with exactly one word: hello" }],
    });
    const elapsed = Date.now() - timing;
    const text = res.content.map(c => c.text).join("");
    if (res.stop_reason && text.length > 0) {
      ok(`response in ${elapsed}ms: "${text.slice(0, 60)}"`);
    } else {
      fail("non-stream", `stop_reason=${res.stop_reason} content="${text.slice(0, 60)}"`);
    }
  } catch (e) {
    // If upstream returned 400/401, the proxy routed correctly.
    // 401 = model needs real API key; 400 = model doesn't support Claude format.
    if (e.status === 401) {
      skip("non-stream", `upstream requires valid API key for model "${MODEL}" (HTTP 401)`);
    } else if (e.status === 400) {
      skip("non-stream", `model "${MODEL}" does not support /v1/messages upstream (HTTP 400)`);
    } else {
      fail("non-stream", e);
    }
  }

  // ── 2. Streaming ────────────────────────────────────────────────
  console.log("\n=== 2. Streaming messages (Anthropic SDK) ===");
  try {
    timing = Date.now();
    const stream = client.messages.stream({
      model: MODEL,
      max_tokens: 256,
      messages: [{ role: "user", content: "reply with exactly one word: hello" }],
    });
    let sawText = false;
    for await (const event of stream) {
      if (event.type === "content_block_delta" && event.delta?.text) sawText = true;
    }
    const finalMsg = await stream.finalMessage();
    const elapsed = Date.now() - timing;
    if (finalMsg.stop_reason && sawText) {
      ok(`stream complete in ${elapsed}ms, stop_reason=${finalMsg.stop_reason}`);
    } else {
      fail("stream", `stop_reason=${finalMsg.stop_reason} sawText=${sawText}`);
    }
  } catch (e) {
    if (e.status === 401) {
      skip("stream", `upstream requires valid API key for model "${MODEL}" (HTTP 401)`);
    } else if (e.status === 400) {
      skip("stream", `model "${MODEL}" does not support /v1/messages upstream (HTTP 400)`);
    } else {
      fail("stream", e);
    }
  }

  // ── Summary ─────────────────────────────────────────────────────
  console.log(`\n============================`);
  console.log(`Results: ${passed} passed, ${failed} failed, ${skipped} skipped`);
  console.log(`============================`);
  process.exit(failed ? 1 : 0);
}

main();
