import os
import json
import httpx
import base64
import uuid
import secrets
from datetime import datetime
from fastapi import APIRouter, Request, Form, Path
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from database import get_sessions_by_project, get_db_conn

router = APIRouter()

VAULT_FILE = "tenant_vault.json"

# ==========================================
# 1. VAULT HELPERS
# ==========================================
def get_vault():
    if os.path.exists(VAULT_FILE):
        with open(VAULT_FILE, "r") as f:
            return json.load(f)
    return {}

def save_project_full(project_id: str, project_name: str, github_repo: str, github_token: str, webhook_secret: str = None):
    vault = get_vault()
    if project_id not in vault:
        vault[project_id] = {
            "project_name": project_name,
            "created_at": datetime.now().strftime("%b %d, %Y %H:%M"),
            "linear_access_token": "",
            "e2b_template": "",
            "secrets": {},
            "github_webhook_status": "pending",
            "is_active": True,
            "webhook_secret": webhook_secret or secrets.token_urlsafe(32)
        }
    else:
        vault[project_id]["project_name"] = project_name
        # Ensure existing projects get a secret too
        if "webhook_secret" not in vault[project_id]:
            vault[project_id]["webhook_secret"] = webhook_secret or secrets.token_urlsafe(32)
        
    vault[project_id]["github_repo"] = github_repo
    vault[project_id]["github_token"] = github_token
    
    with open(VAULT_FILE, "w") as f:
        json.dump(vault, f, indent=2)

def save_tenant_secret(project_id: str, key: str, value: str | bool):
    vault = get_vault()
    if project_id not in vault:
        return False
    vault[project_id][key] = value
    with open(VAULT_FILE, "w") as f:
        json.dump(vault, f, indent=2)
    return True

def delete_project_record(project_id: str):
    vault = get_vault()
    if project_id in vault:
        del vault[project_id]
        with open(VAULT_FILE, "w") as f:
            json.dump(vault, f, indent=2)
        return True
    return False

# ==========================================
# 2. GITHUB INJECTOR
# ==========================================
async def inject_github_workflow(project_id: str, github_repo: str, github_token: str):
    linear_uri = os.getenv("LINEAR_REDIRECT_URI")
    
    # If there is no domain configured, abort the webhook injection.
    if not linear_uri:
        print("[ERROR] Missing LINEAR_REDIRECT_URI in .env. Cannot inject GitHub webhook.")
        return False
        
    saas_url = linear_uri.replace("/auth/linear/callback", "/webhooks/preview-ready")
    pr_closed_url = linear_uri.replace("/auth/linear/callback", f"/webhooks/github-pr-closed?project_id={project_id}")

    preview_yaml = f"""name: Notify AI Agency of Preview
on: pull_request
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - name: Wait for Vercel/Firebase Preview
        run: sleep 30
      - name: Send Webhook to AI Agency
        run: |
          curl -X POST -H "Content-Type: application/json" \\
          -d '{{"tenant_id": "{project_id}", "ticket_id": "${{{{ github.head_ref }}}}", "preview_url": "Ready to review!"}}' \\
          {saas_url}
"""

    closure_yaml = f"""name: Notify AI Agency of PR Closure
on:
  pull_request:
    types: [closed]
jobs:
  notify:
    runs-on: ubuntu-latest
    steps:
      - name: Send PR Closure Webhook
        run: |
          curl -X POST -H "Content-Type: application/json" \\
          -d "{{\\"action\\": \\"closed\\", \\"pull_request\\": {{\\"merged\\": ${{{{ github.event.pull_request.merged }}}}, \\"title\\": \\"${{{{ github.event.pull_request.title }}}}\\", \\"body\\": \\"Skipped for brevity\\"}}}}" \\
          {pr_closed_url}
"""

    headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github.v3+json"}

    async with httpx.AsyncClient() as client:
        # 1. Inject the original Preview Webhook
        encoded_preview = base64.b64encode(preview_yaml.encode("utf-8")).decode("utf-8")
        preview_api_url = f"https://api.github.com/repos/{github_repo}/contents/.github/workflows/ai-agency-webhook.yml"
        preview_payload = {"message": "🤖 AI Agency: Initializing CI/CD Webhooks", "content": encoded_preview, "branch": "main"}

        get_preview_resp = await client.get(preview_api_url, headers=headers)
        if get_preview_resp.status_code == 200:
            preview_payload["sha"] = get_preview_resp.json().get("sha")
            
        put_preview_resp = await client.put(preview_api_url, headers=headers, json=preview_payload)
        
        # 2. Inject the new PR Closure Webhook
        encoded_closure = base64.b64encode(closure_yaml.encode("utf-8")).decode("utf-8")
        closure_api_url = f"https://api.github.com/repos/{github_repo}/contents/.github/workflows/ai-agency-pr-closure.yml"
        closure_payload = {"message": "🤖 AI Agency: Initializing PR Sync Webhook", "content": encoded_closure, "branch": "main"}
        
        get_closure_resp = await client.get(closure_api_url, headers=headers)
        if get_closure_resp.status_code == 200:
            closure_payload["sha"] = get_closure_resp.json().get("sha")
            
        put_closure_resp = await client.put(closure_api_url, headers=headers, json=closure_payload)
        
    # Return True only if BOTH workflow injections succeeded
    return put_preview_resp.status_code in (200, 201) and put_closure_resp.status_code in (200, 201)

# ==========================================
# 3. API ENDPOINTS
# ==========================================
class ProjectRequest(BaseModel):
    project_id: str = None
    project_name: str
    github_repo: str
    github_token: str
    run_test: bool = False 

class ToggleRequest(BaseModel):
    is_active: bool

@router.post("/api/project")
async def api_onboard_project(request: ProjectRequest):
    is_new = not request.project_id
    project_id = request.project_id if request.project_id else str(uuid.uuid4())[:8]
    
    # Generate or fetch secret
    vault = get_vault()
    webhook_secret = vault.get(project_id, {}).get("webhook_secret") if not is_new else secrets.token_urlsafe(32)
    if not webhook_secret:
        webhook_secret = secrets.token_urlsafe(32)

    save_project_full(project_id, request.project_name, request.github_repo, request.github_token, webhook_secret)
    
    if request.run_test:
        success = await inject_github_workflow(project_id, request.github_repo, request.github_token)
        new_status = "success" if success else "failed"
        save_tenant_secret(project_id, "github_webhook_status", new_status)
        return {"success": success, "status": new_status, "project_id": project_id, "is_new": is_new, "tested": True, "webhook_secret": webhook_secret}
    
    return {"success": True, "status": "saved", "project_id": project_id, "is_new": is_new, "tested": False, "webhook_secret": webhook_secret}

@router.post("/api/project/{project_id}/toggle")
async def api_toggle_project(project_id: str, request: ToggleRequest):
    save_tenant_secret(project_id, "is_active", request.is_active)
    return {"success": True}

@router.delete("/api/project/{project_id}")
async def api_delete_project(project_id: str):
    return {"success": delete_project_record(project_id)}

@router.delete("/api/project/{project_id}/linear")
async def api_disconnect_linear(project_id: str):
    vault = get_vault()
    tenant = vault.get(project_id, {})
    
    access_token = tenant.get("linear_access_token")
    webhook_id = tenant.get("linear_webhook_id")

    # If we have a token and a webhook ID, tell Linear to delete the webhook
    if access_token and webhook_id:
        delete_query = """
        mutation webhookDelete($id: String!) {
          webhookDelete(id: $id) {
            success
          }
        }
        """
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.linear.app/graphql",
                json={"query": delete_query, "variables": {"id": webhook_id}},
                headers={"Authorization": f"Bearer {access_token}"}
            )

    # Wipe the credentials from our vault
    save_tenant_secret(project_id, "linear_access_token", "")
    save_tenant_secret(project_id, "linear_api_key", "")
    save_tenant_secret(project_id, "linear_org_id", "")
    save_tenant_secret(project_id, "linear_webhook_id", "")
    
    return {"success": True}

@router.get("/api/project/{project_id}/linear/test")
async def api_test_linear_webhook(project_id: str):
    vault = get_vault()
    tenant = vault.get(project_id, {})
    access_token = tenant.get("linear_access_token")
    
    if not access_token:
        return {"success": False, "exists": False, "message": "No Linear token found."}

    saas_webhook_url = os.getenv("LINEAR_REDIRECT_URI", "").replace("/auth/linear/callback", "/webhook")
    
    check_query = "{ webhooks { nodes { id url enabled } } }"
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.linear.app/graphql",
            json={"query": check_query},
            headers={"Authorization": f"Bearer {access_token}"}
        )
        
    res_json = res.json()
    
    # Check if Linear actively blocked us for not being an admin
    if "errors" in res_json:
        for error in res_json["errors"]:
            if "admin" in error.get("message", "").lower() or error.get("extensions", {}).get("code") == "FORBIDDEN":
                return {"success": False, "exists": False, "error_type": "admin_required"}

    # Safely parse if no errors
    data = res_json.get("data") or {}
    webhooks_obj = data.get("webhooks") or {}
    webhooks = webhooks_obj.get("nodes") or []
    
    is_active = any(wh.get("url") == saas_webhook_url and wh.get("enabled") for wh in webhooks)
    
    return {"success": True, "exists": is_active}

# ==========================================
# 4. HTML RENDER HELPERS
# ==========================================
# onboarding.py (Partial updates to HTML strings)

