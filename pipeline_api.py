from fastapi import APIRouter, HTTPException, BackgroundTasks
from database import get_active_tasks, get_pipeline_task, update_task_status
from engine import run_pipeline_task
from onboarding import get_vault

router = APIRouter()

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