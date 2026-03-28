from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from onboarding import get_layout_header, get_sidebar_html, get_project_sidebar_html, get_vault
from database import get_all_sessions, get_logs_by_ticket, get_logs_by_session
from datetime import datetime

router = APIRouter()

@router.get("/logs", response_class=HTMLResponse)
def view_all_logs(project: str = None):
    raw_data = get_all_sessions()
    vault = get_vault()
    
    # 1. Group the data by Workspace and Ticket ID combo
    tickets = {}
    projects = set() # Track unique projects for the dropdown
    
    for row in raw_data:
        t_id = row.get("ticket_id")
        s_id = row.get("session_id")
        p_id = row.get("project_id")
        last_active = row.get("last_active")
        first_active = row.get("first_active")
        total_input_tokens = row.get("total_input_tokens") or 0
        
        # --- 1. CALCULATE DURATION ---
        duration_str = "0m 0s"
        if first_active and last_active:
            try:
                # SQLite timestamps are usually 'YYYY-MM-DD HH:MM:SS'
                # We slice [:19] to safely ignore any fractional seconds if they exist
                t1 = datetime.strptime(first_active[:19], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(last_active[:19], "%Y-%m-%d %H:%M:%S")
                diff_seconds = (t2 - t1).total_seconds()
                
                mins = int(diff_seconds // 60)
                secs = int(diff_seconds % 60)
                duration_str = f"{mins}m {secs}s"
            except ValueError:
                pass # Fallback to 0m 0s if date format is unexpected

        # --- 2. CALCULATE COST (Gemini 2.5 Pro is ~$1.25 per 1M input tokens) ---
        cost = (total_input_tokens / 1_000_000) * 1.25
        cost_str = f"${cost:.4f}" # Show fractions of a cent since single runs are cheap

        # Resolve project name
        p_name = vault.get(p_id, {}).get("project_name", "Unknown Project") if p_id else "Legacy Data"
        if p_id:
            projects.add((p_id, p_name))
            
        group_key = f"{p_name} | Ticket: {t_id}"
        
        if group_key not in tickets:
            tickets[group_key] = {
                "project_id": p_id or "unknown",
                "ticket_id": t_id,
                "display_title": f"Ticket: {t_id}", 
                "last_active": last_active,
                "sessions": []
            }
            
        if last_active and last_active > tickets[group_key]["last_active"]:
            tickets[group_key]["last_active"] = last_active
            
        if s_id:
            tickets[group_key]["sessions"].append({
                "session_id": s_id,
                "last_active": last_active,
                "duration": duration_str,   
                "cost": cost_str,           
                "tokens": total_input_tokens
            })

    sorted_tickets = sorted(tickets.values(), key=lambda x: x["last_active"] or "", reverse=True)

    # Build dropdown options
    dropdown_options = ""
    for pid, pname in sorted(list(projects), key=lambda x: x[1]):
        dropdown_options += f'<option value="{pid}">{pname}</option>'

    # 2. Determine which sidebar to show based on the URL parameter
    if project and project in vault:
        project_name = vault.get(project, {}).get("project_name", project)
        sidebar_html = get_project_sidebar_html(project, project_name, "logs")
    else:
        sidebar_html = get_sidebar_html("logs")

    html_content = f"""
    <html>
        <head>
            <title>AI Agency - Run History</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <script>
                tailwind.config = {{ darkMode: 'class', theme: {{ extend: {{ colors: {{ 'log-bg': '#1e1e1e', 'log-surface': '#252526', 'log-border': '#333333', 'log-text': '#d4d4d4' }} }} }} }};
                
                // Ensure night mode works properly on load
                if (localStorage.theme === 'dark' || (!('theme' in localStorage) && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
                    document.documentElement.classList.add('dark');
                }} else {{
                    document.documentElement.classList.remove('dark');
                }}
                
                function filterLogs() {{
                    const filterValue = document.getElementById('project-filter').value;
                    const items = document.querySelectorAll('.log-item');
                    items.forEach(item => {{
                        if (filterValue === 'ALL' || item.getAttribute('data-project') === filterValue) {{
                            item.style.display = 'block';
                        }} else {{
                            item.style.display = 'none';
                        }}
                    }});
                }}

                // Auto-filter based on URL parameter
                window.addEventListener('DOMContentLoaded', () => {{
                    const urlParams = new URLSearchParams(window.location.search);
                    const projectId = urlParams.get('project');
                    if (projectId) {{
                        const select = document.getElementById('project-filter');
                        // Ensure the option exists before setting it
                        if ([...select.options].some(opt => opt.value === projectId)) {{
                            select.value = projectId;
                            filterLogs();
                        }}
                    }}
                }});
            </script>
        </head>
        <body class="bg-gray-50 dark:bg-log-bg transition-colors duration-200 h-screen overflow-hidden flex flex-col">
            
            {get_layout_header()}

            <div class="flex flex-1 overflow-hidden">
                
                <!-- Dynamically injected Sidebar -->
                {sidebar_html}

                <div class="flex-1 overflow-y-auto p-8">
                    <div class="max-w-4xl mx-auto">
                        <div class="flex justify-between items-center mb-6 pb-4 border-b border-gray-200 dark:border-log-border">
                            <h1 class="text-2xl font-medium text-gray-900 dark:text-log-text">Run History</h1>
                            <select id="project-filter" onchange="filterLogs()" class="bg-white dark:bg-log-surface text-gray-700 dark:text-gray-300 border border-gray-300 dark:border-log-border rounded px-4 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500 transition cursor-pointer">
                                <option value="ALL">All Projects</option>
                                {dropdown_options}
                            </select>
                        </div>
    """
    
    if not sorted_tickets:
        html_content += '<p class="text-gray-500 dark:text-gray-400">No logs found in the database.</p>'
    else:
        for data in sorted_tickets:
            p_id = data["project_id"]
            t_id = data["ticket_id"]
            html_content += f"""
                <div class="log-item bg-white dark:bg-log-surface p-5 rounded border border-gray-200 dark:border-log-border mb-4" data-project="{p_id}">
                    <div class="flex justify-between items-center mb-3">
                        <a href="/logs/{t_id}" target="_blank" class="text-lg font-medium text-gray-900 dark:text-gray-100 hover:text-blue-600 dark:hover:text-blue-400 hover:underline">{data["display_title"]}</a>
                        <span class="text-xs text-gray-500 dark:text-gray-400">Last updated: {data['last_active']}</span>
                    </div>
            """
            if data["sessions"]:
                html_content += '<div class="pl-4 border-l-2 border-gray-100 dark:border-log-border space-y-2 mt-3">'
                sorted_sessions = sorted(data["sessions"], key=lambda x: x["last_active"] or "", reverse=True)
                for s in sorted_sessions:
                    html_content += f"""
                        <div class="flex items-center justify-between text-sm py-1">
                            <div class="flex items-center">
                                <span class="text-gray-400 dark:text-gray-500 mr-2">↳</span>
                                <a href="/logs/{t_id}/{s['session_id']}" target="_blank" class="text-gray-600 dark:text-gray-400 font-medium hover:text-blue-600 dark:hover:text-blue-400 hover:underline mr-3">Session: {s['session_id']}</a>
                                <span class="text-xs text-gray-400">({s['last_active']})</span>
                            </div>
                            <div class="flex items-center space-x-4 text-xs font-mono text-gray-500 dark:text-gray-400">
                                <span title="Input Tokens">🪙 {s['tokens']:,} tkns</span>
                                <span title="Session Duration">⏱️ {s['duration']}</span>
                                <span title="Estimated Cost" class="text-green-600 dark:text-green-400 font-medium">{s['cost']}</span>
                            </div>
                        </div>
                    """
                html_content += "</div>"
            else:
                html_content += "<div class='text-xs text-gray-400 mt-2'>Legacy logs only</div>"
                
            html_content += "</div>"
            
    html_content += """
                    </div>
                </div>
            </div>
        </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)

# ==========================================
# 1. THE DATA APIs (Returns raw JSON)
# ==========================================
@router.get("/api/logs/{ticket_id}")
def api_get_ticket_logs(ticket_id: str):
    try:
        history = get_logs_by_ticket(ticket_id)
        if not history:
            return {"ticket_id": ticket_id, "history": []}
        return {"ticket_id": ticket_id, "history": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/logs/{ticket_id}/{session_id}")
def api_get_session_logs(ticket_id: str, session_id: str):
    try:
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
    """Generates an HTML page with an interactive Timeline, Copy features, and consistent Log Viewer."""
    return f"""
    <html>
        <head>
            <title>{page_title} - AI Agency</title>
            <style>
                body {{ font-family: 'Courier New', Courier, monospace; background-color: #1e1e1e; color: #d4d4d4; margin: 0; height: 100vh; overflow: hidden; display: flex; flex-direction: column; }}
                
                .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444; padding: 6px 15px; background-color: #1e1e1e; z-index: 10; flex-shrink: 0; }}
                .header-left {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
                .header a {{ color: #4fc1ff; text-decoration: none; font-family: -apple-system, sans-serif; font-weight: 500; font-size: 0.85em; margin-right: 5px; }}
                .header a:hover {{ text-decoration: underline; }}
                
                .header-btn {{ background: #333; color: #fff; border: 1px solid #555; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 0.8em; font-family: -apple-system, sans-serif; transition: 0.1s; }}
                .header-btn:hover {{ background: #4fc1ff; color: #000; border-color: #4fc1ff; }}
                
                /* Filter Bar */
                .filter-bar {{ background: #181818; padding: 6px 15px; border-bottom: 1px solid #333; display: flex; gap: 15px; font-family: -apple-system, sans-serif; font-size: 0.85em; flex-shrink: 0; }}
                .filter-bar label {{ cursor: pointer; display: flex; align-items: center; gap: 4px; color: #aaa; }}
                .filter-bar label:hover {{ color: #fff; }}
                
                /* Layout */
                .layout {{ display: flex; flex: 1; overflow: hidden; }}
                .main-content {{ flex: 1; overflow-y: auto; padding: 10px 15px; display: flex; flex-direction: column; gap: 10px; scroll-behavior: smooth; }}
                
                /* Sidebar */
                .sidebar {{ width: 280px; background: #252526; border-right: 1px solid #444; display: flex; flex-direction: column; flex-shrink: 0; transition: width 0.3s ease; overflow: hidden; }}
                .sidebar.collapsed {{ width: 40px; }}
                
                .sidebar-header {{ padding: 10px; font-family: -apple-system, sans-serif; font-weight: bold; font-size: 0.9em; border-bottom: 1px solid #444; color: #d4d4d4; background: #1e1e1e; display: flex; justify-content: space-between; align-items: center; white-space: nowrap; }}
                
                .sidebar.collapsed .sidebar-title {{ display: none; }}
                .sidebar.collapsed .sidebar-header {{ justify-content: center; padding: 10px 0; }}
                .sidebar.collapsed .sidebar-list {{ display: none; }}
                
                .sidebar-toggle-btn {{ background: none; border: none; color: #858585; cursor: pointer; font-size: 1.1em; padding: 0 5px; transition: color 0.2s; }}
                .sidebar-toggle-btn:hover {{ color: #fff; }}

                .sidebar-list {{ flex: 1; overflow-y: auto; padding: 10px; margin: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }}
                
                .checkpoint-item {{ font-family: -apple-system, sans-serif; padding: 8px; border-radius: 4px; background: #1e1e1e; cursor: pointer; font-size: 0.8em; color: #ccc; border-left: 4px solid transparent; transition: 0.1s; display: flex; flex-direction: column; gap: 4px; }}
                .checkpoint-item:hover {{ background: #333; filter: brightness(1.2); }}
                .checkpoint-time {{ font-family: monospace; font-size: 0.9em; color: #888; }}
                
                /* Checkpoint Colors */
                .cp-human {{ border-left-color: #a855f7; }}
                .cp-error {{ border-left-color: #f43f5e; }}
                .cp-manager {{ border-left-color: #4fc1ff; }}

                .controls {{ display: flex; align-items: center; gap: 8px; font-family: -apple-system, sans-serif; font-size: 0.8em; }}
                .status-dot {{ height: 8px; width: 8px; background-color: #4ade80; border-radius: 50%; display: inline-block; box-shadow: 0 0 6px #4ade80; }}
                
                /* Log Cards */
                .log-card {{ background: #252526; padding: 0 0 8px 0; border-radius: 4px; border-left: 3px solid #4fc1ff; border-bottom: 1px dashed #333; transition: background-color 0.5s ease; overflow: hidden; }}
                .log-card.error {{ border-left-color: #f43f5e; }}
                .log-card.human {{ border-left-color: #a855f7; }}
                
                .log-card.flash-highlight {{ animation: flashAnim 2s ease-out; }}
                @keyframes flashAnim {{ 0% {{ background-color: #5a5a5a; }} 100% {{ background-color: #252526; }} }}
                
                .log-meta {{ font-size: 0.75em; color: #858585; background: transparent; padding: 8px 12px; border-bottom: 1px dashed #333; display: flex; justify-content: space-between; align-items: center; }}
                .log-meta-left {{ display: flex; gap: 6px; align-items: center; }}
                .log-meta-right {{ display: flex; align-items: center; gap: 10px; }}
                
                /* Universal Dropdown Styles (For Copy only) */
                .dropdown {{ position: relative; display: inline-block; }}
                .dropdown-content {{ display: none; position: absolute; right: 0; top: 100%; background-color: #252526; min-width: 150px; box-shadow: 0px 4px 12px rgba(0,0,0,0.5); z-index: 100; border-radius: 3px; border: 1px solid #444; overflow: hidden; }}
                .dropdown-content button {{ color: #d4d4d4; padding: 8px 12px; text-decoration: none; display: block; background: none; border: none; width: 100%; text-align: left; cursor: pointer; font-family: -apple-system, sans-serif; font-size: 0.9em; transition: background 0.1s; }}
                .dropdown-content button:hover {{ background-color: #4fc1ff; color: #000; }}
                .dropdown:hover .dropdown-content {{ display: block; }}
                
                /* Synchronized Button Styles */
                .copy-btn {{ background: none; border: 1px solid #555; color: #aaa; border-radius: 3px; cursor: pointer; font-family: -apple-system, sans-serif; font-size: 0.85em; padding: 2px 6px; transition: 0.1s; display: flex; align-items: center; gap: 4px; }}
                .copy-btn:hover {{ background: #333; color: #fff; border-color: #888; }}
                
                .toggle-btn {{ display: none; background: none; border: none; color: #4fc1ff; cursor: pointer; padding: 0; font-family: -apple-system, sans-serif; font-size: 0.85em; transition: 0.1s; align-items: center; gap: 4px; }}
                .toggle-btn:hover {{ color: #fff; text-decoration: underline; }}
                .log-card.expandable .toggle-btn {{ display: flex; }}
                
                .token-container {{ display: flex; align-items: center; gap: 6px; background: #1e1e1e; padding: 2px 6px; border-radius: 3px; border: 1px solid #333; }}
                .token-bar {{ width: 50px; height: 6px; background: #333; border-radius: 3px; overflow: hidden; }}
                .token-fill {{ height: 100%; transition: width 0.3s ease; }}
                .token-fill.safe {{ background: #4ade80; }}
                .token-fill.danger {{ background: #f43f5e; box-shadow: 0 0 4px #f43f5e; }}
                .token-text {{ font-size: 0.9em; font-family: monospace; color: #aaa; min-width: 65px; text-align: right; }}
                
                /* EXACT FIXED HEIGHT */
                .log-content {{ 
                    font-size: 0.85em; 
                    line-height: 1.2;
                    margin: 0; 
                    padding: 8px 12px;
                    max-height: 4.8em; 
                    overflow: hidden; 
                    white-space: pre-wrap; 
                    word-wrap: break-word;
                }}
                .log-card.expanded .log-content {{ 
                    max-height: none; 
                    overflow: auto; 
                }}

                /* View Filters Logic */
                body.hide-prompts .role-ai_prompt {{ display: none; }}
                body.hide-tools .role-tool_output {{ display: none; }}
                body.hide-raw .role-ai_raw_response {{ display: none; }}
                body.hide-traces .role-system_trace {{ display: none; }}

                /* BARE-METAL JSON VIEWER */
                .json-viewer {{ font-family: inherit; white-space: pre-wrap; word-break: break-word; color: #d4d4d4; }}
                
                /* Inline Node Controls - Flowing naturally next to the node name */
                .node-controls {{ margin-left: 12px; font-size: 0.9em; color: #858585; display: inline-block; font-weight: normal; }}
                .node-controls span {{ cursor: pointer; transition: 0.1s; }}
                .node-controls span:hover {{ text-decoration: underline; color: #fff; }}
                
                details.json-node {{ margin: 2px 0; font-size: inherit; }}
                details.json-node > summary {{ cursor: pointer; color: #d4d4d4; user-select: none; font-weight: normal; outline: none; margin-left: -4px; padding-left: 4px; border-radius: 3px; display: inline-block; transition: 0.1s; font-size: inherit; }}
                details.json-node > summary:hover {{ background-color: #333; }}
                .json-children {{ margin-left: 12px; padding-left: 8px; border-left: 1px dashed #555; font-size: inherit; }}
                .json-row {{ margin: 2px 0; display: flex; align-items: flex-start; flex-wrap: wrap; gap: 4px; font-size: inherit; }}
                .json-key {{ color: #d4d4d4; margin-right: 4px; white-space: nowrap; flex-shrink: 0; font-size: inherit; }}
                .json-string {{ color: #d4d4d4; word-break: break-word; white-space: pre-wrap; flex: 1; min-width: 0; font-size: inherit; }}
                .json-number {{ color: #d4d4d4; font-size: inherit; }}
                .json-boolean {{ color: #d4d4d4; font-size: inherit; }}
                .json-null {{ color: #aaa; font-size: inherit; }}
            </style>
        </head>
        <body>
            <div class="header">
                <div class="header-left">
                    <a href="/logs">← Dashboard</a>
                    <h4 style="margin: 0 10px 0 0; color: #fff; font-size: 0.9em;">{page_title}</h4>
                    <button class="header-btn" onclick="setAllCards(true)">Expand All</button>
                    <button class="header-btn" onclick="setAllCards(false)">Collapse All</button>
                    <button class="header-btn" style="margin-left: 10px;" onclick="document.querySelector('.main-content').scrollTo(0, 0)">↑ Top</button>
                    <button class="header-btn" onclick="document.querySelector('.main-content').scrollTo(0, document.querySelector('.main-content').scrollHeight)">↓ Bottom</button>
                </div>
                <div class="controls">
                    <span id="status-text">Polling</span>
                    <span class="status-dot" id="status-indicator"></span>
                </div>
            </div>

            <div class="filter-bar">
                <strong style="color: #fff; margin-right: 5px;">View Filters:</strong>
                <label><input type="checkbox" onchange="document.body.classList.toggle('hide-prompts', !this.checked); setTimeout(recheckExpandable, 50);" checked> AI Prompts (Input)</label>
                <label><input type="checkbox" onchange="document.body.classList.toggle('hide-tools', !this.checked); setTimeout(recheckExpandable, 50);" checked> Tool Outputs</label>
                <label><input type="checkbox" onchange="document.body.classList.toggle('hide-raw', !this.checked); setTimeout(recheckExpandable, 50);"> Raw JSON Metadata</label>
                <label><input type="checkbox" onchange="document.body.classList.toggle('hide-traces', !this.checked); setTimeout(recheckExpandable, 50);"> System Traces</label>
            </div>

            <div class="layout">
                <div class="sidebar" id="checkpoint-sidebar">
                    <div class="sidebar-header">
                        <span class="sidebar-title">Timeline</span>
                        <button class="sidebar-toggle-btn" id="toggle-sidebar-btn" onclick="toggleSidebar()" title="Toggle Sidebar">◀</button>
                    </div>
                    <ul class="sidebar-list" id="sidebar-list">
                        <div id="cp-placeholder" style="color: #555; font-size: 0.8em; padding: 10px; text-align: center;">No checkpoints yet...</div>
                    </ul>
                </div>

                <div class="main-content" id="main-scroll-area">
                    <div id="log-container">
                        <div style="color: #858585; font-size: 0.8em; margin-bottom: 10px;">Waiting for logs...</div>
                    </div>
                </div>
            </div>

            <script>
                let rawLogData = []; 
                let knownLogCount = 0;
                let isAutoScrollEnabled = true;
                let allExpanded = false;
                let isFetching = false;
                
                const TOKEN_LIMIT = 10000;

                document.body.classList.add('hide-raw', 'hide-traces');

                const mainScrollArea = document.getElementById('main-scroll-area');

                mainScrollArea.addEventListener('scroll', () => {{
                    const scrollPosition = mainScrollArea.clientHeight + mainScrollArea.scrollTop;
                    const pageHeight = mainScrollArea.scrollHeight;
                    isAutoScrollEnabled = (pageHeight - scrollPosition < 30);
                }});

                // SCROLL PRESERVATION LOGIC
                function preserveScroll(btn, actionFn) {{
                    const rectBefore = btn.getBoundingClientRect();
                    actionFn();
                    const rectAfter = btn.getBoundingClientRect();
                    const diff = rectAfter.top - rectBefore.top;
                    if (diff !== 0) {{
                        mainScrollArea.scrollTop += diff;
                    }}
                }}

                // Re-measures cards when they become visible
                function recheckExpandable() {{
                    document.querySelectorAll('.log-card:not(.expandable)').forEach(card => {{
                        const contentDiv = card.querySelector('.log-content');
                        // offsetParent is null if the element is display: none
                        if (contentDiv && contentDiv.offsetParent !== null) {{
                            if (contentDiv.scrollHeight > contentDiv.clientHeight + 2) {{
                                card.classList.add('expandable');
                            }}
                        }}
                    }});
                }}

                function toggleSingleCard(btn, id) {{
                    preserveScroll(btn, () => {{
                        const card = document.getElementById(`card-${{id}}`);
                        const isExpanded = card.classList.contains('expanded');
                        
                        if (!isExpanded) {{
                            card.classList.add('expanded');
                            btn.innerHTML = '<span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▲</span> Collapse';
                        }} else {{
                            card.classList.remove('expanded');
                            btn.innerHTML = '<span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▼</span> Expand';
                        }}
                    }});
                }}
                
                function expandNodes(e, el) {{
                    e.preventDefault(); // Prevents the <summary> tag from natively toggling
                    e.stopPropagation();
                    preserveScroll(el, () => {{
                        const viewer = el.closest('.json-viewer');
                        const details = viewer.querySelectorAll('details.json-node');
                        details.forEach(d => d.open = true);
                    }});
                }}

                function collapseNodes(e, el) {{
                    e.preventDefault(); // Prevents the <summary> tag from natively toggling
                    e.stopPropagation();
                    preserveScroll(el, () => {{
                        const viewer = el.closest('.json-viewer');
                        const details = viewer.querySelectorAll('details.json-node');
                        details.forEach(d => {{
                            if (d.parentElement.classList.contains('json-viewer')) {{
                                d.open = true; // Keep the absolute root open
                            }} else {{
                                d.open = false; // Collapse all children
                            }}
                        }});
                    }});
                }}

                function setAllCards(expand) {{
                    allExpanded = expand;
                    const cards = document.querySelectorAll('.log-card.expandable');
                    cards.forEach(card => {{
                        const toggleBtn = card.querySelector('.toggle-btn');
                        if (expand) {{
                            card.classList.add('expanded');
                            if (toggleBtn) toggleBtn.innerHTML = '<span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▲</span> Collapse';
                        }} else {{
                            card.classList.remove('expanded');
                            if (toggleBtn) toggleBtn.innerHTML = '<span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▼</span> Expand';
                        }}
                    }});
                }}

                function toggleSidebar() {{
                    const sidebar = document.getElementById('checkpoint-sidebar');
                    const btn = document.getElementById('toggle-sidebar-btn');
                    sidebar.classList.toggle('collapsed');
                    btn.innerText = sidebar.classList.contains('collapsed') ? '▶' : '◀';
                }}

                async function copyText(btn, index, stripThoughts = false) {{
                    try {{
                        let rawText = rawLogData[index].content;
                        
                        if (stripThoughts) {{
                            try {{
                                let parsedData = JSON.parse(rawText);
                                function removeThoughts(obj) {{
                                    if (typeof obj !== 'object' || obj === null) return;
                                    if (Array.isArray(obj)) {{
                                        obj.forEach(removeThoughts);
                                    }} else {{
                                        if ('__gemini_function_call_thought_signatures__' in obj) {{
                                            delete obj['__gemini_function_call_thought_signatures__'];
                                        }}
                                        Object.values(obj).forEach(removeThoughts);
                                    }}
                                }}
                                removeThoughts(parsedData);
                                rawText = JSON.stringify(parsedData, null, 2);
                            }} catch (e) {{
                                rawText = rawText.replace(/"__gemini_function_call_thought_signatures__"\\s*:\\s*\\[[\\s\\S]*?\\](,\\s*|\\s*(?=\\}}))/g, '');
                            }}
                        }}

                        await navigator.clipboard.writeText(rawText);
                        
                        const originalText = btn.innerHTML;
                        btn.innerHTML = "✓ Copied";
                        btn.style.color = "#4ade80";
                        
                        setTimeout(() => {{
                            btn.innerHTML = originalText;
                            btn.style.color = "";
                        }}, 2000);
                    }} catch (err) {{
                        console.error('Failed to copy text: ', err);
                        btn.innerHTML = "Failed";
                    }}
                }}

                function jumpToCard(index) {{
                    const card = document.getElementById(`card-${{index}}`);
                    if (card) {{
                        card.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                        card.classList.remove('flash-highlight');
                        void card.offsetWidth;
                        card.classList.add('flash-highlight');
                    }}
                }}

                function appendCheckpoint(index, type, label, summary, timestamp) {{
                    const sidebar = document.getElementById('sidebar-list');
                    const placeholder = document.getElementById('cp-placeholder');
                    if (placeholder) placeholder.remove(); 
                    
                    const li = document.createElement('li');
                    li.className = `checkpoint-item cp-${{type}}`;
                    li.onclick = () => jumpToCard(index);
                    
                    li.innerHTML = `
                        <div style="font-weight: 600;">${{label}}</div>
                        <div style="color: #aaa; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${{summary}}</div>
                        <div class="checkpoint-time">${{timestamp}}</div>
                    `;
                    sidebar.appendChild(li);
                }}

                // --- Universal Log Formatter ---
                function formatLogContent(rawText) {{
                    if (!rawText) return '';
                    const trimmed = String(rawText).trim();
                    
                    if ((trimmed.startsWith('{{') && trimmed.endsWith('}}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {{
                        try {{
                            const parsed = JSON.parse(trimmed);
                            // We pass 'true' for isRoot so the first node embeds the controls inline
                            return '<div class="json-viewer">' + renderJsonTree(parsed, 0, '', false, true) + '</div>';
                        }} catch (e) {{
                            // Fails safely if it's just raw text that happens to have brackets
                        }}
                    }}
                    return escapeHTML(rawText);
                }}

                function renderJsonTree(data, depth, key = '', isArrayIndex = false, isRoot = false) {{
                    let keyHtml = '';
                    if (key !== '') {{
                        keyHtml = isArrayIndex ? '<span class="json-key">' + key + ':</span> ' : '<span class="json-key">"' + escapeHTML(key) + '":</span> ';
                    }}

                    // Inline Controls injected seamlessly after the root node text
                    let controlsHtml = isRoot ? ' <span class="node-controls"><span onclick="expandNodes(event, this)">Expand All</span> &nbsp;|&nbsp; <span onclick="collapseNodes(event, this)">Collapse All</span></span>' : '';

                    if (data === null) return '<div class="json-row">' + keyHtml + '<span class="json-null">null</span></div>';
                    if (typeof data === 'boolean') return '<div class="json-row">' + keyHtml + '<span class="json-boolean">' + data + '</span></div>';
                    if (typeof data === 'number') return '<div class="json-row">' + keyHtml + '<span class="json-number">' + data + '</span></div>';
                    
                    if (typeof data === 'string') {{
                        if (data.includes('\\n') || data.length > 150) {{
                            let html = '<details class="json-node" ' + (depth < 3 ? 'open' : '') + '><summary>' + keyHtml + 'String [' + data.length + ' chars]' + controlsHtml + '</summary><div class="json-children">';
                            html += '<div class="json-row"><span class="json-string">"' + escapeHTML(data) + '"</span></div>';
                            html += '</div></details>';
                            return html;
                        }}
                        return '<div class="json-row">' + keyHtml + '<span class="json-string">"' + escapeHTML(data) + '"</span></div>';
                    }}

                    const openAttr = depth < 3 ? 'open' : '';

                    if (Array.isArray(data)) {{
                        if (data.length === 0) return '<div class="json-row">' + keyHtml + '<span style="color:#888">[]</span></div>';
                        let html = '<details class="json-node" ' + openAttr + '><summary>' + keyHtml + 'Array [' + data.length + ']' + controlsHtml + '</summary><div class="json-children">';
                        data.forEach((item, index) => {{
                            html += renderJsonTree(item, depth + 1, String(index), true, false);
                        }});
                        html += '</div></details>';
                        return html;
                    }}

                    if (typeof data === 'object') {{
                        const keys = Object.keys(data);
                        if (keys.length === 0) return '<div class="json-row">' + keyHtml + '<span style="color:#888">{{}}</span></div>';
                        let html = '<details class="json-node" ' + openAttr + '><summary>' + keyHtml + 'Object {{' + keys.length + '}}' + controlsHtml + '</summary><div class="json-children">';
                        keys.forEach(k => {{
                            html += renderJsonTree(data[k], depth + 1, k, false, false);
                        }});
                        html += '</div></details>';
                        return html;
                    }}
                    return '<div class="json-row">' + keyHtml + escapeHTML(String(data)) + '</div>';
                }}
                // --------------------------------

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
                                rawLogData.push(log); // Always save raw text for pristine copying
                                
                                const isError = log.is_error ? 'error' : '';
                                const isHuman = log.role === 'human' ? 'human' : '';
                                const roleClass = `role-${{log.role}}`;
                                
                                const toolText = log.tool_name ? ` | Tool: ${{log.tool_name}}` : '';
                                const nodeText = log.node_name ? `Node: ${{log.node_name}}` : `Role: ${{log.role}}`;
                                
                                if (log.role === 'human') {{
                                    appendCheckpoint(i, 'human', 'Human Intervention', escapeHTML(log.content).substring(0, 40) + '...', log.timestamp);
                                }} else if (log.is_error) {{
                                    appendCheckpoint(i, 'error', `Error: ${{log.node_name}}`, toolText.replace(' | Tool: ', ''), log.timestamp);
                                }} else if (log.node_name === 'Manager' && log.role === 'ai_tool_call' && log.tool_name === 'RouteDecision') {{
                                    appendCheckpoint(i, 'manager', 'Manager Routing', escapeHTML(log.content).substring(0, 40) + '...', log.timestamp);
                                }}
                                
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
                                card.className = `log-card ${{isError}} ${{isHuman}} ${{roleClass}}`;
                                card.id = `card-${{i}}`;
                                
                                card.innerHTML = `
                                    <div class="log-meta">
                                        <div class="log-meta-left">
                                            <strong style="color: #d4d4d4;">[#${{i + 1}}]</strong>
                                            <span>${{log.timestamp}} | ${{nodeText}}${{toolText}}</span>
                                        </div>
                                        <div class="log-meta-right">
                                            ${{tokenHtml}}
                                            <div class="dropdown">
                                                <button class="copy-btn" id="copy-btn-${{i}}">Copy <span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▼</span></button>
                                                <div class="dropdown-content">
                                                    <button onclick="copyText(document.getElementById('copy-btn-${{i}}'), ${{i}}, false)">Copy All</button>
                                                    <button onclick="copyText(document.getElementById('copy-btn-${{i}}'), ${{i}}, true)">Copy w/o Thoughts</button>
                                                </div>
                                            </div>
                                            <button class="toggle-btn" id="btn-${{i}}" onclick="toggleSingleCard(this, ${{i}})"><span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▼</span> Expand</button>
                                        </div>
                                    </div>
                                    <div class="log-content" id="content-${{i}}">${{formatLogContent(log.content)}}</div>
                                `;
                                
                                container.appendChild(card);
                                
                                setTimeout(() => {{
                                    const contentDiv = document.getElementById(`content-${{i}}`);
                                    if (contentDiv.scrollHeight > contentDiv.clientHeight + 2) {{
                                        card.classList.add('expandable');
                                        if (allExpanded) {{
                                            card.classList.add('expanded');
                                            document.getElementById(`btn-${{i}}`).innerHTML = '<span style="font-family: monospace; font-size: 1.1em; font-weight: bold; pointer-events: none;">▲</span> Collapse';
                                        }}
                                    }}
                                }}, 0);
                            }}
                            
                            knownLogCount = history.length;

                            if (isAutoScrollEnabled) {{
                                mainScrollArea.scrollTo(0, mainScrollArea.scrollHeight);
                                const sidebarList = document.getElementById('sidebar-list');
                                sidebarList.scrollTo(0, sidebarList.scrollHeight);
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

@router.get("/logs/{ticket_id}", response_class=HTMLResponse)
async def view_ticket_logs(ticket_id: str):
    return get_log_viewer_html(
        page_title=f"Full History: {ticket_id}", 
        api_endpoint=f"/api/logs/{ticket_id}"
    )

@router.get("/logs/{ticket_id}/{session_id}", response_class=HTMLResponse)
async def view_session_logs(ticket_id: str, session_id: str):
    return get_log_viewer_html(
        page_title=f"Session: {session_id}", 
        api_endpoint=f"/api/logs/{ticket_id}/{session_id}"
    )