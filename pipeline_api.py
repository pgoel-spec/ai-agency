from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from database import get_active_tasks, get_pipeline_task, update_task_status, get_completed_tasks
from engine import run_pipeline_task
from onboarding import get_vault
import httpx

router = APIRouter()

class ResolveBuildPayload(BaseModel):
    resolution_type: str  # Must be "infra_fix" or "code_fix"
    human_feedback: str = "" # Empty if infra_fix

@router.get("/api/project/{project_id}/pipeline")
def api_get_pipeline(project_id: str):
    tasks = get_active_tasks(project_id)
    return {"success": True, "tasks": tasks}

@router.post("/api/project/{project_id}/task/{task_id}/restart")
def api_restart_task(project_id: str, task_id: str, background_tasks: BackgroundTasks):
    task = get_pipeline_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # 1. Reset status to Pending in the DB
    update_task_status(task_id, "Pending")

    # 2. Fetch the tenant's API keys
    vault = get_vault()
    tenant_config = vault.get(project_id, {})

    # 3. Dispatch the AI thread again using the exact same session_id!
    # LangGraph will automatically use its checkpoints to resume where it left off.
    background_tasks.add_task(run_pipeline_task, task_id, project_id, task["human_input"], tenant_config)
    
    return {"success": True, "message": "Task restarted"}

@router.post("/api/project/{project_id}/task/{task_id}/terminate")
def api_terminate_task(project_id: str, task_id: str):
    # Marking it as Terminated hides it from the UI. 
    # (Note: This doesn't kill the E2B sandbox if it's currently Running, 
    # but it stops it from being retried and clears the dashboard).
    update_task_status(task_id, "Terminated")
    return {"success": True, "message": "Task terminated"}

@router.get("/api/project/{project_id}/pipeline/completed")
def api_get_completed_pipeline(project_id: str):
    tasks = get_completed_tasks(project_id)
    return {"success": True, "tasks": tasks}

