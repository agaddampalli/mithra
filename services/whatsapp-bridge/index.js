import express from "express";
import { createRequire } from "module";
import { makeWASocket, useMultiFileAuthState, DisconnectReason } from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import pino from "pino";

const require = createRequire(import.meta.url);
const SESSION_PATH = process.env.SESSION_PATH || "/sessions/whatsapp";
const PORT = parseInt(process.env.PORT || "3000", 10);

const logger = pino({ level: "warn" });

let sock = null;
let isConnected = false;

async function startBaileys() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_PATH);

  sock = makeWASocket({
    auth: state,
    logger,
    printQRInTerminal: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log("Scan the QR code below to connect WhatsApp:");
      qrcode.generate(qr, { small: true });
    }

    if (connection === "open") {
      isConnected = true;
      console.log("WhatsApp connected.");
    }

    if (connection === "close") {
      isConnected = false;
      const shouldReconnect =
        lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
      console.log("WhatsApp disconnected. Reconnect:", shouldReconnect);
      if (shouldReconnect) {
        setTimeout(startBaileys, 3000);
      }
    }
  });
}

startBaileys().catch((err) => {
  console.error("Baileys startup error:", err);
  process.exit(1);
});

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  res.json({ status: "ok", connected: isConnected });
});

app.post("/send", async (req, res) => {
  const { phone, text } = req.body || {};

  if (!phone || !text) {
    return res.status(400).json({ success: false, error: "phone and text are required" });
  }

  if (!isConnected || !sock) {
    return res.status(503).json({ success: false, error: "WhatsApp not connected" });
  }

  try {
    // Baileys expects JID format: "<phone>@s.whatsapp.net"
    const jid = phone.includes("@") ? phone : `${phone}@s.whatsapp.net`;
    const result = await sock.sendMessage(jid, { text });
    res.json({ success: true, message_id: result?.key?.id || "" });
  } catch (err) {
    res.status(500).json({ success: false, error: err.message });
  }
});

app.listen(PORT, () => {
  console.log(`whatsapp-bridge listening on port ${PORT}`);
});
