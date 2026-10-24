from typing import List
from uagents import Agent, Context, Model

class StockRequest(Model):
    request_id: str
    focus_items: List[str] = []

class StockSnapshot(Model):
    request_id: str
    items: List[dict]
    synthetic: bool = True

agent = Agent()

@agent.on_event("startup")
async def on_start(ctx: Context):
    ctx.logger.info(f"Inventory Agent up. Address: {ctx.agent.address}")

@agent.on_message(model=StockRequest)
async def on_stock_request(ctx: Context, sender: str, msg: StockRequest):
    ctx.logger.info(f"StockRequest {msg.request_id} from {sender}")
    items = [{"name": "insulin", "stock": 120, "daily_demand": 20, "criticality": 5}]  # DUMMY
    await ctx.send(sender, StockSnapshot(request_id=msg.request_id, items=items))
    ctx.logger.info(f"Replied with StockSnapshot {msg.request_id}")