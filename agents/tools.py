import os
import requests
import base64
import time
from datetime import datetime
from e2b_code_interpreter import Sandbox
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

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

def _parse_github_time(ts_str: str) -> datetime:
    """Safely converts a GitHub ISO 8601 string to a datetime object."""
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))

def get_latest_pr(ticket_id: str) -> dict:
    """Fetches the latest PR for the ticket and returns its branch and timestamp."""
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO")
    
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

def get_wip_branch(ticket_id: str) -> dict:
    """Fetches the WIP recovery branch for the ticket and returns its timestamp."""
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO")
    
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

def get_main_branch_timestamp(repo_full: str, github_token: str):
    """Fetches the timestamp of the latest commit on the main branch."""
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

def get_best_resume_branch(ticket_id: str) -> dict:
    """
    Races main, open PRs, and WIP branches against each other.
    Whichever was updated most recently wins.
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO")

    pr_info = get_latest_pr(ticket_id)
    wip_info = get_wip_branch(ticket_id)
    main_ts = get_main_branch_timestamp(repo_full, github_token)

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
        command = f"{gitignore_safeguard}\n{command}"

    try:
        # We execute the modified command string in the E2B sandbox
        execution = workspace.commands.run(
            command,
            envs={
                "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
                "GITHUB_REPO": os.environ.get("GITHUB_REPO"),
            },
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
def update_linear_ticket(issue_id: str, message: str) -> str:
    """
    Adds a comment to an active Linear ticket. 
    Use this to notify the human supervisor when a PR is ready with a Preview URL, 
    or if the AI team is blocked and needs help.
    """
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        return "Error: LINEAR_API_KEY is missing."

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
    
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO") 
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."
    
    # Define a clean envs dict to reuse
    tool_envs = {
        "GITHUB_TOKEN": github_token,
        "GITHUB_REPO": repo_full
    }

    repo_folder = repo_full.split("/")[-1].replace(".git", "")

    # This calls the same function that main.py uses to check for PRs
    existing_pr = get_latest_pr(ticket_id)

    if existing_pr:
        # PATH A: Update Existing PR
        active_branch = config.get("configurable", {}).get("active_resume_branch")
        
        # We push the current working state (HEAD) directly to the PR branch.
        git_commands = f"""
        cd {repo_folder}
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        
        git checkout {active_branch}
        git add .
        git commit -m "Update: {commit_message}" || echo "nothing to commit"
        git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{active_branch} --force
        """
        
        try:
            execution = workspace.commands.run(git_commands, envs=tool_envs)
            if execution.error and "nothing to commit" not in execution.stdout:
                return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            return f"Successfully updated the existing Pull Request on branch '{active_branch}'!"
        except Exception as e:
            return f"Git Execution failed: {str(e)}"
            
    else:
        # PATH B: Create New PR
        new_branch = config.get("configurable", {}).get("pr_branch")
        
        git_commands = f"""
        cd {repo_folder}
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        git checkout -b {new_branch}
        git add .
        git commit -m "{commit_message}"
        # Use the variable explicitly here too
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
        
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO")
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."

    # Standard temporary backup branch name
    wip_branch = config.get("configurable", {}).get("wip_branch")

    # Extract repo folder
    repo_folder = repo_full.split("/")[-1].replace(".git", "")

    # The script uses HEAD:refs/heads/... to push the current state to the remote WIP branch
    # without altering the agent's current local branch checkout.
    git_commands = f"""
    cd {repo_folder}
    git config --global user.email "ai@agency.dev"
    git config --global user.name "AI Developer"
    
    git add .
    git commit -m "WIP Sync: {commit_message}" || echo "Nothing to commit"
    
    # Force push the current local state (HEAD) directly to the remote WIP branch
    git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{wip_branch} --force
    """
    
    try:
        execution = workspace.commands.run(git_commands, envs={
            "GITHUB_TOKEN": github_token,
            "GITHUB_REPO": repo_full  # Add this line
        })

        # Properly evaluate standard bash errors
        if execution.error and "Nothing to commit" not in execution.stdout:
            return f"WIP Sync failed:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            
        return f"Successfully backed up current local state to remote branch '{wip_branch}'."
        
    except Exception as e:
        return f"WIP Sync execution failed: {str(e)}"
