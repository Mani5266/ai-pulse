/**
 * The Worker is the only code a reader of this project touches directly, and it is the one
 * piece not covered by the Python suite. These tests run the real `fetch` handler with the
 * network stubbed, so routing, authentication, command dispatch and the two decisions the
 * Worker genuinely owns — greeting and staleness — are all exercised.
 *
 *   node --test        (from worker/)
 */

import assert from "node:assert/strict";
import { after, beforeEach, describe, it } from "node:test";

import worker from "./index.js";

const FEED_URL = "https://example.invalid/bot.json";
const OWNER = "6706372259";
const STRANGER = "999999999";
const SECRET = "correct-horse-battery-staple";

const ENV = {
  FEED_URL,
  OWNER_CHAT_ID: OWNER,
  WEBHOOK_SECRET: SECRET,
  TELEGRAM_BOT_TOKEN: "123:ABC",
};

/** A feed shaped like the one app/delivery/bot_feed.py writes. */
function feed(overrides = {}) {
  return {
    latest: "🤖 AI-PULSE · today\n\nGemma 4 released",
    generated_at: new Date().toISOString(),
    day: "2026-08-27",
    help: "HELP TEXT",
    owner_only: "OWNER ONLY",
    status: "STATUS TEXT",
    no_briefing: "NO BRIEFING YET",
    greetings: ["hi", "hello", "hey", "good"],
    greeting_prefix: "👋 Hi. Here is today's briefing.\n\n",
    ...overrides,
  };
}

const realFetch = globalThis.fetch;
let sent;

/**
 * Stub the two calls the Worker makes: reading the feed, and sending a message.
 * `feedResponse` lets a test make the feed unavailable.
 */
function stubNetwork({ feedBody = feed(), feedStatus = 200, sendStatus = 200 } = {}) {
  sent = [];
  globalThis.fetch = async (url, init) => {
    if (String(url) === FEED_URL) {
      return new Response(JSON.stringify(feedBody), { status: feedStatus });
    }
    sent.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ ok: sendStatus === 200 }), { status: sendStatus });
  };
}

/** One Telegram update, as the webhook would deliver it. */
function post(text, { chatId = OWNER, secret = SECRET, body = null } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (secret !== null) {
    headers["X-Telegram-Bot-Api-Secret-Token"] = secret;
  }
  return worker.fetch(
    new Request("https://bot.invalid/", {
      method: "POST",
      headers,
      body: body ?? JSON.stringify({ update_id: 1, message: { chat: { id: chatId }, text } }),
    }),
    ENV,
  );
}

beforeEach(() => stubNetwork());
after(() => {
  globalThis.fetch = realFetch;
});

describe("the endpoint", () => {
  it("does not advertise itself to a GET", async () => {
    const response = await worker.fetch(new Request("https://bot.invalid/"), ENV);
    assert.equal(response.status, 404);
  });

  it("refuses a POST with no secret header", async () => {
    const response = await post("hello", { secret: null });
    assert.equal(response.status, 403);
    assert.equal(sent.length, 0);
  });

  it("refuses a POST with the wrong secret", async () => {
    // Without this check the URL alone is enough to make the bot message its owner.
    const response = await post("hello", { secret: "guessed" });
    assert.equal(response.status, 403);
    assert.equal(sent.length, 0);
  });

  it("answers an unparseable body 200 rather than inviting retries forever", async () => {
    const response = await post(null, { body: "not json" });
    assert.equal(response.status, 200);
    assert.equal(sent.length, 0);
  });

  it("ignores an update carrying no chat", async () => {
    const response = await post(null, { body: JSON.stringify({ update_id: 1 }) });
    assert.equal(response.status, 200);
    assert.equal(sent.length, 0);
  });
});

describe("what it answers", () => {
  it("returns the briefing for any ordinary message", async () => {
    await post("whats today news");
    assert.match(sent[0].text, /Gemma 4 released/);
  });

  it("sends with HTML parse mode and no link previews", async () => {
    // The briefing links its own sources; previews would bury them in thumbnails.
    await post("news");
    assert.equal(sent[0].parse_mode, "HTML");
    assert.equal(sent[0].link_preview_options.is_disabled, true);
  });

  it("returns help for /start and /help", async () => {
    await post("/help");
    assert.equal(sent[0].text, "HELP TEXT");
  });

  it("gives the owner /status", async () => {
    await post("/status");
    assert.equal(sent[0].text, "STATUS TEXT");
  });

  it("refuses /status for anyone else", async () => {
    await post("/status", { chatId: STRANGER });
    assert.equal(sent[0].text, "OWNER ONLY");
  });

  it("tells the owner that /refresh cannot run here", async () => {
    await post("/refresh");
    assert.match(sent[0].text, /not available/i);
  });

  it("refuses /refresh for anyone else without explaining the internals", async () => {
    await post("/refresh", { chatId: STRANGER });
    assert.equal(sent[0].text, "OWNER ONLY");
  });

  it("says so when no briefing has been published", async () => {
    stubNetwork({ feedBody: feed({ latest: "" }) });
    await post("news");
    assert.equal(sent[0].text, "NO BRIEFING YET");
  });
});

describe("greeting", () => {
  it("greets before the briefing", async () => {
    await post("hi");
    assert.match(sent[0].text, /^👋 Hi\./);
    assert.match(sent[0].text, /Gemma 4 released/);
  });

  it("greets a greeting that carries a question", async () => {
    await post("hello, what happened today?");
    assert.match(sent[0].text, /^👋 Hi\./);
  });

  it("ignores punctuation around the greeting", async () => {
    await post("hi!");
    assert.match(sent[0].text, /^👋 Hi\./);
  });

  it("does not greet a word that merely starts like one", async () => {
    // "hiring" begins with "hi". Prefix matching would greet half the news.
    await post("hiring freeze at OpenAI");
    assert.doesNotMatch(sent[0].text, /^👋/);
  });

  it("does not greet /latest", async () => {
    await post("/latest");
    assert.doesNotMatch(sent[0].text, /^👋/);
  });

  it("survives a feed published before greetings existed", async () => {
    // An older bot.json has no greetings key. The bot must answer, not throw.
    stubNetwork({ feedBody: feed({ greetings: undefined, greeting_prefix: undefined }) });
    await post("hi");
    assert.match(sent[0].text, /Gemma 4 released/);
  });
});

describe("staleness", () => {
  it("says nothing about a fresh briefing", async () => {
    await post("news");
    assert.doesNotMatch(sent[0].text, /hours old/);
  });

  it("says how old a stale briefing is", async () => {
    const old = new Date(Date.now() - 50 * 3_600_000).toISOString();
    stubNetwork({ feedBody: feed({ generated_at: old }) });
    await post("news");
    assert.match(sent[0].text, /50 hours old/);
  });

  it("says nothing when the timestamp is unreadable", async () => {
    stubNetwork({ feedBody: feed({ generated_at: "not a date" }) });
    await post("news");
    assert.doesNotMatch(sent[0].text, /hours old/);
  });
});

describe("when the feed cannot be read", () => {
  it("answers instead of going silent", async () => {
    // Pages serves a 404 for a moment during every deploy. Silence would leave the reader
    // unable to tell a broken deployment from a message that never arrived.
    stubNetwork({ feedStatus: 404 });
    const response = await post("news");
    assert.equal(response.status, 200);
    assert.equal(sent.length, 1);
    assert.match(sent[0].text, /Could not reach the briefing/);
  });

  it("still returns 200, so Telegram does not retry the update forever", async () => {
    stubNetwork({ feedStatus: 500 });
    const response = await post("hi");
    assert.equal(response.status, 200);
  });
});
