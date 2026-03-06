import os
import requests
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox
from agents.graph import app as agent_team
from agents.tools import get_best_resume_branch
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from database import init_db, log_activity, get_db_conn
from langchain_core.messages import HumanMessage

active_tickets = set() # Global set to track currently processing tickets
load_dotenv() # Load the .env file so the API keys are available to the tools
app = FastAPI()

@app.on_event("startup")
def on_startup():
    init_db()

def get_recent_linear_comments(ticket_id: str) -> str:
    """
    Fetches the last 3 comments from the Linear ticket to build context.
    """
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("[ERROR] Linear API key missing.")
        return ""

    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    
    # GraphQL to get the issue by ID and its most recent comments
    query = """
    query($id: String!) {
      issue(id: $id) {
        comments(first: 3, orderBy: createdAt_DESC) {
          nodes {
            body
          }
        }
      }
    }
    """
    
    response = requests.post(
        url, 
        json={"query": query, "variables": {"id": ticket_id}}, 
        headers=headers
    )
    
    if response.status_code == 200:
        data = response.json()
        comments = data.get("data", {}).get("issue", {}).get("comments", {}).get("nodes", [])
        
        # Reverse them so they read chronologically
        formatted_comments = [f"- {c['body']}" for c in reversed(comments)]
        return "\n".join(formatted_comments)
        
    return "Could not fetch history."

def process_ticket(ticket_id: str, human_input: str, has_history: bool):
    """
    Synchronous background task to run the AI team.
    FastAPI automatically runs standard 'def' background tasks in a separate thread pool,
    so it won't block the main asyncio event loop.
    """
    # 4. Create a fresh sandbox for this run (with a 15 min timeout)
    workspace = Sandbox.create(timeout=900)
    config = {
        "configurable": {
            "thread_id": ticket_id,
            "sandbox": workspace # Pass the sandbox here, NOT in the state!
        }
    }

    # Helper to avoid repeating logging logic 3 times
    def stream_and_log(input_data, config, ticket_id):
        # --- STEP 1: Log the Input (What we are sending to the AI) ---
        # We log the input_data so we know the exact state/prompt the AI received.
        log_activity(
            ticket_id=ticket_id,
            role="system_input", 
            content=str(input_data), # This captures the prompt + state variables
            node_name="Graph_Entry"
        )
        
        for event in agent_team.stream(input_data, config):
            for node_name, node_state in event.items():
                # --- NEW: Node Activation Trace ---
                # This logs the moment a node emits ANY event, capturing the "Silence"
                log_activity(
                    ticket_id=ticket_id,
                    role="system_trace",
                    content=f"Node '{node_name}' activated and processing...",
                    node_name=node_name
                )
                
                if "messages" in node_state and len(node_state["messages"]) > 0:
                    last_msg = node_state["messages"][-1]
                    
                    # --- STEP 2: Log the AI's response (Thought & Tools) ---

                    # Log the "Thought" (The text content)
                    if hasattr(last_msg, "content") and last_msg.content and last_msg.type != "tool":
                        log_activity(
                            ticket_id=ticket_id,
                            role=getattr(last_msg, "type", "ai"),
                            content=last_msg.content, # This is the "Thought"
                            node_name=node_name
                        )

                    # 1. Log AI Tool Calls (The intent to act)
                    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                        for tc in last_msg.tool_calls:
                            log_activity(
                                ticket_id=ticket_id,
                                role="ai",
                                content=tc["args"],
                                node_name=node_name,
                                tool_name=tc["name"]
                            )
                    
                    # 2. Log Tool Outputs (The result of the action)
                    elif hasattr(last_msg, "type") and last_msg.type == "tool":
                        content_str = str(last_msg.content).lower()
                        is_err = any(word in content_str for word in ["error", "failed", "exception", "not found"])
                        log_activity(
                            ticket_id=ticket_id,
                            role="tool",
                            content=last_msg.content,
                            node_name=node_name,
                            tool_name=getattr(last_msg, "name", "unknown_tool"),
                            is_error=is_err
                        )

                    print(f"\n--- Update from {node_name} ---\n{last_msg.content}")

    try:
        # Initial Log: Capture the starting human input
        log_activity(ticket_id, "human", human_input, node_name="Webhook")

        best_branch_info = get_best_resume_branch(ticket_id)

        if best_branch_info:
            branch_name = best_branch_info["branch_name"]
            branch_type = best_branch_info["type"]

            print(f"\n[SYSTEM]: Found '{branch_type}' branch at '{branch_name}'. Team will resume work from here.")

            # Add system override based on the branch type
            if branch_type == "wip":
                system_note = (
                    f"\n\nSYSTEM OVERRIDE: A WIP recovery branch exists at '{branch_name}'. "
                    f"You MUST navigate into the repository folder (clone it first if missing) and run `git checkout {branch_name}` to recover your lost work before doing anything else."
                )
            else:
                system_note = (
                    f"\n\nSYSTEM OVERRIDE: An open Pull Request already exists on branch '{branch_name}'. "
                    f"To avoid merge conflicts, you MUST navigate into the repository folder (clone it first if missing) and run `git checkout {branch_name}` before doing anything else."
                )
            
            prompt = human_input + system_note

            # Create a strictly typed and tagged HumanMessage
            webhook_msg = HumanMessage(
                content=prompt,
                name="Human", # You can call it whatever you want now
                additional_kwargs={"source": "webhook"} # <--- THE MAGIC TAG
            )

            if has_history:
                # --- WIP: CONTEXT REHYDRATION (Checking the Real World) ---
                # recent_comments = get_recent_linear_comments(ticket_id)

                stream_and_log({"messages": [webhook_msg]}, config, ticket_id)
            else:
                # Start a new conversation, but force it to use the PR branch
                initial_state = {
                    "messages": [("user", prompt)],
                    "active_ticket_url": ticket_id,
                    "current_status": "NOT_STARTED",
                    "current_phase": "Planning"
                }
                stream_and_log(initial_state, config, ticket_id)
        else:
            print(f"\n[SYSTEM]: No open PRs found. Dispatching team for new ticket {ticket_id}...")
            
            # If there's no open PR, we start entirely from scratch.
            system_note = "\n\nSYSTEM OVERRIDE: You are in a completely new, empty E2B sandbox. There are no open PRs to resume from. Build the feature from scratch on a new branch."
            prompt = human_input + system_note
            initial_state = {
                "messages": [("user", prompt)],
                "active_ticket_url": ticket_id,
                "current_status": "NOT_STARTED",
                "current_phase": "Planning"
            }

            # ==========================================
            # INTENTIONAL HISTORY RETENTION
            # ==========================================
            # Note: If `has_history` is True but no PR exists, streaming this initial_state 
            # into the existing thread_id does NOT delete the past conversation. 
            #
            # This gives the agents a "memory" of what went wrong previously,
            # preventing them from blindly repeating the exact same mistakes 
            # in this new sandbox session.
            # ==========================================
            stream_and_log(initial_state, config, ticket_id)
    finally:
        # Always clean up the sandbox to save compute!
        workspace.kill()

