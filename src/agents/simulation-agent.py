from typing import List
from uagents import Agent, Context, Model

class SimRequest(Model):
    request_id: str
    items: List[dict]
    scenario: dict

class SimResult(Model):
    request_id: str
    results: List[dict]

agent = Agent()

@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Simulation Agent up. Address: {ctx.agent.address}")

@agent.on_message(model=SimRequest)
async def on_sim_request(ctx: Context, sender: str, msg: SimRequest):
    ctx.logger.info(f"SimRequest {msg.request_id} with {len(msg.items)} items")
    results = [{"item": "insulin", "days_of_cover": 6.0, "stockout_prob": 0.42, "risk_score": 2.1}]  # DUMMY
    await ctx.send(sender, SimResult(request_id=msg.request_id, results=results))
    ctx.logger.info(f"Replied with SimResult {msg.request_id}")