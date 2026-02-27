from langgraph.graph import StateGraph, START, END
from .state import AgencyState
from .nodes import manager_node, developer_node, devops_node

# 1. Initialize the Graph
workflow = StateGraph(AgencyState)

# 2. Add the Nodes (The "Employees")
workflow.add_node("Manager", manager_node)
workflow.add_node("Developer", developer_node)
workflow.add_node("DevOps", devops_node)

# 3. Define the Routing Logic
def router_logic(state: AgencyState):
    """Reads the state to determine where the graph goes next."""
    
    # Failsafe: Prevent infinite loops and budget drain
    if state.get("dev_iteration_count", 0) >= 3:
        return "FINISH"
        
    next_node = state.get("next_agent", "FINISH")
    
    if next_node == "Developer":
        return "Developer"
    elif next_node == "DevOps":
        return "DevOps"
    else:
        return "FINISH"

# 4. Wire the Edges (The "Org Chart")
# The process always starts with the Manager
workflow.add_edge(START, "Manager")

# The Manager uses the router_logic to delegate tasks conditionally
workflow.add_conditional_edges(
    "Manager", 
    router_logic,
    {
        "Developer": "Developer",
        "DevOps": "DevOps",
        "FINISH": END
    }
)

# The Workers must always report back to the Manager when done
workflow.add_edge("Developer", "Manager")
workflow.add_edge("DevOps", "Manager")

# 5. Compile the Executable Graph
app = workflow.compile()