from datetime import datetime, timezone
from typing import List
from uuid import uuid4
from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

INVENTORY_ADDR = "INVENTORY_ADDRESS"
SIMULATION_ADDR = "SIMULATION_ADDRESS"

# ---- message models (must match the other two agents exactly) ----
class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []

class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True

class SimRequest(Model):
    request_id: str
    items: List[dict]
    scenario: dict

class SimResult(Model):
    request_id: str
    results: List[dict]

agent = Agent()
chat_proto = Protocol(spec=chat_protocol_spec)

def make_reply(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )

@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Action Agent up. Address: {ctx.agent.address}")

# ---- 1. user -> Action Agent (from ASI:One) ----
@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    ctx.logger.info(f"User said: {text}")

    rid = uuid4().hex[:8]
    ctx.storage.set(f"user_{rid}", sender)
    ctx.storage.set(f"text_{rid}", text)
    await ctx.send(INVENTORY_ADDR, StockRequest(request_id=rid))
    ctx.logger.info(f"Sent StockRequest {rid} to Inventory")

@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

# ---- 2. Inventory -> Action Agent ----
@agent.on_message(model=StockSnapshot)
async def on_snapshot(ctx: Context, sender: str, msg: StockSnapshot):
    ctx.logger.info(f"Got StockSnapshot {msg.request_id}")
    scenario = {"user_text": ctx.storage.get(f"text_{msg.request_id}") or ""}
    await ctx.send(SIMULATION_ADDR, SimRequest(
        request_id=msg.request_id, items=msg.items, scenario=scenario))
    ctx.logger.info(f"Sent SimRequest {msg.request_id} to Simulation")

# ---- 3. Simulation -> Action Agent -> user ----
@agent.on_message(model=SimResult)
async def on_result(ctx: Context, sender: str, msg: SimResult):
    ctx.logger.info(f"Got SimResult {msg.request_id}")
    user = ctx.storage.get(f"user_{msg.request_id}")
    if not user:
        ctx.logger.error(f"No user stored for {msg.request_id}")
        return
    r = msg.results[0] if msg.results else {}
    await ctx.send(user, make_reply(
        f"Round trip OK (dummy data): {r.get('item')} has {r.get('days_of_cover')} "
        f"days of cover, stockout probability {r.get('stockout_prob')}."))

agent.include(chat_proto, publish_manifest=True)