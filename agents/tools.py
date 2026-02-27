import os
import requests
import base64
import time
from e2b_code_interpreter import Sandbox
from langchain_core.tools import tool

# Global variable to keep the workspace alive across multiple tool calls
current_workspace = None

def get_or_create_workspace():
    global current_workspace
    if current_workspace is None:
        # Securely pass both variables into the E2B environment
        current_workspace = Sandbox(
            timeout=600, 
            envs={
                "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
                "GITHUB_REPO": os.environ.get("GITHUB_REPO")
            }
        )
    return current_workspace

@tool
def run_terminal_command(command: str) -> str:
    """
    Executes a shell/bash command in the persistent cloud workspace.
    Use this for: git clone, ls, cat, pip install, pytest, python main.py, etc.
    """
    try:
        workspace = get_or_create_workspace()
        execution = workspace.commands.run(command)
        
        output = ""
        if execution.stdout:
            output += f"STDOUT:\n{execution.stdout}\n"
        if execution.stderr:
            output += f"STDERR:\n{execution.stderr}\n"
            
        return output if output else "Command executed successfully (no output)."
    except Exception as e:
        return f"Terminal Error: {str(e)}"

@tool
def read_file(file_path: str) -> str:
    """Reads the full contents of a file inside the workspace."""
    try:
        workspace = get_or_create_workspace()
        return workspace.files.read(file_path)
    except Exception as e:
        return f"Error reading file: {str(e)}"

@tool
def write_file(file_path: str, content: str) -> str:
    """Overwrites a file (or creates a new one) inside the workspace with new code."""
    try:
        workspace = get_or_create_workspace()
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

@tool
def create_github_pr(ticket_id: str, commit_message: str, pr_title: str, pr_body: str) -> str:
    """
    Commits all changed files in the persistent E2B workspace, pushes to a new branch, and opens a Pull Request.
    You MUST provide the ticket_id (e.g., AI-5) so the branch is named correctly.
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO") 
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."
        
    repo_folder = repo_full.split("/")[-1].replace(".git", "")
    workspace = get_or_create_workspace()
    
    # Inject the Linear Ticket ID directly into the branch name!
    new_branch = f"ai-feature-{ticket_id}"
    
    git_commands = f"""
    cd {repo_folder}
    git config --global user.email "ai@agency.dev"
    git config --global user.name "AI DevOps"
    git checkout -b {new_branch}
    git add .
    git commit -m "{commit_message}"
    git push https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git {new_branch}
    """
    
    execution = workspace.commands.run(git_commands)
    if execution.error and "nothing to commit" not in execution.stdout:
        return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
        
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
        return f"Successfully pushed code and created PR! Review it here: {pr_url}"
    else:
        return f"Git push succeeded, but PR creation failed: {pr_resp.text}"
    """
    Commits all changed files in the persistent E2B workspace, pushes to a new branch, and opens a Pull Request.
    Use this when the Developer has finished making changes to the cloned repository.
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    repo_full = os.environ.get("GITHUB_REPO") 
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."
        
    # Extract just the folder name (e.g., "ai-agency-test" from "princegoel/ai-agency-test")
    repo_folder = repo_full.split("/")[-1].replace(".git", "")
    workspace = get_or_create_workspace()
    
    import time
    new_branch = f"ai-feature-{int(time.time())}"
    
    # 1. Execute native git commands inside the E2B workspace
    git_commands = f"""
    cd {repo_folder}
    git config --global user.email "ai@agency.dev"
    git config --global user.name "AI DevOps"
    git checkout -b {new_branch}
    git add .
    git commit -m "{commit_message}"
    git push https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git {new_branch}
    """
    
    execution = workspace.commands.run(git_commands)
    if execution.error and "nothing to commit" not in execution.stdout:
        return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
        
    # 2. Use the GitHub REST API to open the Pull Request
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
        return f"Successfully pushed code and created PR! Review it here: {pr_url}"
    else:
        return f"Git push succeeded, but PR creation failed: {pr_resp.text}"