import hmac
import hashlib
import json
import httpx

class WebhookSecurityError(Exception):
    """Custom exception for signature or validation failures."""
    pass

def process_github_webhook(raw_body: bytes, signature_header: str, get_tenant_secret_func, update_db_func):
    """
    Pure Python logic to verify and process a deployment failure webhook.
    Can be run in any framework.
    """
    if not signature_header:
        raise WebhookSecurityError("Missing Webhook Signature.")

    # 1. Parse the JSON safely
    try:
        payload = json.loads(raw_body.decode('utf-8'))
        tenant_id = payload.get("tenant_id")
        branch_name = payload.get("branch_name")
        commit_sha = payload.get("commit_sha")
        error_log = payload.get("error_log", "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WebhookSecurityError("Invalid JSON payload format.")

    # 2. Fetch the tenant's secret (using an injected function)
    webhook_secret = get_tenant_secret_func(tenant_id)
    if not webhook_secret:
        raise WebhookSecurityError("Invalid Tenant ID or missing secret.")

    # 3. HMAC Cryptographic Verification
    expected_signature = hmac.new(
        key=webhook_secret.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature_header):
        raise WebhookSecurityError("Invalid Signature. Potential spoofing attempt.")

    # 4. Business Logic
    task_id = branch_name.replace("feature-", "").replace("wip-", "")
    sanitized_log = error_log[-2500:] if len(error_log) > 2500 else error_log
    
    # 5. Update the Database (using an injected function)
    result = update_db_func(task_id, sanitized_log, commit_sha)
    
    return {"status": "Processed", "task_id": task_id, "db_result": result}

async def process_preview_ready(tenant_id: str, ticket_id: str, preview_url: str, get_vault_func, update_db_func):
    """
    Called by GitHub Actions when a Firebase Preview is successfully deployed.
    Routes the URL to the correct Linear ticket using the tenant's OAuth token.
    """
    # 1. Fetch the vault using our central helper!
    vault = get_vault_func()
        
    tenant_data = vault.get(tenant_id)
    if not tenant_data:
        return {"error": f"Project {tenant_id} not found in vault"}
    
    # Check the toggle
    if not tenant_data.get("is_active", True):
        return {"status": "ignored", "message": "Project is currently paused."}

    # Fallback to the old api_key if they haven't migrated to OAuth yet
    linear_token = tenant_data.get("linear_access_token") or tenant_data.get("linear_api_key")
    if not linear_token:
        return {"error": "No Linear token found for tenant"}

    # Parse using the underscore
    if "_" in ticket_id:
        # Splits "feature-ai-agency-ws_AI-6-1774207968" -> "AI-6-1774207968"
        after_underscore = ticket_id.split("_")[-1] 
        parts = after_underscore.split("-")
        
        # Rejoin just the "AI" and "6"
        linear_issue_id = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else after_underscore
    else:
        linear_issue_id = ticket_id

    # 2. Construct the GraphQL mutation for Linear
    graphql_query = """
    mutation commentCreate($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    variables = {
        "issueId": linear_issue_id,
        "body": f"🚀 **Live Preview Ready:** {preview_url}\n\n*Deployed automatically by AI Agency Developer.*"
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {linear_token}" # OAuth tokens use Bearer
    }

    # 3. Post the comment to Linear
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.linear.app/graphql",
            json={"query": graphql_query, "variables": variables},
            headers=headers
        )

    # 4. Extract the task_id from the payload and mark the task as COMPLETE!
    # (Strips out the branch prefixes to match the database task_id)
    task_id = ticket_id.replace("feature-", "").replace("wip-", "")
    
    try:
        update_db_func(task_id, "Completed")
    except Exception as e:
        print(f"\n[DB ERROR] Failed to mark task {task_id} as completed: {str(e)}")

    if response.status_code == 200:
        return {"status": "success", "message": "Preview URL posted and task completed."}
    else:
        return {"status": "error", "details": response.text}