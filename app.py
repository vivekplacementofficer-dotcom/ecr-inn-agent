"""
ECR Inn WhatsApp booking agent.

What this does:
  Guest sends a WhatsApp message -> Twilio forwards it here ->
  this server asks Claude (using ECR Inn's knowledge base + system prompt) ->
  the reply is sent back to the guest on WhatsApp.

You normally do NOT need to edit this file. Edit knowledge-base.md instead.
Setup steps are in SETUP-GUIDE.md.
"""

import os
from pathlib import Path

from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import anthropic

# ---------------------------------------------------------------------------
# Configuration (these come from environment variables / the .env file)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

# Where to send a heads-up when the agent needs a human (your WhatsApp number).
# Format: "whatsapp:+91XXXXXXXXXX". Leave blank to disable handoff alerts.
STAFF_WHATSAPP_NUMBER = os.environ.get("STAFF_WHATSAPP_NUMBER", "")
# Your Twilio WhatsApp sender, e.g. the sandbox number "whatsapp:+14155238886".
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

BASE_DIR = Path(__file__).parent
SYSTEM_PROMPT = (BASE_DIR / "system-prompt.md").read_text(encoding="utf-8")
KNOWLEDGE_BASE = (BASE_DIR / "knowledge-base.md").read_text(encoding="utf-8")

# Combine the agent instructions with the inn's details into one system prompt.
FULL_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\n# ECR INN KNOWLEDGE BASE (source of truth)\n\n"
    + KNOWLEDGE_BASE
)

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN
    else None
)

# Simple in-memory conversation memory: { guest_phone: [ {role, content}, ... ] }
# Note: this resets if the server restarts. Fine to start; see SETUP-GUIDE.md
# for the note about upgrading to a database later.
conversations = {}
MAX_TURNS = 20  # keep the last 20 messages per guest to control cost


def ask_claude(guest_id, guest_message):
    """Send the guest's message (plus recent history) to Claude and return the reply."""
    history = conversations.get(guest_id, [])
    history.append({"role": "user", "content": guest_message})

    response = claude.messages.create(
        model=MODEL,
        max_tokens=600,
        system=FULL_SYSTEM_PROMPT,
        messages=history[-MAX_TURNS:],
    )
    reply = response.content[0].text.strip()

    history.append({"role": "assistant", "content": reply})
    conversations[guest_id] = history[-MAX_TURNS:]
    return reply


def maybe_alert_staff(guest_id, guest_message, reply):
    """If the reply suggests a handoff, send the staff a WhatsApp heads-up."""
    if not (twilio_client and STAFF_WHATSAPP_NUMBER and TWILIO_WHATSAPP_FROM):
        return
    handoff_signals = ["team member", "get right back", "check with our team"]
    if any(s in reply.lower() for s in handoff_signals):
        try:
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=STAFF_WHATSAPP_NUMBER,
                body=(
                    f"🔔 ECR Inn agent needs you.\n"
                    f"Guest: {guest_id}\n"
                    f"Said: {guest_message}\n"
                    f"Agent replied: {reply}"
                ),
            )
        except Exception as exc:  # don't let an alert failure break the guest reply
            app.logger.warning("Staff alert failed: %s", exc)


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Twilio sends incoming WhatsApp messages here."""
    guest_id = request.form.get("From", "unknown")
    guest_message = (request.form.get("Body") or "").strip()

    if not guest_message:
        reply = "Hi! 🙏 Welcome to ECR Inn. How can I help with your stay?"
    else:
        try:
            reply = ask_claude(guest_id, guest_message)
            maybe_alert_staff(guest_id, guest_message, reply)
        except Exception as exc:
            app.logger.error("Claude call failed: %s", exc)
            reply = (
                "Sorry, I had a small hiccup. A team member will get back to "
                "you shortly. 🙏"
            )

    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(str(twiml), mimetype="application/xml")


@app.route("/", methods=["GET"])
def health():
    """Open this in a browser to check the server is alive."""
    return "ECR Inn WhatsApp agent is running. ✅"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
