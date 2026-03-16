import os
import requests
import base64
import time
from datetime import datetime
from e2b_code_interpreter import Sandbox
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
import shlex

# Create schemas to hide the 'workspace' parameter from the LLM
class TerminalInput(BaseModel):
    command: str = Field(description="The shell command to execute")

class ReadFileInput(BaseModel):
    file_path: str = Field(description="Path to the file to read")

class WriteFileInput(BaseModel):
    file_path: str = Field(description="Path to the file")
    content: str = Field(description="Content to write")

class GitHubPRInput(BaseModel):
    ticket_id: str = Field(description="Linear ticket ID")
    commit_message: str = Field(description="Commit message")
    pr_title: str = Field(description="PR Title")
    pr_body: str = Field(description="PR Body")

class ManageDevServerInput(BaseModel):
    action: str = Field(description="'start', 'logs', or 'stop'")
    command: str = Field(description="The server command (e.g., 'npm run dev'). Only needed for 'start'.", default="")

class InspectFrontendInput(BaseModel):
    url: str = Field(description="The local URL to inspect, e.g. http://localhost:5000")

def _parse_github_time(ts_str: str) -> datetime:
    """Safely converts a GitHub ISO 8601 string to a datetime object."""
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))

def get_latest_pr(ticket_id: str, github_token: str = None, repo_full: str = None) -> dict:
    """Fetches the latest PR for the ticket and returns its branch and timestamp."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    url = f"https://api.github.com/repos/{repo_full}/pulls?state=open&sort=updated&direction=desc"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        for pr in resp.json():
            head_ref = pr.get("head", {}).get("ref", "")
            if ticket_id.lower() in head_ref.lower():
                return {
                    "name": head_ref,
                    "timestamp": _parse_github_time(pr.get("updated_at")),
                    "url": pr.get("html_url")
                }
    return None

def get_wip_branch(ticket_id: str, github_token: str = None, repo_full: str = None) -> dict:
    """Fetches the WIP recovery branch for the ticket and returns its timestamp."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    # --- You must define the headers before making the request ---
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    resp = requests.get(f"https://api.github.com/repos/{repo_full}/branches", headers=headers)
    if resp.status_code != 200:
        return None
        
    branches = resp.json()
    
    # Filter for WIP branches belonging to this ticket
    wip_prefix = f"wip-recovery-{ticket_id}"
    matching_branches = [b for b in branches if wip_prefix.lower() in b["name"].lower()]
    
    if not matching_branches:
        return None
        
    best_branch = None
    best_time = None
    
    # Fetch the commit dates to find the most recently updated WIP branch
    for branch in matching_branches:
        commit_url = branch.get("commit", {}).get("url")
        if commit_url:
            commit_resp = requests.get(commit_url, headers=headers)
            if commit_resp.status_code == 200:
                ts_str = commit_resp.json().get("commit", {}).get("author", {}).get("date")
                if ts_str:
                    branch_time = _parse_github_time(ts_str)
                    if best_time is None or branch_time > best_time:
                        best_time = branch_time
                        best_branch = branch["name"]
                        
    if best_branch:
        return {"name": best_branch, "timestamp": best_time}
        
    return None

