import time
import requests
import sqlite3
from datetime import datetime
from e2b_code_interpreter import Sandbox
from agents.graph import workflow
from langgraph.checkpoint.sqlite import SqliteSaver
from agents.tools import get_best_resume_branch
from langchain_core.messages import HumanMessage
from database import log_activity, PIPELINE_DB_PATH

def get_recent_linear_comments(ticket_id: str, linear_token: str) -> str:
    """
    Fetches the last 3 comments from the Linear ticket to build context.
    """
    if not linear_token:
        return ""

    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": f"Bearer {linear_token}", # Use Bearer for OAuth!
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

def process_ticket(ticket_id: str, project_id: str, human_input: str, tenant_config: dict):
    """
    Synchronous background task to run the AI team.
    FastAPI automatically runs standard 'def' background tasks in a separate thread pool,
    so it won't block the main asyncio event loop.
    """
    # Dynamically route the sandbox environment based on tenant architecture
    target_template = tenant_config.get("e2b_template")
    
    # Create a fresh sandbox for this run (with a 15 min timeout)
    if target_template:
        print(f"\n[SYSTEM]: Booting E2B Sandbox using custom template: {target_template}")
        workspace = Sandbox.create(template=target_template, timeout=900)
    else:
        print(f"\n[SYSTEM]: Booting default E2B Sandbox")
        workspace = Sandbox.create(timeout=900)

    # --- Pre-warm the environment ---
    github_token = tenant_config.get("github_token")
    github_repo = tenant_config.get("github_repo")

    if github_token and github_repo:
        setup_cmd = f"git clone https://{github_token}@github.com/{github_repo}.git"
        workspace.commands.run(setup_cmd)
    
    # The "Store at Boot" Strategy
    # Dynamically find where the repo was just cloned (works regardless of Dockerfile WORKDIR)
    default_dir_exec = workspace.commands.run("pwd")
    default_dir = default_dir_exec.stdout.strip() if default_dir_exec.stdout else "/"
    
    repo_root_exec = workspace.commands.run(f"find '{default_dir}' -maxdepth 3 -type d -name '.git' | head -n 1 | xargs dirname")
    repo_root = repo_root_exec.stdout.strip() if repo_root_exec.stdout else default_dir
    
    # ==========================================
    # GLOBAL ENVIRONMENT PREPARATION
    # ==========================================
    import shlex
    client_secrets = tenant_config.get("secrets", {})
    all_env_vars = {
        "GITHUB_TOKEN": github_token,
        "GITHUB_REPO": github_repo,

        # --- FORCED PATH INJECTIONS ---
        "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
        # ------------------------------

        **client_secrets
    }
    
    # Write the .env file directly into the sandbox (For Servers/Frameworks)
    env_file_content = "\n".join([f"{k}={v}" for k, v in all_env_vars.items() if v])
    workspace.files.write(f"{repo_root}/.env", env_file_content)

    # Create the secure export string (For Bash/CLI Commands)
    global_exports = " ".join([f"{k}={shlex.quote(str(v))}" for k, v in all_env_vars.items() if v])
    export_prefix = f"export {global_exports} && " if global_exports else ""
    # ==========================================

    config = {
        "configurable": {
            "thread_id": ticket_id,
            "sandbox": workspace, # Pass the sandbox here, NOT in the state
            "tenant_config": tenant_config,
            "repo_root": repo_root,
            "export_prefix": export_prefix
        }
    }

    try:
        # --- Initialize thread-safe checkpointer ---
        with SqliteSaver.from_conn_string("checkpoints.sqlite") as memory:
            # Compile the agent team for THIS specific thread
            agent_team = workflow.compile(checkpointer=memory)

            # Helper to avoid repeating logging logic 3 times
            def stream_and_log(input_data, config, ticket_id, session_id):
                # --- STEP 1: Log the Input (What we are sending to the AI) ---
                # We log the input_data so we know the exact state/prompt the AI received.
                log_activity(
                    ticket_id=ticket_id,
                    session_id=session_id,
                    project_id=project_id,
                    role="system_input", 
                    content=str(input_data), # This captures the prompt + state variables
                    node_name="Graph_Entry"
                )
                
                for event in agent_team.stream(input_data, config):
                    for node_name, node_state in event.items():
                        # --- Node Activation Trace ---
                        # This logs the moment a node emits ANY event, capturing the "Silence"
                        log_activity(
                            ticket_id=ticket_id,
                            session_id=session_id,
                            project_id=project_id,
                            role="system_trace",
                            content=f"Node '{node_name}' activated and processing...",
                            node_name=node_name
                        )
                        
                        if "messages" in node_state and len(node_state["messages"]) > 0:
                            last_msg = node_state["messages"][-1]

                            # --- EXHAUSTIVE AI LOGGING ---
                            if last_msg.type == "ai":
                                # 1. Log the Raw Message Object for maximum detail
                                # This captures metadata, thought signatures, and usage stats
                                log_activity(
                                    ticket_id=ticket_id,
                                    session_id=session_id,
                                    project_id=project_id,
                                    role="ai_raw_response",
                                    content=last_msg.dict() if hasattr(last_msg, "dict") else str(last_msg),
                                    node_name=node_name
                                )

                                # 2. Log Tool Calls (The intent to act)
                                if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                                    for tc in last_msg.tool_calls:
                                        log_activity(
                                            ticket_id=ticket_id,
                                            session_id=session_id,
                                            project_id=project_id,
                                            role="ai_tool_call",
                                            content=tc["args"],
                                            node_name=node_name,
                                            tool_name=tc["name"]
                                        )
                            
                            # 2. Log Tool Outputs (The result of the action)
                            elif last_msg.type == "tool":
                                content_str = str(last_msg.content).lower()
                                is_err = any(word in content_str for word in ["error", "failed", "exception", "not found"])
                                log_activity(
                                    ticket_id=ticket_id,
                                    session_id=session_id,
                                    project_id=project_id,
                                    role="tool_output",
                                    content=last_msg.content,
                                    node_name=node_name,
                                    tool_name=getattr(last_msg, "name", "unknown_tool"),
                                    is_error=is_err
                                )

                            print(f"\n--- Update from {node_name} ---\n{last_msg.content}")
            
            # Initial Log: Capture the starting human input
            best_branch_info = get_best_resume_branch(
                ticket_id=ticket_id, 
                github_token=tenant_config.get("github_token"),
                repo_full=tenant_config.get("github_repo")
            )
            short_time = datetime.now().strftime("%b %d, %H:%M")

            if best_branch_info:
                branch_name = best_branch_info["branch_name"]
                branch_type = best_branch_info["type"]

                print(f"\n[SYSTEM]: Found '{branch_type}' branch at '{branch_name}'. Team will resume work from here.")

                # --- Extract the Session ID from the branch name ---
                # If the branch is 'wip-recovery-123-17000', the session_id is '123-17000'
                session_id = branch_name.replace("wip-recovery-", "").replace("feature-", "")

                # --- Update Config to use the Shared Memory ID ---
                config["configurable"]["thread_id"] = session_id
                config["configurable"]["ticket_id"] = ticket_id
                config["configurable"]["wip_branch"] = f"wip-recovery-{session_id}"
                config["configurable"]["pr_branch"] = f"feature-{session_id}"
                config["configurable"]["active_resume_branch"] = branch_name # So we know what to checkout

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
                    name="Human",
                    additional_kwargs={"source": "webhook", "timestamp": short_time}
                )

                # --- Update Config and Check True History ---
                current_state = agent_team.get_state(config)
                has_history = bool(current_state.values)

                # Log the webhook immediately so it appears at the top of the UI timeline, before the AI streams.
                log_activity(ticket_id, "human", human_input, node_name="Webhook", session_id=session_id, project_id=project_id)

                if has_history:
                    # --- WIP: CONTEXT REHYDRATION (Checking the Real World) ---
                    # recent_comments = get_recent_linear_comments(ticket_id)

                    stream_and_log({"messages": [webhook_msg], "session_id": session_id}, config, ticket_id, session_id)
                else:
                    # Start a new conversation, but force it to use the PR branch
                    initial_state = {
                        "messages": [webhook_msg],
                        "active_ticket_url": ticket_id,
                        "session_id": session_id,
                        "project_id": project_id,
                        "current_status": "NOT_STARTED",
                        "current_phase": "Planning"
                    }
                    stream_and_log(initial_state, config, ticket_id, session_id)
            else:
                print(f"\n[SYSTEM]: No active PR or WIP branches found ahead of main. Dispatching team for new ticket {ticket_id}...")
                
                # --- Generate a shared Session ID ---
                session_timestamp = int(time.time())
                session_id = f"{ticket_id}-{session_timestamp}"

                # Log the webhook immediately so it appears at the top of the UI timeline, before the AI streams.
                log_activity(ticket_id, "human", human_input, node_name="Webhook", session_id=session_id, project_id=project_id)

                # --- Set up the shared config ---
                config["configurable"]["thread_id"] = session_id
                config["configurable"]["ticket_id"] = ticket_id
                config["configurable"]["wip_branch"] = f"wip-recovery-{session_id}"
                config["configurable"]["pr_branch"] = f"feature-{session_id}"
                config["configurable"]["active_resume_branch"] = "main"

                # If there's no open PR, we start entirely from scratch.
                system_note = "\n\nSYSTEM OVERRIDE: You are in a completely new, empty E2B sandbox. There are no open PRs to resume from. Build the feature from scratch on a new branch."
                prompt = human_input + system_note
                
                # --- Create object so we don't lose timestamp ---
                webhook_msg = HumanMessage(
                    content=prompt,
                    name="Human",
                    additional_kwargs={"source": "webhook", "timestamp": short_time}
                )

                initial_state = {
                    "messages": [webhook_msg],
                    "active_ticket_url": ticket_id,
                    "session_id": session_id,
                    "current_status": "NOT_STARTED",
                    "current_phase": "Planning"
                }

                # ==========================================
                # STRICT SESSION ISOLATION
                # ==========================================
                # Note: Because we assign a newly generated session_id to the 
                # thread_id config, LangGraph will start with a completely blank memory. 
                # The AI will have no context of previous failures on this ticket.
                # This ensures a clean slate for the new sandbox environment.
                # ==========================================
                stream_and_log(initial_state, config, ticket_id, session_id)
    
    finally:
        # Always clean up the sandbox to save compute!
        workspace.kill()