def get_layout_header():
    return """
    <script>
        // Universally inject Dark Mode logic so it works on all pages
        if (localStorage.theme === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
            document.documentElement.classList.add('dark');
        } else {
            document.documentElement.classList.remove('dark');
        }
        window.toggleTheme = function() {
            document.documentElement.classList.toggle('dark');
            localStorage.theme = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
        };
    </script>
    <header class="h-16 bg-white dark:bg-log-surface border-b border-gray-200 dark:border-log-border flex items-center justify-between px-6 flex-shrink-0 z-40">
        <a href="/" class="flex items-center space-x-2 transition hover:opacity-80">
            <div class="w-8 h-8 bg-blue-600 rounded flex items-center justify-center text-white font-medium">AI</div>
            <span class="text-xl font-medium text-gray-900 dark:text-log-text tracking-wider">AI AGENCY</span>
        </a>
        <button onclick="toggleTheme()" class="p-2 rounded bg-gray-100 dark:bg-[#333333] text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 transition">
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z"></path></svg>
        </button>
    </header>
    """

def get_sidebar_html(active_page="dashboard"):
    """Global sidebar used on Dashboard and Global Logs."""
    vault = get_vault()
    
    dash_active = "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 font-medium" if active_page == "dashboard" else "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-[#333333]"
    logs_active = "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 font-medium" if active_page == "logs" else "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-[#333333]"
    
    # Updated: "Select Project..." instead of "Jump to Workspace..."
    dropdown_options = '<option value="">Select Project...</option>'
    for pid, data in vault.items():
        pname = data.get("project_name", pid)
        dropdown_options += f'<option value="{pid}">{pname}</option>'
    
    return f"""
    <div class="w-64 bg-white dark:bg-log-surface border-r border-gray-200 dark:border-log-border flex flex-col flex-shrink-0">
        <div class="p-4 border-b border-gray-200 dark:border-log-border">
            <select onchange="if(this.value) window.location.href='/project/' + this.value" class="w-full bg-gray-50 dark:bg-[#333333] text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 transition cursor-pointer">
                {dropdown_options}
            </select>
        </div>
        <nav class="flex-1 p-4 space-y-2">
            <a href="/" class="flex items-center space-x-3 px-4 py-3 rounded transition {dash_active}">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"></path></svg>
                <span>Dashboard</span>
            </a>
            <a href="/logs" class="flex items-center space-x-3 px-4 py-3 rounded transition {logs_active}">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                <span>Run History</span>
            </a>
        </nav>
    </div>
    """

def get_project_sidebar_html(project_id, project_name, active_page="pipeline"):
    """Project-specific sidebar. Includes the new Technical Plan tab."""
    pipe_active = "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 font-medium" if active_page == "pipeline" else "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-[#333333]"
    plan_active = "bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 font-medium" if active_page == "plan" else "text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-[#333333]"
    
    return f"""
    <div class="w-64 bg-white dark:bg-log-surface border-r border-gray-200 dark:border-log-border flex flex-col flex-shrink-0">
        <div class="p-4 border-b border-gray-200 dark:border-log-border">
            <a href="/" class="text-sm text-gray-500 hover:text-blue-500 transition flex items-center mb-2">
                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
                Back to Dashboard
            </a>
            <h2 class="font-medium text-gray-900 dark:text-white truncate" title="{project_name}">{project_name}</h2>
        </div>
        <nav class="flex-1 p-4 space-y-2">
            <a href="/project/{project_id}" class="flex items-center space-x-3 px-4 py-3 rounded transition {pipe_active}">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 002-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
                <span>Ticket Pipeline</span>
            </a>
            <a href="/project/{project_id}/plan" class="flex items-center space-x-3 px-4 py-3 rounded transition {plan_active}">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                <span>Technical Plan</span>
            </a>
            <a href="/logs?project={project_id}" class="flex items-center space-x-3 px-4 py-3 rounded transition text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-[#333333]">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                <span>Run History</span>
            </a>
        </nav>
    </div>
    """

def generate_card_html(proj_id, data):
    proj_name = data.get("project_name", proj_id).replace("'", "\\'")
    repo = data.get("github_repo", "").replace("'", "\\'")
    token = data.get("github_token", "")
    created_at = data.get("created_at", "Legacy Entry")
    is_active = data.get("is_active", True)
    
    has_linear = bool(data.get("linear_access_token") or data.get("linear_api_key"))
    tracker_badge = '<span class="px-2 py-1 bg-green-50 dark:bg-green-900/20 border border-green-200 text-green-700 dark:text-green-400 text-xs font-medium rounded">Connected</span>' if has_linear else '<span class="px-2 py-1 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 text-yellow-700 dark:text-yellow-400 text-xs font-medium rounded">Pending</span>'
    
    gh_status = data.get("github_webhook_status", "pending")
    if gh_status == "success": gh_badge = '<span class="px-2 py-1 bg-green-50 dark:bg-green-900/20 border border-green-200 text-green-700 dark:text-green-400 text-xs font-medium rounded">Active</span>'
    elif gh_status == "pending": gh_badge = '<span class="px-2 py-1 bg-gray-50 dark:bg-[#333333] border border-gray-200 text-gray-600 dark:text-gray-300 text-xs font-medium rounded">Pending Check</span>'
    else: gh_badge = '<span class="px-2 py-1 bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-700 dark:text-red-400 text-xs font-medium rounded">Failed</span>'
    
    has_lin_str = "true" if has_linear else "false"
    wh_secret = data.get("webhook_secret", "")
    bg_color = "bg-blue-600" if is_active else "bg-gray-300 dark:bg-[#333333]"
    translate_dot = "translate-x-5" if is_active else "translate-x-0"

    return f"""
    <div class="bg-white dark:bg-log-surface p-6 rounded border border-gray-200 dark:border-log-border flex flex-col justify-between relative">
        <div>
            <div class="flex justify-between items-start mb-1">
                <h3 class="text-lg font-medium text-gray-800 dark:text-log-text break-all pr-2">{proj_name}</h3>
                <div class="flex space-x-2 flex-shrink-0">
                    <button onclick="event.preventDefault(); event.stopPropagation(); openModal('{proj_id}', '{proj_name}', '{repo}', '{token}', '{gh_status}', {has_lin_str}, '{wh_secret}')" class="text-gray-400 hover:text-blue-600 transition bg-transparent p-1" title="Edit Project">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z"></path></svg>
                    </button>
                    <button onclick="event.preventDefault(); event.stopPropagation(); deleteProject('{proj_id}')" class="text-gray-400 hover:text-red-600 transition bg-transparent p-1" title="Delete Project">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                    </button>
                </div>
            </div>
            
            <p class="text-xs text-gray-400 mb-4">Added: {created_at}</p>
            
            <div class="text-sm text-gray-500 dark:text-gray-400 mb-4 flex items-center">
                <svg class="w-4 h-4 mr-1 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24"><path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/></svg>
                <a href="https://github.com/{repo}" target="_blank" class="hover:text-blue-500 transition hover:underline">{repo}</a>
            </div>
        </div>
        
        <div>
            <div class="flex items-center justify-between border-t border-gray-100 dark:border-log-border pt-3 mt-2">
                <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">1. GitHub Webhook</span>
                {gh_badge}
            </div>
            <div class="flex items-center justify-between border-t border-gray-100 dark:border-log-border pt-3 mt-3">
                <span class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">2. Issue Tracking</span>
                {tracker_badge}
            </div>

            <div class="mt-4 pt-4 border-t border-gray-100 dark:border-log-border flex items-center justify-between">
                <a href="/project/{proj_id}" class="text-blue-500 text-sm font-medium hover:underline transition">Open Workspace →</a>
                <button type="button" onclick="event.preventDefault(); event.stopPropagation(); toggleActive('{proj_id}', {str(not is_active).lower()})" class="flex items-center cursor-pointer" title="Pause or Resume AI Processing">
                    <span class="text-xs font-medium text-gray-500 dark:text-gray-400 mr-2 uppercase">{"Active" if is_active else "Paused"}</span>
                    <div class="w-10 h-5 flex items-center {bg_color} rounded-full p-1 transition duration-300 ease-in-out">
                        <div class="bg-white w-3.5 h-3.5 rounded-full transform {translate_dot} transition duration-300 ease-in-out"></div>
                    </div>
                </button>
            </div>
        </div>
    </div>
    """