@router.post("/api/project/{project_id}/task/{task_id}/resolve-build")
def api_resolve_build(project_id: str, task_id: str, payload: ResolveBuildPayload, background_tasks: BackgroundTasks):
    """Unblocks a task that failed CI/CD and wakes up the AI agent."""
    
    task = get_pipeline_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    if task.get("status") != "BLOCKED_PR_BUILD":
        raise HTTPException(status_code=400, detail="Task is not blocked by a build failure.")

    error_log = task.get("build_error_log", "No log provided.")

    # Formulate the new context to pass to the engine
    if payload.resolution_type == "infra_fix":
        new_human_input = (
            "SYSTEM ALERT: The human has resolved the infrastructure/billing issue that caused "
            "the deployment to fail. Your code was likely fine. Please review your state and "
            "use your tools to push the code again to re-trigger the CI/CD pipeline."
        )
    elif payload.resolution_type == "code_fix":
        new_human_input = (
            f"SYSTEM ALERT: The CI/CD pipeline failed with the following log:\n"
            f"```log\n{error_log}\n```\n\n"
            f"HUMAN FEEDBACK: '{payload.human_feedback}'\n\n"
            f"Please analyze the logs to identify ALL root causes (code, infrastructure, routing, or missing dependencies). "
            f"Address the underlying issues and push the code again."
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid resolution type.")

    # 1. Reset status to Pending in the DB
    update_task_status(task_id, "Pending")

    # 2. Fetch the tenant's API keys (Matching api_restart_task logic)
    vault = get_vault()
    tenant_config = vault.get(project_id, {})

    # 3. Dispatch the AI thread again using the new injected input
    # LangGraph will automatically append this input to the exact same thread/session!
    background_tasks.add_task(run_pipeline_task, task_id, project_id, new_human_input, tenant_config)

    return {"success": True, "message": "Agent resumed successfully."}

@router.post("/api/project/{project_id}/task/{task_id}/sync-github")
async def api_sync_github_status(project_id: str, task_id: str):
    task = get_pipeline_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # 1. STATE PERSISTENCE: Read the exact branch from the DB (No guessing!)
    branch_name = task.get("branch_name") 
    if not branch_name:
        # Fallback for tasks created before the DB migration
        branch_name = f"feature-{task_id}"

    # 2. Dynamically fetch the tenant's specific GitHub credentials
    vault = get_vault()
    tenant_config = vault.get(project_id, {})
    github_token = tenant_config.get("github_token")
    github_repo = tenant_config.get("github_repo")

    if not github_token or not github_repo:
        raise HTTPException(status_code=400, detail="GitHub credentials missing for this project.")

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    # 3. DETERMINISTIC TARGETING: Query ONLY the specific workflow for this exact branch
    workflow_filename = "firebase-hosting-pull-request.yml" # Change if your file is named differently
    runs_url = f"https://api.github.com/repos/{github_repo}/actions/workflows/{workflow_filename}/runs"

    async with httpx.AsyncClient() as client:
        res = await client.get(runs_url, headers=headers, params={"branch": branch_name, "per_page": 1})

    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"GitHub API Error: {res.text}")

    data = res.json()
    if data.get("total_count", 0) == 0:
        return {"success": False, "message": f"Deployment has not started yet for branch '{branch_name}'.", "status": task.get("status")}

    # 4. Parse the latest deterministic run and sync our database
    latest_run = data["workflow_runs"][0]
    run_status = latest_run["status"]
    conclusion = latest_run["conclusion"]
    run_url = latest_run["html_url"]

    if run_status != "completed":
        return {"success": True, "message": "Action is still running on GitHub.", "status": task.get("status")}

    if conclusion == "success":
        update_task_status(task_id, "Completed")
        return {"success": True, "message": "Action succeeded! Synced status to Completed.", "status": "Completed"}

    elif conclusion in ["failure", "cancelled", "timed_out"]:
        error_msg = f"GitHub Action {conclusion}. Please check the logs here: {run_url}"
        
        # --- FETCH ACTUAL TERMINAL LOGS FOR FAILURES ---
        if conclusion == "failure":
            jobs_url = latest_run.get("jobs_url")
            if jobs_url:
                try:
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        # Get all jobs in this run
                        jobs_res = await client.get(jobs_url, headers=headers)
                        if jobs_res.status_code == 200:
                            jobs_data = jobs_res.json()
                            
                            # Find the specific job that failed
                            for job in jobs_data.get("jobs", []):
                                if job.get("conclusion") == "failure":
                                    job_id = job["id"]

                                    # FETCH EXACT UI ERRORS (ANNOTATIONS)
                                    anno_url = f"https://api.github.com/repos/{github_repo}/check-runs/{job_id}/annotations"
                                    anno_res = await client.get(anno_url, headers=headers)
                                    ui_errors = ""
                                    if anno_res.status_code == 200:
                                        for ann in anno_res.json():
                                            if ann.get("annotation_level") == "failure":
                                                ui_errors += f"File: {ann.get('path', 'Unknown')} Line: {ann.get('start_line', 'Unknown')}\nError: {ann.get('message', '')}\n\n"

                                    # FETCH RAW TERMINAL LOGS (AS FALLBACK)
                                    log_url = f"https://api.github.com/repos/{github_repo}/actions/jobs/{job_id}/logs"
                                    log_res = await client.get(log_url, headers=headers)

                                    tail_log = "No logs available."
                                    if log_res.status_code == 200:
                                        raw_lines = log_res.text.splitlines()
                                        error_chunks = []
                                        
                                        # 1. Intelligently scan for GitHub's native error tags
                                        for i, line in enumerate(raw_lines):
                                            if '##[error]' in line or 'Error: Process completed with exit code' in line:
                                                # Grab the 15 lines before the error marker to capture the actual tool crash
                                                start_idx = max(0, i - 15)
                                                end_idx = min(len(raw_lines), i + 2) # Grab the error line itself + 1 extra
                                                
                                                clean_chunk = []
                                                for j in range(start_idx, end_idx):
                                                    # Strip the verbose ISO 8601 timestamps (e.g., '2026-04-25T10:18:42.404Z ') for a cleaner AI prompt
                                                    parts = raw_lines[j].split("Z ", 1)
                                                    clean_line = parts[1] if len(parts) > 1 else raw_lines[j]
                                                    clean_chunk.append(clean_line)
                                                    
                                                error_chunks.append("\n".join(clean_chunk))
                                                
                                        if error_chunks:
                                            # Combine all found error contexts
                                            combined_errors = "\n\n... [next error] ...\n\n".join(error_chunks)
                                            tail_log = combined_errors[-2500:] if len(combined_errors) > 2500 else combined_errors
                                        else:
                                            # 2. Absolute fallback if no error tags exist (Strip teardown, take tail)
                                            raw_log = log_res.text
                                            for marker in ["Cleaning up orphan processes", "Post job cleanup", "##[group]Complete job"]:
                                                if marker in raw_log:
                                                    raw_log = raw_log.split(marker)[0]
                                            tail_log = raw_log[-2500:] if len(raw_log) > 2500 else raw_log
                                        
                                    # COMBINE THEM FOR THE AI
                                    error_msg = "GitHub Action Failed.\n\n"
                                    if ui_errors:
                                        error_msg += f"=== EXACT UI ERRORS ===\n{ui_errors}\n"
                                    error_msg += f"=== TERMINAL LOG TAIL ===\n{tail_log}\n\nFull run details: {run_url}"
                                    break
                except Exception as e:
                    print(f"[GITHUB SYNC ERROR] Failed to fetch detailed job logs: {str(e)}")
        # ----------------------------------------------------
        
        update_task_status(task_id, "BLOCKED_PR_BUILD", error_log=error_msg)
        return {"success": True, "message": f"Action {conclusion}! Synced status to BLOCKED.", "status": "BLOCKED_PR_BUILD"}

    else:
        return {"success": True, "message": f"Action ended with status: {conclusion}", "status": task.get("status")}