def get_main_branch_timestamp(github_token: str = None, repo_full: str = None) -> str:
    """Fetches the timestamp of the latest commit on the main branch."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    url = f"https://api.github.com/repos/{repo_full}/branches/main"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        ts_str = resp.json().get("commit", {}).get("commit", {}).get("author", {}).get("date")
        return _parse_github_time(ts_str)
    return None

def get_best_resume_branch(ticket_id: str, github_token: str, repo_full: str) -> dict:
    """
    Races main, open PRs, and WIP branches against each other.
    Whichever was updated most recently wins.
    """
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    pr_info = get_latest_pr(ticket_id, github_token=github_token, repo_full=repo_full)
    wip_info = get_wip_branch(ticket_id, github_token=github_token, repo_full=repo_full)
    main_ts = get_main_branch_timestamp(github_token=github_token, repo_full=repo_full)

    candidates = []
    
    # 1. Add main
    if main_ts:
        candidates.append({"type": "main", "branch_name": "main", "timestamp": main_ts})
        
    # 2. Add Open PR
    if pr_info and pr_info["timestamp"]:
        candidates.append({"type": "pr", "branch_name": pr_info["name"], "timestamp": pr_info["timestamp"]})
        
    # 3. Add WIP
    if wip_info and wip_info["timestamp"]:
        candidates.append({"type": "wip", "branch_name": wip_info["name"], "timestamp": wip_info["timestamp"]})

    # If nothing exists, start fresh
    if not candidates:
        return None

    # Sort all valid candidates by timestamp (Newest first)
    candidates.sort(key=lambda x: x["timestamp"], reverse=True)
    winner = candidates[0]

    # If main is the newest, it means the PR was merged and the WIP is stale. 
    # Returning None tells main.py to ignore overrides and start fresh!
    if winner["type"] == "main":
        return None
        
    # Otherwise, return the winning branch (PR or WIP)
    return winner

@tool(args_schema=TerminalInput)
def run_terminal_command(command: str, config: RunnableConfig) -> str:
    """
    Executes a bash command in the terminal. 
    Has a strict timeout. For background processes, use the 'start, curl, kill' pattern.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    # --- ENFORCE GITIGNORE BEFORE INSTALLS ---
    # Detect if the agent is trying to install massive dependency folders
    install_keywords = [
        "npm install", "npm i", "yarn add", "yarn install", 
        "pnpm install", "pnpm i", "pip install", "poetry install"
    ]
    
    if any(keyword in command.lower() for keyword in install_keywords):
        # Dynamically prepend a bash script to safely append standard ignores 
        # and remove duplicates (using sort -u) without interrupting the actual command.
        gitignore_safeguard = (
            "echo -e 'node_modules/\\nvenv/\\n.venv/\\n__pycache__/\\n*.pyc\\n.env\\n.next/\\nbuild/\\ndist/' >> .gitignore;"
            "sort -u .gitignore -o .gitignore;"
        )
        command = f"{command}\n{gitignore_safeguard}"
    
    # Extract the specific tenant's config
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    
    # Fallback to os.environ ONLY for local dev/testing
    github_token = tenant_config.get("github_token") or os.environ.get("GITHUB_TOKEN")
    github_repo = tenant_config.get("github_repo") or os.environ.get("GITHUB_REPO")
    client_secrets = tenant_config.get("secrets", {})

    # Merge everything into the secure terminal environment
    tool_envs = {
        "GITHUB_TOKEN": github_token,
        "GITHUB_REPO": github_repo,
        **client_secrets
    }

    try:
        # We execute the modified command string in the E2B sandbox
        execution = workspace.commands.run(
            command,
            envs=tool_envs,
            timeout=120
        )
        
        output = ""
        if execution.stdout:
            output += f"STDOUT:\n{execution.stdout}\n"
        if execution.stderr:
            output += f"STDERR:\n{execution.stderr}\n"
            
        if execution.error and not execution.stderr:
            output += "\nProcess exited with error code but no STDERR."
            
        return output if output else "Command executed successfully with no output."
        
    except TimeoutError:
         return "Error: Command timed out after 120 seconds. If you started a persistent server, you must run it in the background and kill it in the same command block."
    except Exception as e:
         return f"Terminal Error: {str(e)}"

@tool(args_schema=ReadFileInput)
def read_file(file_path: str, config: RunnableConfig) -> str:
    """Reads the full contents of a file inside the workspace."""

    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    try:
        return workspace.files.read(file_path)
    except Exception as e:
        return f"Error reading file: {str(e)}"

@tool(args_schema=WriteFileInput)
def write_file(file_path: str, content: str, config: RunnableConfig) -> str:
    """Overwrites a file (or creates a new one) inside the workspace with new code."""
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    try:
        workspace.files.write(file_path, content)
        return f"Successfully saved changes to {file_path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"

