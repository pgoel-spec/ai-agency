import os
import requests
import base64
import time
import textwrap
from typing import List
from datetime import datetime
from e2b_code_interpreter import Sandbox
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langchain_tavily import TavilySearch
import shlex
import json

# Create schemas to hide the 'workspace' parameter from the LLM
class TerminalInput(BaseModel):
    thought: str = Field(description="Your step-by-step reasoning for why this command is necessary and what you expect it to output.")
    command: str = Field(description="The shell command to execute")

class ReadFileInput(BaseModel):
    thought: str = Field(description="Explain WHY you are reading this file. If you already ran generate_repo_map, explain why that output was insufficient.")
    file_path: str = Field(description="The ABSOLUTE path to the file to read")

class WriteFileInput(BaseModel):
    chain_of_thought: str = Field(
        ..., 
        description=(
            "A multi-layered recursive analysis: "
            "1. Core Logic: The technical approach for this change. "
            "2. Thought behind the Thought: Why this is the most robust path. "
            "3. Risk Audit: Potential side effects or breaking changes. "
            "4. Verification Plan: How you will prove this works in the sandbox."
        )
    )
    # No deeply nested Pydantic models. Using 'files' as a list[dict] rather than 
    # a Pydantic object to prevent Gemini from hallucinating nested schema titles.
    files: list[dict] = Field(
        ..., 
        description="A list of files to create or edit. MUST be a list of dictionaries, each containing exactly two keys: 'file_path' (The ABSOLUTE path) and 'content'."
    )

class GitHubPRInput(BaseModel):
    ticket_id: str = Field(description="Linear ticket ID")
    commit_message: str = Field(description="Commit message")
    pr_title: str = Field(description="PR Title")
    pr_body: str = Field(description="PR Body")

# Add this schema near your other Input schemas at the top of the file
class WebSearchInput(BaseModel):
    thought: str = Field(description="Explain what error or concept you are researching.")
    query: str = Field(description="The search query to look up on the internet (e.g., 'Firebase Express rewrite 404').")

class GenerateRepoMapInput(BaseModel):
    thought: str = Field(description="Confirm that you are mapping the repo to understand the architecture before writing code.")
    target_dir: str = Field(description="The directory to map. Leave as '.' to use the default repository.")

import json # Ensure this is imported at the top!

class RunUnitTestsInput(BaseModel):
    thought: str = Field(description="Your reasoning for running these specific unit tests.")
    test_command: str = Field(description="The command to run tests (e.g., 'npm test' or 'npx jest').", default="npm test")

class RunBackendTestInput(BaseModel):
    thought: str = Field(description="Your logical reasoning...")
    test_command: str = Field(description="The terminal command to execute your test script (e.g., 'node tests/api.test.js' or 'pytest'). You MUST use write_file to create this test script before calling this tool.")

class RunFrontendTestInput(BaseModel):
    thought: str = Field(description="Your reasoning for what frontend behavior you are testing.")
    url_path: str = Field(description="The relative URL path to test (e.g., '/' or '/snake').")
    wait_for_selector: str = Field(description="Optional CSS selector to wait for before inspecting the console.", default="")

@tool(args_schema=RunUnitTestsInput)
def run_unit_tests(thought: str, test_command: str, config: RunnableConfig) -> str:
    """Runs isolated unit tests without booting a dev server."""
    workspace = config.get("configurable", {}).get("sandbox")
    repo_root = config.get("configurable", {}).get("repo_root", ".")
    export_prefix = config.get("configurable", {}).get("export_prefix", "")

    if not workspace: return "Error: No active sandbox."
    
    try:
        # Auto-heal dependencies silently if missing
        workspace.commands.run(f"cd '{repo_root}' && [ ! -d 'node_modules' ] && npm install --no-audit --no-fund")
        
        execution = workspace.commands.run(f"cd '{repo_root}' && {export_prefix} {test_command}")
        return f"UNIT TEST RESULTS:\nSTDOUT:\n{execution.stdout}\nSTDERR:\n{execution.stderr}"
    except Exception as e:
        return f"Failed to execute tests: {str(e)}"

