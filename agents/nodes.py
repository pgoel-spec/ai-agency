import os
import json
import copy
from pydantic import BaseModel, Field
from langchain_google_vertexai.model_garden import ChatAnthropicVertex
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from .prompts import get_developer_prompt
from .state import AgencyState, DEVELOPER_ITERATION_LIMIT
from .tools import (
    update_linear_ticket, create_github_pr, run_terminal_command, 
    read_file, write_file, sync_wip_commit, web_search, generate_repo_map,
    run_backend_test, run_frontend_test, run_unit_tests
)
from database import get_logs_by_ticket, log_activity
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result,
    retry_if_exception
)
# Import the specific LangChain Google error
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError
# Import the specific Google API errors to catch
from google.api_core import exceptions as google_exceptions
from datetime import datetime

def get_short_time():
    return datetime.now().strftime("%b %d, %H:%M")

def is_retryable_error(exception):
    """
    Checks if the error is a 429 Resource Exhausted error.
    This prevents retrying on fatal errors like safety filters or invalid arguments.
    """
    if isinstance(exception, ChatGoogleGenerativeAIError):
        # Retry if '429' or 'RESOURCE_EXHAUSTED' is in the error message
        return "429" in str(exception) or "RESOURCE_EXHAUSTED" in str(exception)
    
    # Also catch the raw Google exception if it ever bubbles up
    if isinstance(exception, google_exceptions.ResourceExhausted):
        return True
        
    return False

# Define the smart retry strategy
# wait_exponential(multiplier=1, min=4, max=60) means:
# Wait 4s, 8s, 16s, 32s, then capped at 60s between retries.
llm_retry_decorator = retry(
    retry=retry_if_exception(is_retryable_error),
    wait=wait_exponential(multiplier=1, min=4, max=60), 
    stop=stop_after_attempt(5),
    before_sleep=lambda retry_state: print(f"Quota exhausted. Retrying in {retry_state.next_action.sleep} seconds...")
)

@llm_retry_decorator
def safe_invoke_llm(llm, prompt_list):
    return llm.invoke(prompt_list)

# Define the Routing Schema
class RouteDecision(BaseModel):
    """Call this tool to route the task to the next appropriate team member."""
    reasoning: str = Field(description="Your step-by-step reasoning for why you are choosing this agent.")
    next_agent: str = Field(description="The name of the agent to route to. Must be 'Developer', 'DevOps', or 'FINISH'.")

class ReportStatus(BaseModel):
    """Call this tool to report your progress or completion back to the Manager."""
    thought: str = Field(description="Detailed summary of the work you just completed and what the team should do next, or explanation of exactly what you are blocked on.")
    phase: str = Field(description="The phase you just worked on (e.g., 'Implementation').")
    status: str = Field(description="Must be 'IN_PROGRESS', 'COMPLETE', or 'BLOCKED'")

class DevOpsReport(BaseModel):
    """Call this tool to report your progress or completion back to the Manager."""
    thought: str = Field(description="Detailed summary of the DevOps work completed (e.g., PRs created).")
    status: str = Field(description="Must be 'IN_PROGRESS' or 'COMPLETE'")
    pr_url: str = Field(description="The URL of the created PR, if applicable.", default="")

manager_tools = [RouteDecision, update_linear_ticket]
developer_tools = [ReportStatus, run_terminal_command, read_file, write_file, sync_wip_commit, run_backend_test, run_frontend_test, run_unit_tests, web_search, generate_repo_map]
devops_tools = [DevOpsReport, create_github_pr]

# Initialize the Models via Vertex AI with JSON enforcement
project_id = os.environ.get("PROJECT_ID")

# Configuration common to all agents to ensure structured output
base_config = {
    # "model_kwargs": {"response_mime_type": "application/json"},
    "project": project_id,
    "max_retries": 2,
    "vertexai": True,
    "max_output_tokens": 8192,
}

# Manager keeps the default temperature (1.0) for high-level strategy
manager_config = {**base_config, "temperature": 1.0}

# Workers use 0.1 for deterministic, precise code generation
worker_config = {**base_config, "temperature": 0.1}

manager_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro", # Recommended version for stable JSON
    **manager_config
).bind_tools(manager_tools)

devops_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    **worker_config
).bind_tools(devops_tools)

developer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    **worker_config
).bind_tools(developer_tools)

""" developer_llm = ChatAnthropicVertex(
    project=project_id, 
    location="asia-southeast1", 
    model_name="claude-sonnet-4-6",
    max_retries=2 # Give it more attempts
).bind_tools([execute_sandbox_code])
 """

# Helper function to extract names and descriptions
def format_tool_descriptions(tools_list) -> str:
    descriptions = []
    for t in tools_list:
        # Handle @tool decorators
        if hasattr(t, "name") and hasattr(t, "description"):
            descriptions.append(f"- {t.name}: {t.description}")
        # Handle Pydantic BaseModels (like ReportStatus)
        elif hasattr(t, "__name__") and hasattr(t, "__doc__"):
            descriptions.append(f"- {t.__name__}: {t.__doc__}")
    return "\n".join(descriptions)

def get_tool_names(tools_list) -> list:
    """Extracts just the names of the tools to show the Manager."""
    names = []
    for t in tools_list:
        if hasattr(t, "name"):
            names.append(t.name)         # For @tool functions
        elif hasattr(t, "__name__"):
            names.append(t.__name__)     # For Pydantic schemas
    return names

