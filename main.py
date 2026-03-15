import os
import requests
import asyncio
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox
from agents.graph import app as agent_team
from agents.tools import get_best_resume_branch
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from database import init_db, log_activity, get_db_conn
from langchain_core.messages import HumanMessage
from datetime import datetime
import time
import json

active_tickets = set() # Global set to track currently processing tickets
load_dotenv() # Load the .env file so the API keys are available to the tools
app = FastAPI()

@app.on_event("startup")
def on_startup():
    init_db()

def read_tenant_vault(team_id: str) -> dict:
    """Synchronous helper to read the vault file."""
    try:
        with open("tenant_vault.json", "r") as f:
            import json
            vault = json.load(f)
            return vault.get(team_id, {}) 
    except FileNotFoundError:
        return {} # Fails gracefully if the file isn't created yet

def get_recent_linear_comments(ticket_id: str) -> str:
    """
    Fetches the last 3 comments from the Linear ticket to build context.
    """
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("[ERROR] Linear API key missing.")
        return ""

    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": api_key,
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

def process_ticket(ticket_id: str, human_input: str, tenant_config: dict):
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
    
    config = {
        "configurable": {
            "thread_id": ticket_id,
            "sandbox": workspace, # Pass the sandbox here, NOT in the state!
            "tenant_config": tenant_config
        }
    }

    # Helper to avoid repeating logging logic 3 times
    def stream_and_log(input_data, config, ticket_id, session_id):
        # --- STEP 1: Log the Input (What we are sending to the AI) ---
        # We log the input_data so we know the exact state/prompt the AI received.
        log_activity(
            ticket_id=ticket_id,
            session_id=session_id,
            role="system_input", 
            content=str(input_data), # This captures the prompt + state variables
            node_name="Graph_Entry"
        )
        
        for event in agent_team.stream(input_data, config):
            for node_name, node_state in event.items():
                # --- NEW: Node Activation Trace ---
                # This logs the moment a node emits ANY event, capturing the "Silence"
                log_activity(
                    ticket_id=ticket_id,
                    session_id=session_id,
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
                            role="tool_output",
                            content=last_msg.content,
                            node_name=node_name,
                            tool_name=getattr(last_msg, "name", "unknown_tool"),
                            is_error=is_err
                        )

                    print(f"\n--- Update from {node_name} ---\n{last_msg.content}")

    try:
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
                    "current_status": "NOT_STARTED",
                    "current_phase": "Planning"
                }
                stream_and_log(initial_state, config, ticket_id, session_id)
        else:
            print(f"\n[SYSTEM]: No active PR or WIP branches found ahead of main. Dispatching team for new ticket {ticket_id}...")
            
            # --- Generate a shared Session ID ---
            session_timestamp = int(time.time())
            session_id = f"{ticket_id}-{session_timestamp}"

            # --- Set up the shared config ---
            config["configurable"]["thread_id"] = session_id
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
        
        log_activity(ticket_id, "human", human_input, node_name="Webhook", session_id=session_id)
    
    finally:
        # Always clean up the sandbox to save compute!
        workspace.kill()

def process_ticket_with_lock(ticket_id: str, human_input: str, tenant_config: dict = None):
    """Wraps process_ticket to ensure the lock is released when the job finishes."""
    if tenant_config is None:
        tenant_config = {}
    
    try:
        # Run the existing AI logic
        process_ticket(ticket_id, human_input, tenant_config)
    finally:
        # Always remove the ticket from the active list when finished or if it crashes
        active_tickets.discard(ticket_id)

