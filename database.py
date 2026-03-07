import sqlite3
import json

DB_PATH = "agency_logs.db"

def get_db_conn():
    # timeout=10 is vital for SQLite when using FastAPI BackgroundTasks
    return sqlite3.connect(DB_PATH, timeout=10)

def init_db():
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                role TEXT,        -- 'human', 'ai', or 'tool'
                node_name TEXT,   -- 'Developer', 'Manager', etc.
                content TEXT,     -- The actual message or tool output
                tool_name TEXT,   -- Name of the tool (if applicable)
                is_error BOOLEAN  -- 1 if it failed, 0 if it succeeded
            )
        ''')

def log_activity(ticket_id, role, content, node_name=None, tool_name=None, is_error=False):
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
                INSERT INTO activity_logs (ticket_id, role, node_name, content, tool_name, is_error)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (ticket_id, role, node_name, str(content), tool_name, is_error))
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