# Helper function for Filtering
def filter_agent_history(messages: list, agent_role: str) -> list:
    """Filters history so an agent only sees the Human request and its own past actions.
    Includes Progressive Memory Management to prevent token limits from being exceeded."""
    
    dev_tool_names = get_tool_names(developer_tools)
    devops_tool_names = get_tool_names(devops_tools)
    manager_tool_names = get_tool_names(manager_tools)
    
    # Define what the agent IS allowed to see
    allowed_tools = []
    if agent_role == "Developer":
        allowed_tools = dev_tool_names
    elif agent_role == "DevOps":
        allowed_tools = devops_tool_names
    elif agent_role == "Manager":
        allowed_tools = manager_tool_names + ["ReportStatus", "DevOpsReport"]
    
    # ---------------------------------------------------------
    # SESSION BOUNDARY
    # We explicitly look for the "source": "webhook" tag in additional_kwargs.
    # ---------------------------------------------------------
    last_real_human_index = -1
    for i, msg in enumerate(messages):
        # Safely get kwargs whether it's an object or tuple
        kwargs = getattr(msg, "additional_kwargs", {})
        
        # Exact match for our custom webhook tag
        if kwargs.get("source") == "webhook":
            last_real_human_index = i
    
    filtered_messages = []
    
    # ---------------------------------------------------------
    # PHASE 1: STANDARD FILTERING (Role Isolation)
    # ---------------------------------------------------------
    for i, msg in enumerate(messages):
        # Convert tuples to objects immediately so logic is consistent
        if isinstance(msg, tuple):
            m_type, m_content = msg[0], msg[1]
            if m_type in ["user", "human"]:
                msg = HumanMessage(content=m_content)
            elif m_type == "ai":
                msg = AIMessage(content=m_content)
            else:
                continue # Skip unknown tuple types

        msg_type = getattr(msg, "type", "")
        msg_kwargs = getattr(msg, "additional_kwargs", {})
        
        if msg_type == "human":
            msg_name = getattr(msg, "name", "")
            
            # Is it the actual webhook? Let everyone see it.
            if msg_kwargs.get("source") == "webhook":
                if i == last_real_human_index:
                    # It is the CURRENT session's webhook. Let the agent see the active override.
                    filtered_messages.append(msg)
                else:
                    # It is a PAST session's webhook. Strip the old override so it doesn't confuse the agent!
                    old_msg = msg.copy()
                    old_msg.content = old_msg.content.split("SYSTEM OVERRIDE")[0].strip()
                    filtered_messages.append(old_msg)
            
            # ==========================================
            # BROADCAST PROTOCOL
            # Any message tagged as "Broadcast" is globally visible to all agents.
            # ==========================================
            elif msg_name == "Broadcast":
                filtered_messages.append(msg)
            
            # If the message is from the Manager, only show it if it's for THIS agent
            elif msg_name == "Manager":
                if f"Routing to {agent_role}" in msg.content:
                    filtered_messages.append(msg)
            else:
                # Always include original client requests or human updates
                filtered_messages.append(msg)
            
        elif msg_type == "tool":
            # Only show tools that belong to this agent's specific role
            if msg.name in allowed_tools:
                filtered_messages.append(msg)
                
        elif msg_type == "ai":
            # IDENTITY SHIELD: Prevent agents from stealing each other's text thoughts!
            msg_name = getattr(msg, "name", "")
            if msg_name and msg_name.lower() not in [agent_role.lower(), "ai", ""]:
               continue
            
            kwargs_copy = copy.deepcopy(getattr(msg, "additional_kwargs", {}))

            # PRUNING LOGIC:
            # If the AI message happened BEFORE the last real webhook (last_real_human_index), 
            # it's from an old session. Strip the signature to save tokens!
            if last_real_human_index != -1 and i < last_real_human_index:
                kwargs_copy.pop("__gemini_function_call_thought_signatures__", None)

            # Create a BRAND NEW message to bypass Pydantic mutation blocks
            new_msg = AIMessage(
                content=msg.content,
                tool_calls=getattr(msg, "tool_calls", []),
                name=msg_name,
                additional_kwargs=kwargs_copy
            )    

            # Check if the tools are allowed for this specific agent
            if hasattr(new_msg, "tool_calls") and new_msg.tool_calls:
                is_allowed = all(tc["name"] in allowed_tools for tc in new_msg.tool_calls)
                if is_allowed:
                    filtered_messages.append(new_msg)
            else:
                # Always include text-only AI messages from the agent to themselves
                filtered_messages.append(new_msg)
    
    # ---------------------------------------------------------
    # PHASE 2: PROGRESSIVE MEMORY MANAGEMENT (Token Optimization)
    # ---------------------------------------------------------
    MAX_CHARS_THRESHOLD = 60000  # Trigger truncation if prompt exceeds ~15,000 tokens
    PROTECTED_WINDOW = 6         # Never truncate the most recent 6 messages

    def get_total_length(msgs):
        return sum(len(str(getattr(m, "content", ""))) for m in msgs)

    # --- FIND LATEST CURRENT-SESSION REPO MAP ---
    # 1. Find the start index of the current session in the filtered array
    current_session_start_idx = 0
    for i, msg in enumerate(filtered_messages):
        if getattr(msg, "additional_kwargs", {}).get("source") == "webhook":
            current_session_start_idx = i
            
    # 2. Find the index of the latest generate_repo_map inside the current session
    protected_repo_map_idx = -1
    for i in range(len(filtered_messages) - 1, current_session_start_idx - 1, -1):
        msg = filtered_messages[i]
        if getattr(msg, "type", "") == "tool" and getattr(msg, "name", "") == "generate_repo_map":
            protected_repo_map_idx = i
            break # Found the most recent one, stop looking
    # -------------------------------------------------

    if get_total_length(filtered_messages) > MAX_CHARS_THRESHOLD:
        # Loop from the oldest message forward, stopping before the protected window
        for i in range(len(filtered_messages) - PROTECTED_WINDOW):
            msg = filtered_messages[i]
            
            # --- IMMUNITY CHECK ---
            if i == protected_repo_map_idx:
                continue # Skip truncation entirely to preserve the full map
            # ---------------------------

            # We only truncate massive Tool Output messages (skip AI thoughts and Human prompts)
            if getattr(msg, "type", "") == "tool" and len(str(msg.content)) > 2000:
                
                # Copy the message so we don't permanently corrupt the LangGraph state database!
                new_msg = msg.copy()
                content_str = str(new_msg.content)
                tool_name = getattr(new_msg, "name", "")
                
                if tool_name == "read_file":
                    # For huge files, strip entirely. The AI already extracted the logic previously.
                    new_msg.content = (
                        f"<system_memory_manager>\n"
                        f"Large file read omitted to save context space. (Original size: {len(content_str)} chars).\n"
                        f"If you need to reference this file again, use the read_file tool.\n"
                        f"</system_memory_manager>"
                    )
                # --- COMPLETELY REMOVE OLD REPO MAPS ---
                elif tool_name == "generate_repo_map":
                    new_msg.content = (
                        f"<system_memory_manager>\n"
                        f"Legacy repo map completely removed to save context space. (Original size: {len(content_str)} chars).\n"
                        f"</system_memory_manager>"
                    )
                # ----------------------------------------------
                elif tool_name == "run_terminal_command":
                    # For terminal commands, the exit code/error is almost always at the very end.
                    new_msg.content = (
                        f"<system_memory_manager>\n"
                        f"Terminal output truncated to save context space. Showing only the final 1500 characters.\n"
                        f"</system_memory_manager>\n\n"
                        f"... [TRUNCATED] ...\n\n"
                        f"{content_str[-1500:]}"
                    )
                else:
                    # Generic fallback for any other huge tools
                    new_msg.content = (
                        f"<system_memory_manager>\n"
                        f"Tool output truncated to save context space. Showing only the final 1500 characters.\n"
                        f"</system_memory_manager>\n\n"
                        f"... [TRUNCATED] ...\n\n"
                        f"{content_str[-1500:]}"
                    )
                
                # Replace the original massive message with the slimmed-down version
                filtered_messages[i] = new_msg
                
                # Check if we have shed enough weight. If yes, stop truncating!
                if get_total_length(filtered_messages) <= MAX_CHARS_THRESHOLD:
                    break
                
    return filtered_messages