def get_modal_html():
    # Dynamically grab the base domain from the environment
    base_uri = os.getenv("LINEAR_REDIRECT_URI", "")
    if not base_uri:
        webhook_target = "https://<YOUR_DOMAIN_OR_NGROK>/webhooks/pr-build-failed"
    else:
        webhook_target = base_uri.replace("/auth/linear/callback", "/webhooks/pr-build-failed")

    return f"""
    <div id="panel-overlay" onclick="closeModal()" class="fixed inset-0 bg-gray-900 bg-opacity-60 backdrop-blur-sm hidden z-40 transition-opacity"></div>

    <div id="onboard-panel" class="fixed inset-y-0 right-0 z-50 w-full max-w-xl bg-white dark:bg-log-surface shadow-2xl transform translate-x-full transition-transform duration-300 ease-in-out flex flex-col border-l border-gray-200 dark:border-log-border">
        
        <div class="px-8 py-5 border-b border-gray-200 dark:border-log-border flex justify-between items-center bg-gray-50 dark:bg-[#1e1e1e] flex-shrink-0">
            <div>
                <h2 id="modal-title" class="text-2xl font-bold text-gray-800 dark:text-log-text">New Project</h2>
                <p id="modal-subtitle" class="text-sm text-gray-600 dark:text-gray-400 mt-1">Configure your AI Developer workspace.</p>
            </div>
            <button onclick="closeModal()" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition p-2 rounded-full hover:bg-gray-200 dark:hover:bg-[#333333]">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>

        <div class="flex-1 overflow-y-auto">
            
            <div id="connection-health" class="px-8 py-4 bg-gray-100 dark:bg-[#1a1a1a] border-b border-gray-200 dark:border-log-border hidden">
                <h3 class="text-sm font-bold text-gray-800 dark:text-log-text mb-3 uppercase tracking-wider">Connection Health</h3>
                <div class="grid grid-cols-2 gap-4">
                    <div class="flex items-center text-sm text-gray-700 dark:text-gray-300">
                        <span id="health-repo" class="w-3 h-3 rounded-full bg-gray-400 mr-2 shadow-sm"></span> Repository Access
                    </div>
                    <div class="flex items-center text-sm text-gray-700 dark:text-gray-300">
                        <span id="health-webhooks" class="w-3 h-3 rounded-full bg-gray-400 mr-2 shadow-sm"></span> GitHub Webhooks
                    </div>
                    <div class="flex items-center text-sm text-gray-700 dark:text-gray-300">
                        <span id="health-linear" class="w-3 h-3 rounded-full bg-gray-400 mr-2 shadow-sm"></span> Issue Tracker
                    </div>
                </div>
            </div>

            <div class="py-2 bg-white dark:bg-log-surface">
                
                <div>
                    <button type="button" onclick="toggleAccordion('step-1')" class="w-full flex justify-between items-center group focus:outline-none px-8 py-4">
                        <div class="text-left pr-4">
                            <h3 class="text-base font-bold text-gray-800 dark:text-gray-200 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">Step 1: GitHub Configuration</h3>
                            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 font-normal">The AI needs read/write access to your repository to analyze code, write features, and automatically create Pull Requests.</p>
                        </div>
                        <svg id="icon-step-1" class="w-5 h-5 text-gray-400 group-hover:text-blue-500 transform transition-transform duration-300 rotate-180 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
                    </button>
                    
                    <div id="content-step-1" class="overflow-hidden transition-all duration-500 ease-in-out max-h-[1500px] opacity-100">
                        <div class="px-8 pb-8 pt-2">
                            <form id="step-1" class="space-y-6">
                                
                                <input type="hidden" id="project-id" value="">
                                
                                <div>
                                    <label class="block text-sm font-bold text-gray-700 dark:text-gray-300">Project Name</label>
                                    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-2">A friendly name for your dashboard.</p>
                                    <input type="text" id="project-name" required class="block w-full rounded-md border border-gray-300 dark:border-log-border shadow-sm p-2.5 focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-[#1e1e1e] dark:text-log-text" placeholder="e.g. Acme Corp Web App">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-bold text-gray-700 dark:text-gray-300 flex items-center">
                                        GitHub Repository
                                        <div class="relative ml-2 flex items-center">
                                            <svg class="w-4 h-4 text-gray-400 cursor-help tooltip-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                            <div class="tooltip-text hidden absolute z-10 w-64 p-2 mt-1 text-xs text-white bg-gray-800 rounded-lg shadow-lg left-6 top-0">Exact URL slug. Token must have access to this.</div>
                                        </div>
                                    </label>
                                    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-2">Copy the exact path from your GitHub URL (Format: <strong>Organization/Repository</strong>).</p>
                                    <input type="text" id="github-repo" required class="block w-full rounded-md border border-gray-300 dark:border-log-border shadow-sm p-2.5 focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-[#1e1e1e] dark:text-log-text" placeholder="e.g. facebook/react">
                                </div>
                                
                                <div>
                                    <label class="block text-sm font-bold text-gray-700 dark:text-gray-300">GitHub Fine-Grained Token</label>
                                    <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-2 leading-relaxed">
                                        Go to GitHub Settings &gt; Developer Settings &gt; Personal Access Tokens &gt; Fine-grained tokens. <br>
                                        Requires <strong>Read & Write</strong> access for: <strong>Contents</strong>, <strong>Workflows</strong>, and <strong>Pull Requests</strong>.
                                    </p>
                                    <div class="relative">
                                        <input type="password" id="github-token" required class="block w-full rounded-md border border-gray-300 dark:border-log-border shadow-sm p-2.5 pr-10 focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-[#1e1e1e] dark:text-log-text" placeholder="github_pat_...">
                                        <button type="button" onclick="togglePasswordVisibility('github-token', 'eye-icon-token')" class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition">
                                            <svg id="eye-icon-token" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
                                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.543 7-1.275 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>
                                            </svg>
                                        </button>
                                    </div>
                                </div>
                                
                                <div class="flex justify-end pt-2">
                                    <button type="button" id="btn-test-connection" onclick="submitOnboardForm(true)" class="text-sm bg-gray-900 hover:bg-black dark:bg-gray-200 dark:hover:bg-white dark:text-black text-white font-bold py-2.5 px-5 rounded-lg transition shadow-sm">
                                        Test Connection & Inject Webhooks
                                    </button>
                                </div>
                            </form>
                        </div>
                    </div>
                </div>

                <div id="step-2-container" class="border-t-2 border-dotted border-gray-200 dark:border-gray-700 opacity-50 pointer-events-none transition-opacity duration-300">
                    <button type="button" onclick="toggleAccordion('step-2')" class="w-full flex justify-between items-center group focus:outline-none px-8 py-4">
                        <div class="text-left pr-4">
                            <h3 class="text-base font-bold text-gray-800 dark:text-gray-200 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">Step 2: Issue Tracking</h3>
                            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 font-normal">Select your team's project management tool so your AI can automatically read tickets, update statuses, and notify you when PRs are ready.</p>
                        </div>
                        <svg id="icon-step-2" class="w-5 h-5 text-gray-400 group-hover:text-blue-500 transform transition-transform duration-300 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
                    </button>
                    
                    <div id="content-step-2" class="overflow-hidden transition-all duration-500 ease-in-out max-h-0 opacity-0">
                        <div class="px-8 pb-8 pt-2">
                            <p class="text-xs text-orange-500 mb-4 font-medium" id="step-2-warning">Please save Step 1 first to unlock this section.</p>
                            
                            <div class="space-y-3">
                                <div class="border border-gray-200 dark:border-log-border rounded-lg p-5 flex items-center justify-between bg-white dark:bg-[#1e1e1e] shadow-sm">
                                    <div class="flex items-center space-x-4">
                                        <div class="w-10 h-10 bg-gray-900 dark:bg-black rounded-lg flex items-center justify-center text-white font-bold text-xl"><svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm-1.87 17.5l-2.06-2.06L13.63 9.88l2.06 2.06-5.56 5.56z"/></svg></div>
                                        <div>
                                            <h4 class="font-bold text-gray-800 dark:text-log-text text-sm">Linear</h4>
                                            <p class="text-xs text-gray-500 dark:text-gray-400" id="linear-status-text">Not connected</p>
                                        </div>
                                    </div>
                                    <div class="flex space-x-2">
                                        <a id="linear-oauth-btn" href="#" target="_blank" class="text-sm bg-gray-900 hover:bg-black dark:bg-gray-200 dark:hover:bg-white dark:text-black text-white font-bold py-2 px-4 rounded-lg transition shadow-sm">Connect</a>
                                        <button id="linear-verify-btn" type="button" onclick="verifyLinearWebhook()" class="hidden text-sm bg-gray-100 hover:bg-gray-200 dark:bg-[#333333] dark:hover:bg-[#444444] text-gray-700 dark:text-gray-300 font-bold py-2 px-4 rounded-lg border border-gray-300 dark:border-log-border transition shadow-sm">Verify</button>
                                        <button id="linear-disconnect-btn" type="button" onclick="disconnectLinear()" class="hidden text-sm bg-red-50 hover:bg-red-100 text-red-600 font-bold py-2 px-4 rounded-lg border border-red-200 transition shadow-sm">Disconnect</button>
                                    </div>
                                </div>
                                <div class="border border-gray-100 dark:border-log-border rounded-lg p-5 flex items-center justify-between opacity-60 bg-gray-50 dark:bg-[#1e1e1e]">
                                    <div class="flex items-center space-x-4">
                                        <div class="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center text-white font-bold text-xl shadow-sm">
                                            <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M11.53 2c.48 0 .96.18 1.33.55l8.59 8.59c.73.73.73 1.92 0 2.66l-8.59 8.59c-.37.37-.85.55-1.33.55s-.96-.18-1.33-.55l-8.59-8.59c-.73-.73-.73-1.92 0-2.66l8.59-8.59c.37-.37.85-.55 1.33-.55zm-4.3 11.41l2.97-2.97-2.97-2.97-2.97 2.97 2.97 2.97zm8.59 0l2.97-2.97-2.97-2.97-2.97 2.97 2.97 2.97zm-4.29 4.3l2.97-2.97-2.97-2.97-2.97 2.97 2.97 2.97z"/></svg>
                                        </div>
                                        <div>
                                            <h4 class="font-bold text-gray-800 dark:text-log-text text-sm">Jira Software</h4>
                                            <p class="text-xs text-gray-500 dark:text-gray-400">Coming soon</p>
                                        </div>
                                    </div>
                                    <div>
                                        <button type="button" disabled class="text-sm bg-gray-200 dark:bg-[#333333] text-gray-400 dark:text-gray-500 font-bold py-2 px-4 rounded-lg cursor-not-allowed">Connect</button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="border-t-2 border-dotted border-gray-200 dark:border-gray-700">
                    <button type="button" onclick="toggleAccordion('step-3')" class="w-full flex justify-between items-center group focus:outline-none px-8 py-4">
                        <div class="text-left pr-4">
                            <h3 class="text-base font-bold text-gray-800 dark:text-gray-200 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors">Step 3: CI/CD Setup (Optional)</h3>
                            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 font-normal">To allow the AI to automatically catch failed deployments and fix build errors, you need to add this manual integration to your GitHub Actions.</p>
                        </div>
                        <svg id="icon-step-3" class="w-5 h-5 text-gray-400 group-hover:text-blue-500 transform transition-transform duration-300 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"></path></svg>
                    </button>
                    
                    <div id="content-step-3" class="overflow-hidden transition-all duration-500 ease-in-out max-h-0 opacity-0">
                        <div class="px-8 pb-8 pt-2 space-y-8">
                            
                            <div>
                                <label class="block text-sm font-bold text-gray-700 dark:text-gray-300">Step A: Add GitHub Secret</label>
                                <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-2">Go to your Repository &gt; Settings &gt; Secrets and Variables &gt; Actions. Create a new secret named <code class="bg-gray-100 dark:bg-[#2d2d2d] px-1 rounded text-red-500">AGENCY_WEBHOOK_SECRET</code> with this exact value:</p>
                                <div class="relative">
                                    <input type="password" id="snippet-webhook-secret" readonly class="block w-full rounded-md border border-gray-300 dark:border-log-border shadow-sm p-2.5 pr-10 focus:ring-blue-500 focus:border-blue-500 bg-white dark:bg-[#1e1e1e] dark:text-log-text outline-none" value="Save project first to generate secret...">
                                    <button type="button" onclick="togglePasswordVisibility('snippet-webhook-secret', 'eye-icon-secret')" class="absolute inset-y-0 right-0 pr-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition">
                                        <svg id="eye-icon-secret" class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path>
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.543 7-1.275 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>
                                        </svg>
                                    </button>
                                </div>
                            </div>
                            
                            <div>
                                <label class="block text-sm font-bold text-gray-700 dark:text-gray-300">Step B: Update Workflow YAML</label>
                                <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-2">Paste this snippet at the <strong>very end</strong> of your deployment <code>.yml</code> file (e.g., your Firebase or Vercel deploy action).</p>
                                
                                <textarea id="yaml-template" class="hidden">      - name: Report Build Failure to AI Agency
        if: failure()
        env:
          GH_TOKEN: ${{{{ github.token }}}}
        run: |
          # 1. Smart Extractor: Look for a trapped build.log file first
          BUILD_LOG_PATH=$(find . -name "build.log" | head -n 1)
          
          if [ -n "$BUILD_LOG_PATH" ]; then
            RAW_LOG=$(tail -c 2500 "$BUILD_LOG_PATH")
            LOG_SOURCE="Extracted from $BUILD_LOG_PATH"
          else
            # 2. Fallback: Use GitHub CLI to grab native terminal stdout
            RAW_LOG=$(gh run view ${{{{ github.run_id }}}} --log-failed | tail -c 2500 || echo "Failed to fetch native logs.")
            LOG_SOURCE="Extracted from GitHub Native Logs"
          fi
          
          # 3. Format cleanly for the AI Prompt
          ERROR_LOG="=== LOG SOURCE: $LOG_SOURCE ===
          $RAW_LOG"
          
          ESCAPED_LOG=$(echo "$ERROR_LOG" | jq -Rs .)
          
          curl -X POST -H "Content-Type: application/json" \\
          -H "X-Agency-Signature: ${{{{ secrets.AGENCY_WEBHOOK_SECRET }}}}" \\
          -d "{{\\"tenant_id\\": \\"__TENANT_ID__\\", \\"branch_name\\": \\"${{{{ github.head_ref || github.ref_name }}}}\\", \\"commit_sha\\": \\"${{{{ github.sha }}}}\\", \\"error_log\\": $ESCAPED_LOG}}" \\
          {webhook_target}</textarea>
                                
                                <div class="relative mt-2 group">
                                    <pre id="yaml-pre-container" class="block w-full rounded-md border border-gray-300 dark:border-log-border shadow-sm p-4 bg-white dark:bg-[#1e1e1e] text-gray-800 dark:text-gray-300 text-xs overflow-x-auto font-mono leading-relaxed transition-all duration-200"><code id="yaml-snippet-code">Save project first to generate code...</code></pre>
                                    <button type="button" onclick="copyYamlSnippet(this)" class="absolute top-3 right-3 flex items-center text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition bg-white dark:bg-[#2d2d2d] rounded-md p-1.5 shadow-sm border border-gray-200 dark:border-log-border" title="Copy code">
                                        <svg class="w-3.5 h-3.5 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                                        <span class="btn-text font-bold text-xs">Copy</span>
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

            </div>
        </div>

        <div class="px-8 py-5 border-t border-gray-200 dark:border-log-border flex justify-end space-x-3 bg-gray-50 dark:bg-[#1e1e1e] flex-shrink-0">
            <button type="button" onclick="closeModal()" class="px-5 py-2.5 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-[#2d2d2d] border border-gray-300 dark:border-log-border rounded-lg hover:bg-gray-50 dark:hover:bg-[#3d3d3d] transition shadow-sm">
                Cancel
            </button>
            <button type="button" onclick="submitOnboardForm(false)" class="px-5 py-2.5 text-sm font-medium text-white bg-blue-600 border border-transparent rounded-lg hover:bg-blue-700 transition shadow-sm">
                Save Workspace Configuration
            </button>
        </div>

    </div>
    """

