from fastapi import FastAPI, Request
from agents.graph import app as agent_team
from dotenv import load_dotenv

# Load the .env file so the API keys are available to the tools
load_dotenv()

app = FastAPI()

@app.post("/webhook")
async def linear_webhook(request: Request):
    # Parse the incoming JSON from Linear
    payload = await request.json()
    
    ticket_id = payload.get("data", {}).get("id")
    ticket_title = payload.get("data", {}).get("title", "New Task")
    
    # We MUST put the ticket_id into the actual text message so the AI can read it!
    initial_state = {
        "messages": [("user", f"New Task Assigned: {ticket_title}\n\nIMPORTANT: The active Linear Ticket ID for this task is {ticket_id}.")],
        "active_ticket_url": ticket_id
    }
    
    # Wake up the AI team and invoke the workflow
    result = agent_team.invoke(initial_state)
    
    return {"status": "AI Team Dispatched", "ticket": ticket_id}