def manager_node(state: AgencyState) -> dict:
    """The Supervisor: Routes tasks and updates Linear tickets based on team status."""

    # Dynamic Registry (Keep your existing available_agents list)
    # This can eventually move to a config file or database
    available_agents = [
        {
            "role": "Developer",
            "skills": get_tool_names(developer_tools),
            "description": "Best for writing code, modifying files, and local testing."
        },
        {
            "role": "DevOps",
            "skills": get_tool_names(devops_tools),
            "description": "Best for creating Pull Requests and pushing code to Git."
        }
    ]

    messages = state.get("messages", [])
    
    # 1. PRE-COMPUTE DEBUG CONTEXT
    dev_iterations = state.get("dev_iteration_count", 0)
    detailed_debug_context = ""

    if dev_iterations >= DEVELOPER_ITERATION_LIMIT:
        ticket_id = state.get("active_ticket_url", "UNKNOWN")
        # Pull the last 20 raw events to give a broad look at the failure loop
        try:
            raw_logs = get_logs_by_ticket(ticket_id)[-20:] 
            log_summary = "\n".join([
                f"[{l['role']}]: {l['tool_name'] if l['tool_name'] else ''} -> {str(l['content'])[:400]}" 
                for l in raw_logs
            ])
            
            detailed_debug_context = (
                f"\n\n[!!! CRITICAL AUDIT - ITERATION LIMIT {DEVELOPER_ITERATION_LIMIT} REACHED !!!]\n"
                f"The Developer has attempted this task {dev_iterations} times without success. "
                f"Below are the RAW terminal and tool logs from their last attempts.\n"
                f"ACTION REQUIRED: Identify if they are repeating a mistake, hitting a sandbox restriction, "
                f"or using an incorrect path. Provide an explicit 'STRATEGY PIVOT' in your reasoning.\n"
                f"--- RAW LOG START ---\n{log_summary}\n--- RAW LOG END ---\n"
            )
        except Exception as e:
            detailed_debug_context = f"\n[SYSTEM ERROR]: Failed to retrieve debug logs: {str(e)}"

    # 2. Safely Extract the Original Client Request (handles both tuples and Message objects)
    original_task_raw = "No task provided."
    if messages:
        first_msg = messages[0]
        if isinstance(first_msg, tuple):
            original_task_raw = first_msg[1] # Extract the string from ("user", "string")
        else:
            original_task_raw = first_msg.content # Extract from an actual AIMessage/HumanMessage

    # Clean the original task to separate the request from the backend override
    original_task = original_task_raw.split("SYSTEM OVERRIDE")[0].strip()

    # 3. Extract the Active System Override (The Branch/PR instruction)
    active_system_override = "No specific override provided."
    for msg in reversed(messages):
        msg_content = msg[1] if isinstance(msg, tuple) else getattr(msg, "content", "")
        if "SYSTEM OVERRIDE" in msg_content:
            # Grab everything after the phrase "SYSTEM OVERRIDE:"
            parts = msg_content.split("SYSTEM OVERRIDE")
            if len(parts) > 1:
                active_system_override = parts[1].strip()
            break
    
    # 4. DETECT HUMAN RESUME (Safe from tool loops & original task)
    is_human_resume = False
    
    if len(messages) > 1:
        for msg in reversed(messages[1:]):
            msg_type = getattr(msg, "type", "") if not isinstance(msg, tuple) else msg[0]
            msg_name = getattr(msg, "name", "") if not isinstance(msg, tuple) else ""
            
            # Skip the Manager's own internal thoughts/tools
            if msg_name == "Manager" or msg_type == "tool" or (msg_type == "ai" and msg_name == "Manager"):
                continue 
                
            # If the first major actor we hit is a human, it's a resume!
            if msg_type in ["human", "user"]:
                is_human_resume = True
                break
                
            # If we hit the Developer or DevOps, the AI is still working
            if msg_type == "ai":
                break

    # 5. TEAM WORK LOG
    # Build a clean, chronological log from the team's reports (SKIPPING the first message)
    work_log_entries = []
    is_blocked = False # Flag to check if we need human help

    for msg in messages[1:]:
        msg_type = getattr(msg, "type", "") if not isinstance(msg, tuple) else msg[0]
        kwargs = getattr(msg, "additional_kwargs", {})
        
        # Safely grab the timestamp if it is implemented
        time_stamp = kwargs.get("timestamp", "")
        time_prefix = f"[{time_stamp}] " if time_stamp else ""

        # Capture Human Interventions
        if msg_type in ["human", "user"]:
            # Ignore our internal routing messages (which use name="Manager")
            if getattr(msg, "name", "") != "Manager":
                content = getattr(msg, "content", "") if not isinstance(msg, tuple) else msg[1]
                
                # Strip the noisy override text from the timeline
                clean_content = content.split("SYSTEM OVERRIDE")[0].strip()
                
                if clean_content:
                    work_log_entries.append(f"{time_prefix} [HUMAN UPDATE]: {clean_content}")

        # Capture Agent Reports chronologically
        elif hasattr(msg, "tool_calls") and msg.tool_calls:
            for tool in msg.tool_calls:
                if tool["name"] in ["ReportStatus", "DevOpsReport"]:
                    status = tool["args"].get("status", "UNKNOWN")
                    thought = tool["args"].get("thought", "")
                    role = "Developer" if tool["name"] == "ReportStatus" else "DevOps"
                    
                    if status == "COMPLETE":
                        work_log_entries.append(f"{time_prefix} [{role} - {status}]: {thought}")
                    elif status == "BLOCKED":
                        is_blocked = True
                        work_log_entries.append(f"{time_prefix} [{role} - BLOCKED]: {thought}")

    numbered_log = [f"Step {i+1}: {entry}" for i, entry in enumerate(work_log_entries)]
    work_log_str = "\n".join(numbered_log) if numbered_log else "No work has been completed yet."
    # print(work_log_str)

    # 6. Add a fallback warning so the Manager knows if the Developer hit a timeout
    current_status = state.get("current_status", "NOT_STARTED")
    
    status_warning = ""
    if current_status == "IN_PROGRESS" and not is_blocked:
        status_warning = "\n[SYSTEM WARNING]: The Developer was working but hit an iteration limit or timeout before calling ReportStatus. They are currently IN_PROGRESS. Route back to them so they can finish the job.\n"

    # 7. The Intelligent Prompt
    system_prompt = SystemMessage(content=(
        f"You are an intelligent Engineering Manager coordinating an AI team.\n\n"
        f"TEAM REGISTRY:\n{json.dumps(available_agents, indent=2)}\n\n"
        
        f"--- ORIGINAL CLIENT REQUEST ---\n"
        f"{original_task}\n\n"
        
        f"--- ACTIVE SYSTEM STATUS ---\n"
        f"SYSTEM OVERRIDE: {active_system_override}\n\n"

        f"--- TEAM WORK LOG ---\n"
        f"{work_log_str}\n"
        f"{status_warning}\n" # Reset dev_iteration_count if this is uncommented to prevent infinite loops
        f"{detailed_debug_context}\n"
        f"--------------------------------------------------\n\n"
        
        "--- ENVIRONMENTAL CONSTRAINTS ---\n"
        "The Developer operates in a headless, non-interactive Linux sandbox. They CANNOT execute commands that require a web browser or terminal user input (e.g., `[Y/n]` prompts, `firebase login`, `aws configure`, `npm login`).\n"
        "Do NOT instruct the Developer to authenticate with cloud providers. All necessary API keys and client secrets are automatically injected into the Developer's OS environment variables via the system vault. If you see CLI warnings about being 'unauthenticated' or 'offline', ignore them unless they are fatal crash errors.\n\n"
        
        "YOUR CORE RESPONSIBILITIES:\n"
        "1. Read the Team Work Log from top to bottom. This is a chronological log and the last step is the current state of the project.\n"
        "2. Read the Original Client Request and compare it against the Team Work Log.\n"
        "3. If the [CRITICAL AUDIT] block is present, perform a deep-dive analysis of the raw logs to unblock the developer.\n"
        "4. Intelligently deduce the current state of the project and what needs to happen next.\n\n"

        "ROUTING RULES (Evaluate from top to bottom):\n"
        "1. SCOPE CREEP VS REVISIONS (CRITICAL): If there is a SYSTEM OVERRIDE showing an open PR, you MUST evaluate the Original Client Request. If the request is a tweak, bug fix, or direct revision to the current feature, route to 'Developer'. If the request is a completely new, unrelated feature, use `update_linear_ticket` to reply: 'I have already submitted a Pull Request for this ticket. To avoid Git conflicts and keep our work organized, please open a new Linear ticket for this new feature.' Then route to 'FINISH'.\n"
        "2. TIMEOUT RECOVERY: If you see a [SYSTEM WARNING] that the Developer hit a timeout, route back to the 'Developer' immediately so they can finish their thought.\n"
        "3. DISCOVERY & MAPPING: If the Developer is still exploring the file structure or reading core configuration files to build a 'System Map', route to 'Developer' to allow them to finish their architectural analysis.\n"
        "4. RESUMING WORK: If the human replied to a BLOCKED ticket, evaluate whether the codebase requires modifications based on their message:\n"
        "   - CODE FIX NEEDED: If new code needs to be written, debugged, or fixed based on the human's feedback and logs, route to the 'Developer' to resume work.\n"
        "   - DEPLOYMENT ONLY: If the code is already fine, no new code is needed, and ONLY a deployment/push is required (e.g., the human manually resolved an external configuration or infrastructure issue), route directly to 'DevOps'.\n"
        "5. EMERGENCY WIP SAVE: If the Developer is 'BLOCKED' and DevOps has not saved the code yet: Route to 'DevOps' for an emergency WIP commit.\n"
        "6. SLEEP (WIP SAVED): If DevOps just reported pushing a 'WIP BLOCKED PR', the code is safely saved.  Use `update_linear_ticket` to relay the exact instructions/blocker to the human, then Route to 'FINISH' to sleep until the human acts.\n"
        "7. FINALIZING CODE: If the Developer's status is 'COMPLETE', route to 'DevOps' to push the final code and create a production Pull Request.\n"
        "8. SLEEP (PROJECT DONE): If DevOps has created the final Pull Request, use `update_linear_ticket` to notify the human of success, then route to 'FINISH'.\n"
        "9. STANDARD BUILD: If none of the above apply, and the core feature still needs to be built or is 'IN_PROGRESS', route to the 'Developer'.\n"
        "10. IMPORTANT: `RouteDecision` is a handoff tool which must be called in a separate, final turn. DO NOT call `RouteDecision` in the same turn as any other tool.\n\n"
    ))

    # ---------------------------------------------------------
    # MINIMAL API-MANDATED HISTORY FOR THE MANAGER
    # The Manager relies entirely on the Work Log in the System Prompt.
    # It only needs to see conversational history if it is mid-tool-execution
    # (e.g., it just updated the ticket and now needs to route).
    # ---------------------------------------------------------
    minimal_history = []
    
    # We scan backwards to grab ONLY the immediate preceding tool block
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", "")
        msg_name = getattr(msg, "name", "")
        
        if msg_type == "tool":
            # Keep tool responses at the end of the chain
            minimal_history.insert(0, msg)
            
        elif msg_type == "ai" and msg_name == "Manager" and hasattr(msg, "tool_calls") and msg.tool_calls:
            # We found the Manager's active tool call that triggered the tools above.
            # Keep it so the API resolves the chain, and STOP looking backwards!
            minimal_history.insert(0, msg)
            break 
            
        else:
            # The second we hit a Human message, a Broadcast, or a Developer message, 
            # we break. The Manager doesn't need to see them; they are in the System Prompt.
            break

    # ---------------------------------------------------------
    # Fetch the Manager's own native history (its past tool calls)
    # isolated_history = filter_agent_history(state["messages"], agent_role="Manager")
    # print(isolated_history)
    # ---------------------------------------------------------
    
    # Add a simple Human message to trigger the Gemini API
    human_trigger = HumanMessage(content="Please review the project state and make your routing decision or update the ticket.")

    # Invoke the Manager with both the system instructions and the human trigger
    # print(f"Invoking Manager with System Prompt:\n{system_prompt}\n\nIsolated History:\n{isolated_history}\n\nHuman Trigger:\n{human_trigger}")
    # response = manager_llm.invoke([system_prompt] + isolated_history + [human_trigger])

    # Construct the full prompt list
    full_prompt = [system_prompt] + minimal_history + [human_trigger]
    response = safe_invoke_llm(manager_llm, full_prompt)

    # Extract the exact token usage from Google's response metadata
    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = response.usage_metadata.get("input_tokens", 0)

    # Log the prompt WITH the accurate token count
    log_activity(
        ticket_id=state.get("active_ticket_url", "UNKNOWN"),
        session_id=state.get("session_id"),
        role="ai_prompt",
        content=full_prompt,
        node_name="Manager",
        input_tokens=tokens_used
    )

    # The Safety Catch
    if not response.content and not getattr(response, "tool_calls", []):
        # The agent glitched and returned absolutely nothing. 
        # Force it to wake up and take an action.
        response.content = "SYSTEM: You returned an empty response. Please explain your thought process and either use a tool to continue, or use ReportStatus to end your turn."

    # Safely merge the LLM's thoughts with a timestamp
    new_kwargs = copy.deepcopy(getattr(response, "additional_kwargs", {}))
    new_kwargs["timestamp"] = get_short_time()

    # Reconstruct the message
    # response = AIMessage(content=response.content, tool_calls=response.tool_calls, name="Manager", additional_kwargs=new_kwargs)
    response = response.copy(update={"name": "Manager", "additional_kwargs": new_kwargs})
    return {"messages": [response]}

