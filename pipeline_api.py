from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from database import get_active_tasks, get_pipeline_task, update_task_status, get_completed_tasks
from engine import run_pipeline_task
from onboarding import get_vault

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
            f"Please use `write_file` to fix this issue and push the code again."
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