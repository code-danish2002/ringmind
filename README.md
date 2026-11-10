# RingMind

**RingMind** is a real-time AI phone agent. Its voice, **Maya**, is the phone assistant for **Nimbus Mobile**, a demo mobile carrier. Callers talk to her in real time. She looks up their account, verifies who they are, answers billing and order questions, cancels orders, books store appointments, answers policy questions from the company documents, opens tickets, schedules callbacks, and hangs up when the caller is done. Every call is recorded with its transcript in an admin dashboard.

All LLMs are free-tier models (Groq first, with GLM, Cerebras and Gemini as fallbacks). Deepgram handles speech, and the whole stack runs in Docker.

```
 Caller ── Twilio number ───────────── WebSocket (µ-law 8 kHz) ──┐
 Caller ── softphone ─ Asterisk (Docker) ─ AudioSocket (TCP) ────┤
 Browser ─ /talk page ──────────────── WebSocket (PCM 8 kHz) ────┤
                                                                  ▼
                              ┌────────── CallSession (app/session.py) ───────────┐
                              │ VAD (barge-in) → Deepgram STT (endpointing)        │
                              │ → LLM pool (Groq → GLM → …, failover, tools)       │
                              │ → grounding guard → Deepgram TTS → caller          │
                              └──────┬───────────────────────┬─────────────────────┘
                                     │ tools                 │ every line, tool call, latency
                     ┌───────────────┴──────────┐   ┌────────┴────────┐
                     │ CRM adapter (Postgres)    │   │ calls,          │
                     │ Knowledge base (Chroma,   │   │ call_events     │ → /admin dashboard
                     │   hybrid vector+keyword)  │   └─────────────────┘
                     └───────────────────────────┘
```

## Quick start

```bash
cp .env.example .env              # add GROQ_API_KEY and DEEPGRAM_API_KEY; set ADMIN_PASSWORD
docker compose up -d --build      # Postgres + bot; demo data and knowledge base load on first start
open http://localhost:8080/talk   # talk to Maya through your laptop mic (headphones recommended)
open http://localhost:8080/admin  # call history (user "admin", password = ADMIN_PASSWORD)
curl localhost:8080/ready         # {"ready": true, "checks": {...}}
```

## Calling Maya from your phone

**Free, over Wi-Fi.** This runs Asterisk in Docker, and you use a free softphone app on your phone.
1. Set `LAN_IP` (from `ipconfig getifaddr en0`) and `SIP_PASSWORD` in `.env`.
2. Run `docker compose --profile phone up -d --build`.
3. In Zoiper or Linphone, add account `1001` with your `SIP_PASSWORD` and server `LAN_IP:5060` (UDP).
4. Dial **100**. Maya greets you as Priya. Account `1002` calls as an unknown number, and **200** is an echo test.

Full setup and troubleshooting are in [deploy/asterisk/README.md](deploy/asterisk/README.md). Your phone must be on the same Wi-Fi as the Mac. When the Mac's IP changes, update `LAN_IP` and run the same command again.

**Real phone number (Twilio).**
1. Expose the bot with `ngrok http 8080` and set `PUBLIC_URL` to the ngrok URL.
2. Set `TWILIO_AUTH_TOKEN`, which turns on webhook signature checks.
3. Point your Twilio number's voice webhook to `POST https://<PUBLIC_URL>/twilio/voice`.

Live transfer to a human also needs `TWILIO_ACCOUNT_SID` and `TRANSFER_NUMBER`.

## Demo data

Dates are relative to the day the database was seeded. To start over, or to have Maya recognise your own number as Priya, run `python -m scripts.seed_db --reset --phone +91XXXXXXXXXX`.

| Caller | Phone | PIN | Date of birth | Try asking |
|---|---|---|---|---|
| Priya Sharma (Plus) | +15551234567 | 1234 | 1990-04-12 | Where's my Pixel? Cancel my case order. Book a screen repair tomorrow. |
| Marcus Lee (Unlimited, AutoPay) | +15559876543 | 4321 | 1985-09-30 | What's my balance? When is my store appointment? |
| Elena Garcia (Basic, **suspended**) | +15555550123 | 2468 | 1978-01-22 | Why is my service suspended? Where's my SIM card? |
| Arjun Mehta (Unlimited) | +919876543210 | 1357 | 1993-07-03 | Where is my Galaxy? |
| Sofia Rossi (Plus) | +15553334444 | 9090 | 2000-12-01 | Cancel my watch band order. |
| Unknown | +15550001111 | – | – | Plan prices, store hours, return policy, callback |

## What Maya can do