@tool(args_schema=RunBackendTestInput)
def run_backend_test(thought: str, test_command: str, config: RunnableConfig) -> str:
    """Deterministically boots the environment, executes a custom test command you provide, and safely cleans up. You MUST write an integration test script (e.g., tests/api.test.js using fetch) via write_file before calling this."""
    workspace = config.get("configurable", {}).get("sandbox")
    repo_root = config.get("configurable", {}).get("repo_root", ".")
    export_prefix = config.get("configurable", {}).get("export_prefix", "")
    
    if not workspace: return "Error: No active sandbox."

    runner_path = f"/tmp/test_runner_{int(time.time())}.js"
    
    node_runner_script = r"""
    const { execSync, spawn } = require('child_process');
    const http = require('http');
    const fs = require('fs');
    const path = require('path');

    const REPO_ROOT = __REPO_ROOT__;
    const TEST_COMMAND = __TEST_COMMAND__;
    const PORT = 5000;

    function healDependencies() {
        console.log('[SYSTEM] Scanning for missing dependencies...');
        const findCmd = `find '${REPO_ROOT}' -name "node_modules" -prune -o -name "package.json" -print`;
        try {
            const files = execSync(findCmd, { encoding: 'utf-8' }).split(/\r?\n/).filter(Boolean);
            for (const file of files) {
                const dir = path.dirname(file);
                if (!fs.existsSync(path.join(dir, 'node_modules'))) {
                    console.log(`[SYSTEM] Installing missing dependencies in ${dir}...`);
                    try {
                        execSync('npm install --no-audit --no-fund', { cwd: dir, stdio: 'pipe' });
                        console.log(`[SYSTEM] Successfully installed dependencies in ${dir}.`);
                    } catch (npmError) {
                        console.error('\n[CRITICAL] npm install failed in ' + dir + '!');
                        console.error('=== NPM LOGS ===\n' + (npmError.stderr ? npmError.stderr.toString() : npmError.message));
                        process.exit(1);
                    }
                }
            }
        } catch (e) {
            console.error('[SYSTEM] Dependency check script failed:', e.message);
        }
    }

    // DETERMINISTIC LOG PROBING
    function waitForEnvironment(serverProcess, port, timeoutMs = 45000) {
        return new Promise((resolve) => {
            let isResolved = false;
            let logs = '';
            
            const finish = (status) => {
                if (!isResolved) {
                    isResolved = true;
                    resolve(status);
                }
            };

            const timeout = setTimeout(() => finish(false), timeoutMs);

            // 1. Log-Based Probing (Instant & Deterministic for Firebase)
            const checkLogs = (data) => {
                if (isResolved) return;
                logs += data.toString();
                
                if (logs.includes('All emulators ready')) {
                    clearTimeout(timeout);
                    finish(true);
                }
            };
            
            serverProcess.stdout.on('data', checkLogs);
            serverProcess.stderr.on('data', checkLogs);

            // 2. Port Polling Fallback (For generic Node.js/Express apps)
            const interval = setInterval(() => {
                if (isResolved) return clearInterval(interval);
                http.get(`http://127.0.0.1:${port}/`, () => {
                    if (!logs.includes('firebase')) {
                        clearTimeout(timeout);
                        clearInterval(interval);
                        finish(true);
                    }
                }).on('error', () => {});
            }, 2000);
        });
    }

    async function main() {
        try { execSync('fuser -k -9 5000/tcp 5001/tcp 5002/tcp 5003/tcp 2>/dev/null'); } catch(e) {}

        healDependencies();

        let firebaseDir = REPO_ROOT;
        try {
            const fbPath = execSync(`find '${REPO_ROOT}' -maxdepth 3 -name firebase.json | head -n 1`, { encoding: 'utf-8' }).trim();
            if (fbPath) firebaseDir = path.dirname(fbPath);
        } catch (e) {}

        console.log(`[SYSTEM] Booting server in ${firebaseDir}...`);
        
        const serverProcess = spawn('npx', ['firebase', 'emulators:start'], { 
            cwd: firebaseDir,
            detached: true 
        });

        let serverLogs = '';
        serverProcess.stdout.on('data', d => serverLogs += d.toString());
        serverProcess.stderr.on('data', d => serverLogs += d.toString());

        const isReady = await waitForEnvironment(serverProcess, PORT);

        if (!isReady) {
            console.log(`\n[CRITICAL] Server failed to boot or bind to port ${PORT} within 45 seconds.`);
            console.log(`=== SERVER BOOT LOGS ===\n${serverLogs.slice(-2500)}`);
            try { process.kill(-serverProcess.pid, 'SIGKILL'); } catch(e) {}
            process.exit(1);
        } else {
            console.log(`[SYSTEM] Environment ready. Executing test command: ${TEST_COMMAND}\n`);
            
            try {
                // Execute the AI's custom test script
                const testOutput = execSync(TEST_COMMAND, { cwd: REPO_ROOT, encoding: 'utf-8', stdio: 'pipe' });
                console.log('=== TEST PASSED ===\n' + testOutput);
            } catch (testError) {
                // If the AI's test script crashes (Exit code 1), bubble up the native stack trace AND the backend logs!
                console.log('=== TEST FAILED ===');
                if (testError.stdout) console.log(testError.stdout.toString());
                if (testError.stderr) console.log(testError.stderr.toString());
                
                console.log('\n=== BACKEND SERVER LOGS (Diagnostic) ===\n' + serverLogs.slice(-2500));
            }
        }

        try { process.kill(-serverProcess.pid, 'SIGTERM'); } catch(e) {}
        setTimeout(() => {
            try { execSync('fuser -k -9 5000/tcp 5001/tcp 5002/tcp 5003/tcp 2>/dev/null'); } catch(e) {}
            process.exit(0);
        }, 1000);
    }

    main();
    """

    node_runner_script = node_runner_script.replace("__REPO_ROOT__", json.dumps(repo_root))
    node_runner_script = node_runner_script.replace("__TEST_COMMAND__", json.dumps(test_command))

    try:
        workspace.files.write(runner_path, node_runner_script)
        result = workspace.commands.run(f"{export_prefix} node {runner_path}")
        workspace.commands.run(f"rm -f {runner_path}")
        return result.stdout or result.stderr
    except Exception as e:
        return f"CRITICAL TOOL EXCEPTION: {str(e)}\n\n(Note to AI: The testing orchestrator crashed. Check system state.)"

