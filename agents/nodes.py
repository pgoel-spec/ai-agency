import os
import json
import copy
from pydantic import BaseModel, Field
from langchain_google_vertexai import ChatVertexAI
from langchain_google_vertexai.model_garden import ChatAnthropicVertex
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from .state import AgencyState, DEVELOPER_ITERATION_LIMIT
from .tools import (
    update_linear_ticket, create_github_pr, run_terminal_command, 
    read_file, write_file, sync_wip_commit, manage_dev_server, inspect_frontend
)
from database import get_logs_by_ticket, log_activity
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    retry_if_result
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
    retry=retry_if_exception_type((ChatGoogleGenerativeAIError, google_exceptions.ResourceExhausted)),
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
developer_tools = [ReportStatus, run_terminal_command, read_file, write_file, sync_wip_commit, manage_dev_server, inspect_frontend]
devops_tools = [DevOpsReport, create_github_pr]

# Initialize the Models via Vertex AI with JSON enforcement
project_id = os.environ.get("PROJECT_ID")

# Configuration common to all agents to ensure structured output
json_config = {
    # "model_kwargs": {"response_mime_type": "application/json"},
    "project": project_id,
    "max_retries": 2,
    "vertexai": True,
    "max_output_tokens": 8192
}

manager_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro", # Recommended version for stable JSON
    **json_config
).bind_tools([RouteDecision, update_linear_ticket])

devops_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    **json_config
).bind_tools(devops_tools)