| Tool | Needs verification |
|---|---|
| Recent orders, order status (carrier, tracking, ship date, ETA) | no |
| Upcoming store appointments | no |
| Free appointment times at a store on a date | no |
| Knowledge base search (plans, billing policy, returns, stores, security, troubleshooting) | no |
| Support ticket, callback request | no |
| Billing summary (balance, next bill, AutoPay, last payment) | **yes** |
| Cancel an order (only if it hasn't shipped) | **yes** |
| Book or cancel a store appointment (no double booking) | **yes** |
| Transfer to a human (Twilio), end call | no |

## Safety

- **Account scoping:** the caller is identified by caller ID, and every tool only reads that caller's own records. The model never chooses whose account to open.
- **Verification:** billing, cancellations and appointment changes need a 4-digit PIN or the date of birth. PINs are stored as PBKDF2 hashes and replaced with `****` in call logs. Three wrong attempts lock verification for the rest of the call.
- **Grounding guard** (`app/agent/grounding.py`): before a sentence is spoken, every money amount and calendar date in it must appear in a tool result, the call context or something the caller said. If one doesn't, the sentence is dropped and the model is told to look it up. If that happens twice, Maya offers a callback. In live testing this stopped a made-up "$42.75 balance".
- **Hang-up guard:** `end_call` is refused unless the caller signalled they're finished or Maya said goodbye, and never right after Maya asked a question.
- **Speech cleanup:** `<think>` blocks, markdown and emojis are removed before text-to-speech.
- **Logs:** the server logs hold no transcripts or tool results. Those are stored only in the call record, which is behind the admin password.

## Connecting your own data

1. **Documents** (FAQs, policies, manuals): put `.md`, `.txt` or `.pdf` files in `data/kb/` and run `python -m scripts.ingest_kb`. In Docker, run `docker compose exec app python -m scripts.ingest_kb`. Each file replaces its earlier version.
2. **Business systems:** `app/tools/crm.py` is the only file that talks to customer data. Re-implement its methods (`find_customer_by_phone`, `verify_customer`, `billing_summary`, `recent_orders`, `get_order`, `cancel_order`, `available_slots`, `book_appointment`, …) against your database or API, such as Shopify, Zoho, Salesforce, Freshdesk or your own backend. Return the same dictionary shapes and nothing else changes.
3. **New actions:** add a schema to `TOOLS` in `app/tools/registry.py` and a branch in `_dispatch`. If the action is sensitive, add it to `NEEDS_VERIFICATION`.
4. **Company and persona:** set `COMPANY_NAME`, `BOT_NAME`, `TTS_VOICE` and `TIMEZONE`. The speaking style is in `SYSTEM_PROMPT` in `app/agent/llm.py`.

## Operations

| | |
|---|---|
| Health | `/health` (process up) and `/ready` (database, LLM keys, speech key, capacity; returns 503 when not ready) |
| Capacity | `MAX_CONCURRENT_CALLS`. Extra callers hear a "lines are busy" message; Twilio gets busy TwiML. |
| Call length | `MAX_CALL_MINUTES`. Maya wraps up politely when it's reached. |
| Silence | She asks "Are you still there?" twice, then hangs up. During a slow lookup she says "still checking" instead of going silent. |
| Outages | LLM: rate-limited, failing or slow models (no first token within 4 s) are benched and the next model answers. Deepgram: keep-alive messages and automatic reconnects; if speech recognition stays down, Maya apologises and ends the call. Database: if the customer lookup fails, the call continues as an unknown caller. |
| Records | `calls` holds outcome, turns, verified, average response ms and models used. `call_events` holds transcript lines, tool calls (PIN masked), latency and guard events. |
| Shutdown | Active calls are finalised in the database. |
| Latency | Fixed phrases are pre-synthesised at startup, and one HTTP connection pool is shared by all calls. |

## Development

```bash
uv venv -p 3.12 && source .venv/bin/activate && uv pip install -r requirements.txt
docker compose up -d db                        # Postgres on localhost:5433
python -m app.main                             # bot on :8000 (seeds demo data, builds the KB)
pytest                                         # 70 tests; DB tests use database ringmind_test
python -m scripts.verify_models                # every LLM in the pool: speed and tool calling
python -m scripts.chat_cli --phone +15559876543          # type instead of talk
python -m scripts.call_test --server ws://localhost:8000 # scripted *spoken* call through the full stack
```

`scripts.call_test` speaks each caller line with real TTS and streams it to the bot in real time. Run it after any prompt or model change, then read the transcript in `/admin`.

## Known limitations

- **Free-tier LLM limits:** Groq's free tier allows only a few requests per minute on each model. During a busy call the 120b model is rate-limited and smaller models take over, and their answers are noticeably weaker. That is why the grounding and hang-up guards exist. Real traffic needs paid capacity or more provider keys.
- **Latency:** from the caller finishing speaking to Maya's first audio takes about 1.5–2.5 s, and longer on turns that need several lookups. Streaming TTS over a WebSocket and a faster model would cut this.
- **Transfer:** live transfer to a human works on Twilio only. On Asterisk, Maya offers a callback instead.
- **Language:** English only. Change `STT_LANGUAGE`, `TTS_VOICE` and the prompt to support another language.