def process_ticket_with_lock(ticket_id: str, human_input: str, has_history: bool):
    """Wraps process_ticket to ensure the lock is released when the job finishes."""
    try:
        # Run the existing AI logic
        process_ticket(ticket_id, human_input, has_history)
    finally:
        # Always remove the ticket from the active list when finished or if it crashes
        active_tickets.discard(ticket_id)

@app.get("/logs/{ticket_id}")
async def get_ticket_logs(ticket_id: str):
    try:
        from database import get_logs_by_ticket
        history = get_logs_by_ticket(ticket_id)
        
        if not history:
            raise HTTPException(status_code=404, detail="No logs found.")
            
        return {"ticket_id": ticket_id, "history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook")
async def linear_webhook(request: Request, background_tasks: BackgroundTasks):
    # Parse the incoming JSON from Linear
    payload = await request.json()
    
    # Intelligently parse the payload (Handles both "Issue" and "Comment" webhooks)
    webhook_type = payload.get("type", "Issue")
    
    if webhook_type == "Comment":
        # If the webhook is a comment, the ID is nested differently
        ticket_id = payload.get("data", {}).get("issue", {}).get("id", "UNKNOWN-ID")
        human_input = payload.get("data", {}).get("body", "No comment provided.")
    else:
        # Standard new issue creation
        ticket_id = payload.get("data", {}).get("id", "UNKNOWN-ID")
        ticket_title = payload.get("data", {}).get("title", "New Task")
        ticket_description = payload.get("data", {}).get("description", "")
        
        human_input = (
            f"New Task Assigned: {ticket_title}\n\n"
            f"Description: {ticket_description}\n\n"
            f"IMPORTANT: The active Linear Ticket ID for this task is {ticket_id}.\n"
            f"IMPORTANT: Do not guess or invent a repository URL. Use the environment variable $GITHUB_REPO.\n"
        )

    # CONCURRENCY CHECK: Intercept identical tickets
    if ticket_id in active_tickets:
        return {
            "status": "Ignored",
            "message": "This ticket is already being processed. Wait for the active process to finish and resend the request.",
            "ticket": ticket_id
        }
        
    # Add to active tickets before doing DB lookups or dispatching
    active_tickets.add(ticket_id)
    
    # Define the Checkpointer Configuration (The Thread ID)
    config = {"configurable": {"thread_id": ticket_id}}
    
    # Check the database to see if this ticket already has a history
    current_state = agent_team.get_state(config)
    has_history = bool(current_state.values)

    # Offload the blocking AI execution to a background thread
    background_tasks.add_task(process_ticket_with_lock, ticket_id, human_input, has_history)

    # Return immediately so Linear doesn't timeout the webhook!
    return {"status": "AI Team Dispatched/Resumed in background", "ticket": ticket_id}