@tool
def update_linear_ticket(message: str, config: RunnableConfig) -> str:
    """
    Adds a comment to an active Linear ticket. 
    Use this to notify the human supervisor when a PR is ready with a Preview URL, 
    or if the AI team is blocked and needs help.
    """
    # Securely extract the ticket ID from the system config
    ticket_id = config.get("configurable", {}).get("ticket_id")
    
    if not ticket_id:
        return "Error: System failed to inject the active ticket ID."
    
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    linear_api_key = tenant_config.get("linear_api_key") or os.environ.get("LINEAR_API_KEY")
    
    if not linear_api_key:
        return "Error: LINEAR_API_KEY is missing in tenant configuration."

    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    
    # Linear's native GraphQL mutation for adding a comment
    query = """
    mutation CommentCreate($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    
    response = requests.post(
        url, 
        json={"query": query, "variables": {"issueId": issue_id, "body": message}}, 
        headers=headers
    )
    
    if response.status_code == 200 and "errors" not in response.json():
        return f"Successfully posted update to ticket {issue_id}"
    return f"Failed to update ticket: {response.text}"

@tool(args_schema=GitHubPRInput)
def create_github_pr(ticket_id: str, commit_message: str, pr_title: str, pr_body: str, config: RunnableConfig) -> str:
    """
    Commits all changed files, pushes to a branch, and opens a Pull Request.
    If an open PR already exists for this ticket, it dynamically updates that PR instead of creating a duplicate.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."
    
    # Extract specific tenant's GitHub credentials
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    github_token = tenant_config.get("github_token") or os.environ.get("GITHUB_TOKEN")
    repo_full = tenant_config.get("github_repo") or os.environ.get("GITHUB_REPO")
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."
    
    active_resume_branch = config.get("configurable", {}).get("active_resume_branch", "main")
    
    # Define a clean envs dict to reuse
    tool_envs = {
        "GITHUB_TOKEN": github_token,
        "GITHUB_REPO": repo_full
    }

    repo_folder = repo_full.split("/")[-1].replace(".git", "")

    # This calls the same function that main.py uses to check for PRs
    existing_pr = get_latest_pr(ticket_id, github_token=github_token, repo_full=repo_full)

    if existing_pr:
        # PATH A: Update Existing PR
        
        # Add the prefix dynamically for this path
        tool_envs["COMMIT_MSG"] = f"Update: {commit_message}"

        # We push the current working state (HEAD) directly to the PR branch.
        git_commands = f"""
        GIT_DIR=$(find /home/user -maxdepth 3 -type d -name ".git" | head -n 1)
        if [ -z "$GIT_DIR" ]; then 
            echo "Empty sandbox detected. Cloning fresh copy..."
            cd /home/user
            git clone https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git
            GIT_DIR=$(find /home/user -maxdepth 3 -type d -name ".git" | head -n 1)
            if [ -z "$GIT_DIR" ]; then exit 1; fi
        fi
        cd "$(dirname "$GIT_DIR")"
        
        # Ensure we are on the exact WIP branch before executing PR logic
        git fetch origin
        git checkout {active_resume_branch}
        
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        
        git add .
        git commit -m "$COMMIT_MSG" || echo "nothing to commit"
        git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{active_resume_branch} --force
        """
        
        try:
            execution = workspace.commands.run(git_commands, envs=tool_envs)
            if execution.error and "nothing to commit" not in execution.stdout:
                return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            return f"Successfully updated the existing Pull Request on branch '{active_resume_branch}'!"
        except Exception as e:
            return f"Git Execution failed: {str(e)}"
            
    else:
        # PATH B: Create New PR
        new_branch = config.get("configurable", {}).get("pr_branch")
        
        # Just use the raw message for this path
        tool_envs["COMMIT_MSG"] = commit_message

        git_commands = f"""
        GIT_DIR=$(find /home/user -maxdepth 3 -type d -name ".git" | head -n 1)
        if [ -z "$GIT_DIR" ]; then 
            echo "Empty sandbox detected. Cloning fresh copy..."
            cd /home/user
            git clone https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git
            GIT_DIR=$(find /home/user -maxdepth 3 -type d -name ".git" | head -n 1)
            if [ -z "$GIT_DIR" ]; then exit 1; fi
        fi
        cd "$(dirname "$GIT_DIR")"
        
        # Ensure we are on the exact WIP branch before executing PR logic
        git fetch origin
        git checkout {active_resume_branch}
        
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        git checkout -b {new_branch}
        git add .
        git commit -m "$COMMIT_MSG"
        git push https://$GITHUB_TOKEN@github.com/{repo_full}.git {new_branch}
        """
        
        try:
            execution = workspace.commands.run(git_commands, envs=tool_envs)
            if execution.error and "nothing to commit" not in execution.stdout:
                return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
        except Exception as e:
            return f"Git Execution failed: {str(e)}"
            
        # Create the PR via GitHub API
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        base_url = f"https://api.github.com/repos/{repo_full}"
        
        pr_resp = requests.post(
            f"{base_url}/pulls",
            headers=headers,
            json={
                "title": pr_title,
                "body": pr_body,
                "head": new_branch,
                "base": "main"
            }
        )
        
        if pr_resp.status_code == 201:
            pr_url = pr_resp.json().get("html_url")
            return f"Successfully pushed code to branch '{new_branch}' and created PR! Review it here: {pr_url}"
        else:
            return f"Git push succeeded, but PR creation failed: {pr_resp.text}"