def manager_tools_node(state: AgencyState, config: RunnableConfig) -> dict:
    """Executes tools for the Manager and updates the global state."""
    
    # Scan backwards to find the AI message that requested the tools
    messages = state.get("messages", [])
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    
    messages_to_add = []
    updates = {"messages": messages_to_add}
    
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        for tool in last_ai_msg.tool_calls:
            
            if tool["name"] == "update_linear_ticket":
                result = update_linear_ticket.invoke(tool["args"], config=config)
                messages_to_add.append(ToolMessage(
                    content=str(result), 
                    name=tool["name"], 
                    tool_call_id=tool["id"]
                ))
                    
            elif tool["name"] == "RouteDecision":
                next_agent = tool["args"].get("next_agent", "FINISH")
                reasoning = tool["args"].get("reasoning", "No reasoning provided.")
                # print(f"\n[Intelligent Manager Routing]: {next_agent}\nReasoning: {reasoning}\n")
                
                # Add the ToolMessage to satisfy the LLM API
                messages_to_add.append(ToolMessage(
                    content=f"Successfully routed to {next_agent}", 
                    name=tool["name"], 
                    tool_call_id=tool["id"]
                ))

                # Add the HumanMessage so downstream agents can see the direction
                routing_msg = HumanMessage(
                    content=f"MANAGER DIRECTION: Routing to {next_agent}. Reason: {reasoning}",
                    name="Manager",
                    additional_kwargs={"timestamp": get_short_time()}
                )
                messages_to_add.append(routing_msg)
                
                # We update the state dictionary here!
                updates["next_agent"] = next_agent
                
                # If routing to a worker, ensure they wake up in the correct status
                if next_agent in ["Developer", "DevOps"]:
                    updates["current_status"] = "IN_PROGRESS"
                    updates["dev_iteration_count"] = 0
                    
    return updates

