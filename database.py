import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any

AGENCY_DB_PATH = "agency_logs.db"
PIPELINE_DB_PATH = "pipeline.db"

@contextmanager
def get_db_conn():
    """
    Yields a database connection that automatically commits transactions 
    and securely closes the connection when finished to prevent memory leaks.
    """
    # timeout=10 is vital for SQLite when using FastAPI BackgroundTasks
    conn = sqlite3.connect(AGENCY_DB_PATH, timeout=10)
    try:
        # The inner 'with' manages the transaction (commit/rollback)
        with conn:
            yield conn
    finally:
        # This guarantees the file descriptor is released back to the OS
        conn.close()

def init_db():
    with get_db_conn() as conn:
        # Enable concurrent reads and writes
        conn.execute('PRAGMA journal_mode=WAL;')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT,
                project_id TEXT,
                session_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT,        -- 'human', 'ai', or 'tool'
                node_name TEXT,   -- 'Developer', 'Manager', etc.
                content TEXT,     -- The actual message or tool output
                tool_name TEXT,   -- Name of the tool (if applicable)
                is_error BOOLEAN  -- 1 if it failed, 0 if it succeeded
                input_tokens INTEGER DEFAULT 0
            )
        ''')

        # Safely migrate existing databases by attempting to add new columns
        # Each gets its own try/except so one failure doesn't block the others
        try:
            conn.execute("ALTER TABLE activity_logs ADD COLUMN project_id TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists, safe to ignore
            
        try:
            conn.execute('ALTER TABLE activity_logs ADD COLUMN session_id TEXT')
        except sqlite3.OperationalError:
            pass 
            
        try:
            conn.execute('ALTER TABLE activity_logs ADD COLUMN input_tokens INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass

def log_activity(ticket_id: str, role: str, content: Any, node_name: str = None, tool_name: str = None, is_error: bool = False, session_id: str = None, project_id: str = None, input_tokens: int = 0):
    """Saves a single event to the database with serialization."""
    try:
        # Serialization
        content_str = ""
        if isinstance(content, (dict, list)):
            try:
                # default=str forces any non-serializable objects to use their 
                # native __str__ representation instead of crashing json.dumps.
                content_str = json.dumps(content, default=str, ensure_ascii=False)
            except Exception as json_err:
                # Absolute fallback: if JSON fails, just cast the whole thing to a string
                print(f"[Log Warning] JSON serialization failed, falling back to string: {json_err}")
                content_str = str(content)
        else:
            # If it's already a string, boolean, etc.
            content_str = str(content)
        
        # Inserts a new log entry into the database
        with get_db_conn() as conn:
            conn.execute('''
                INSERT INTO activity_logs (ticket_id, project_id, session_id, role, node_name, content, tool_name, is_error, input_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ticket_id, project_id, session_id, role, node_name, content_str, tool_name, is_error, input_tokens))

    except Exception as e:
        # If the actual database insert fails, print it prominently
        print(f"CRITICAL LOGGING FAILURE: {e}")

def get_logs_by_ticket(ticket_id: str):
    """Fetches and formats logs for a specific ticket."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, role, node_name, tool_name, content, is_error, input_tokens
            FROM activity_logs 
            WHERE ticket_id = ? 
            ORDER BY timestamp ASC
        ''', (ticket_id,))
        return [dict(row) for row in cursor.fetchall()]

def get_logs_by_session(ticket_id: str, session_id: str):
    """Fetches logs isolated to a specific ticket and session."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, role, node_name, tool_name, content, is_error, session_id, input_tokens
            FROM activity_logs 
            WHERE ticket_id = ? AND session_id = ?
            ORDER BY timestamp ASC
        ''', (ticket_id, session_id))
        return [dict(row) for row in cursor.fetchall()]

def get_all_sessions():
    """Fetches all unique ticket and session combinations."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        
        # We fetch everything, grouping by both so we know exactly which sessions belong to which tickets
        cursor.execute('''
            SELECT ticket_id, session_id, MAX(project_id) as project_id, MIN(timestamp) as first_active, MAX(timestamp) as last_active, SUM(input_tokens) as total_input_tokens
            FROM activity_logs 
            WHERE ticket_id IS NOT NULL
            GROUP BY ticket_id, session_id
            ORDER BY last_active DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]

def get_sessions_by_project(project_id: str):
    """Fetches all unique ticket and session combinations for a specific project."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ticket_id, session_id, MAX(timestamp) as last_active
            FROM activity_logs 
            WHERE project_id = ? AND ticket_id IS NOT NULL
            GROUP BY ticket_id, session_id
            ORDER BY last_active DESC
        ''', (project_id,))
        return [dict(row) for row in cursor.fetchall()]

def init_pipeline_db():
    """Initializes the persistent task queue database."""
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                project_id TEXT,
                status TEXT,
                human_input TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                build_error_log TEXT,
                latest_commit_sha TEXT
            )
        """)

        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN build_error_log TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
            
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN latest_commit_sha TEXT")
        except sqlite3.OperationalError:
            pass
    
        conn.commit()

def get_active_tasks(project_id: str):
    """Fetches all tasks that are currently Pending, Running, Failed, or BLOCKED_PR_BUILD."""
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row  # This lets us return dicts easily
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM tasks 
               WHERE project_id = ? AND status NOT IN ('Completed', 'Terminated') 
               ORDER BY updated_at DESC""",
            (project_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

def get_pipeline_task(task_id: str):
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_task_status(task_id: str, new_status: str, error_log: str = None, commit_sha: str = None):
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        if error_log is not None or commit_sha is not None:
            # Update status AND the new webhook columns
            conn.execute(
                """UPDATE tasks 
                   SET status = ?, updated_at = ?, build_error_log = ?, latest_commit_sha = ? 
                   WHERE task_id = ?""",
                (new_status, datetime.utcnow().isoformat(), error_log, commit_sha, task_id)
            )
        else:
            # Standard update (just status and timestamp)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (new_status, datetime.utcnow().isoformat(), task_id)
            )
        conn.commit()

def get_completed_tasks(project_id: str):
    """Fetches all tasks that have finished or been dismissed."""
    with sqlite3.connect(PIPELINE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT * FROM tasks 
               WHERE project_id = ? AND status IN ('Completed', 'Terminated') 
               ORDER BY updated_at DESC""",
            (project_id,)
        )
        return [dict(row) for row in cursor.fetchall()]