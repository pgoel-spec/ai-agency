import os
from langchain_google_vertexai import ChatVertexAI
from langchain_google_vertexai.model_garden import ChatAnthropicVertex
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from .state import AgencyState
from .tools import update_linear_ticket, create_github_pr, run_terminal_command, read_file, write_file

# 1. Initialize the Models via Vertex AI
# The Manager gets the Linear tool. The Dev gets the E2B Sandbox tool.
project_id = os.environ.get("PROJECT_ID")

manager_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    project=project_id,
    max_retries=2,
    vertexai=True, # This flag ensures it uses GCP/Vertex AI securely
    max_output_tokens=8192
).bind_tools([update_linear_ticket])

devops_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    project=project_id,
    max_retries=2,
    vertexai=True,
    max_output_tokens=8192
).bind_tools([update_linear_ticket, create_github_pr])

developer_llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-pro",
    project=project_id,
    max_retries=2,
    vertexai=True,
    max_output_tokens=8192
).bind_tools([run_terminal_command, read_file, write_file])

""" developer_llm = ChatAnthropicVertex(
    project=project_id, 
    location="asia-southeast1", 
    model_name="claude-sonnet-4-6",
    max_retries=2 # Give it more attempts
).bind_tools([execute_sandbox_code])
 """

def manager_node(state: AgencyState) -> dict:
    """The Supervisor: Routes tasks and communicates with the human manager."""
    system_prompt = SystemMessage(content=(
        "You are the Engineering Manager of an autonomous AI dev team. "
        "Review the conversation and the active ticket. "
        "DO NOT write or execute code yourself. "
        "If code needs to be written or tested, ONLY reply with 'ROUTE TO DEVELOPER'. "
        "If tests have passed and code is ready to ship, ONLY reply with 'ROUTE TO DEVOPS'. "
        "If a Preview URL is ready or the team is blocked, use your tool to update the Linear ticket, "
        "then reply with 'ROUTE TO FINISH'."
    ))
    
    response = manager_llm.invoke([system_prompt] + state["messages"])
    
    # Bulletproof the state: force list-based contents into standard strings
    if isinstance(response.content, list):
        content_str = response.content[0].get("text", "") if len(response.content) > 0 else ""
        response.content = content_str
    else:
        content_str = str(response.content)

    # Prevent Gemini from crashing on empty strings
    if not response.content or response.content.strip() == "":
        response.content = "[Agent executing action]"
        
    # Make extraction case-insensitive
    content_lower = content_str.lower()
    next_agent = "Developer" 
    if "route to devops" in content_lower:
        next_agent = "DevOps"
    elif "route to finish" in content_lower:
        next_agent = "FINISH"
        
    # We MUST append the Manager's message to the state so the conversation moves forward!
    messages_to_add = [response] 
    
    # Execute Linear tool if Manager decides to use it
    if response.tool_calls:
        for tool in response.tool_calls:
            if tool["name"] == "update_linear_ticket":
                result = update_linear_ticket.invoke(tool["args"])
                messages_to_add.append(ToolMessage(content=str(result), name=tool["name"], tool_call_id=tool["id"]))
            else:
                # Catch-all for hallucinated tools
                messages_to_add.append(ToolMessage(content=f"Error: Tool '{tool['name']}' does not exist.", name=tool["name"], tool_call_id=tool["id"]))
                
    return {"messages": messages_to_add, "next_agent": next_agent}

def developer_node(state: AgencyState) -> dict:
    """The Worker: Interacts with the persistent repo, writes code, and runs tests."""
    import time
    time.sleep(3) # Keeping the rate limit safety net
    
    # Consistent approach: The AI relies entirely on the E2B environment variables
    system_prompt = SystemMessage(content=(
        "You are a Senior Full-Stack Developer. You have access to a persistent cloud terminal. "
        "Step 1: Use run_terminal_command to clone the GitHub repository using your environment variables: "
        "`git clone https://$GITHUB_TOKEN@github.com/$GITHUB_REPO.git` "
        "Step 2: Explore the repository using terminal commands or the read_file tool. "
        "Step 3: Modify or create the necessary files using the write_file tool. "
        "Step 4: Run the test suite or execute the code using run_terminal_command to verify your changes. "
        "Do not route back to the manager until you have verified the code works perfectly."
    ))
    
    response = developer_llm.invoke([system_prompt] + state["messages"])
    
    # Prevent Gemini from crashing on empty strings
    if not response.content or response.content.strip() == "":
        response.content = "[Agent executing action]"
        
    # Bulletproof the state
    if isinstance(response.content, list):
        extracted_text = ""
        for item in response.content:
            if isinstance(item, dict) and "text" in item:
                extracted_text += item["text"]
            elif isinstance(item, str):
                extracted_text += item
        response.content = extracted_text
        
    messages_to_add = [response]
    
    # Map out the available tools so the agent can execute any of them dynamically
    tool_map = {
        "run_terminal_command": run_terminal_command,
        "read_file": read_file,
        "write_file": write_file
    }
    
    if response.tool_calls:
        for tool in response.tool_calls:
            if tool["name"] in tool_map:
                result = tool_map[tool["name"]].invoke(tool["args"])
                messages_to_add.append(ToolMessage(content=str(result), name=tool["name"], tool_call_id=tool["id"]))
            else:
                messages_to_add.append(ToolMessage(content=f"Error: Tool '{tool['name']}' does not exist.", name=tool["name"], tool_call_id=tool["id"]))
    
    new_count = state.get("dev_iteration_count", 0) + 1
    return {"messages": messages_to_add, "dev_iteration_count": new_count}

def devops_node(state: AgencyState) -> dict:
    """The Delivery Agent: Handles Git operations and CI/CD triggers."""
    import time
    time.sleep(3) # Keeping the rate limit safety net
    
    system_prompt = SystemMessage(content=(
        "You are a DevOps Engineer. The Developer has successfully modified and tested the code in the shared workspace. "
        "Step 1: Use the create_github_pr tool to commit all workspace changes, push them to a new branch, and open a Pull Request. "
        "Step 2: Once the PR is created, use the update_linear_ticket tool to post the real GitHub PR URL back to the human manager."
    ))
    
    response = devops_llm.invoke([system_prompt] + state["messages"])
    
    # Prevent Gemini from crashing on empty strings
    if not response.content or response.content.strip() == "":
        response.content = "[Agent executing action]"
        
    # Bulletproof the state
    if isinstance(response.content, list):
        extracted_text = ""
        for item in response.content:
            if isinstance(item, dict) and "text" in item:
                extracted_text += item["text"]
            elif isinstance(item, str):
                extracted_text += item
        response.content = extracted_text
        
    messages_to_add = [response]
    
    if response.tool_calls:
        for tool in response.tool_calls:
            if tool["name"] == "update_linear_ticket":
                result = update_linear_ticket.invoke(tool["args"])
                messages_to_add.append(ToolMessage(content=str(result), name=tool["name"], tool_call_id=tool["id"]))
            elif tool["name"] == "create_github_pr":
                result = create_github_pr.invoke(tool["args"])
                messages_to_add.append(ToolMessage(content=str(result), name=tool["name"], tool_call_id=tool["id"]))
            else:
                messages_to_add.append(ToolMessage(content=f"Error: Tool '{tool['name']}' does not exist.", name=tool["name"], tool_call_id=tool["id"]))
                
    return {"messages": messages_to_add}