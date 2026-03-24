import httpx
import asyncio
import sqlite3
import re
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from datetime import datetime

# Import from our modularized files
from database import init_db, init_pipeline_db, PIPELINE_DB_PATH
from onboarding import router as onboarding_router, get_vault
from log_viewer import router as log_viewer_router
from engine import run_pipeline_task
from pipeline_api import router as pipeline_router

load_dotenv() # Load the .env file so the API keys are available to the tools

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- ON STARTUP ---
    init_db()          # Your original DB init
    init_pipeline_db() # Our new pipeline DB init
    
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        cursor = conn.cursor()
        # Find any tasks left "Running" from a previous crash/Ctrl+C
        cursor.execute(
            "UPDATE tasks SET status = 'Pending', updated_at = ? WHERE status = 'Running'",
            (datetime.utcnow().isoformat(),)
        )
        zombies = cursor.rowcount
        conn.commit()
        if zombies > 0:
            print(f"\n[PIPELINE] 🧹 Swept {zombies} zombie task(s) back to 'Pending'.")
            
    yield # The FastAPI server is now running normally
    
    # --- ON SHUTDOWN ---
    pass

app = FastAPI(lifespan=lifespan)

# Snap in our modular routers
app.include_router(onboarding_router)
app.include_router(log_viewer_router)
app.include_router(pipeline_router)

class PreviewPayload(BaseModel):
    tenant_id: str
    ticket_id: str
    preview_url: str

@app.post("/webhooks/preview-ready")
async def handle_preview_ready(payload: PreviewPayload):
    """
    Called by GitHub Actions when a Firebase Preview is successfully deployed.
    Routes the URL to the correct Linear ticket using the tenant's OAuth token.
    """
    # 1. Fetch the vault using our central helper!
    vault = get_vault()
        
    tenant_data = vault.get(payload.tenant_id)
    if not tenant_data:
        return {"error": f"Project {payload.tenant_id} not found in vault"}
    
    # Check the toggle
    if not tenant_data.get("is_active", True):
        return {"status": "ignored", "message": "Project is currently paused."}

    # Fallback to the old api_key if they haven't migrated to OAuth yet
    linear_token = tenant_data.get("linear_access_token") or tenant_data.get("linear_api_key")
    if not linear_token:
        return {"error": "No Linear token found for tenant"}

    # 2. Construct the GraphQL mutation for Linear
    graphql_query = """
    mutation commentCreate($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    variables = {
        "issueId": payload.ticket_id,
        "body": f"🚀 **Live Preview Ready:** {payload.preview_url}\n\n*Deployed automatically by AI Agency Developer.*"
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {linear_token}" # OAuth tokens use Bearer
    }

    # 3. Post the comment to Linear
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.linear.app/graphql",
            json={"query": graphql_query, "variables": variables},
            headers=headers
        )

    if response.status_code == 200:
        return {"status": "success", "message": "Preview URL posted to Linear."}
    else:
        return {"status": "error", "details": response.text}

@app.post("/webhook")
async def linear_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    
    # --- 1. Map to your internal Project ID ---
    vault = await asyncio.to_thread(get_vault)
    linear_org_id = payload.get("organizationId")
    project_id = None

    # Search the vault for the project matching this Linear Organization
    for pid, data in vault.items():
        if data.get("linear_org_id") == linear_org_id:
            project_id = pid
            break
            
    # Fallback for legacy records that haven't re-authenticated
    if not project_id:
        legacy_team_id = payload.get("data", {}).get("teamId", "DEFAULT_TEAM")
        if legacy_team_id in vault:
            project_id = legacy_team_id
        else:
            return {"status": "Ignored", "message": "Unrecognized Linear Organization or Team."}
    
    # Safely load the config and check the toggle
    tenant_config = vault.get(project_id, {})
    if not tenant_config.get("is_active", True):
        return {"status": "Ignored", "message": "Project is currently paused or missing."}

    # --- 2. Extract the globally unique, human-readable Session ID ---
    url = payload.get("url", "")
    match = re.search(r'https://linear\.app/([^/]+)/issue/([A-Za-z]+-\d+)', url)
    
    if match:
        workspace_slug = match.group(1)
        short_ticket_id = match.group(2).upper()
        # Boom! Globally unique AND completely human-readable!
        session_id = f"{workspace_slug}-{short_ticket_id}"
    else:
        # Ultimate fallback to the long UUID just in case the URL format is weird
        session_id = payload.get("data", {}).get("issueId") or payload.get("data", {}).get("id")

    print(f"\n[DEBUG] Starting LangGraph Session -> {session_id}")
    
    # --- 3. Intelligently parse the human input ---
    webhook_type = payload.get("type", "Issue")
    
    if webhook_type == "Comment":
        human_input = payload.get("data", {}).get("body", "No comment provided.")
    else:
        # Standard new issue creation
        ticket_title = payload.get("data", {}).get("title", "New Task")
        ticket_description = payload.get("data", {}).get("description", "")

        human_input = (
            f"New Task Assigned: {ticket_title}\n\n"
            f"Description: {ticket_description}\n\n"
            f"IMPORTANT: The active Linear Ticket ID for this task is {session_id}.\n"
            f"IMPORTANT: Do not guess or invent a repository URL. Use the environment variable $GITHUB_REPO.\n"
        )

    # --- 4. PIPELINE QUEUE: Insert or Update the Database ---
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM tasks WHERE task_id = ?", (session_id,))
        row = cursor.fetchone()
        
        if row:
            current_status = row[0]
            # Prevent concurrent runs
            if current_status in ["Pending", "Running"]:
                return {
                    "status": "Ignored",
                    "message": f"This task is currently {current_status}. Wait for it to finish.",
                    "ticket": session_id
                }
            else:
                # If it previously Failed or Completed, and we got a NEW webhook (like a new comment),
                # we reset it back to Pending and update the prompt with the new comment!
                cursor.execute(
                    "UPDATE tasks SET status = 'Pending', human_input = ?, updated_at = ? WHERE task_id = ?",
                    (human_input, now, session_id)
                )
        else:
            # Brand new task, insert it
            cursor.execute(
                "INSERT INTO tasks (task_id, project_id, status, human_input, created_at, updated_at) VALUES (?, ?, 'Pending', ?, ?, ?)",
                (session_id, project_id, human_input, now, now)
            )
        conn.commit()

    # --- 5. DISPATCH: Send it to our new persistent wrapper ---
    background_tasks.add_task(run_pipeline_task, session_id, project_id, human_input, tenant_config)

    return {"status": "Task queued in persistent pipeline", "ticket": session_id}