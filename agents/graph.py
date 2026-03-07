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

def developer_router_logic(state: AgencyState):
    """
    The internal loop: If the developer called a tool, go to the tools node. 
    If the developer just replied with text (finished), go report to the Manager.
    """
    # Failsafe to break a stubborn developer out of an infinite coding loop
    if state.get("dev_iteration_count", 0) >= DEVELOPER_ITERATION_LIMIT:
        print("DEBUG: Developer hit iteration limit. Forcing report to Manager.")
        return "Manager"

    # Find the most recent AI Message to check for tool calls
    messages = state["messages"]
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)    
    
    # Priority Check: If there are tools OTHER than ReportStatus, execute them first
    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        # Check if any tool calls in this turn are NOT handled by the node itself
        has_functional_tools = any(
            tc["name"] not in ["ReportStatus"] for tc in last_ai_msg.tool_calls
        )
        if has_functional_tools:
            return "DeveloperTools"

    # If no functional tools are pending, respect the status update
    current_status = state.get("current_status")
    if current_status in ["COMPLETE", "BLOCKED"]:
        return "Manager"
      
    return "Manager"

def devops_router_logic(state: AgencyState):
    """
    Internal loop for DevOps: Brain -> Tools -> Brain -> Manager.
    """
    
    messages = state["messages"]
    last_ai_msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)

    if last_ai_msg and hasattr(last_ai_msg, "tool_calls") and last_ai_msg.tool_calls:
        # Check if there are any tools other than the report
        has_functional_tools = any(
            tc["name"] not in ["DevOpsReport"] for tc in last_ai_msg.tool_calls
        )
        if has_functional_tools:
            return "DevOpsTools"
    
    if state.get("current_status") == "COMPLETE":
        return "Manager"
        
    return "Manager"

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
    developer_router_logic,
    {"DeveloperTools": "DeveloperTools", "Manager": "Manager", "Developer": "Developer"}
)
# After tools execute, always go back to the Developer's brain to review the output
workflow.add_edge("DeveloperTools", "Developer")

# DevOps Loop
workflow.add_conditional_edges(
    "DevOps", 
    devops_router_logic,
    {"DevOpsTools": "DevOpsTools", "Manager": "Manager", "DevOps": "DevOps"}
)
# After tools execute, always go back to the DevOps brain to summarize success
workflow.add_edge("DevOpsTools", "DevOps")

# 1. Create a persistent SQLite connection
# check_same_thread=False is crucial for FastAPI so it doesn't crash on concurrent requests
conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False, isolation_level=None)

# 2. Initialize the memory saver with the connection
memory = SqliteSaver(conn)
memory.setup()

# 3. Compile the workflow
app = workflow.compile(checkpointer=memory)