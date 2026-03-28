import hmac
import hashlib
import json

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