"""Talk to the agent by typing — exercises the LLM pool, the CRM and the KB without any telephony.

    python -m scripts.chat_cli [--phone +15551234567]
"""

import argparse
import asyncio
import logging

from app.agent.llm import VoiceAgent
from app.config import get_settings
from app.tools.crm import get_crm
from app.tools.registry import CallContext, spoken_dates


async def main(phone: str) -> None:
    ctx = CallContext(call_id="cli", caller_phone=phone)
    ctx.customer = await get_crm().find_customer_by_phone(phone)
    if ctx.customer:
        ctx.recent_orders = [spoken_dates(o) for o in await get_crm().recent_orders(ctx.customer["id"], limit=3)]
    print(f"Caller: {phone} -> {ctx.customer['name'] if ctx.customer else 'unknown customer'}")
    agent = VoiceAgent(get_settings(), ctx)
    agent.note('Call connected. You already greeted the caller with: "Thanks for calling, how can I help?"')
    print("bot> Thanks for calling, how can I help?")
    while True:
        try:
            text = await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            return
        if not text.strip():
            continue
        async for sentence in agent.respond(text):
            print(f"bot> {sentence}")
        if ctx.pending_actions:
            print(f"   [actions: {ctx.pending_actions}]")
            if "hangup" in ctx.pending_actions:
                return
            ctx.pending_actions.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="+15551234567")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    asyncio.run(main(args.phone))