@tool(args_schema=RunFrontendTestInput)
def run_frontend_test(thought: str, url_path: str, config: RunnableConfig, wait_for_selector: str = "") -> str:
    """Deterministically boots the environment, runs Playwright, and safely cleans up."""
    workspace = config.get("configurable", {}).get("sandbox")
    repo_root = config.get("configurable", {}).get("repo_root", ".")
    export_prefix = config.get("configurable", {}).get("export_prefix", "")

    if not workspace: return "Error: No active sandbox."

    runner_path = f"/tmp/frontend_runner_{int(time.time())}.js"

    node_runner_script = r"""
    const { execSync, spawn } = require('child_process');
    const http = require('http');
    const fs = require('fs');
    const path = require('path');
    const { chromium } = require('playwright');

    const REPO_ROOT = __REPO_ROOT__;
    const TARGET_URL_PATH = __TARGET_URL_PATH__;
    const TARGET_SELECTOR = __TARGET_SELECTOR__;
    const PORT = 5000;
    const TARGET_URL = `http://127.0.0.1:${PORT}${TARGET_URL_PATH}`;

    function healDependencies() {
        console.log('[SYSTEM] Scanning for missing dependencies...');
        const findCmd = `find '${REPO_ROOT}' -name "node_modules" -prune -o -name "package.json" -print`;
        try {
            const files = execSync(findCmd, { encoding: 'utf-8' }).split(/\r?\n/).filter(Boolean);
            for (const file of files) {
                const dir = path.dirname(file);
                if (!fs.existsSync(path.join(dir, 'node_modules'))) {
                    console.log(`[SYSTEM] Installing missing dependencies in ${dir}...`);
                    try {
                        execSync('npm install --no-audit --no-fund', { cwd: dir, stdio: 'pipe' });
                        console.log(`[SYSTEM] Successfully installed dependencies in ${dir}.`);
                    } catch (npmError) {
                        console.error('\n[CRITICAL] npm install failed in ' + dir + '!');
                        console.error('=== NPM LOGS ===\n' + (npmError.stderr ? npmError.stderr.toString() : npmError.message));
                        process.exit(1);
                    }
                }
            }
        } catch (e) {
            console.error('[SYSTEM] Dependency check script failed:', e.message);
        }
    }

    // DETERMINISTIC LOG PROBING
    function waitForEnvironment(serverProcess, port, timeoutMs = 45000) {
        return new Promise((resolve) => {
            let isResolved = false;
            let logs = '';
            
            const finish = (status) => {
                if (!isResolved) {
                    isResolved = true;
                    resolve(status);
                }
            };

            const timeout = setTimeout(() => finish(false), timeoutMs);

            // 1. Log-Based Probing (Instant & Deterministic for Firebase)
            const checkLogs = (data) => {
                if (isResolved) return;
                logs += data.toString();
                
                // Resolves the exact millisecond Firebase finishes mounting background functions
                if (logs.includes('All emulators ready')) {
                    clearTimeout(timeout);
                    finish(true);
                }
            };
            
            serverProcess.stdout.on('data', checkLogs);
            serverProcess.stderr.on('data', checkLogs);

            // 2. Port Polling Fallback (For generic Node.js/Express apps)
            const interval = setInterval(() => {
                if (isResolved) return clearInterval(interval);
                http.get(`http://127.0.0.1:${port}/`, () => {
                    // Only fallback to port-trusting if we aren't booting Firebase
                    if (!logs.includes('firebase')) {
                        clearTimeout(timeout);
                        clearInterval(interval);
                        finish(true);
                    }
                }).on('error', () => {});
            }, 2000);
        });
    }

    async function runTest() {
        let browser;
        try {
            browser = await chromium.launch({ args: ['--no-sandbox'] });
            const page = await browser.newPage();
            const errors = [];
            
            page.on('console', msg => { 
                if(msg.type() === 'error' || msg.type() === 'warning') {
                    const loc = msg.location();
                    const locStr = loc.url ? ` (at ${loc.url}:${loc.lineNumber})` : '';
                    errors.push(`[${msg.type().toUpperCase()}] ${msg.text()}${locStr}`); 
                }
            });
            
            page.on('pageerror', err => errors.push(`[PAGE CRASH] ${err.message}`));
            
            page.on('response', response => {
                if (response.status() >= 400) {
                    errors.push(`[NETWORK ${response.status()}] ${response.request().method()} ${response.url()}`);
                }
            });
            
            await page.addInitScript(() => {
                const originalSetTimeout = window.setTimeout;
                window.setTimeout = function(callback, delay, ...args) {
                    return originalSetTimeout(callback, 0, ...args);
                };
            });

            await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 15000 });
            await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});

            if (TARGET_SELECTOR) {
                try {
                    await page.waitForSelector(TARGET_SELECTOR, { state: 'attached', timeout: 5000 });
                } catch (e) {
                    errors.push(`[TIMEOUT] Expected element '${TARGET_SELECTOR}' never appeared.`);
                }
            }

            await page.waitForTimeout(500);
            
            let result = "=== BROWSER DIAGNOSTICS ===\\n";
            result += errors.length ? errors.join('\\n') : "SUCCESS: No console errors detected.";
            return result;

        } catch (e) {
            return `Failed to inspect URL: ${e.message}`;
        } finally {
            if (browser) await browser.close();
        }
    }

    async function main() {
        try { execSync('fuser -k -9 5000/tcp 5001/tcp 5002/tcp 5003/tcp 2>/dev/null'); } catch(e) {}

        healDependencies();

        let firebaseDir = REPO_ROOT;
        try {
            const fbPath = execSync(`find '${REPO_ROOT}' -maxdepth 3 -name firebase.json | head -n 1`, { encoding: 'utf-8' }).trim();
            if (fbPath) firebaseDir = path.dirname(fbPath);
        } catch (e) {}

        console.log(`[SYSTEM] Booting server in ${firebaseDir}...`);
        
        const serverProcess = spawn('npx', ['firebase', 'emulators:start'], { 
            cwd: firebaseDir,
            detached: true 
        });

        let serverLogs = '';
        serverProcess.stdout.on('data', d => serverLogs += d.toString());
        serverProcess.stderr.on('data', d => serverLogs += d.toString());

        const isReady = await waitForEnvironment(serverProcess, PORT);

        if (!isReady) {
            console.log(`\n[CRITICAL] Server failed to boot or bind to port ${PORT} within 30 seconds.`);
            console.log(`=== SERVER BOOT LOGS ===\n${serverLogs.slice(-2500)}`);
            try { process.kill(-serverProcess.pid, 'SIGKILL'); } catch(e) {}
            process.exit(1);
        } else {
            console.log(`[SYSTEM] Server successfully bound to port ${PORT}. Running tests...`);
            const result = await runTest();
            console.log('\n=== TEST RESULTS ===\n' + result);
            
            // If the test hit a logical server error (404/500), the AI needs the backend logs to debug!
            if (result.includes('HTTP_STATUS:4') || result.includes('HTTP_STATUS:5') || result.includes('TEST FAILED')) {
                console.log('\n=== BACKEND SERVER LOGS (Diagnostic) ===\n' + serverLogs.slice(-2000));
            }
        }

        try { process.kill(-serverProcess.pid, 'SIGTERM'); } catch(e) {}
        setTimeout(() => {
            try { execSync('fuser -k -9 5000/tcp 5001/tcp 5002/tcp 5003/tcp 2>/dev/null'); } catch(e) {}
            process.exit(0);
        }, 1000);
    }

    main();
    """

    node_runner_script = node_runner_script.replace("__REPO_ROOT__", json.dumps(repo_root))
    node_runner_script = node_runner_script.replace("__TARGET_URL_PATH__", json.dumps(url_path))
    node_runner_script = node_runner_script.replace("__TARGET_SELECTOR__", json.dumps(wait_for_selector))

    try:
        workspace.files.write(runner_path, node_runner_script)
        # Global NODE_PATH required for Playwright module resolution
        run_cmd = f"{export_prefix} NODE_PATH=$(npm root -g) node {runner_path}"
        result = workspace.commands.run(run_cmd)
        
        workspace.commands.run(f"rm -f {runner_path}")
        return result.stdout or result.stderr
        
    except Exception as e:
        return f"CRITICAL TOOL EXCEPTION: {str(e)}\n\n(Note to AI: The testing orchestrator crashed. Check system state.)"