def developer_node(state: AgencyState) -> dict:
    """The Worker's Brain: Analyzes state and decides the next tool to call, or finishes."""
    import time
    time.sleep(3) # Keeping the rate limit safety net
    
    # Fetch the existing state so we don't accidentally erase it!
    current_phase = state.get("current_phase", "Implementation & Testing")
    current_status = state.get("current_status", "IN_PROGRESS")
    last_report = state.get("last_report", "")

    # Dynamically generate the skills list
    dynamic_skills = format_tool_descriptions(developer_tools)

    # --- NEW: Fetch the prompt from our prompts module ---
    # You can easily toggle this by changing the string to "original" or "optimized"
    # Or, grab it from an environment variable: os.environ.get("PROMPT_VERSION", "optimized")
    prompt_version = os.environ.get("DEVELOPER_PROMPT_VERSION", "original")
    prompt_content = get_developer_prompt(dynamic_skills=dynamic_skills, version=prompt_version)
    system_prompt = SystemMessage(content=prompt_content)
    
    # Strip out Manager and DevOps messages so the Developer only sees its own history
    isolated_history = filter_agent_history(state["messages"], agent_role="Developer")

    # Gemini Crash Failsafe: Ensure there is at least one User message
    has_user_msg = any(getattr(m, "type", "") == "human" or isinstance(m, HumanMessage) for m in isolated_history)
    if not has_user_msg:
        isolated_history.insert(0, HumanMessage(content="Please review the task and begin development."))

    # Construct the full prompt list
    full_prompt = [system_prompt] + isolated_history
    response = safe_invoke_llm(developer_llm, full_prompt)

    # DEBUGGING: Print the exact finish reason and raw metadata to your terminal
    # print("--- DEBUG INFO ---")
    # print("FINISH REASON:", getattr(response, "response_metadata", {}).get("finish_reason", "UNKNOWN"))
    # print("RAW METADATA:", getattr(response, "response_metadata", {}))
    # print("------------------")

    # Extract the exact token usage from Google's response metadata
    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = response.usage_metadata.get("input_tokens", 0)

    # Log the prompt WITH the accurate token count
    log_activity(
        ticket_id=state.get("active_ticket_url", "UNKNOWN"),
        session_id=state.get("session_id"),
        role="ai_prompt",
        content=full_prompt,
        node_name="Developer",
        input_tokens=tokens_used
    )

    # Safely merge the LLM's thoughts with a timestamp
    new_kwargs = copy.deepcopy(getattr(response, "additional_kwargs", {}))
    new_kwargs["timestamp"] = get_short_time()

    # Reconstruct as a clean AIMessage to ensure serializability
    # response = AIMessage(content=response.content, tool_calls=response.tool_calls, name="Developer", additional_kwargs=new_kwargs)
    response = response.copy(update={"name": "Developer", "additional_kwargs": new_kwargs})
    messages_to_add = [response]

    # The Safety Catch
    if not response.content and not getattr(response, "tool_calls", []):
        # The agent glitched and returned absolutely nothing. 
        # Force it to wake up and take an action.
        nudge = HumanMessage(content="SYSTEM: Your previous response was empty. Please state your thoughts, invoke a tool to proceed, or use the ReportStatus tool if you are completely finished.")
        messages_to_add.append(nudge)
    
    return {"messages": messages_to_add}

