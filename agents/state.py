from typing import Annotated, Sequence, TypedDict, Literal
from langchain_core.messages import BaseMessage
import operator

class AgencyState(TypedDict):
    # The core conversation and code history
    # operator.add ensures new messages are appended, not overwritten
    messages: Annotated[Sequence[BaseMessage], operator.add]
    
    # Tracks the current Linear/Jira ticket for HITL updates
    active_ticket_url: str 
    
    # The router flag: The Manager updates this to dictate the next step
    next_agent: Literal["Developer", "DevOps", "FINISH"]
    
    # Sandbox feedback loop: tracks consecutive QA failures to prevent infinite loops
    dev_iteration_count: int