/**
 * The bot, as a webhook.
 *
 * Telegram POSTs an update here and this replies. There is no polling, no process to keep
 * alive and no machine to pay for: a Cloudflare Worker runs on request, and the free
 * allowance of 100,000 requests a day is far beyond what one person's bot will ever use.
 *
 * This file deliberately contains no project text and no formatting. Every reply the bot
 * can give is rendered by the daily pipeline and published to the static site as
 * `bot.json`; this fetches that file and picks a field. Two renderers in one project would
 * drift, and the Python one is the one under test.
 *
 * The single thing decided here is staleness, because it depends on when somebody asks
 * rather than on when the briefing was written. It is the counterpart of
 * `_staleness_note` in app/delivery/bot.py.
 *
 * Configure with:
 *   wrangler secret put TELEGRAM_BOT_TOKEN     the bot token
 *   wrangler secret put WEBHOOK_SECRET         a random string, also given to setWebhook
 *   wrangler secret put OWNER_CHAT_ID          your chat id
 *   [vars] FEED_URL in wrangler.toml           where bot.json is published
 */

const STALE_AFTER_HOURS = 36;
const FEED_TTL_SECONDS = 300;

/** Commands that belong to the owner. A guest is told why, rather than ignored. */
const OWNER_COMMANDS = new Set(["/status", "/refresh"]);

/** Refresh runs the pipeline, which no Worker can do. Said plainly rather than pretended. */
const REFRESH_UNAVAILABLE =
  "Rebuilding is not available here. The briefing is rebuilt by the daily run.";

export default {
  async fetch(request, env) {
    // Telegram only ever POSTs. Anything else is a scanner, a browser, or a mistake, and
    // none of them need to learn that this URL is a bot.
    if (request.method !== "POST") {
      return new Response("Not found", { status: 404 });
    }

    // Without this the endpoint is an open relay: anyone who guesses the URL can forge an
    // update and make the bot message the owner. Telegram sends the secret on every call
    // when setWebhook was given one.
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      // A 200 on a malformed body: Telegram retries anything else, forever, for an update
      // that will never parse.
      return new Response("ok");
    }

    const message = update.message ?? update.edited_message;
    const chatId = message?.chat?.id;
    if (!chatId) {
      return new Response("ok");
    }

    const text = (message.text ?? "").trim();
    const command = text.split(/\s+/)[0].toLowerCase();

    let reply;
    try {
      reply = await answer(command, String(chatId), env);
    } catch (error) {
      // The briefing is published and the site is up; this Worker failing to read it is
      // not worth a retry storm from Telegram.
      console.log(`feed unavailable: ${error}`);
      return new Response("ok");
    }

    if (reply) {
      await send(chatId, reply, env);
    }
    return new Response("ok");
  },
};

/** What to say, given a command and who is asking. */
async function answer(command, chatId, env) {
  const feed = await loadFeed(env);

  if (command === "/refresh") {
    return chatId === env.OWNER_CHAT_ID ? REFRESH_UNAVAILABLE : feed.owner_only;
  }
  if (OWNER_COMMANDS.has(command)) {
    return chatId === env.OWNER_CHAT_ID ? feed.status : feed.owner_only;
  }
  if (command === "/start" || command === "/help") {
    return feed.help;
  }
  if (!feed.latest) {
    return feed.no_briefing;
  }
  return greetingPrefix(command, feed) + feed.latest + stalenessNote(feed.generated_at);
}

/**
 * Say hello back, when the message was a hello.
 *
 * Answering "hi" with five stories and no acknowledgement reads like a machine that did
 * not hear you. The words and the line are both published in the feed, so this decides
 * only whether to use them — the local bot in app/delivery/bot.py greets on the same list.
 */
function greetingPrefix(command, feed) {
  // Both ends, to match Python's str.strip(".,!?;:") exactly. A list shared between two
  // implementations is only shared if they normalise the same way.
  const word = command.replace(/^[.,!?;:]+/, "").replace(/[.,!?;:]+$/, "");
  return (feed.greetings ?? []).includes(word) ? (feed.greeting_prefix ?? "") : "";
}

/**
 * The published answers.
 *
 * Cached for five minutes at the edge. The file changes once a day, so almost every reply
 * is served without a second network hop, and a burst of messages costs one fetch.
 */
async function loadFeed(env) {
  const response = await fetch(env.FEED_URL, {
    cf: { cacheTtl: FEED_TTL_SECONDS, cacheEverything: true },
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return await response.json();
}

/**
 * How old the briefing is, when that is worth saying.
 *
 * The publisher cannot compute this: it writes the file once and the file is read for a
 * day. Silent below the threshold, because a note on every fresh briefing is noise.
 */
function stalenessNote(generatedAt) {
  const generated = Date.parse(generatedAt);
  if (Number.isNaN(generated)) {
    return "";
  }
  const hours = (Date.now() - generated) / 3_600_000;
  if (hours < STALE_AFTER_HOURS) {
    return "";
  }
  return `\n\n<i>This briefing is ${Math.round(hours)} hours old.</i>`;
}

/** One message. Failure is logged, never thrown: Telegram would retry the whole update. */
async function send(chatId, text, env) {
  const response = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        // The briefing links its own sources; previews would bury them in thumbnails.
        link_preview_options: { is_disabled: true },
      }),
    },
  );
  if (!response.ok) {
    // Never log the URL: the bot token is in its path.
    console.log(`telegram: HTTP ${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
}
