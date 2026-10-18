import { EmailMessage } from "cloudflare:email";

const FROM = "darkfac@ggcampos.com";
const TO = "guigrokchief@mail.grokbot.com";

function b64(s) {
  let bin = "";
  for (const b of new TextEncoder().encode(s)) bin += String.fromCharCode(b);
  return btoa(bin);
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Method Not Allowed", { status: 405 });
    if (request.headers.get("Authorization") !== `Bearer ${env.MAILER_TOKEN}`) {
      return new Response("Unauthorized", { status: 401 });
    }
    const { subject = "DarkFac", text = "" } = await request.json();
    const body = (b64(text).match(/.{1,76}/g) || [""]).join("\r\n");
    const raw = [
      `From: DarkFac <${FROM}>`,
      `To: ${TO}`,
      `Subject: =?UTF-8?B?${b64(subject)}?=`,
      `Message-ID: <${crypto.randomUUID()}@ggcampos.com>`,
      `Date: ${new Date().toUTCString()}`,
      "MIME-Version: 1.0",
      "Content-Type: text/plain; charset=UTF-8",
      "Content-Transfer-Encoding: base64",
      "",
      body,
    ].join("\r\n");
    try {
      await env.GROKBOT_MAIL.send(new EmailMessage(FROM, TO, raw));
    } catch (e) {
      return Response.json({ ok: false, error: String(e) }, { status: 502 });
    }
    return Response.json({ ok: true, to: TO });
  },
};