def _parse_github_time(ts_str: str) -> datetime:
    """Safely converts a GitHub ISO 8601 string to a datetime object."""
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))

def get_latest_pr(ticket_id: str, github_token: str = None, repo_full: str = None) -> dict:
    """Fetches the latest PR for the ticket and returns its branch and timestamp."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    url = f"https://api.github.com/repos/{repo_full}/pulls?state=open&sort=updated&direction=desc"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        for pr in resp.json():
            head_ref = pr.get("head", {}).get("ref", "")
            if ticket_id.lower() in head_ref.lower():
                return {
                    "name": head_ref,
                    "timestamp": _parse_github_time(pr.get("updated_at")),
                    "url": pr.get("html_url")
                }
    return None

def get_wip_branch(ticket_id: str, github_token: str = None, repo_full: str = None) -> dict:
    """Fetches the WIP recovery branch for the ticket and returns its timestamp."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    # --- You must define the headers before making the request ---
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    resp = requests.get(f"https://api.github.com/repos/{repo_full}/branches", headers=headers)
    if resp.status_code != 200:
        return None
        
    branches = resp.json()
    
    # --- Look for the new 'wip-{ticket_id}' prefix ---
    # (We also include the old 'wip-recovery' prefix to safely catch any in-flight branches)
    matching_branches = [
        b for b in branches 
        if f"wip-{ticket_id}".lower() in b["name"].lower() 
        or f"wip-recovery-{ticket_id}".lower() in b["name"].lower()
    ]
    
    if not matching_branches:
        return None
        
    best_branch = None
    best_time = None
    
    # Fetch the commit dates to find the most recently updated WIP branch
    for branch in matching_branches:
        commit_url = branch.get("commit", {}).get("url")
        if commit_url:
            commit_resp = requests.get(commit_url, headers=headers)
            if commit_resp.status_code == 200:
                ts_str = commit_resp.json().get("commit", {}).get("author", {}).get("date")
                if ts_str:
                    branch_time = _parse_github_time(ts_str)
                    if best_time is None or branch_time > best_time:
                        best_time = branch_time
                        best_branch = branch["name"]
                        
    if best_branch:
        return {"name": best_branch, "timestamp": best_time}
        
    return None