# ==========================================
# 5. UI ROUTES
# ==========================================
@router.get("/", response_class=HTMLResponse)
def admin_dashboard():
    vault = get_vault()
    cards_html = ""
    if not vault:
        cards_html = """
        <div class="col-span-full text-center py-16 bg-white dark:bg-log-surface rounded-xl border border-dashed border-gray-300 dark:border-log-border">
            <h3 class="text-lg font-medium text-gray-900 dark:text-log-text mb-1">No projects yet</h3>
            <p class="text-gray-500 dark:text-gray-400 mb-4">Click "New Project" to add your first workspace.</p>
            <button onclick="openModal()" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-6 rounded-lg transition shadow-sm">
                New Project
            </button>
        </div>
        """
    else:
        for proj_id, data in vault.items():
            cards_html += generate_card_html(proj_id, data)

    return f"""
    <html>
        <head>
            <title>AI Agency Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <script>
                tailwind.config = {{ 
                    darkMode: 'class',
                    theme: {{
                        extend: {{
                            colors: {{
                                'log-bg': '#1e1e1e',
                                'log-surface': '#252526',
                                'log-border': '#333333',
                                'log-text': '#d4d4d4'
                            }}
                        }}
                    }}
                }}
                
                // Instantly apply theme before page paint to prevent flicker
                if (localStorage.theme === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
                    document.documentElement.classList.add('dark');
                }} else {{
                    document.documentElement.classList.remove('dark');
                }}

                function toggleTheme() {{
                    document.documentElement.classList.toggle('dark');
                    localStorage.theme = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
                }}
                // Listen for messages from the Linear OAuth popup
                // Bulletproof cross-tab communication using LocalStorage
                window.addEventListener('storage', (event) => {{
                    if (event.key === 'LINEAR_AUTH_SUCCESS') {{
                        const statusText = document.getElementById('linear-status-text');
                        if (statusText) {{
                            statusText.innerText = "Connected and active";
                            statusText.classList.replace("text-gray-500", "text-green-600");
                            
                            document.getElementById('linear-oauth-btn').classList.add('hidden');
                            document.getElementById('linear-disconnect-btn').classList.remove('hidden');
                        }}
                    }} else if (event.key === 'LINEAR_AUTH_FAILED') {{
                        alert("Linear connection was cancelled.");
                    }}
                }});
            </script>
            <style>.tooltip-icon:hover + .tooltip-text {{ display: block; }}</style>
        </head>
        <body class="bg-gray-50 dark:bg-log-bg transition-colors duration-200 h-screen overflow-hidden flex flex-col">
            
            {get_layout_header()}

            <div class="flex flex-1 overflow-hidden">
                {get_sidebar_html("dashboard")}
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-5xl mx-auto">
                        <div class="flex justify-between items-center mb-8 pb-4 border-b border-gray-200 dark:border-gray-700">
                            <div>
                                <h1 class="text-2xl font-medium mb-6 text-gray-900 dark:text-log-text">Dashboard</h1>
                                <p class="text-gray-600 dark:text-gray-400 mt-1">Manage your active AI workspaces.</p>
                            </div>
                            <button onclick="openModal()" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2 px-6 rounded-lg shadow-sm transition flex items-center">
                                <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                                New Project
                            </button>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3 gap-6">
                            {cards_html}
                        </div>
                    </div>
                </div>
            </div>

            {get_modal_html()}
            
            <script>
                function updateYamlSnippet(tenantId) {{
                    const template = document.getElementById('yaml-template').value;
                    document.getElementById('yaml-snippet-code').innerText = template.replace('__TENANT_ID__', tenantId);
                }}

                async function copyYamlSnippet(btn) {{
                    const codeText = document.getElementById('yaml-snippet-code').innerText;
                    try {{
                        await navigator.clipboard.writeText(codeText);
                        const span = btn.querySelector('.btn-text');
                        const originalText = span.innerText;
                        span.innerText = "Copied!";
                        btn.classList.add("text-green-600", "dark:text-green-400");
                        setTimeout(() => {{
                            span.innerText = originalText;
                            btn.classList.remove("text-green-600", "dark:text-green-400");
                        }}, 2000);
                    }} catch (err) {{
                        alert("Failed to copy code.");
                    }}
                }}

                function togglePasswordVisibility(inputId, iconId) {{
                    const input = document.getElementById(inputId);
                    const icon = document.getElementById(iconId);
                    if (input.type === "password") {{
                        input.type = "text";
                        icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"></path>';
                    }} else {{
                        input.type = "password";
                        icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.543 7-1.275 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>';
                    }}
                }}

                function toggleBlur(containerId, iconId) {{
                    const container = document.getElementById(containerId);
                    const icon = document.getElementById(iconId);
                    if (container.classList.contains('blur-sm')) {{
                        container.classList.remove('blur-sm', 'select-none');
                        icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21"></path>';
                    }} else {{
                        container.classList.add('blur-sm', 'select-none');
                        icon.innerHTML = '<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.543 7-1.275 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path>';
                    }}
                }}

                function toggleAccordion(stepId) {{
                    const steps = ['step-1', 'step-2', 'step-3'];

                    steps.forEach(id => {{
                        const content = document.getElementById('content-' + id);
                        const icon = document.getElementById('icon-' + id);

                        if (id === stepId) {{
                            if (content.classList.contains('max-h-0')) {{
                                content.classList.remove('max-h-0', 'opacity-0');
                                content.classList.add('max-h-[1500px]', 'opacity-100');
                                icon.classList.add('rotate-180');
                            }} else {{
                                content.classList.remove('max-h-[1500px]', 'opacity-100');
                                content.classList.add('max-h-0', 'opacity-0');
                                icon.classList.remove('rotate-180');
                            }}
                        }} else {{
                            content.classList.remove('max-h-[1500px]', 'opacity-100');
                            content.classList.add('max-h-0', 'opacity-0');
                            icon.classList.remove('rotate-180');
                        }}
                    }});
                }}

                function openModal(pId = '', pName = '', repo = '', token = '', ghStatus = '', hasLin = false, whSecret = '') {{
                    document.getElementById('project-id').value = pId;
                    document.getElementById('project-name').value = pName;
                    document.getElementById('github-repo').value = repo;
                    document.getElementById('github-token').value = token;
                    
                    const panel = document.getElementById('onboard-panel');
                    const overlay = document.getElementById('panel-overlay');
                    const step2Container = document.getElementById('step-2-container');
                    const warning2 = document.getElementById('step-2-warning');
                    const healthSection = document.getElementById('connection-health');
                    
                    ['step-1', 'step-2', 'step-3'].forEach(id => {{
                        const content = document.getElementById('content-' + id);
                        const icon = document.getElementById('icon-' + id);
                        if (id === 'step-1') {{
                            content.classList.remove('max-h-0', 'opacity-0');
                            content.classList.add('max-h-[1500px]', 'opacity-100');
                            icon.classList.add('rotate-180');
                        }} else {{
                            content.classList.remove('max-h-[1500px]', 'opacity-100');
                            content.classList.add('max-h-0', 'opacity-0');
                            icon.classList.remove('rotate-180');
                        }}
                    }});
                    
                    if (pId !== '') {{
                        document.getElementById('linear-oauth-btn').href = `/install/linear?project_id=${{pId}}`;
                        document.getElementById('modal-title').innerText = "Workspace Settings";
                        document.getElementById('modal-subtitle').innerText = `Update details for ${{pName}}.`;
                        
                        updateYamlSnippet(pId);
                        document.getElementById('snippet-webhook-secret').value = whSecret || "Save project to generate secret...";
                        
                        step2Container.classList.remove('opacity-50', 'pointer-events-none');
                        warning2.classList.add('hidden');
                        
                        healthSection.classList.remove('hidden');
                        document.getElementById('health-repo').className = 'w-3 h-3 rounded-full mr-2 shadow-sm ' + (ghStatus === 'success' ? 'bg-green-500' : 'bg-red-500');
                        document.getElementById('health-webhooks').className = 'w-3 h-3 rounded-full mr-2 shadow-sm ' + (ghStatus === 'success' ? 'bg-green-500' : 'bg-yellow-500');
                        document.getElementById('health-linear').className = 'w-3 h-3 rounded-full mr-2 shadow-sm ' + (hasLin ? 'bg-green-500' : 'bg-gray-400');

                        if (hasLin) {{
                            document.getElementById('linear-status-text').innerText = "Connected and active";
                            document.getElementById('linear-status-text').classList.replace("text-gray-500", "text-green-600");
                            document.getElementById('linear-oauth-btn').classList.add('hidden');
                            document.getElementById('linear-disconnect-btn').classList.remove('hidden');
                            document.getElementById('linear-verify-btn').classList.remove('hidden');
                        }} else {{
                            document.getElementById('linear-status-text').innerText = "Not connected";
                            document.getElementById('linear-status-text').classList.replace("text-green-600", "text-gray-500");
                            document.getElementById('linear-oauth-btn').classList.remove('hidden');
                            document.getElementById('linear-disconnect-btn').classList.add('hidden');
                            document.getElementById('linear-verify-btn').classList.add('hidden');
                        }}

                    }} else {{
                        document.getElementById('linear-oauth-btn').href = "#"; 
                        document.getElementById('modal-title').innerText = "New Project";
                        document.getElementById('modal-subtitle').innerText = "Configure your AI Developer workspace.";
                        document.getElementById('linear-status-text').innerText = "Not connected";
                        document.getElementById('linear-status-text').classList.replace("text-green-600", "text-gray-500");
                        document.getElementById('linear-oauth-btn').classList.remove('hidden');
                        document.getElementById('linear-disconnect-btn').classList.add('hidden');
                        document.getElementById('linear-verify-btn').classList.add('hidden');
                        
                        updateYamlSnippet("YOUR_TENANT_ID");
                        document.getElementById('snippet-webhook-secret').value = "Save project first...";
                        
                        step2Container.classList.add('opacity-50', 'pointer-events-none');
                        warning2.classList.remove('hidden');
                        healthSection.classList.add('hidden');
                    }}

                    overlay.classList.remove('hidden');
                    setTimeout(() => {{
                        panel.classList.remove('translate-x-full');
                    }}, 10);
                }}

                function closeModal() {{ 
                    const panel = document.getElementById('onboard-panel');
                    const overlay = document.getElementById('panel-overlay');
                    
                    panel.classList.add('translate-x-full');
                    
                    setTimeout(() => {{
                        overlay.classList.add('hidden');
                        window.location.reload(); 
                    }}, 300); 
                }}

                async function toggleActive(pId, newState) {{
                    try {{
                        await fetch(`/api/project/${{pId}}/toggle`, {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ is_active: newState }})
                        }});
                        window.location.reload();
                    }} catch (err) {{ alert("Failed to toggle status."); }}
                }}

                async function submitOnboardForm(runTest) {{
                    const form = document.getElementById('step-1');
                    if (!form.checkValidity()) {{ form.reportValidity(); return; }}

                    const testBtn = document.getElementById('btn-test-connection');
                    const origTxt = testBtn.innerText;
                    if (runTest) {{ testBtn.innerText = "Testing & Injecting..."; testBtn.classList.add("opacity-70"); }}

                    const payload = {{
                        project_id: document.getElementById('project-id').value,
                        project_name: document.getElementById('project-name').value,
                        github_repo: document.getElementById('github-repo').value,
                        github_token: document.getElementById('github-token').value,
                        run_test: runTest
                    }};

                    try {{
                        const res = await fetch('/api/project', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(payload) }});
                        const data = await res.json();
                        
                        if (data.project_id) {{
                            document.getElementById('project-id').value = data.project_id;
                            document.getElementById('linear-oauth-btn').href = `/install/linear?project_id=${{data.project_id}}`;
                            
                            if (data.webhook_secret) {{
                                updateYamlSnippet(data.project_id);
                                document.getElementById('snippet-webhook-secret').value = data.webhook_secret;
                            }}
                        }}
                        
                        if (runTest) {{
                            testBtn.innerText = origTxt; testBtn.classList.remove("opacity-70");
                            if (!data.success) alert("GitHub Test failed! Draft saved, verify permissions.");
                            else {{
                                alert("GitHub Webhooks successfully injected!");
                                document.getElementById('step-2-container').classList.remove('opacity-50', 'pointer-events-none');
                                document.getElementById('step-2-warning').classList.add('hidden');
                                
                                document.getElementById('connection-health').classList.remove('hidden');
                                document.getElementById('health-repo').className = 'w-3 h-3 rounded-full mr-2 shadow-sm bg-green-500';
                                document.getElementById('health-webhooks').className = 'w-3 h-3 rounded-full mr-2 shadow-sm bg-green-500';
                                
                                toggleAccordion('step-2');
                            }}
                        }} else {{
                            closeModal(); 
                        }}
                    }} catch (err) {{ alert("Network error."); }}
                }}

                async function verifyLinearWebhook() {{
                    const pId = document.getElementById('project-id').value;
                    if (!pId) return;
                    
                    const btn = document.getElementById('linear-verify-btn');
                    const origText = btn.innerText;
                    btn.innerText = "Checking...";
                    
                    try {{
                        const res = await fetch(`/api/project/${{pId}}/linear/test`);
                        const data = await res.json();
                        
                        if (data.error_type === "admin_required") {{
                            btn.innerText = "Admin Required";
                            btn.classList.add("text-orange-600", "border-orange-300");
                            alert("Only workspace admins can verify webhook status. If an admin connected this project, you are good to go!");
                        }} else if (data.exists) {{
                            btn.innerText = "✓ Verified";
                            btn.classList.add("text-green-600", "border-green-300");
                        }} else {{
                            btn.innerText = "⚠ Missing";
                            btn.classList.add("text-yellow-600", "border-yellow-300");
                            alert("Webhook not found. Try disconnecting and reconnecting.");
                        }}
                    }} catch (err) {{
                        btn.innerText = "Error";
                    }}

                    setTimeout(() => {{
                        btn.innerText = origText;
                        btn.className = "text-sm bg-gray-100 hover:bg-gray-200 dark:bg-[#333333] dark:hover:bg-[#444444] text-gray-700 dark:text-gray-300 font-bold py-2.5 px-4 rounded-lg border border-gray-300 dark:border-log-border transition shadow-sm";
                    }}, 4000);
                }}

                async function deleteProject(pId) {{
                    if (confirm("Remove this project permanently?")) {{
                        const res = await fetch(`/api/project/${{pId}}`, {{ method: 'DELETE' }});
                        if (res.ok) window.location.reload();
                    }}
                }}

                async function disconnectLinear() {{
                    const pId = document.getElementById('project-id').value;
                    if (!pId) return;
                    if (confirm("Disconnect Linear?")) {{
                        const res = await fetch(`/api/project/${{pId}}/linear`, {{ method: 'DELETE' }});
                        if (res.ok) window.location.reload();
                    }}
                }}
            </script>
        </body>
    </html>
    """