def developer_tools_node(state: AgencyState, config: RunnableConfig) -> dict:
    """The Worker's Hands: Executes the tools requested by the Developer Brain."""
    
    last_ai_msg = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
    messages_to_add = []
    
    # The Registry
    tool_map = {
        "run_terminal_command": run_terminal_command,
        "read_file": read_file,
        "write_file": write_file,
        "sync_wip_commit": sync_wip_commit,
        "run_backend_test": run_backend_test,
        "run_frontend_test": run_frontend_test,
        "run_unit_tests": run_unit_tests,
        "web_search": web_search,
        "generate_repo_map": generate_repo_map
    }

    # Fetch existing status
    has_verified = state.get("has_verified_code", False)
    current_phase = state.get("current_phase", "Implementation & Testing")
    current_status = state.get("current_status", "IN_PROGRESS")
    last_report = state.get("last_report", "")

    updates = {}

    if hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        for tool in last_ai_msg.tool_calls:
            tool_name = tool["name"]
            
            # --- 1. HANDLE CONTROLLER TOOL (ReportStatus) ---
            if tool_name == "ReportStatus":
                attempted_status = tool["args"].get("status", current_status)
                
                # The Verification Gate
                if attempted_status == "COMPLETE" and not has_verified:
                    print("\n[SYSTEM]: Blocked AI from completing task without verification.")
                    messages_to_add.append(ToolMessage(
                        content="SYSTEM ERROR: Verification Gate Failed! You cannot report COMPLETE because you have not observed the results of your code changes. You MUST start the server and use `inspect_frontend` or check `manage_dev_server` logs to prove the code works before calling ReportStatus.",
                        name=tool_name,
                        tool_call_id=tool["id"]
                    ))
                else:
                    # Gate passed, extract the new variables
                    new_phase = tool["args"].get("phase", current_phase)
                    new_report = tool["args"].get("thought", last_report)
                    
                    # Update the state variables
                    updates["current_phase"] = new_phase
                    updates["current_status"] = attempted_status
                    updates["last_report"] = new_report
                    print(f"[Developer Status]: {attempted_status} - {new_report}")

                    messages_to_add.append(ToolMessage(
                        content="Status successfully logged in system state.",
                        name=tool_name,
                        tool_call_id=tool["id"]
                    ))
                continue # Skip the dynamic execution block below

            # --- VERIFICATION TRACKING LOGIC ---
            if tool_name == "write_file":
                # Check if the AI is ONLY updating the technical plan
                files_written = tool["args"].get("files", [])
                
                # If every file being written is just the plan, do NOT reset the verification flag
                is_only_docs = all("TECHNICAL_PLAN.md" in f.get("file_path", "") for f in files_written)
                
                if not is_only_docs:
                    has_verified = False # Reset verification when actual code changes
            elif tool_name in ["run_frontend_test", "run_backend_test", "run_unit_tests"]:
                has_verified = True  # Successfully ran a pipeline test
            elif tool_name == "run_terminal_command":
                cmd = tool["args"].get("command", "").lower()
                # If the agent runs a test file, allow it to satisfy the Verification Gate
                if "test.js" in cmd or "npm test" in cmd or "pytest" in cmd or "jest" in cmd:
                    has_verified = True  
            # -----------------------------------

            # Dynamically execute actual tools
            if tool_name in tool_map:
                args = tool["args"].copy()
                
                try:
                    result = tool_map[tool_name].invoke(args, config=config)
                    messages_to_add.append(ToolMessage(
                        content=str(result), 
                        name=tool_name, 
                        tool_call_id=tool["id"]
                    ))
                except Exception as e:
                    messages_to_add.append(ToolMessage(
                        content=f"Error executing {tool_name}: {str(e)}", 
                        name=tool_name, 
                        tool_call_id=tool["id"]
                    ))
            else:
                messages_to_add.append(ToolMessage(
                    content=f"Error: Tool '{tool_name}' not found.", 
                    name=tool_name, 
                    tool_call_id=tool["id"]
                ))
    
    # Increment execution count to prevent infinite loops
    new_count = state.get("dev_iteration_count", 0) + 1
    updates["messages"] = messages_to_add
    updates["dev_iteration_count"] = new_count
    updates["has_verified_code"] = has_verified
    return updates

