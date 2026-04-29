import httpx
import asyncio
import sqlite3
import time
import re
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel
from datetime import datetime

# Import from our modularized files
from database import init_db, init_pipeline_db, get_pipeline_conn, update_task_status
from dashboard_ui import router as dashboard_router, get_vault
from log_viewer import router as log_viewer_router
from engine import run_pipeline_task
from pipeline_api import router as pipeline_router
from webhook_handler import process_github_webhook, process_preview_ready, WebhookSecurityError, process_pr_closure

load_dotenv() # Load the .env file so the API keys are available to the tools

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- ON STARTUP ---
    init_db()          # DB init
    init_pipeline_db() # Pipeline DB init
    
    with get_pipeline_conn() as conn:
        cursor = conn.cursor()
        # Find any tasks left "Running" from a previous crash/Ctrl+C
        cursor.execute(
            "UPDATE tasks SET status = 'Pending', updated_at = ? WHERE status = 'Running'",
            (datetime.utcnow().isoformat(),)
        )
        zombies = cursor.rowcount
        if zombies > 0:
            print(f"\n[PIPELINE] 🧹 Swept {zombies} zombie task(s) back to 'Pending'.")
            
    yield # The FastAPI server is now running normally
    
    # --- ON SHUTDOWN ---
    pass

app = FastAPI(lifespan=lifespan)

# Snap in our modular routers
app.include_router(dashboard_router)
app.include_router(log_viewer_router)
app.include_router(pipeline_router)

class PreviewPayload(BaseModel):
    tenant_id: str
    ticket_id: str
    preview_url: str

@app.post("/webhooks/preview-ready")
async def handle_preview_ready(payload: PreviewPayload):
    return await process_preview_ready(
        tenant_id=payload.tenant_id,
        ticket_id=payload.ticket_id,
        preview_url=payload.preview_url,
        get_vault_func=get_vault,
        update_db_func=lambda t_id, status: update_task_status(t_id, status)
    )

@app.post("/webhooks/pr-build-failed")
async def fastapi_adapter(request: Request, x_agency_signature: str = Header(None)):
    raw_body = await request.body()
    
    try:
        result = process_github_webhook(
            raw_body=raw_body,
            signature_header=x_agency_signature,
            # Map directly to existing helpers via lambdas
            get_tenant_secret_func=lambda t_id: get_vault().get(t_id, {}).get("webhook_secret"),
            update_db_func=lambda t_id, log, sha: update_task_status(t_id, "BLOCKED_PR_BUILD", error_log=log, commit_sha=sha)
        )
        return result
    except WebhookSecurityError as e:
        raise HTTPException(status_code=401, detail=str(e))

@app.post("/webhook")
async def linear_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    
    # Fail-Fast Filter: Only process creations, ignore background syncs
    action = payload.get("action")
    if action != "create":
        return {"status": "Ignored", "message": f"Ignoring webhook with action: {action}"}

    # --- 1. Map to your internal Project ID DIRECTLY FROM THE URL ---
    project_id = request.query_params.get("project_id")
    
    vault = await asyncio.to_thread(get_vault)
    if not project_id or project_id not in vault:
        return {"status": "Ignored", "message": f"Invalid or missing project_id in webhook URL."}
    
    # Safely load the config and check the toggle
    tenant_config = vault.get(project_id, {})
    if not tenant_config.get("is_active", True):
        return {"status": "Ignored", "message": "Project is currently paused or missing."}

    # --- 2. Extract and define the exact Nomenclature IDs ---
    url = payload.get("url") or payload.get("data", {}).get("issue", {}).get("url") or payload.get("data", {}).get("url", "")
    match = re.search(r'https://linear\.app/([^/]+)/issue/([A-Za-z]+-\d+)', url)
    
    if match:
        workspace_slug = match.group(1)
        short_ticket_id = match.group(2).upper()
        # BASE: Globally unique issue identifier (e.g., ai-agency-ws-AI-6)
        ticket_id = f"{workspace_slug}_{short_ticket_id}"
    else:
        # Ultimate fallback to the long UUID just in case the URL format is weird
        ticket_id = payload.get("data", {}).get("issueId") or payload.get("data", {}).get("id")

    # SUFFIX: The specific timestamp for this execution attempt (e.g., 1774207968)
    session_id = str(int(time.time()))
    
    # UNIQUE KEY: The exact DB entry and log file name (e.g., ai-agency-ws-AI-6-1774207968)
    task_id = f"{ticket_id}-{session_id}"

    print(f"\n[DEBUG] Webhook triggered for Ticket: {ticket_id} | Spawning Task: {task_id}")
    
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
            f"---\n"
            f"*Initialized automatically via Linear webhook.*"
        )

    # --- 4. PIPELINE QUEUE: Insert the Database Row ---
    now = datetime.utcnow().isoformat()
    with get_pipeline_conn() as conn:
        cursor = conn.cursor()
        
        # Check for concurrency using the base ticket_id
        # We look for the most recent task run for THIS specific ticket
        cursor.execute(
            "SELECT status FROM tasks WHERE task_id LIKE ? ORDER BY created_at DESC LIMIT 1", 
            (f"{ticket_id}-%",)
        )
        row = cursor.fetchone()
        
        if row:
            current_status = row[0]
            # Prevent concurrent runs ONLY if the AI is actively executing.
            if current_status == "Running":
                return {
                    "status": "Ignored",
                    "message": "This task is currently Running. Wait for it to finish.",
                    "ticket": ticket_id
                }
        
        # If no task is running, we insert a BRAND NEW row for this specific run.
        # This prevents us from overwriting old logs if the user comments on a completed ticket!
        cursor.execute(
            "INSERT INTO tasks (task_id, project_id, status, human_input, created_at, updated_at) VALUES (?, ?, 'Pending', ?, ?, ?)",
            (task_id, project_id, human_input, now, now)
        )
        conn.commit()

    # --- 5. DISPATCH: Send it to our new persistent wrapper ---
    # We pass 'task_id' so the engine knows exactly which DB row and log file to use!
    background_tasks.add_task(run_pipeline_task, task_id, project_id, human_input, tenant_config)

    return {"status": "Task queued in persistent pipeline", "ticket": ticket_id, "task": task_id}

@app.post("/webhooks/github-pr-closed")
async def github_pr_closed_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    project_id = request.query_params.get("project_id")
    
    if payload.get("action") == "closed":
        # 1. Use the pure parser to get the AI string
        human_input = process_pr_closure(payload)
        
        # 2. Fetch the tenant config (Main.py already does this for Linear)
        vault = get_vault()
        tenant_config = vault.get(project_id)
        
        if not tenant_config:
            return {"status": "Ignored", "message": "Project not found."}
            
        # 3. Dispatch to the engine
        task_id = f"doc-sync-{int(time.time())}"
        background_tasks.add_task(run_pipeline_task, task_id, project_id, human_input, tenant_config)
        
        return {"status": "Doc sync task queued."}
        
    return {"status": "Ignored - Action is not closed."}