@tool
def sync_wip_commit(commit_message: str, config: RunnableConfig) -> str:
    """
    Saves your current progress to a temporary remote WIP branch so it is not lost if the server crashes.
    Call this whenever you finish writing a significant piece of code, before moving on.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."
        
    # Extract specific tenant's GitHub credentials
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    github_token = tenant_config.get("github_token") or os.environ.get("GITHUB_TOKEN")
    repo_full = tenant_config.get("github_repo") or os.environ.get("GITHUB_REPO")
    wip_branch = config.get("configurable", {}).get("wip_branch")
    
    if not github_token or not repo_full:
        return "Error: Missing GITHUB_TOKEN or GITHUB_REPO in tenant configuration."

    # Extract repo folder
    repo_folder = repo_full.split("/")[-1].replace(".git", "")

    # The script uses HEAD:refs/heads/... to push the current state to the remote WIP branch
    # without altering the agent's current local branch checkout.
    git_commands = f"""
    GIT_DIR=$(find /home/user -maxdepth 3 -type d -name ".git" | head -n 1)
    if [ -z "$GIT_DIR" ]; then
        echo "Error: No git repository found in the workspace. Did you clone it?"
        exit 1
    fi
    cd "$(dirname "$GIT_DIR")"
    
    git config --global user.email "ai@agency.dev"
    git config --global user.name "AI Developer"
    
    git add .
    git commit -m "$COMMIT_MSG" || echo "Nothing to commit"
    
    # Force push the current local state (HEAD) directly to the remote WIP branch
    git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{wip_branch} --force
    """
    
    try:
        execution = workspace.commands.run(git_commands, envs={
            "GITHUB_TOKEN": github_token,
            "GITHUB_REPO": repo_full,
            "COMMIT_MSG": f"WIP Sync: {commit_message}"
        })

        # Properly evaluate standard bash errors
        if execution.error and "Nothing to commit" not in execution.stdout:
            return f"WIP Sync failed:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            
        return f"Successfully backed up current local state to remote branch '{wip_branch}'."
        
    except Exception as e:
        return f"WIP Sync execution failed: {str(e)}"

@tool(args_schema=ManageDevServerInput)
def manage_dev_server(action: str, command: str, config: RunnableConfig) -> str:
    """Manages background development servers. Use this to start a server without timing out, check its logs, and stop it."""
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace: return "Error: No active sandbox."
    
    # Consistent file paths
    log_file = "/tmp/server_logs.txt"
    pid_file = "/tmp/server_pid.txt"

    if action == "start":
        if not command: return "Error: Must provide a command."
        
        # Clean up old state first
        workspace.commands.run(f"rm {log_file} {pid_file} || echo 'clean'")

        # Extract tenant secrets for the background server
        tenant_config = config.get("configurable", {}).get("tenant_config", {})
        client_secrets = tenant_config.get("secrets", {})

        try:
            # Force write the initial line BEFORE starting the server
            workspace.files.write(log_file, "=== SERVER INITIALIZING IN BACKGROUND ===\nWait at least 20s for logs to appear...\n")

            # Intelligently find the directory containing firebase.json
            cd_cmd = "cd $(find /home/user -maxdepth 3 -name firebase.json -printf '%h\\n' | head -n 1) || cd /home/user/public-ledger"
            
            # Native E2B background execution.
            # We must manually redirect stdout/stderr to a file within the command if we want to read the logs later.
            # Use '>>' to APPEND to the log file so we don't erase the line we just wrote
            # Group the commands inside { } so that ALL output (even from the cd command) is captured in the log file.
            secure_command = f"{{ {cd_cmd} && {command}; }} >> {log_file} 2>&1"
            
            # Inject only THIS client's secrets
            process = workspace.commands.run(
                f"bash -c {shlex.quote(secure_command)}", 
                background=True,
                envs=client_secrets 
            )
            
            # Record the PID
            workspace.files.write(pid_file, str(process.pid))
            
            # Synchronous Polling Loop
            print(f"[SYSTEM] Waiting for server (PID {process.pid}) to boot...")
            max_wait_time = 30
            booted = False
            
            for _ in range(max_wait_time // 2):
                time.sleep(2) # Pause the Python thread, delaying the LLM's next turn
                
                # Read the current state of the logs from E2B
                current_logs_result = workspace.commands.run(f"tail -n 50 {log_file}")
                current_logs = current_logs_result.stdout or ""
                
                # Check for Firebase success/failure signatures
                if "All emulators ready" in current_logs or "listening on port" in current_logs:
                    booted = True
                    break
                if any(err in current_logs for err in ["Failed to start", "port taken", "Error:", "No such file", "command not found"]):
                    break # Boot failed, break early to show the agent the error
                    
            # Fetch final logs to return to the agent immediately
            final_logs = workspace.commands.run(f"tail -n 50 {log_file}").stdout
            
            if booted:
                return f"SUCCESS: Server started securely in the background. Initial Boot Logs:\n{final_logs}"
            else:
                return f"WARNING: Server failed to start or crashed. Review the logs to diagnose the issue:\n{final_logs}"
            
        except Exception as e:
            return f"Failed to start server: {str(e)}"
    
    elif action == "logs":
        # Check if file exists, then tail.
        cmd = f"[ -f {log_file} ] && tail -n 100 {log_file} || echo 'No log file found yet. Wait longer.'"
        res = workspace.commands.run(f"bash -c {shlex.quote(cmd)}")
        return f"Server Logs:\n{res.stdout or res.stderr}"
        
    elif action == "stop":
        cleanup_cmd = f"""
        echo "Stopping parent process recorded in {pid_file}..."
        PID=$(cat {pid_file} || echo "")
        if [ ! -z "$PID" ]; then kill -9 $PID || echo "PID dead"; fi
        
        echo "Aggressively cleaning Firebase ports and Zombie Java processes..."
        fuser -k 8080/tcp 5000/tcp 5001/tcp 4000/tcp 4400/tcp || true
        
        # Kill any lingering Java (Firebase Emulator) or Node processes
        pkill -9 -f java || true
        pkill -9 -f firebase || true
        
        rm -f {pid_file} {log_file}
        """
        workspace.commands.run(f"bash -c {shlex.quote(cleanup_cmd)}")
        return "Server and all related ports forcefully stopped and cleaned."
    
    return "Invalid action."

@tool(args_schema=InspectFrontendInput)
def inspect_frontend(url: str, config: RunnableConfig) -> str:
    """Opens a headless browser, navigates to the URL, and returns console errors and network failures."""
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox."
    
    # Write a quick Playwright script to the temp directory
    script = f"""
    const {{ chromium }} = require('playwright');
    (async () => {{
        try {{
            const browser = await chromium.launch({{ args: ['--no-sandbox'] }});
            const page = await browser.newPage();
            const errors = [];
            
            page.on('console', msg => {{ 
                if(msg.type() === 'error' || msg.type() === 'warning') errors.push(`[${{msg.type().toUpperCase()}}] ${{msg.text()}}`); 
            }});
            page.on('pageerror', err => errors.push(`[PAGE CRASH] ${{err.message}}`));
            
            await page.goto('{url}', {{ waitUntil: 'networkidle', timeout: 15000 }});
            
            console.log("=== BROWSER DIAGNOSTICS ===");
            console.log(errors.length ? errors.join('\\n') : "SUCCESS: No console errors detected.");
            await browser.close();
        }} catch (e) {{
            console.log("Failed to inspect URL: " + e.message);
        }}
    }})();
    """
    workspace.files.write("/tmp/inspect.js", script)
    
    # Quietly install playwright if it's missing, then run the script
    workspace.commands.run("cd /tmp && npm install playwright")
    res = workspace.commands.run("node /tmp/inspect.js")
    
    return res.stdout or res.stderr