def devops_node(state: AgencyState) -> dict:
    """The DevOps Brain: Decides whether to call tools or report completion to the Manager."""
    import time
    time.sleep(3) 
    
    ticket_id = state.get("active_ticket_url", "UNKNOWN")
    current_phase = state.get("current_phase", "Delivery & Integration")
    current_status = state.get("current_status", "IN_PROGRESS")
    last_report = state.get("last_report", "")

    # Dynamically generate the skills list!
    dynamic_skills = format_tool_descriptions(devops_tools)

    # Dynamic Skill-Based Prompt
    system_prompt = SystemMessage(content=(
        "You are a DevOps Engineer.\n\n"
        
        f"AVAILABLE SKILLS:\n{dynamic_skills}\n\n"
        
        f"The Developer has modified code. The active Linear Ticket ID is: {ticket_id}.\n\n"
        
        "YOUR INSTRUCTIONS:\n"
        "1. Read the Team Work Log to intelligently understand the current state.\n"
        "2. IF DEVELOPER IS 'COMPLETE': Use the `create_github_pr` tool to commit the finished code, push it to origin, and open a standard Pull Request ready for production review.\n"
        "3. IF DEVELOPER IS 'BLOCKED': Use the SAME `create_github_pr` tool to rescue the incomplete code from the ephemeral sandbox. The tool will automatically name the branch. Title the Pull Request 'WIP: Blocked awaiting human' so the human can easily review and fix the broken code.\n"
        "4. Completion: When finished, call `DevOpsReport(status='COMPLETE', thought='[State whether you created a final PR or a WIP Blocked PR, include the GitHub URL, and specify the EXACT branch name used]', pr_url='<INSERT URL HERE>')` to hand control back to the Manager.\n"
        "5. DO NOT use terminal commands or write code. Do NOT update the Linear ticket.\n"
        "6. IMPORTANT: `DevOpsReport` is a handoff tool which must be called in a separate, final turn. DO NOT call `DevOpsReport` in the same turn as any other tool.\n\n"
    ))

    isolated_history = filter_agent_history(state["messages"], agent_role="DevOps")
    # Construct the full prompt list
    full_prompt = [system_prompt] + isolated_history
    response = safe_invoke_llm(devops_llm, full_prompt)

    # Extract the exact token usage from Google's response metadata
    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = response.usage_metadata.get("input_tokens", 0)

    # Log the prompt WITH the accurate token count
    log_activity(
        ticket_id=state.get("active_ticket_url", "UNKNOWN"),
        session_id=state.get("session_id"),
        role="ai_prompt",
        content=full_prompt,
        node_name="DevOps",
        input_tokens=tokens_used
    )

    # Safely merge the LLM's thoughts with a timestamp
    new_kwargs = copy.deepcopy(getattr(response, "additional_kwargs", {}))
    new_kwargs["timestamp"] = get_short_time()

    # response = AIMessage(content=response.content, tool_calls=response.tool_calls, name="DevOps", additional_kwargs=new_kwargs)
    response = response.copy(update={"name": "DevOps", "additional_kwargs": new_kwargs})
    messages_to_add = [response]
    
    # The Safety Catch
    if not response.content and not getattr(response, "tool_calls", []):
        # The agent glitched and returned absolutely nothing. 
        # Force it to wake up and take an action.
        nudge = HumanMessage(content="SYSTEM: Your previous response was empty. Please state your thoughts, invoke a tool to proceed, or use the ReportStatus tool if you are completely finished.")
        messages_to_add.append(nudge)
    
    return {"messages": messages_to_add}

