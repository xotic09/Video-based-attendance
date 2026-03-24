import sqlite3
def create_connection(db_file):
    """Create a database connection to the SQLite database specified by db_file."""
    conn = sqlite3.connect(db_file)
    return conn
def create_tables(conn):
    """Create tables in the database."""
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sno TEXT NOT NULL UNIQUE,
        roll_no TEXT NOT NULL UNIQUE,
        class TEXT NOT NULL
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        subject TEXT NOT NULL,
        teacher TEXT NOT NULL,
        date TEXT NOT NULL,
        status TEXT CHECK(status IN ('Present', 'Absent')) NOT NULL,
        FOREIGN KEY (student_id) REFERENCES students (id)
    )
    ''')
    conn.commit()
def main():
    """Main function to create the database and tables."""
    database = "students_attendance.db"
    conn = create_connection(database)
    create_tables(conn)
    conn.close()
if __name__ == "__main__":
    main()