# ==========================================
# 6. LINEAR OAUTH ROUTES
# ==========================================
@router.get("/install/linear")
def install_linear(project_id: str):
    client_id = os.getenv("LINEAR_CLIENT_ID")
    redirect_uri = os.getenv("LINEAR_REDIRECT_URI")
    url = f"https://linear.app/oauth/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=read,write,issues:create,comments:create&state={project_id}&prompt=consent&actor=app"
    return RedirectResponse(url=url)

@router.get("/auth/linear/callback", response_class=HTMLResponse)
async def linear_callback(state: str, code: str = None, error: str = None):
    # 1. Handle Cancelled / Denied Auth
    if error or not code: 
        return """
        <html>
            <body style="background-color: #1e1e1e; color: #d4d4d4; font-family: sans-serif; text-align: center; padding: 50px;">
                <h2>Connection Cancelled</h2>
                <p style="color: #f43f5e;">You can close this tab and return to the dashboard.</p>
                <script>
                    localStorage.setItem('LINEAR_AUTH_FAILED', Date.now().toString());
                    setTimeout(() => { window.close(); }, 1000);
                </script>
            </body>
        </html>
        """

    payload = {
        "client_id": os.getenv("LINEAR_CLIENT_ID"),
        "client_secret": os.getenv("LINEAR_CLIENT_SECRET"),
        "redirect_uri": os.getenv("LINEAR_REDIRECT_URI"),
        "code": code,
        "grant_type": "authorization_code"
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post("https://api.linear.app/oauth/token", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})

    if response.status_code != 200:
        return f"<html><body><h3>Linear Auth Failed</h3><p>{response.text}</p></body></html>"

    access_token = response.json().get("access_token")
    save_tenant_secret(state, "linear_access_token", access_token)

    # Fetch Linear Organization ID to map webhooks
    graphql_query = "{ organization { id } }"
    async with httpx.AsyncClient() as client:
        org_res = await client.post(
            "https://api.linear.app/graphql",
            json={"query": graphql_query},
            headers={"Authorization": f"Bearer {access_token}"}
        )
        org_id = org_res.json().get("data", {}).get("organization", {}).get("id")
        if org_id:
            save_tenant_secret(state, "linear_org_id", org_id)
    
    # Automatically register the webhook in Linear (No duplicates)
    # We dynamically build the webhook URL using your existing redirect URI
    base_uri = os.getenv("LINEAR_REDIRECT_URI", "")
    
    # Do not attempt to register a webhook if we don't know our own domain.
    if not base_uri:
        print("[ERROR] Missing LINEAR_REDIRECT_URI in .env. Skipping Linear webhook registration.")
    else:
        base_webhook = base_uri.replace("/auth/linear/callback", "/webhook")
        saas_webhook_url = f"{base_webhook}?project_id={state}"

        async with httpx.AsyncClient() as client:
            # Check if we already have a webhook for this URL
            check_query = "{ webhooks { nodes { id url } } }"
            check_res = await client.post(
                "https://api.linear.app/graphql",
                json={"query": check_query},
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            # Safely parse the response, falling back to empty dicts/lists if Linear returns None
            check_json = check_res.json()
            if "errors" in check_json:
                print(f"\n[Linear API Error] Webhook Check: {check_json['errors']}")

            data_obj = check_json.get("data") or {}
            webhooks_obj = data_obj.get("webhooks") or {}
            existing_webhooks = webhooks_obj.get("nodes") or []
            
            webhook_id = next((wh.get("id") for wh in existing_webhooks if wh.get("url") == saas_webhook_url), None)
            
            # If it doesn't exist, create it
            if not webhook_id:
                create_query = """
                mutation webhookCreate($url: String!) {
                  webhookCreate(input: {
                    url: $url,
                    resourceTypes: ["Issue", "Comment"],
                    label: "AI Agency Developer",
                    allPublicTeams: true
                  }) {
                    webhook { id }
                  }
                }
                """
                create_res = await client.post(
                    "https://api.linear.app/graphql",
                    json={"query": create_query, "variables": {"url": saas_webhook_url}},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                
                create_json = create_res.json()
                if "errors" in create_json:
                    print(f"\n[Linear API Error] Webhook Create: {create_json['errors']}")
                
                # Safely parse the response (falls back to empty dicts if None)
                create_data = create_json.get("data") or {}
                webhook_create = create_data.get("webhookCreate") or {}
                webhook_obj = webhook_create.get("webhook") or {}
                webhook_id = webhook_obj.get("id")
            
            # Save the ID so we can delete it later!
            if webhook_id:
                save_tenant_secret(state, "linear_webhook_id", webhook_id)

    # Handle Successful Auth
    return """
    <html>
        <body style="background-color: #1e1e1e; color: #d4d4d4; font-family: sans-serif; text-align: center; padding: 50px;">
            <h2>Connection Successful!</h2>
            <p style="color: #4ade80;">Linear is connected. You can safely close this tab and return to the dashboard.</p>
            <script>
                // Use LocalStorage as a bulletproof bridge (bypasses window.opener security blocks)
                localStorage.setItem('LINEAR_AUTH_SUCCESS', Date.now().toString());
                
                // Attempt to close the tab automatically
                setTimeout(() => { window.close(); }, 1000);
            </script>
        </body>
    </html>
    """

@router.get("/project/{project_id}", response_class=HTMLResponse)
def project_workspace(project_id: str = Path(...)):
    vault = get_vault()
    project_data = vault.get(project_id)
    
    if not project_data:
        return HTMLResponse("<h1>Project Not Found</h1>", status_code=404)

    project_name = project_data.get("project_name", project_id)
    logs_data = get_sessions_by_project(project_id)

    # Build the Logs HTML server-side
    logs_html = ""
    if not logs_data:
        logs_html = '<p class="text-gray-500 dark:text-gray-400 italic">No history found for this project yet.</p>'
    else:
        for session in logs_data:
            t_id = session['ticket_id']
            s_id = session['session_id']
            last_active = session['last_active']
            logs_html += f"""
                <div class="flex items-center justify-between p-4 bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded-lg shadow-sm mb-3">
                    <div>
                        <div class="font-mono text-sm font-bold text-gray-900 dark:text-gray-100">{t_id}</div>
                        <div class="text-xs text-gray-500 mt-1">Session: {s_id}</div>
                    </div>
                    <div class="flex items-center gap-4">
                        <span class="text-xs text-gray-400">{last_active}</span>
                        <a href="/logs/{t_id}/{s_id}" target="_blank" class="text-xs bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 px-3 py-1 rounded hover:bg-gray-200 dark:hover:bg-gray-700 transition">View Full Log</a>
                    </div>
                </div>
            """

    html_content = f"""
    <html>
        <head>
            <title>{project_name} - AI Agency</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <script>
                tailwind.config = {{ darkMode: 'class', theme: {{ extend: {{ colors: {{ 'log-bg': '#1e1e1e', 'log-surface': '#252526', 'log-border': '#333333', 'log-text': '#d4d4d4' }} }} }} }};
            </script>
        </head>
        <body class="bg-gray-50 dark:bg-log-bg h-screen flex flex-col">
            {get_layout_header()}
            <div class="flex flex-1 overflow-hidden">
                {get_project_sidebar_html(project_id, project_name, "pipeline")}
                
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-5xl mx-auto">
                        <div class="flex justify-between items-center mb-8">
                            <div>
                                <h1 class="text-3xl font-medium text-gray-900 dark:text-white">Workspace</h1>
                                <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">Project ID: {project_id}</p>
                            </div>
                            <input type="hidden" id="project-id" value="{project_id}">
                        </div>

                        <div>
                            <div class="flex items-center gap-2 mb-4">
                                <span class="w-2 h-2 rounded-full bg-blue-500 animate-pulse"></span>
                                <h2 class="text-lg font-medium text-gray-900 dark:text-white">Live Tickets</h2>
                            </div>
                            <div id="pipeline-container" class="space-y-3 mb-8">
                                <!-- Unified loading text -->
                                <p class="text-sm text-gray-500 dark:text-gray-400">Fetching tickets...</p>
                            </div>
                            
                            <div class="flex items-center gap-2 mb-4 mt-8 pt-6 border-t border-gray-200 dark:border-log-border">
                                <h2 class="text-lg font-medium text-gray-900 dark:text-white">Completed & Dismissed Tickets</h2>
                            </div>
                            <div id="completed-pipeline-container" class="space-y-3">
                                <!-- Unified loading text -->
                                <p class="text-sm text-gray-500 dark:text-gray-400">Fetching tickets...</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <script>
                // Cache variables to prevent unnecessary re-renders
                let lastPipelinePayload = "";
                let lastCompletedPayload = "";

                async function fetchPipeline() {{
                    const pId = document.getElementById('project-id').value;
                    try {{
                        const res = await fetch('/api/project/' + pId + '/pipeline');
                        const data = await res.json();
                        
                        // Only re-render if the data actually changed
                        const currentPayload = JSON.stringify(data.tasks || []);
                        if (currentPayload === lastPipelinePayload) {{
                            return; // Exit silently without wiping the DOM
                        }}
                        lastPipelinePayload = currentPayload;

                        const container = document.getElementById('pipeline-container');
                        
                        if (!data.tasks || data.tasks.length === 0) {{
                            container.innerHTML = '<p class="text-sm text-gray-500 dark:text-gray-400 bg-white dark:bg-[#252526] p-4 rounded border border-gray-200 dark:border-log-border">No live tickets currently processing.</p>';
                            return;
                        }}

                        let html = '';
                        data.tasks.forEach(function(task) {{
                            // Added safety checks so the loop never breaks
                            let tId = task.task_id || 'Unknown';
                            let parts = tId.split('-');
                            let sId = parts.pop();           // Extracts the timestamp (1774207968)
                            let ticketId = parts.join('-');  // Rejoins the rest (ai-agency-ws-AI-6)
                            let statusStr = (task.status || 'Unknown').toUpperCase();
                            
                            // Default controls (Just View Logs)
                            let controls = '<a href="/logs/' + ticketId + '/' + sId + '" target="_blank" class="text-xs text-blue-500 hover:underline transition ml-2">View Logs</a>';
                            
                            // Add action buttons if the task is stuck or failed
                            if (task.status === "Failed" || task.status === "Pending") {{
                                let btnText = task.status === "Failed" ? "Restart" : "Force Start";
                                controls = '<button onclick="restartTask(event, \\'' + tId + '\\')" class="text-xs bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-400 hover:bg-blue-100 border border-blue-200 dark:border-blue-800 px-3 py-1 rounded transition">' + btnText + '</button>' +
                                           '<button onclick="terminateTask(\\'' + tId + '\\')" class="text-xs text-gray-500 hover:text-red-500 px-3 py-1 transition">Dismiss</button>' + 
                                           controls;
                            }}

                            if (task.status === "AWAITING_PREVIEW" || task.status === "BLOCKED_PR_BUILD") {{
                                controls = '<button onclick="syncGithub(event, \\'' + tId + '\\')" class="text-xs bg-gray-50 dark:bg-[#333333] text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 border border-gray-300 dark:border-log-border px-3 py-1 rounded transition flex items-center" title="Manually check GitHub Actions status">' +
                                           '<svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>' +
                                           'Sync Status</button>' + controls;
                            }}

                            // --- Add Orange color for Blocked state ---
                            let statusColor = "text-blue-600 bg-blue-50 dark:bg-blue-900/20 border-blue-200";
                            if (task.status === "Failed") statusColor = "text-red-600 bg-red-50 dark:bg-red-900/20 border-red-200";
                            else if (task.status === "BLOCKED_PR_BUILD") statusColor = "text-orange-600 bg-orange-50 dark:bg-orange-900/20 border-orange-200";

                            // --- 1-Line Status Reason Snippet ---
                            let reasonSnippet = '';
                            if (task.build_error_log) {{
                                let cleanLog = task.build_error_log.replace(/</g, "&lt;").replace(/>/g, "&gt;");
                                // Grab just the first line and truncate it so it doesn't break the layout
                                let shortLog = cleanLog.split('\\n')[0].substring(0, 85);
                                if (cleanLog.length > 85) shortLog += '...';
                                
                                // Color it red if it's a failure or blocked state, otherwise gray
                                let textClass = (task.status === "Failed" || task.status === "BLOCKED_PR_BUILD") 
                                    ? "text-red-500/90 dark:text-red-400/90" 
                                    : "text-gray-500";
                                    
                                reasonSnippet = '<div class="text-xs ' + textClass + ' mt-1 truncate max-w-md" title="' + cleanLog + '">' + shortLog + '</div>';
                            }}
                            
                            // --- Construct the Error Block if Blocked ---
                            let errorBlock = '';
                            if (task.status === "BLOCKED_PR_BUILD") {{
                                let errorLog = task.build_error_log || "No error log available.";
                                errorLog = errorLog.replace(/</g, "&lt;").replace(/>/g, "&gt;"); // Safely escape HTML
                                
                                errorBlock = '<div class="mt-4 pt-4 border-t border-red-100 dark:border-red-900/30">' +
                                    '<div class="flex justify-between items-center mb-2">' +
                                        '<p class="text-xs font-bold text-red-600 dark:text-red-400">Build Failed:</p>' +
                                        '<button onclick="copyBuildLog(event, \\'' + tId + '\\')" class="text-xs bg-gray-100 hover:bg-gray-200 dark:bg-[#333333] dark:hover:bg-[#444444] text-gray-700 dark:text-gray-300 font-bold py-1 px-2 rounded transition border border-gray-300 dark:border-log-border flex items-center"><svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg><span class="btn-text">Copy</span></button>' +
                                    '</div>' +
                                    '<div id="err-log-' + tId + '" class="bg-gray-900 text-gray-300 p-3 rounded text-xs font-mono overflow-x-auto mb-3 max-h-32 overflow-y-auto whitespace-pre-wrap">' + errorLog + '</div>' +
                                    '<div class="flex flex-col gap-2">' +
                                        '<div class="flex items-center gap-2">' +
                                            '<input type="text" id="feedback-' + tId + '" placeholder="Optional: Tell AI how to fix this..." class="flex-1 text-xs bg-gray-50 dark:bg-[#1e1e1e] border border-gray-300 dark:border-log-border rounded px-2 py-1.5 focus:outline-none focus:border-blue-500 text-gray-800 dark:text-gray-200">' +
                                            '<button onclick="resolveBuild(event, \\'' + tId + '\\', \\'code_fix\\')" class="text-xs bg-blue-600 hover:bg-blue-700 text-white font-medium px-3 py-1.5 rounded transition whitespace-nowrap">Ask AI to Fix</button>' +
                                        '</div>' +
                                        '<div class="flex items-center gap-2 mt-1">' +
                                            '<span class="text-xs text-gray-500 dark:text-gray-400">- OR -</span>' +
                                            '<button onclick="resolveBuild(event, \\'' + tId + '\\', \\'infra_fix\\')" class="text-xs bg-gray-100 dark:bg-[#333333] hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-gray-600 font-medium px-3 py-1.5 rounded transition whitespace-nowrap">I fixed it manually (Retry)</button>' +
                                        '</div>' +
                                    '</div>' +
                                '</div>';
                            }}

                            // --- Inject errorBlock into the card layout ---
                            // --- Inject reasonSnippet under the Task ID ---
                            html += '<div class="p-4 bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded mb-3 flex flex-col">' +
                                        '<div class="flex items-center justify-between">' +
                                            '<div class="flex items-center gap-4 overflow-hidden">' +
                                                '<div class="shrink-0 px-2 py-1 rounded text-xs font-medium border ' + statusColor + '">' +
                                                    statusStr +
                                                '</div>' +
                                                '<div class="min-w-0">' + 
                                                    '<div class="font-mono text-sm font-medium text-gray-900 dark:text-gray-100">' + tId + '</div>' +
                                                    reasonSnippet + 
                                                '</div>' +
                                            '</div>' +
                                            '<div class="flex items-center gap-2 shrink-0">' +
                                                controls +
                                            '</div>' +
                                        '</div>' +
                                        errorBlock + 
                                    '</div>';
                        }});
                        container.innerHTML = html;
                    }} catch (err) {{
                        console.error("Error fetching live pipeline:", err);
                        document.getElementById('pipeline-container').innerHTML = '<p class="text-sm text-red-500 p-4">Error loading live tickets.</p>';
                    }}
                }}

                async function fetchCompletedPipeline() {{
                    const pId = document.getElementById('project-id').value;
                    try {{
                        const res = await fetch('/api/project/' + pId + '/pipeline/completed');
                        const data = await res.json();
                        
                        // Only re-render if the data actually changed
                        const currentPayload = JSON.stringify(data.tasks || []);
                        if (currentPayload === lastCompletedPayload) {{
                            return; // Exit silently
                        }}
                        lastCompletedPayload = currentPayload;

                        const container = document.getElementById('completed-pipeline-container');

                        if (!data.tasks || data.tasks.length === 0) {{
                            container.innerHTML = '<p class="text-sm text-gray-500 dark:text-gray-400 bg-white dark:bg-[#252526] p-4 rounded border border-gray-200 dark:border-log-border">No completed tickets yet.</p>';
                            return;
                        }}

                        let html = '';
                        data.tasks.forEach(function(task) {{
                            // Added safety checks
                            let statusColor = task.status === "Terminated" ? "text-gray-600 bg-gray-50 dark:bg-[#333333] border-gray-200 dark:border-gray-600" : "text-green-600 bg-green-50 dark:bg-green-900/20 border-green-200";
                            let tId = task.task_id || 'Unknown';
                            let parts = tId.split('-');
                            let sId = parts.pop();           // Extracts the timestamp (1774207968)
                            let ticketId = parts.join('-');  // Rejoins the rest (ai-agency-ws-AI-6)
                            let timeStr = task.updated_at ? new Date(task.updated_at).toLocaleTimeString() : 'Unknown Time';
                            let statusStr = (task.status || 'Unknown').toUpperCase();
                            
                            html += '<div class="flex items-center justify-between p-4 bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded mb-3 opacity-80">' +
                                        '<div class="flex items-center gap-4">' +
                                            '<div class="px-2 py-1 rounded text-xs font-medium border ' + statusColor + '">' +
                                                statusStr +
                                            '</div>' +
                                            '<div>' +
                                                '<div class="font-mono text-sm font-medium text-gray-900 dark:text-gray-100">' + tId + '</div>' +
                                                '<div class="text-xs text-gray-500 mt-1">Finished: ' + timeStr + '</div>' +
                                            '</div>' +
                                        '</div>' +
                                        '<div class="flex items-center gap-3">' +
                                            '<a href="/logs/' + ticketId + '/' + sId + '" target="_blank" class="text-xs text-blue-500 hover:underline">View Logs</a>' +
                                        '</div>' +
                                    '</div>';
                        }});
                        container.innerHTML = html;
                    }} catch (err) {{ 
                        console.error("Error fetching completed pipeline:", err); 
                        document.getElementById('completed-pipeline-container').innerHTML = '<p class="text-sm text-red-500 p-4">Error loading completed tickets.</p>';
                    }}
                }}

                async function restartTask(event, taskId) {{
                    const pId = document.getElementById('project-id').value;
                    event.target.innerText = "Starting...";
                    event.target.disabled = true;
                    await fetch('/api/project/' + pId + '/task/' + taskId + '/restart', {{ method: 'POST' }});
                    fetchPipeline();
                }}

                async function terminateTask(taskId) {{
                    const pId = document.getElementById('project-id').value;
                    await fetch('/api/project/' + pId + '/task/' + taskId + '/terminate', {{ method: 'POST' }});
                    fetchPipeline();
                    fetchCompletedPipeline();
                }}

                async function syncGithub(event, taskId) {{
                    const pId = document.getElementById('project-id').value;
                    const btn = event.currentTarget;
                    const origHTML = btn.innerHTML;
                    
                    btn.innerHTML = "Syncing...";
                    btn.disabled = true;
                    btn.classList.add("opacity-50");

                    try {{
                        const res = await fetch('/api/project/' + pId + '/task/' + taskId + '/sync-github', {{ method: 'POST' }});
                        const data = await res.json();
                        alert(data.message);
                        fetchPipeline(); // Instantly refresh UI to reflect the new synced status
                    }} catch (err) {{
                        alert("Network error: Failed to reach the server to sync with GitHub.");
                    }} finally {{
                        btn.innerHTML = origHTML;
                        btn.disabled = false;
                        btn.classList.remove("opacity-50");
                    }}
                }}
                
                async function resolveBuild(event, taskId, resolutionType) {{
                    const pId = document.getElementById('project-id').value;
                    const btn = event.currentTarget;
                    const origHTML = btn.innerHTML;
                    
                    let feedback = '';
                    if (resolutionType === 'code_fix') {{
                        const inputField = document.getElementById('feedback-' + taskId);
                        if (inputField) {{
                            feedback = inputField.value;
                        }}
                    }}

                    btn.innerHTML = "Waking AI...";
                    btn.disabled = true;
                    btn.classList.add("opacity-50");

                    try {{
                        const res = await fetch('/api/project/' + pId + '/task/' + taskId + '/resolve-build', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{
                                resolution_type: resolutionType,
                                human_feedback: feedback
                            }})
                        }});
                        const data = await res.json();
                        
                        if (!data.success) {{
                            alert("Failed: " + data.message);
                        }}
                        fetchPipeline(); // Instantly refresh UI
                    }} catch (err) {{
                        alert("Network error: Failed to reach the server.");
                    }} finally {{
                        btn.innerHTML = origHTML;
                        btn.disabled = false;
                        btn.classList.remove("opacity-50");
                    }}
                }}

                async function copyBuildLog(event, tId) {{
                    const btn = event.currentTarget;
                    const logText = document.getElementById('err-log-' + tId).innerText;
                    try {{
                        await navigator.clipboard.writeText(logText);
                        const span = btn.querySelector('.btn-text');
                        const origText = span.innerText;
                        span.innerText = "Copied!";
                        btn.classList.add("text-green-600", "dark:text-green-400");
                        setTimeout(() => {{
                            span.innerText = origText;
                            btn.classList.remove("text-green-600", "dark:text-green-400");
                        }}, 2000);
                    }} catch(e) {{ alert("Failed to copy."); }}
                }}

                // Start polling
                fetchPipeline();
                fetchCompletedPipeline();
                setInterval(fetchPipeline, 3000);
                setInterval(fetchCompletedPipeline, 5000);
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.get("/project/{project_id}/plan", response_class=HTMLResponse)
def project_technical_plan(project_id: str = Path(...)):
    """Renders the workspace Technical Plan page."""
    vault = get_vault()
    project_data = vault.get(project_id)
    
    if not project_data:
        return HTMLResponse("<h1>Project Not Found</h1>", status_code=404)

    project_name = project_data.get("project_name", project_id)
    
    # Query technical_plans table
    architecture = "No architecture defined yet."
    changelog = []
    explored = []
    
    try:
        with get_db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT system_architecture, changelog, explored_approaches FROM technical_plans WHERE project_id = ?", (project_id,))
            row = cursor.fetchone()
            if row:
                architecture = row[0]
                try:
                    changelog = json.loads(row[1]) if row[1] else []
                except:
                    pass
                try:
                    explored = json.loads(row[2]) if row[2] else []
                except:
                    pass
    except Exception as e:
        architecture = f"Error loading plan: {str(e)}"

    # Build HTML lists
    changelog_html = "".join([f'<li class="mb-2">{item}</li>' for item in changelog]) if changelog else '<li class="text-gray-500 italic">No history yet.</li>'
    explored_html = "".join([f'<li class="mb-2">{item}</li>' for item in explored]) if explored else '<li class="text-gray-500 italic">No explored approaches logged.</li>'

    html_content = f"""
    <html>
        <head>
            <title>{project_name} - Technical Plan</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <script>
                tailwind.config = {{ darkMode: 'class', theme: {{ extend: {{ colors: {{ 'log-bg': '#1e1e1e', 'log-surface': '#252526', 'log-border': '#333333', 'log-text': '#d4d4d4' }} }} }} }};
                
                async function triggerSync(btn) {{
                    const origText = btn.innerHTML;
                    btn.innerText = "Checking Repo...";
                    btn.disabled = true;
                    btn.classList.add("opacity-50", "cursor-not-allowed");
                    
                    try {{
                        const res = await fetch(`/api/project/{project_id}/plan/sync`, {{ method: 'POST' }});
                        const data = await res.json();
                        alert(data.message);
                    }} catch (err) {{
                        alert("Network error occurred.");
                    }}
                    
                    btn.innerHTML = origText;
                    btn.disabled = false;
                    btn.classList.remove("opacity-50", "cursor-not-allowed");
                }}
            </script>
        </head>
        <body class="bg-gray-50 dark:bg-log-bg h-screen flex flex-col font-sans">
            {get_layout_header()}
            <div class="flex flex-1 overflow-hidden">
                {get_project_sidebar_html(project_id, project_name, "plan")}
                
                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-5xl mx-auto space-y-8">
                        
                        <div class="border-b border-gray-200 dark:border-log-border pb-4 flex justify-between items-end">
                            <div>
                                <h1 class="text-3xl font-medium text-gray-900 dark:text-white">Technical Plan</h1>
                                <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">Live architecture and historical logs for {project_name}</p>
                            </div>
                            <button onclick="triggerSync(this)" class="bg-blue-600 hover:bg-blue-700 text-white font-bold py-2.5 px-5 rounded-lg shadow-sm transition text-sm flex items-center">
                                <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                                Sync with Repo
                            </button>
                        </div>
                        
                        <div class="bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded-xl p-6 shadow-sm">
                            <h2 class="text-xl font-bold text-gray-800 dark:text-gray-200 mb-4 flex items-center">
                                <svg class="w-5 h-5 mr-2 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 002-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path></svg>
                                System Architecture
                            </h2>
                            <div class="prose dark:prose-invert max-w-none text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 dark:bg-[#1e1e1e] p-5 rounded-lg border border-gray-200 dark:border-gray-700">{architecture}</div>
                        </div>
                        
                        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
                            <div class="bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded-xl p-6 shadow-sm">
                                <h2 class="text-xl font-bold text-gray-800 dark:text-gray-200 mb-4 flex items-center">
                                    <svg class="w-5 h-5 mr-2 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                    Changelog
                                </h2>
                                <ul class="list-disc list-outside pl-5 text-sm text-gray-700 dark:text-gray-300">
                                    {changelog_html}
                                </ul>
                            </div>
                            
                            <div class="bg-white dark:bg-[#252526] border border-gray-200 dark:border-log-border rounded-xl p-6 shadow-sm">
                                <h2 class="text-xl font-bold text-gray-800 dark:text-gray-200 mb-4 flex items-center">
                                    <svg class="w-5 h-5 mr-2 text-orange-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
                                    Explored Approaches & Failures
                                </h2>
                                <ul class="list-disc list-outside pl-5 text-sm text-gray-700 dark:text-gray-300">
                                    {explored_html}
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)