def get_main_branch_timestamp(github_token: str = None, repo_full: str = None) -> str:
    """Fetches the timestamp of the latest commit on the main branch."""
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    if not github_token or not repo_full:
        return None
        
    url = f"https://api.github.com/repos/{repo_full}/branches/main"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        ts_str = resp.json().get("commit", {}).get("commit", {}).get("author", {}).get("date")
        return _parse_github_time(ts_str)
    return None

def get_best_resume_branch(ticket_id: str, github_token: str, repo_full: str) -> dict:
    """
    Races main, open PRs, and WIP branches against each other.
    Whichever was updated most recently wins.
    """
    github_token = github_token or os.environ.get("GITHUB_TOKEN")
    repo_full = repo_full or os.environ.get("GITHUB_REPO")

    pr_info = get_latest_pr(ticket_id, github_token=github_token, repo_full=repo_full)
    wip_info = get_wip_branch(ticket_id, github_token=github_token, repo_full=repo_full)
    main_ts = get_main_branch_timestamp(github_token=github_token, repo_full=repo_full)

    candidates = []
    
    # 1. Add main
    if main_ts:
        candidates.append({"type": "main", "branch_name": "main", "timestamp": main_ts})
        
    # 2. Add Open PR
    if pr_info and pr_info["timestamp"]:
        candidates.append({"type": "pr", "branch_name": pr_info["name"], "timestamp": pr_info["timestamp"]})
        
    # 3. Add WIP
    if wip_info and wip_info["timestamp"]:
        candidates.append({"type": "wip", "branch_name": wip_info["name"], "timestamp": wip_info["timestamp"]})

    # If nothing exists, start fresh
    if not candidates:
        return None

    # Sort all valid candidates by timestamp (Newest first)
    candidates.sort(key=lambda x: x["timestamp"], reverse=True)
    winner = candidates[0]

    # If main is the newest, it means the PR was merged and the WIP is stale. 
    # Returning None tells main.py to ignore overrides and start fresh!
    if winner["type"] == "main":
        return None
        
    # Otherwise, return the winning branch (PR or WIP)
    return winner