def run_pipeline_task(session_id: str, project_id: str, human_input: str, tenant_config: dict):
    """
    Standard background task (synchronous) wrapper.
    It manages the SQLite queue state and calls the execution engine directly.
    """
    # 1. Mark task as Running
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        conn.execute("UPDATE tasks SET status = 'Running', updated_at = ? WHERE task_id = ?", 
                     (datetime.utcnow().isoformat(), session_id))
        conn.commit()
    
    try:
        # 2. Execute your actual LangGraph agent
        # Notice we removed "with_lock" because the SQLite DB handles our concurrency now
        process_ticket(session_id, project_id, human_input, tenant_config)
        
        # 3. If it finishes without crashing, mark as Completed
        with sqlite3.connect(PIPELINE_DB_PATH) as conn:
            conn.execute("UPDATE tasks SET status = 'Completed', updated_at = ? WHERE task_id = ?", 
                         (datetime.utcnow().isoformat(), session_id))
            conn.commit()
            
    except Exception as e:
        print(f"\n[PIPELINE ERROR] Task {session_id} crashed: {str(e)}")
        # 4. If it crashes (e.g. Git error, LLM limit), mark as Failed so the user can retry
        with sqlite3.connect(PIPELINE_DB_PATH) as conn:
            conn.execute("UPDATE tasks SET status = 'Failed', updated_at = ? WHERE task_id = ?", 
                         (datetime.utcnow().isoformat(), session_id))
            conn.commit()