@app.get("/logs", response_class=HTMLResponse)
async def view_all_logs():
    from database import get_all_sessions
    raw_data = get_all_sessions()
    
    # 1. Group the data by Ticket ID
    tickets = {}
    for row in raw_data:
        t_id = row.get("ticket_id")
        s_id = row.get("session_id")
        last_active = row.get("last_active")
        
        if t_id not in tickets:
            tickets[t_id] = {
                "last_active": last_active,
                "sessions": []
            }
            
        # Ensure the parent ticket reflects the absolute most recent activity
        if last_active and last_active > tickets[t_id]["last_active"]:
            tickets[t_id]["last_active"] = last_active
            
        # Add the session if it exists (ignoring legacy NULLs for the sub-list)
        if s_id:
            tickets[t_id]["sessions"].append({
                "session_id": s_id,
                "last_active": last_active
            })

    # Sort tickets by most recently active overall
    sorted_tickets = sorted(tickets.items(), key=lambda x: x[1]["last_active"] or "", reverse=True)

    # 2. Build the HTML UI
    html_content = """
    <html>
        <head>
            <title>AI Agency - Log Dashboard</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 40px; background-color: #f9fafb; color: #111827; }
                .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px 40px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
                h1 { border-bottom: 2px solid #e5e7eb; padding-bottom: 10px; margin-bottom: 20px; }
                .ticket-group { margin-bottom: 25px; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px; background-color: #ffffff; }
                .ticket-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
                .ticket-link { font-size: 1.2em; font-weight: bold; color: #1d4ed8; text-decoration: none; }
                .ticket-link:hover { text-decoration: underline; }
                .session-list { list-style-type: none; padding-left: 15px; border-left: 2px solid #e5e7eb; margin-top: 10px; }
                .session-item { margin-bottom: 8px; font-size: 0.95em; }
                .session-link { color: #059669; text-decoration: none; font-weight: 500; }
                .session-link:hover { text-decoration: underline; }
                .meta-time { font-size: 0.85em; color: #6b7280; margin-left: 10px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>AI Run History</h1>
    """
    
    if not sorted_tickets:
        html_content += "<p>No logs found in the database.</p>"
    else:
        for t_id, data in sorted_tickets:
            # The main ticket link (shows ALL logs, including legacy)
            html_content += f"""
                <div class="ticket-group">
                    <div class="ticket-header">
                        <a href="/logs/{t_id}" class="ticket-link">Ticket: {t_id} (View All Logs)</a>
                        <span class="meta-time">Last updated: {data['last_active']}</span>
                    </div>
            """
            
            # The sub-links for specific sessions
            if data["sessions"]:
                html_content += "<ul class='session-list'>"
                # Sort sessions newest first
                sorted_sessions = sorted(data["sessions"], key=lambda x: x["last_active"] or "", reverse=True)
                for s in sorted_sessions:
                    html_content += f"""
                        <li class="session-item">
                            ↳ <a href="/logs/{t_id}/{s['session_id']}" class="session-link">Session: {s['session_id']}</a>
                            <span class="meta-time">({s['last_active']})</span>
                        </li>
                    """
                html_content += "</ul>"
            else:
                html_content += "<div class='meta-time' style='margin-left:0; margin-top:10px;'>Legacy logs only (No specific sessions recorded)</div>"
                
            html_content += "</div>"
            
    html_content += """
            </div>
        </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)

# ==========================================
# 1. THE DATA APIs (Returns raw JSON)
# ==========================================
@app.get("/api/logs/{ticket_id}")
async def api_get_ticket_logs(ticket_id: str):
    try:
        from database import get_logs_by_ticket
        history = get_logs_by_ticket(ticket_id)
        if not history:
            return {"ticket_id": ticket_id, "history": []}
        return {"ticket_id": ticket_id, "history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs/{ticket_id}/{session_id}")
async def api_get_session_logs(ticket_id: str, session_id: str):
    try:
        from database import get_logs_by_session
        history = get_logs_by_session(ticket_id, session_id)
        if not history:
            return {"ticket_id": ticket_id, "session_id": session_id, "history": []}
        return {"ticket_id": ticket_id, "session_id": session_id, "history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 2. THE HTML UI (Auto-Polling Viewer)
# ==========================================
def get_log_viewer_html(page_title: str, api_endpoint: str):
    """Generates an HTML page that automatically polls the API for new logs."""
    return f"""
    <html>
        <head>
            <title>{page_title} - AI Agency</title>
            <style>
                body {{ font-family: 'Courier New', Courier, monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 0 10px; margin: 0; }}
                
                .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444; padding: 6px 0; margin-bottom: 6px; position: sticky; top: 0; background-color: #1e1e1e; z-index: 10; }}
                .header-left {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
                .header a {{ color: #4fc1ff; text-decoration: none; font-family: -apple-system, sans-serif; font-weight: 500; font-size: 0.85em; margin-right: 5px; }}
                .header a:hover {{ text-decoration: underline; }}
                
                .header-btn {{ background: #333; color: #fff; border: 1px solid #555; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 0.8em; font-family: -apple-system, sans-serif; transition: 0.1s; }}
                .header-btn:hover {{ background: #4fc1ff; color: #000; border-color: #4fc1ff; }}
                
                .controls {{ display: flex; align-items: center; gap: 8px; font-family: -apple-system, sans-serif; font-size: 0.8em; }}
                .status-dot {{ height: 8px; width: 8px; background-color: #4ade80; border-radius: 50%; display: inline-block; box-shadow: 0 0 6px #4ade80; }}
                
                #log-container {{ display: flex; flex-direction: column; gap: 4px; padding-bottom: 20px; }}
                .log-card {{ background: #252526; padding: 4px 6px; border-radius: 2px; border-left: 3px solid #4fc1ff; }}
                .log-card.error {{ border-left-color: #f43f5e; }}
                .log-card.human {{ border-left-color: #a855f7; }}
                
                .log-meta {{ font-size: 0.75em; color: #858585; margin-bottom: 2px; border-bottom: 1px dashed #333; padding-bottom: 2px; display: flex; justify-content: space-between; align-items: center; }}
                .log-meta-left {{ display: flex; gap: 6px; align-items: center; }}
                .log-meta-right {{ display: flex; align-items: center; gap: 10px; }}
                
                .token-container {{ display: flex; align-items: center; gap: 6px; background: #1e1e1e; padding: 2px 6px; border-radius: 3px; border: 1px solid #333; }}
                .token-bar {{ width: 50px; height: 6px; background: #333; border-radius: 3px; overflow: hidden; }}
                .token-fill {{ height: 100%; transition: width 0.3s ease; }}
                .token-fill.safe {{ background: #4ade80; }}
                .token-fill.danger {{ background: #f43f5e; box-shadow: 0 0 4px #f43f5e; }}
                .token-text {{ font-size: 0.9em; font-family: monospace; color: #aaa; min-width: 65px; text-align: right; }}
                
                .log-content {{ font-size: 0.85em; line-height: 1.15; margin: 0; max-height: 4.6em; overflow: hidden; white-space: pre-wrap; word-wrap: break-word; }}
                .log-card.expanded .log-content {{ max-height: none; }}
                
                .toggle-btn {{ display: none; background: none; border: none; color: #4fc1ff; cursor: pointer; padding: 0; font-family: monospace; font-size: 1.1em; font-weight: bold; }}
                .toggle-btn:hover {{ text-decoration: underline; }}
                .log-card.expandable .toggle-btn {{ display: inline-block; }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="header-left">
                    <a href="/logs">← Dashboard</a>
                    <h4 style="margin: 0 5px 0 0; color: #fff; font-size: 0.9em;">{page_title}</h4>
                    <button class="header-btn" onclick="setAllCards(true)">Expand All</button>
                    <button class="header-btn" onclick="setAllCards(false)">Collapse All</button>
                    <button class="header-btn" style="margin-left: 10px;" onclick="window.scrollTo(0, 0)">↑ Top</button>
                    <button class="header-btn" onclick="window.scrollTo(0, document.documentElement.scrollHeight)">↓ Bottom</button>
                </div>
                <div class="controls">
                    <span id="status-text">Polling</span>
                    <span class="status-dot" id="status-indicator"></span>
                </div>
            </div>

            <div id="log-container">
                <div style="color: #858585; font-size: 0.8em;">Waiting for logs...</div>
            </div>

            <script>
                let knownLogCount = 0;
                let isAutoScrollEnabled = true;
                let allExpanded = false;
                let isFetching = false;
                
                const TOKEN_LIMIT = 10000;

                window.addEventListener('scroll', () => {{
                    const scrollPosition = window.innerHeight + window.scrollY;
                    const pageHeight = document.documentElement.scrollHeight;
                    isAutoScrollEnabled = (pageHeight - scrollPosition < 30);
                }});

                function toggleSingleCard(btn, id) {{
                    const card = document.getElementById(`card-${{id}}`);
                    card.classList.toggle('expanded');
                    btn.innerText = card.classList.contains('expanded') ? '▲ Collapse' : '▼ Expand';
                }}

                function setAllCards(expand) {{
                    allExpanded = expand;
                    const cards = document.querySelectorAll('.log-card.expandable');
                    cards.forEach(card => {{
                        const toggleBtn = card.querySelector('.toggle-btn');
                        if (expand) {{
                            card.classList.add('expanded');
                            toggleBtn.innerText = '▲ Collapse';
                        }} else {{
                            card.classList.remove('expanded');
                            toggleBtn.innerText = '▼ Expand';
                        }}
                    }});
                }}

                async function fetchLogs() {{
                    if (isFetching) return;
                    isFetching = true;
                    
                    try {{
                        const fetchUrl = '{api_endpoint}?_t=' + Date.now();
                        const response = await fetch(fetchUrl, {{ cache: 'no-store' }});
                        const data = await response.json();
                        const history = data.history || [];

                        if (history.length > knownLogCount) {{
                            const container = document.getElementById('log-container');
                            if (knownLogCount === 0) container.innerHTML = ''; 
                            
                            for (let i = knownLogCount; i < history.length; i++) {{
                                const log = history[i];
                                const isError = log.is_error ? 'error' : '';
                                const isHuman = log.role === 'human' ? 'human' : '';
                                
                                const toolText = log.tool_name ? ` | Tool: ${{log.tool_name}}` : '';
                                const nodeText = log.node_name ? `Node: ${{log.node_name}}` : `Role: ${{log.role}}`;
                                
                                let tokenHtml = '';
                                if (log.role === 'ai_prompt') {{
                                    const tokens = log.input_tokens || 0;
                                    const percent = Math.min(100, (tokens / TOKEN_LIMIT) * 100);
                                    const statusClass = tokens > TOKEN_LIMIT ? 'danger' : 'safe';
                                    
                                    tokenHtml = `
                                        <div class="token-container" title="Soft limit: 10k tokens">
                                            <span class="token-text">${{tokens.toLocaleString()}} tkns</span>
                                            <div class="token-bar">
                                                <div class="token-fill ${{statusClass}}" style="width: ${{percent}}%;"></div>
                                            </div>
                                        </div>
                                    `;
                                }}
                                
                                const card = document.createElement('div');
                                card.className = `log-card ${{isError}} ${{isHuman}}`;
                                card.id = `card-${{i}}`;
                                
                                card.innerHTML = `
                                    <div class="log-meta">
                                        <div class="log-meta-left">
                                            <strong style="color: #d4d4d4;">[#${{i + 1}}]</strong>
                                            <span>${{log.timestamp}} | ${{nodeText}}${{toolText}}</span>
                                        </div>
                                        <div class="log-meta-right">
                                            ${{tokenHtml}}
                                            <button class="toggle-btn" id="btn-${{i}}" onclick="toggleSingleCard(this, ${{i}})">▼ Expand</button>
                                        </div>
                                    </div>
                                    <div class="log-content" id="content-${{i}}">${{escapeHTML(log.content)}}</div>
                                `;
                                
                                container.appendChild(card);
                                
                                setTimeout(() => {{
                                    const contentDiv = document.getElementById(`content-${{i}}`);
                                    if (contentDiv.scrollHeight > contentDiv.clientHeight + 2) {{
                                        card.classList.add('expandable');
                                        if (allExpanded) {{
                                            card.classList.add('expanded');
                                            document.getElementById(`btn-${{i}}`).innerText = '▲ Collapse';
                                        }}
                                    }}
                                }}, 0);
                            }}
                            
                            knownLogCount = history.length;

                            if (isAutoScrollEnabled) {{
                                window.scrollTo(0, document.documentElement.scrollHeight);
                            }}
                            
                            const indicator = document.getElementById('status-indicator');
                            indicator.style.opacity = '0.2';
                            setTimeout(() => indicator.style.opacity = '1', 200);
                        }}
                    }} catch (err) {{
                        console.error("Failed to fetch logs:", err);
                        document.getElementById('status-text').innerText = "Disconnected";
                        document.getElementById('status-indicator').style.backgroundColor = "#f43f5e";
                    }} finally {{
                        isFetching = false;
                    }}
                }}

                function escapeHTML(str) {{
                    return String(str)
                        .replace(/&/g, "&amp;")
                        .replace(/</g, "&lt;")
                        .replace(/>/g, "&gt;")
                        .replace(/"/g, "&quot;")
                        .replace(/'/g, "&#039;");
                }}

                fetchLogs();
                setInterval(fetchLogs, 3000);
            </script>
        </body>
    </html>
    """

@app.get("/logs/{ticket_id}", response_class=HTMLResponse)
async def view_ticket_logs(ticket_id: str):
    return get_log_viewer_html(
        page_title=f"Full History: {ticket_id}", 
        api_endpoint=f"/api/logs/{ticket_id}"
    )

@app.get("/logs/{ticket_id}/{session_id}", response_class=HTMLResponse)
async def view_session_logs(ticket_id: str, session_id: str):
    return get_log_viewer_html(
        page_title=f"Session: {session_id}", 
        api_endpoint=f"/api/logs/{ticket_id}/{session_id}"
    )

@app.post("/webhook")
async def linear_webhook(request: Request, background_tasks: BackgroundTasks):
    # Parse the incoming JSON from Linear
    payload = await request.json()
    
    # Intelligently parse the payload (Handles both "Issue" and "Comment" webhooks)
    webhook_type = payload.get("type", "Issue")
    
    if webhook_type == "Comment":
        # If the webhook is a comment, the ID is nested differently
        ticket_id = payload.get("data", {}).get("issue", {}).get("id", "UNKNOWN-ID")
        human_input = payload.get("data", {}).get("body", "No comment provided.")

        # Extract team_id for comments
        team_id = payload.get("data", {}).get("issue", {}).get("teamId", "DEFAULT_TEAM")
    else:
        # Standard new issue creation
        ticket_id = payload.get("data", {}).get("id", "UNKNOWN-ID")
        ticket_title = payload.get("data", {}).get("title", "New Task")
        ticket_description = payload.get("data", {}).get("description", "")
        
        # Extract team_id for issues
        team_id = payload.get("data", {}).get("teamId", "DEFAULT_TEAM")

        human_input = (
            f"New Task Assigned: {ticket_title}\n\n"
            f"Description: {ticket_description}\n\n"
            f"IMPORTANT: The active Linear Ticket ID for this task is {ticket_id}.\n"
            f"IMPORTANT: Do not guess or invent a repository URL. Use the environment variable $GITHUB_REPO.\n"
        )
    
    # Load Tenant Config From Vault asynchronously without blocking the event loop
    tenant_config = await asyncio.to_thread(read_tenant_vault, team_id)

    # CONCURRENCY CHECK: Intercept identical tickets
    if ticket_id in active_tickets:
        return {
            "status": "Ignored",
            "message": "This ticket is already being processed. Wait for the active process to finish and resend the request.",
            "ticket": ticket_id
        }
        
    # Add to active tickets before doing DB lookups or dispatching
    active_tickets.add(ticket_id)
    
    # Offload the blocking AI execution to a background thread
    background_tasks.add_task(process_ticket_with_lock, ticket_id, human_input, tenant_config)

    # Return immediately so Linear doesn't timeout the webhook!
    return {"status": "AI Team Dispatched/Resumed in background", "ticket": ticket_id}
