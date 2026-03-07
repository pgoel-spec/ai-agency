import sqlite3
import json
from contextlib import contextmanager

DB_PATH = "agency_logs.db"

@contextmanager
def get_db_conn():
    """
    Yields a database connection that automatically commits transactions 
    and securely closes the connection when finished to prevent memory leaks.
    """
    # timeout=10 is vital for SQLite when using FastAPI BackgroundTasks
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        # The inner 'with' manages the transaction (commit/rollback)
        with conn:
            yield conn
    finally:
        # This guarantees the file descriptor is released back to the OS
        conn.close()

def init_db():
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT,
                session_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT,        -- 'human', 'ai', or 'tool'
                node_name TEXT,   -- 'Developer', 'Manager', etc.
                content TEXT,     -- The actual message or tool output
                tool_name TEXT,   -- Name of the tool (if applicable)
                is_error BOOLEAN  -- 1 if it failed, 0 if it succeeded
            )
        ''')
        # Safely migrate existing databases by attempting to add the column
        try:
            conn.execute('ALTER TABLE activity_logs ADD COLUMN session_id TEXT')
        except sqlite3.OperationalError:
            pass # Column already exists, safe to ignore

def log_activity(ticket_id, role, content, node_name=None, tool_name=None, is_error=False, session_id=None):
    """Saves a single event to the database."""
    try:
        # If content is a list or dict (like tool args), convert to JSON string
        if isinstance(content, (dict, list)):
            def message_serializer(obj):
                # LangChain messages have a .dict() method
                if hasattr(obj, "dict"):
                    return obj.dict()
                # Fallback for any other non-serializable objects
                return str(obj)
            
            content = json.dumps(content, default=message_serializer, ensure_ascii=False)
        
        with get_db_conn() as conn:
            conn.execute('''
                INSERT INTO activity_logs (ticket_id, session_id, role, node_name, content, tool_name, is_error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (ticket_id, session_id, role, node_name, str(content), tool_name, is_error))
    except Exception as e:
        print(f"CRITICAL LOGGING FAILURE: {e}")

def get_logs_by_ticket(ticket_id: str):
    """Fetches and formats logs for a specific ticket."""
    with get_db_conn() as conn:
        conn.row_factory = sqlite3.Row 
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, role, node_name, tool_name, content, is_error 
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
            SELECT timestamp, role, node_name, tool_name, content, is_error, session_id
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
            SELECT ticket_id, session_id, MAX(timestamp) as last_active
            FROM activity_logs 
            WHERE ticket_id IS NOT NULL
            GROUP BY ticket_id, session_id
            ORDER BY last_active DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]