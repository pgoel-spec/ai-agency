from typing import Annotated, Sequence, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# --- GLOBAL CONFIGURATION ---
DEVELOPER_ITERATION_LIMIT = 25

class AgencyState(TypedDict):
    # The core conversation and code history
    # operator.add ensures new messages are appended, not overwritten
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # Tracks the current Linear/Jira ticket for HITL updates
    active_ticket_url: str

    # Track the active session to tie node-level logs together
    session_id: str
    
    # The router flag: The Manager updates this to dictate the next step
    next_agent: str
    
    # Sandbox feedback loop: Tracks consecutive QA failures to prevent infinite loops
    dev_iteration_count: int

    # Track phase and status explicitly
    current_phase: str
    current_status: str
    last_report: str
    
    # Tracks if the AI has tested its code since the last write_file
    has_verified_code: bool