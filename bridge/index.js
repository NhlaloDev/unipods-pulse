// WhatsApp group bridge for Pulse.
// Logs into WhatsApp Web with a dedicated phone number (scan the QR once), then:
//   - streams every message in the watched groups into Pulse's knowledge base (live, no exports needed)
//   - replies in the group when the bot is @mentioned, when someone replies to the bot,
//     or when a message starts with "!pulse" / "!catchup"
//   - answers every direct message privately (good for "catch me up" without spamming the group)
// Note: this uses the unofficial whatsapp-web.js library. Use a dedicated number, not a personal one.
require("dotenv").config({ path: require("path").join(__dirname, "..", ".env") });
const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");

const PULSE_URL = (process.env.PULSE_URL || "http://localhost:8000").replace(/\/$/, "");
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || "";
const WATCH = (process.env.WATCH_GROUPS || "").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
const TRIGGER = /^!(pulse|ask|catchup|catch up|lastmeeting|last meeting|meetings|help)\b/i;

async function post(path, body) {
  const r = await fetch(PULSE_URL + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Admin-Token": ADMIN_TOKEN },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: require("path").join(__dirname, ".wwebjs_auth") }),
  puppeteer: { args: ["--no-sandbox", "--disable-setuid-sandbox"] },
});

client.on("qr", qr => { console.log("Scan this QR with the bot's WhatsApp (Linked devices):"); qrcode.generate(qr, { small: true }); });
client.on("ready", () => console.log(`Pulse bridge ready as ${client.info.pushname}. Watching: ${WATCH.length ? WATCH.join(", ") : "all groups"}`));
client.on("auth_failure", m => console.error("Auth failure:", m));

client.on("message", async msg => {
  try {
    if (msg.type !== "chat" || !msg.body) return; // text only; media is skipped
    const chat = await msg.getChat();
    const contact = await msg.getContact();
    const author = contact.pushname || contact.name || contact.number || "someone";
    const me = client.info.wid._serialized;

    if (chat.isGroup) {
      if (WATCH.length && !WATCH.includes(chat.name.toLowerCase())) return;
      await post("/api/ingest/message", {
        platform: "whatsapp", channel: chat.name, author, text: msg.body,
        ts: new Date(msg.timestamp * 1000).toISOString(),
      });
      let quotedBot = false;
      if (msg.hasQuotedMsg) quotedBot = (await msg.getQuotedMessage()).fromMe;
      const mentioned = (msg.mentionedIds || []).some(id => (id._serialized || id) === me);
      if (!mentioned && !quotedBot && !TRIGGER.test(msg.body)) return;
    }

    let text = msg.body.replace(/@\d+/g, "").trim();
    text = text.replace(/^!(pulse|ask)\b/i, "").replace(/^!/, "").trim() || "help";
    await chat.sendStateTyping();
    const { reply } = await post("/api/chat", { text, session_id: `wa:${chat.id._serialized}:${author}`, asker: author });
    await msg.reply(reply);
  } catch (e) {
    console.error("bridge error:", e.message);
  }
});

client.initialize();