developer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    **json_config
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

    if get_total_length(filtered_messages) > MAX_CHARS_THRESHOLD:
        # Loop from the oldest message forward, stopping before the protected window
        for i in range(len(filtered_messages) - PROTECTED_WINDOW):
            msg = filtered_messages[i]
            
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
        
        "YOUR CORE RESPONSIBILITIES:\n"
        "1. Read the Team Work Log from top to bottom. This is a chronological log and the last step is the current state of the project.\n"
        "2. Read the Original Client Request and compare it against the Team Work Log.\n"
        "3. If the [CRITICAL AUDIT] block is present, perform a deep-dive analysis of the raw logs to unblock the developer.\n"
        "4. Intelligently deduce the current state of the project and what needs to happen next.\n\n"

        "ROUTING RULES (Evaluate from top to bottom):\n"
        "1. TIMEOUT RECOVERY: If you see a [SYSTEM WARNING] that the Developer hit a timeout, route back to the 'Developer' immediately so they can finish their thought.\n"
        "2. DISCOVERY & MAPPING: If the Developer is still exploring the file structure or reading core configuration files to build a 'System Map', route to 'Developer' to allow them to finish their architectural analysis.\n"
        "3. RESUMING WORK: If the human just replied to a blocked ticket (meaning the blocker is resolved), route to the 'Developer' so they can resume work on the WIP branch even if the previous status was 'BLOCKED'.\n"
        "4. EMERGENCY WIP SAVE: If the Developer is 'BLOCKED' and DevOps has not saved the code yet: Route to 'DevOps' for an emergency WIP commit.\n"
        "5. SLEEP (WIP SAVED): If DevOps just reported pushing a 'WIP BLOCKED PR', the code is safely saved.  Use `update_linear_ticket` to relay the exact instructions/blocker to the human, then Route to 'FINISH' to sleep until the human acts.\n"
        "6. FINALIZING CODE: If the Developer's status is 'COMPLETE', route to 'DevOps' to push the final code and create a production Pull Request.\n"
        "7. SLEEP (PROJECT DONE): If DevOps has created the final Pull Request, use `update_linear_ticket` to notify the human of success, then route to 'FINISH'.\n"
        "8. STANDARD BUILD: If none of the above apply, and the core feature still needs to be built or is 'IN_PROGRESS', route to the 'Developer'.\n"
        "9. IMPORTANT: `RouteDecision` is a handoff tool which must be called in a separate, final turn. DO NOT call `RouteDecision` in the same turn as any other tool.\n\n"
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

    # Dynamic Skill-Based Prompt
    system_prompt = SystemMessage(content=(
        "You are a Senior Full-Stack Developer.\n\n"
        
        f"AVAILABLE SKILLS:\n{dynamic_skills}\n\n"

        "PROCESS PHASE: Implementation & Testing. In this phase, you follow this lifecycle:\n"
        "1. Environment Initialization: Preparing the workspace and cloning the repo.\n"
        "2. Codebase Exploration: Understanding existing patterns and architecture.\n"
        "3. Implementation: Writing clean, modular, and functional code.\n"
        "4. Verification: Running tests and executing code to ensure zero regressions.\n\n"

        "PROCESS-TO-SKILL MAPPING:\n"
        "- Initialization/Verification: Use `run_terminal_command` for git, pytest, and execution.\n"
        "- Exploration: Use `read_file` to examine code logic.\n"
        "- Implementation: Use `write_file` to create or modify code.\n"
        "- Backup/Saving: Use `sync_wip_commit` to save your progress remotely.\n\n"

        "YOUR INSTRUCTIONS:\n"
        "1. Work through the process step-by-step. After every tool call, analyze the output before deciding your next move. You can call `write_file` and immediately call `sync_wip_commit` in the exact same response to save time.\n"
        #"1. Work through the process intelligently. You are encouraged to BATCH multiple tool calls in a single turn whenever possible to save time. For example, you can call `write_file` and immediately call `sync_wip_commit` in the exact same response.\n"
        "2. Initialization & Dependencies: If the repository isn't cloned yet, clone it using: `git clone https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git`. Immediately run `ls` to observe the actual folder name created by git. Use `cd <folder_name>` to enter the repository before running git commands or modifying files. CRITICAL: Whenever you clone a fresh repo or check out a branch, you MUST actively check for dependency files (e.g., `package.json`, `requirements.txt`, `pom.xml`). If they exist, you must run the appropriate install command (e.g., `npm install`, `pip install -r requirements.txt`) in that specific directory before attempting to run any tests or start servers.\n"
        "3. Codebase Exploration: Do NOT guess file paths. If you just cloned the code, all files are located inside the folder you observed in the previous step. When using `read_file` or `write_file`, you must include that folder name at the start of your path. Your first priority is to build a 'System Map':\n"
        "   - Run `ls -R` to understand the full directory hierarchy.\n"
        "   - Identify and read the 'Source of Truth' files: Look for dependency manifests (e.g., package.json, requirements.txt, pyproject.toml) and core schemas or state definitions.\n"
        "   - Locate the main Entry Points (e.g., main.py, index.js, app.py, or Dockerfiles) to understand the execution flow.\n"
        "   - Use this information to create a mental map of the architecture before modifying any code. This ensures you respect existing design patterns and folder structures.\n"
        
        "4. Progress Evaluation & Completion: You must intelligently evaluate your progress against the original request. You MUST call the `ReportStatus` tool to yield control back to the Manager, selecting ONE of the following three states:\n"
        "4.1. STATE: 'COMPLETE'\n"
        "   - Use this ONLY when the core feature is 100 percent built, fully tested, and utilizes real data.\n"
        "   - If you left ANY placeholders, used mock data, or require the human to do anything, do not use the 'COMPLETE' status.\n\n"
        
        "4.2. STATE: 'IN_PROGRESS'\n"
        "   - Use this if you have successfully completed a chunk of the task, but the overall feature is not finished yet, and you are NOT blocked.\n"
        "   - Example: You built the HTML/CSS successfully and want to log your progress before starting the complex API logic.\n\n"
    
        "4.3. STATE: 'BLOCKED' (Human-in-the-Loop & Secrets Protocol)\n"
        "   - You are operating in a restricted sandbox to build a Production App. DO NOT fake the implementation with mock data.\n"
        "   - Step A (Coding): NEVER hardcode API keys or secrets into the codebase. Write your code to read secrets securely from environment variables (e.g., `process.env.NEWS_API_KEY`). YOU MUST also add the variable name (without the real value) to a `.env.example` file in the root of the project.\n"
        "   - Step B (Frontend Security): If the frontend requires a secret API key, you MUST NOT put the key in the frontend JS. You must proxy the API call through your backend server/functions to protect the secret.\n"
        "   - Step C (Verification Before Blocking): Client secrets from the vault are ALWAYS automatically injected into your `run_terminal_command` and `manage_dev_server` environments. Before assuming you are blocked, you MUST run a test (e.g., run your unit tests or start the server) to check if the environment variable is already present. If your code successfully reads the secret, DO NOT block; continue working.\n"
        "   - Step D (Blocking): ONLY if your test confirms the secret is truly missing (e.g., throws an undefined error), call `ReportStatus(status='BLOCKED')`. In the `thought` parameter, instruct the client exactly like this: 'I am blocked. Please complete these two steps: 1) Securely add the [NAME_OF_VARIABLE] API key to your Project Settings Vault in the AI Agency dashboard so I can test it. 2) Add the exact same key to your GitHub Repository Secrets so the Firebase auto-deploy will work. Reply \"Done\" to this ticket when finished.'\n\n"
        
        "5. DO NOT attempt to use `git commit` or `git push` yourself via the terminal under any circumstances. If you need to save your progress, you MUST use the dedicated `sync_wip_commit` tool. You must call `sync_wip_commit` regularly after every major `write_file` operation or significant codebase change to ensure your progress is continuously backed up.\n"
        "6. IMPORTANT: `ReportStatus` is a handoff tool which must be called in a separate, final turn. DO NOT call `ReportStatus` in the same turn as any other tool.\n\n"
        
        "DESIGN STANDARDS:\n"
        "- When writing frontend code, you MUST create highly polished, modern, and 'slick' user interfaces.\n"
        "- Do not use basic vanilla CSS unless absolutely necessary. By default, import Tailwind CSS via CDN (`<script src=\"https://cdn.tailwindcss.com\"></script>`) for rapid, modern styling.\n"
        "- Ensure all interfaces are fully responsive (mobile-first), use modern typography (e.g., Inter or Roboto via Google Fonts), incorporate soft shadows, rounded corners (`rounded-lg`), and proper whitespace (`gap`, `p-`, `m-`).\n"
        "- Add subtle hover states and CSS transitions to make the UI feel interactive.\n\n"

        "CI/CD & SECRETS MANAGEMENT:\n"
        "1. `.env.example`: Whenever you introduce a new environment variable (e.g., `process.env.STRIPE_KEY`), you MUST document it by adding the variable name to a `.env.example` file at the root of the project.\n"
        "2. GitHub Actions / Firebase Auto-Deploy: If the project uses GitHub Actions (check `.github/workflows/` for `.yml` files), the CI/CD pipeline will fail if it cannot access the new secret. You MUST update the workflow `.yml` file to inject the secret into the build or deploy step. Map the environment variable to the GitHub Secret exactly like this under the `env:` block of the relevant step:\n"
        "   env:\n"
        "     [VARIABLE_NAME]: ${{ secrets.[VARIABLE_NAME] }}\n\n"

        "DIRECTORY & FIREBASE RULES:\n"
        "1. NEVER move, create, or modify `firebase.json` or `.firebaserc` inside a subfolder (like `public`, `src`, or `dist`).\n"
        "2. Firebase configuration files MUST always remain at the absolute root of the repository.\n"
        "3. Only place your actual frontend code (HTML, CSS, JS) inside the `public` directory. Make sure `firebase.json` points its hosting 'public' key to this folder.\n"
        "4. When configuring `firebase.json` rewrites, you MUST place specific function routes (e.g., `/api/**`) BEFORE any catch-all frontend routes (e.g., `**` for Single Page Apps).\n\n"
        
        "RESUMING WORK (AFTER Human-in-the-Loop or Crash):\n"
        "If the Manager routes a task back to you or overrides your system state with an active branch:\n"
        "1. DO NOT start from scratch on the `main` branch. Always run `git checkout <branch-name>` so you pull down the latest code.\n"
        "2. SECRETS INJECTION: If the human just replied 'Done' regarding a secret API key, that secret is now securely injected into your terminal environment by the SaaS backend. Do not try to create a local `.env` file yourself. Just run your tests (e.g., `node test.js`) and the code will successfully read `process.env...`.\n"
        "3. Intelligently assess your progress and continue working on the original ticket. If you hit a new roadblock, fall back to the Human-in-the-Loop protocol and ask the human for help. If the feature is ready for a Pull Request, report COMPLETE.\n"

       "RULES FOR TESTING & DEBUGGING (ENVIRONMENT CRITICAL):\n"
        "1. RESTRICTED ENVIRONMENT: You do not have root/sudo access. NEVER use 'sudo' or the '-g' (global) flag with npm or pip. To execute CLI tools, you MUST use 'npx' (e.g., 'npx firebase emulators:start') to ensure the local version is used.\n"
        "2. THE HAO LOOP: When fixing a bug, you MUST follow the Hypothesis -> Action -> Observation loop.\n"
        "   - Hypothesis: State what you think is wrong.\n"
        "   - Action: Use `write_file` to fix it.\n"
        "   - Observation: You MUST test the code to observe if it worked before declaring the task COMPLETE.\n"
        "3. SERVER MANAGEMENT & WAITING: Never use `run_terminal_command` to start a web server (it will cause a timeout). You MUST use the `manage_dev_server` tool to start/stop servers in the background (e.g., `manage_dev_server(action='start', command='npx firebase emulators:start')`). IMPORTANT: The tool will automatically pause and wait for the server to boot before returning the initial logs to you. You do not need to wait multiple turns. If the boot was successful, you can immediately test the port or call `inspect_frontend` in your very next turn.\n"
        "4. OBSERVATION TOOLS: To complete your HAO loop, you must observe results via `manage_dev_server(action='logs')` or `inspect_frontend(url='http://localhost:5000')`. Always attempt to verify your code on the live server first.\n"
        "5. UNIT TESTING PIVOT (FALLBACK): If local ports fail to bind, `manage_dev_server` continues to return empty logs or errors, OR `curl` results in 'connection refused':\n"
        "   - ABANDON live server testing immediately. Do not attempt `inspect_frontend` again.\n"
        "   - Use `write_file` to create isolated unit test files (e.g., `jest`, `pytest`, or a simple `test_logic.js` script that directly imports your backend functions/logic).\n"
        "   - Run these unit tests directly via `run_terminal_command(command='node test_logic.js')` to verify your logic without booting a network server.\n"
        "   - Prove your code works via this unit test output to satisfy your Observation and bypass the Verification Gate.\n"
        
        # "RULES FOR TESTING:\n"
        # "1. RESTRICTED ENVIRONMENT: You do not have root/sudo access. NEVER use 'sudo' or the '-g' (global) flag with npm or pip or try to modify system directories like `/usr/lib/`. This will cause errors.\n"
        # "2. To execute CLI tools (like firebase, tsc, or next), you MUST use 'npx' (e.g., 'npx firebase deploy') to ensure the local version is used.\n"
        # "3. If a terminal command returns 'context deadline exceeded' or 'timeout', it means you ran a persistent/blocking process (like a web server) in the background without proper detachment. You must kill the process and use short-lived test scripts instead.\n"
        # "4. EMULATOR WORKAROUND: If the Firebase Emulator fails to bind to ports, do not retry. Instead, create a standalone Node.js test script that imports your Express app/functions. Use a library like 'supertest' (if available) or mock the Request/Response objects manually to verify logic without a live network server.\n"
        # "5. To test a web server, use the 'Start, Curl, Kill' pattern in a SINGLE terminal command. Use `$!` to securely kill ONLY the background server you just started.\n"
        # "6. Always ensure ports are freed immediately after testing so you don't get 'Address already in use' errors.\n"
        # "7. To test logic, write a quick Python/Node test script, run it, and read the output.\n"

    ))
    
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
        "manage_dev_server": manage_dev_server,
        "inspect_frontend": inspect_frontend
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
                has_verified = False # Reset verification when code changes
            elif tool_name == "inspect_frontend":
                has_verified = True  # Successfully tested frontend
            elif tool_name == "manage_dev_server" and tool["args"].get("action") == "logs":
                has_verified = True  # Successfully checked backend logs
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