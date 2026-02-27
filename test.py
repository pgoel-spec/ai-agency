# test.py
from dotenv import load_dotenv
load_dotenv()

from agents.graph import app as agent_team

# Format a fake Linear webhook payload
real_ticket_id = "AI-5"

initial_state = {
    "messages": [
        ("user", f"New Task Assigned: Create a simple python script that prints 'Hello from E2B' and execute it in the sandbox. \n\nIMPORTANT: The active Linear Ticket ID for this task is {real_ticket_id}.")
    ],
    "active_ticket_url": real_ticket_id
}

print("Waking up AI team...")

# Stream the output so you can watch the agents think and pass state
for output in agent_team.stream(initial_state):
    for key, value in output.items():
        print(f"\n--- Output from Node: {key} ---")
        if "messages" in value and value["messages"]:
            last_msg = value["messages"][-1]
            # Print the text if it exists
            if last_msg.content:
                print(last_msg.content)
            # Print the tool request if it exists
            if hasattr(last_msg, 'tool_calls') and last_msg.tool_calls:
                print(f"[Action: Executing Tool] {last_msg.tool_calls[0]['name']}")