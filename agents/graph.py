import sqlite3
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from .state import AgencyState, DEVELOPER_ITERATION_LIMIT
from .nodes import (
    manager_node, 
    manager_tools_node,
    developer_node, 
    developer_tools_node, 
    devops_node, 
    devops_tools_node
)

# ==========================================
# ROUTING LOGIC (The "Decision Making")
# ==========================================

def manager_brain_router(state: AgencyState):
    """Routes out of the Manager's Brain."""

    messages = state.get("messages", [])
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    
    # If the Manager called ANY tool, go to ManagerTools
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        return "ManagerTools"

    # Failsafe: If the LLM just returned text without calling any tools, 
    # force it back to itself to try again, or default to finish to prevent a crash.
    return "FINISH"

def manager_tools_router(state: AgencyState):
    """
    Routes out of the Manager's Hands. 
    """
    messages = state.get("messages", [])
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls"):
        for tc in last_ai_msg.tool_calls:
            # If the Manager just made a routing decision, exit the loop immediately!
            if tc["name"] == "RouteDecision":
                target = state.get("next_agent", "FINISH")
                return target if target in ["Developer", "DevOps"] else "FINISH"
                
    # If RouteDecision was NOT called (e.g. they only updated the Linear ticket),
    # loop back to the Manager brain so it can evaluate what to do next.
    return "Manager"

def developer_brain_router(state: AgencyState):
    """
    Routes out of the Developer's Brain.
    """
    # Failsafe to break a stubborn developer out of an infinite coding loop
    if state.get("dev_iteration_count", 0) >= DEVELOPER_ITERATION_LIMIT:
        print("DEBUG: Developer hit iteration limit. Forcing report to Manager.")
        return "Manager"

    # Find the most recent AI Message to check for tool calls
    messages = state["messages"]
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)    
    
    # Route to DeveloperTools if ANY tool was called.
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        return "DeveloperTools"

    # If no functional tools are pending, respect the status update
    current_status = state.get("current_status")
    if current_status in ["COMPLETE", "BLOCKED"]:
        return "Manager"

    # Set the routing when there is a text response and no tools are called
    return "Developer"

def developer_tools_router(state: AgencyState):
    """
    Routes out of the Developer's Hands.
    If the status successfully changed to COMPLETE or BLOCKED (passing the Verification Gate),
    go to Manager. Otherwise, loop back to Developer.
    """
    current_status = state.get("current_status")
    if current_status in ["COMPLETE", "BLOCKED"]:
        return "Manager"
    return "Developer"

def devops_brain_router(state: AgencyState):
    """
    Routes out of the DevOps Brain.
    Since DevOpsReport is a tool, the brain ALWAYS routes to DevOpsTools to finish a task
    or execute GitHub commands.
    """
    messages = state["messages"]
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)

    # Did the AI call ANY tools? Go to the Hands!
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        return "DevOpsTools"
    
    # If the AI just output text (no tools), loop back to the DevOps node
    return "DevOps"


def devops_tools_router(state: AgencyState):
    """
    Routes out of the DevOps Hands.
    This is where the actual "Task Finished" routing happens.
    """
    # If the DevOpsReport tool successfully updated the state to COMPLETE,
    # we leave the DevOps loop and return control to the Manager.
    if state.get("current_status") == "COMPLETE":
        return "Manager"
        
    # Otherwise, the agent was just running normal tools (like create_github_pr). 
    # Loop back to the Brain to let it read the output.
    return "DevOps"

# ==========================================
# THE ORCHESTRATOR (The "Org Chart")
# ==========================================

workflow = StateGraph(AgencyState)

# Add the Nodes (The "Employees")
workflow.add_node("Manager", manager_node)
workflow.add_node("ManagerTools", manager_tools_node)
workflow.add_node("Developer", developer_node)
workflow.add_node("DeveloperTools", developer_tools_node)
workflow.add_node("DevOps", devops_node)
workflow.add_node("DevOpsTools", devops_tools_node)

# The process always starts with the Manager
workflow.add_edge(START, "Manager")

# The Manager uses the manager_router_logic to delegate tasks conditionally
workflow.add_conditional_edges(
    "Manager", 
    manager_brain_router,
    {"ManagerTools": "ManagerTools", "FINISH": END}
)
workflow.add_conditional_edges(
    "ManagerTools",
    manager_tools_router,
    {"Manager": "Manager", "Developer": "Developer", "DevOps": "DevOps", "FINISH": END}
)

# Developer Loop: The Developer decides whether to use a tool or report back to the Manager
workflow.add_conditional_edges(
    "Developer", 
    developer_brain_router,
    {"DeveloperTools": "DeveloperTools", "Manager": "Manager", "Developer": "Developer"}
)
workflow.add_conditional_edges(
    "DeveloperTools", 
    developer_tools_router,
    {"Manager": "Manager", "Developer": "Developer"}
)

# DevOps Loop
workflow.add_conditional_edges(
    "DevOps", 
    devops_brain_router,
    {"DevOpsTools": "DevOpsTools", "Manager": "Manager", "DevOps": "DevOps"}
)
workflow.add_conditional_edges(
    "DevOpsTools", 
    devops_tools_router,
    {"Manager": "Manager", "DevOps": "DevOps"}
)

# 1. Create a persistent SQLite connection
# check_same_thread=False is crucial for FastAPI so it doesn't crash on concurrent requests
conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False, isolation_level=None)

# 2. Initialize the memory saver with the connection
memory = SqliteSaver(conn)
memory.setup()

# 3. Compile the workflow
app = workflow.compile(checkpointer=memory)