def devops_tools_node(state: AgencyState, config: RunnableConfig) -> dict:
    """The DevOps Hands: Executes the actual PR tool and status reports."""
    
    last_ai_msg = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
    messages_to_add = []
    
    # The Registry: Add any future DevOps tools here
    tool_map = {
        "create_github_pr": create_github_pr,
    }

    # Fetch existing state
    current_status = state.get("current_status", "IN_PROGRESS")
    last_report = state.get("last_report", "")
    
    # Prepare state updates
    updates = {}
    
    if hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        for tool in last_ai_msg.tool_calls:
            tool_name = tool["name"]
            
            # Handle Controller Tool (DevOpsReport)
            if tool_name == "DevOpsReport":
                attempted_status = tool["args"].get("status", current_status)
                new_report = tool["args"].get("thought", last_report)
                pr_url = tool["args"].get("pr_url", "")
                
                print(f"[DevOps Status]: {attempted_status} | PR: {pr_url}")
                
                # Execute it inline
                messages_to_add.append(ToolMessage(
                    content="Status successfully logged in system state.",
                    name=tool_name,
                    tool_call_id=tool["id"]
                ))

                # ==========================================
                # BROADCAST PROTOCOL (P2P COMMUNICATION)
                # Agents emit a HumanMessage with name="Broadcast" to
                # share important information globally across the team.
                # ==========================================
                messages_to_add.append(HumanMessage(
                    content=f"DEVOPS BROADCAST: Code pushed. PR URL: {pr_url} | Notes: {new_report}",
                    name="Broadcast",
                    additional_kwargs={"timestamp": get_short_time()}
                ))
                
                # Update the state variables
                updates["current_status"] = attempted_status
                updates["last_report"] = new_report
                continue # Skip the dynamic execution block below

            # Execute Standard Tools
            if tool_name in tool_map:
                args = tool["args"].copy()
                
                try:
                    result = tool_map[tool_name].invoke(args, config=config)
                    messages_to_add.append(ToolMessage(
                        content=str(result), 
                        name=tool_name, 
                        tool_call_id=tool["id"]
                    ))
                except Exception as e:
                    messages_to_add.append(ToolMessage(
                        content=f"Error executing {tool_name}: {str(e)}", 
                        name=tool_name, 
                        tool_call_id=tool["id"]
                    ))
            else:
                # Fallback if the model hallucinates a tool
                messages_to_add.append(ToolMessage(
                    content=f"Error: Tool '{tool_name}' not found in registry.", 
                    name=tool_name, 
                    tool_call_id=tool["id"]
                ))
                
    updates["messages"] = messages_to_add
    return updates