@tool(args_schema=TerminalInput)
def run_terminal_command(thought: str, command: str, config: RunnableConfig) -> str:
    """
    Executes a bash command in the terminal. 
    Has a strict timeout. For background processes, use the 'start, curl, kill' pattern.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    # --- THE SERVER GUARDRAIL ---
    forbidden_server_commands = [
        "firebase emulators:start", 
        "npm run dev", 
        "npm start", 
        "node server.js",
        "python -m http.server",
        "flask run",
        "uvicorn",
        "ng serve"
    ]
    
    # If the AI tries to run a blocking server command, reject it instantly.
    if any(bad_cmd in command.lower() for bad_cmd in forbidden_server_commands):
        return (
            "CRITICAL SYSTEM ERROR: You attempted to start a persistent web server using `run_terminal_command`. "
            "This is strictly forbidden because it will freeze the terminal. "
            "You MUST use the `run_backend_test` or `run_frontend_test` tools to automatically boot the environment and test your code instead."
        )

    # --- ENFORCE GITIGNORE BEFORE INSTALLS ---
    # Detect if the agent is trying to install massive dependency folders
    install_keywords = [
        "npm install", "npm i", "yarn add", "yarn install", 
        "pnpm install", "pnpm i", "pip install", "poetry install"
    ]

    # Grab the globally stored repo path
    repo_root = config.get("configurable", {}).get("repo_root", ".")
    export_prefix = config.get("configurable", {}).get("export_prefix", "")
    
    if any(keyword in command.lower() for keyword in install_keywords):
        # Safely append standard ignores and remove duplicates (using sort -u) without interrupting the actual command.
        gitignore_safeguard = (
            f"if [ -d \"{repo_root}\" ]; then "
            f"  echo -e 'node_modules/\\nvenv/\\n.venv/\\n__pycache__/\\n*.pyc\\n.env\\n.next/\\nbuild/\\ndist/' >> \"{repo_root}/.gitignore\"; "
            f"  sort -u \"{repo_root}/.gitignore\" -o \"{repo_root}/.gitignore\"; "
            f"fi;"
        )
        command = f"{command}\n{gitignore_safeguard}"

    # Inject the exports BEFORE the directory navigation and command
    full_command = f"{export_prefix}cd '{repo_root}' && {command}"

    try:
        # We execute the modified command string in the E2B sandbox
        execution = workspace.commands.run(
            full_command,
            timeout=120
        )
        
        output = ""
        if execution.stdout:
            output += f"STDOUT:\n{execution.stdout}\n"
        if execution.stderr:
            output += f"STDERR:\n{execution.stderr}\n"
            
        if execution.error and not execution.stderr:
            output += "\nProcess exited with error code but no STDERR."
            
        return output if output else "Command executed successfully with no output."
        
    except TimeoutError:
         return "Error: Command timed out after 120 seconds. If you started a persistent server, you must run it in the background and kill it in the same command block."
    except Exception as e:
         return f"Terminal Error: {str(e)}"

@tool(args_schema=ReadFileInput)
def read_file(thought: str, file_path: str, config: RunnableConfig) -> str:
    """Reads the full contents of a file inside the workspace."""

    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    # If the AI uses a relative path, force it inside the actual git repository
    if not file_path.startswith("/"):
        repo_root = config.get("configurable", {}).get("repo_root", ".")
        file_path = os.path.join(repo_root, file_path)

    try:
        return workspace.files.read(file_path)
    except Exception as e:
        return f"Error reading file: {str(e)}"

@tool(args_schema=WriteFileInput)
def write_file(chain_of_thought: str, files: list, config: RunnableConfig) -> str:
    """Creates multiple files (or overwrites existing) inside the workspace simultaneously. Use this to keep frontend and backend logic in sync in a single turn."""
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    repo_root = config.get("configurable", {}).get("repo_root", ".")

    results = [f"CHAIN OF THOUGHT: {chain_of_thought}\n"] # Refined internal log
    
    for f in files:
        file_path = f.get("file_path")
        content = f.get("content")
        
        if not file_path or not content:
            results.append(f"Error: Missing file_path or content in one of the file objects.")
            continue

        # If the AI uses a relative path, force it inside the actual git repository
        if not file_path.startswith("/"):
            file_path = os.path.join(repo_root, file_path)
            
        try:
            workspace.files.write(file_path, content)
            results.append(f"Successfully saved changes to {file_path}")
        except Exception as e:
            results.append(f"Error writing {file_path}: {str(e)}")
            
    return "\n".join(results)

@tool
def update_linear_ticket(message: str, config: RunnableConfig) -> str:
    """
    Adds a comment to an active Linear ticket. 
    Use this to notify the human supervisor when a PR is ready with a Preview URL, 
    or if the AI team is blocked and needs help.
    """
    # Securely extract the ticket ID from the system config
    ticket_id = config.get("configurable", {}).get("ticket_id")
    
    if not ticket_id:
        return "Error: System failed to inject the active ticket ID."
    
    linear_issue_id = ticket_id.split("_")[-1] if "_" in ticket_id else ticket_id
    
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    # Intelligently support BOTH the new OAuth token and the legacy API key
    linear_token = tenant_config.get("linear_access_token") or tenant_config.get("linear_api_key") or os.environ.get("LINEAR_API_KEY")
    
    if not linear_token:
        # Return a soft warning instead of an Error so the AI can gracefully proceed to FINISH
        return "Warning: Linear integration is missing or disconnected. Ticket update skipped."

    # Dynamically format the header
    if linear_token.startswith("lin_api_"):
        auth_header = linear_token
    else:
        auth_header = f"Bearer {linear_token}" # OAuth tokens require the Bearer prefix

    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": auth_header, 
        "Content-Type": "application/json",
    }
    
    # Linear's native GraphQL mutation for adding a comment
    query = """
    mutation CommentCreate($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    
    response = requests.post(
        url, 
        json={"query": query, "variables": {"issueId": linear_issue_id, "body": message}}, 
        headers=headers
    )
    
    if response.status_code == 200 and "errors" not in response.json():
        return f"Successfully posted update to ticket {linear_issue_id}"
    return f"Failed to update ticket: {response.text}"

@tool(args_schema=GitHubPRInput)
def create_github_pr(ticket_id: str, commit_message: str, pr_title: str, pr_body: str, config: RunnableConfig) -> str:
    """
    Commits all changed files, pushes to a branch, and opens a Pull Request.
    If an open PR already exists for this ticket, it dynamically updates that PR instead of creating a duplicate.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."
    
    # Extract specific tenant's GitHub credentials
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    github_token = tenant_config.get("github_token") or os.environ.get("GITHUB_TOKEN")
    repo_full = tenant_config.get("github_repo") or os.environ.get("GITHUB_REPO")
    
    if not github_token or not repo_full:
        return "Error: GITHUB_TOKEN or GITHUB_REPO environment variable is missing."
    
    active_resume_branch = config.get("configurable", {}).get("active_resume_branch", "main")
    
    # Define a clean envs dict to reuse
    tool_envs = {
        "GITHUB_TOKEN": github_token,
        "GITHUB_REPO": repo_full
    }

    repo_root = config.get("configurable", {}).get("repo_root", ".")

    # This calls the same function that main.py uses to check for PRs
    existing_pr = get_latest_pr(ticket_id, github_token=github_token, repo_full=repo_full)

    if existing_pr:
        # PATH A: Update Existing PR
        
        # Add the prefix dynamically for this path
        tool_envs["COMMIT_MSG"] = f"Update: {commit_message}"

        # We push the current working state (HEAD) directly to the PR branch.
        git_commands = f"""
        cd '{repo_root}'
        
        # Ensure we are on the exact WIP branch before executing PR logic
        git fetch origin
        git checkout {active_resume_branch}
        
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        
        git add .
        git commit -m "$COMMIT_MSG" || echo "nothing to commit"
        git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{active_resume_branch} --force
        """
        
        try:
            execution = workspace.commands.run(git_commands, envs=tool_envs)
            if execution.error and "nothing to commit" not in execution.stdout:
                return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            return f"Successfully updated the existing Pull Request on branch '{active_resume_branch}'!"
        except Exception as e:
            return f"Git Execution failed: {str(e)}"
            
    else:
        # PATH B: Create New PR
        new_branch = config.get("configurable", {}).get("pr_branch")
        
        # Just use the raw message for this path
        tool_envs["COMMIT_MSG"] = commit_message

        git_commands = f"""
        cd '{repo_root}'
        
        # Ensure we are on the exact WIP branch before executing PR logic
        git fetch origin
        git checkout {active_resume_branch}
        
        git config --global user.email "ai@agency.dev"
        git config --global user.name "AI DevOps"
        git checkout -b {new_branch}
        git add .
        git commit -m "$COMMIT_MSG"
        git push https://$GITHUB_TOKEN@github.com/{repo_full}.git {new_branch}
        """
        
        try:
            execution = workspace.commands.run(git_commands, envs=tool_envs)
            if execution.error and "nothing to commit" not in execution.stdout:
                return f"Git Push Error:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
        except Exception as e:
            return f"Git Execution failed: {str(e)}"
            
        # Create the PR via GitHub API
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        base_url = f"https://api.github.com/repos/{repo_full}"
        
        pr_resp = requests.post(
            f"{base_url}/pulls",
            headers=headers,
            json={
                "title": pr_title,
                "body": pr_body,
                "head": new_branch,
                "base": "main"
            }
        )
        
        if pr_resp.status_code == 201:
            pr_url = pr_resp.json().get("html_url")
            return f"Successfully pushed code to branch '{new_branch}' and created PR! Review it here: {pr_url}"
        else:
            return f"Git push succeeded, but PR creation failed: {pr_resp.text}"

#@tool #Uncomment this later, as of now, I dont want save changes until the tool starts working better
def sync_wip_commit(thought: str, commit_message: str, config: RunnableConfig) -> str:
    """
    Saves your current progress to a temporary remote WIP branch so it is not lost if the server crashes.
    Call this whenever you finish writing a significant piece of code, before moving on.
    """
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."
        
    # Extract specific tenant's GitHub credentials
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    github_token = tenant_config.get("github_token") or os.environ.get("GITHUB_TOKEN")
    repo_full = tenant_config.get("github_repo") or os.environ.get("GITHUB_REPO")
    wip_branch = config.get("configurable", {}).get("wip_branch")
    
    if not github_token or not repo_full:
        return "Error: Missing GITHUB_TOKEN or GITHUB_REPO in tenant configuration."

    repo_root = config.get("configurable", {}).get("repo_root", ".")

    # The script uses HEAD:refs/heads/... to push the current state to the remote WIP branch
    # without altering the agent's current local branch checkout.
    git_commands = f"""
    cd '{repo_root}'
    
    git config --global user.email "ai@agency.dev"
    git config --global user.name "AI Developer"
    
    git add .
    git commit -m "$COMMIT_MSG" || echo "Nothing to commit"
    
    # Force push the current local state (HEAD) directly to the remote WIP branch
    git push https://$GITHUB_TOKEN@github.com/{repo_full}.git HEAD:refs/heads/{wip_branch} --force
    """
    
    try:
        execution = workspace.commands.run(git_commands, envs={
            "GITHUB_TOKEN": github_token,
            "GITHUB_REPO": repo_full,
            "COMMIT_MSG": f"WIP Sync: {commit_message}"
        })

        # Properly evaluate standard bash errors
        if execution.error and "Nothing to commit" not in execution.stdout:
            return f"WIP Sync failed:\nSTDOUT: {execution.stdout}\nSTDERR: {execution.stderr}"
            
        return f"Successfully backed up current local state to remote branch '{wip_branch}'."
        
    except Exception as e:
        return f"WIP Sync execution failed: {str(e)}"

@tool(args_schema=WebSearchInput)
def web_search(thought: str, query: str, config: RunnableConfig) -> str:
    """Searches the internet for framework documentation, tutorials, or solutions to persistent errors."""
    
    # Extract API key securely using your existing multitenant architecture
    tenant_config = config.get("configurable", {}).get("tenant_config", {})
    tavily_api_key = tenant_config.get("tavily_api_key") or os.environ.get("TAVILY_API_KEY")
    
    if not tavily_api_key:
        return "Error: TAVILY_API_KEY is missing. Cannot perform web search."
    
    try:
        # LangChain's Tavily tool reads directly from the environment, so we temporarily inject it
        os.environ["TAVILY_API_KEY"] = tavily_api_key
        
        # Initialize and run the native search
        search = TavilySearch(max_results=3)
        results = search.invoke({"query": query})
        
        return str(results)
    except Exception as e:
        return f"Web search failed: {str(e)}"

@tool(args_schema=GenerateRepoMapInput)
def generate_repo_map(thought: str, target_dir: str, config: RunnableConfig) -> str:
    """Instantly maps the entire repository directory tree and reads all relevant file contents."""
    workspace = config.get("configurable", {}).get("sandbox")
    if not workspace:
        return "Error: No active sandbox found in configuration."

    # Define the blueprint variable here so it is generalized and easy to change later
    plan_filename = "TECHNICAL_PLAN.md"

    # This Python script will run inside the E2B sandbox to rapidly aggregate the codebase
    ingest_script = textwrap.dedent("""
    import os

    def generate_map(root_dir, plan_filename):
        ignore_dirs = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', 'build', 'dist', '.next', '.firebase'}
        ignore_exts = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.pdf', '.zip', '.tar', '.gz', '.mp4', '.pyc', '.lock'}
        
        tree_str = "DIRECTORY TREE:\\n================================================\\n"
        content_str = "\\n\\nFILE CONTENTS:\\n================================================\\n"
        
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Modify dirnames in-place to skip ignored directories
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
            
            level = dirpath.replace(root_dir, '').count(os.sep)
            indent = ' ' * 4 * level
            # Print the absolute path of the directory
            tree_str += f"{indent}{dirpath}/\\n"
            
            subindent = ' ' * 4 * (level + 1)
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext in ignore_exts or f.endswith('-lock.json'):
                    continue
                
                # ALWAYS add to the Directory Tree so the AI knows it exists
                tree_str += f"{subindent}{f}\\n"

                # SKIP adding to File Contents if it is plan file
                if f == plan_filename:
                    continue
                
                filepath = os.path.join(dirpath, f)
                try:
                    with open(filepath, 'r', encoding='utf-8') as file:
                        content = file.read()
                        # Soft truncate massive files (e.g. minified bundles)
                        if len(content) > 30000: 
                            content = content[:30000] + "\\n\\n... [TRUNCATED DUE TO SIZE] ..."
                        
                        # Add to content output
                        content_str += f"\\n--- FILE: {filepath} ---\\n"
                        content_str += f"{content}\\n"
                except Exception:
                    pass # Skip unreadable or binary files
        
        return tree_str + content_str

    if __name__ == "__main__":
        import sys
        import os
        
        # Accept the path passed from the LangGraph config
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        plan_file_arg = sys.argv[2] if len(sys.argv) > 2 else ""
        
        if os.path.exists(os.path.join(target, ".git")):
            print(generate_map(target, plan_file_arg))
        else:
            print(f"Error: No git repository found at {target}. You must clone the repository first.")
    """)
    
    try:
        repo_root = config.get("configurable", {}).get("repo_root", ".")
        
        # Calculate the exact path the AI wants to map
        if target_dir == "." or not target_dir:
            actual_target = repo_root
        elif not target_dir.startswith("/"):
            actual_target = os.path.join(repo_root, target_dir)
        else:
            actual_target = target_dir

        # Write the script to the sandbox and execute it against the actual_target
        workspace.files.write("/tmp/ingest_repo.py", ingest_script)

        # Pass the plan_filename as the second argument to the inner python script
        res = workspace.commands.run(f"python3 /tmp/ingest_repo.py '{actual_target}' '{plan_filename}'")
        
        output = res.stdout or res.stderr
        return output if output else "Repository map generated, but it was empty."
    except Exception as e:
        return f"Failed to generate repository map: {str(e)}"