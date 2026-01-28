"""
Database migration script to add start_date and end_date to quotes
"""
import sqlite3
import os

db_path = 'instance/erp_rent.db'

if not os.path.exists(db_path):
    print(f"Database {db_path} not found!")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("Starting database migration...")

try:
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(quote)")
    columns = [col[1] for col in cursor.fetchall()]
    
    migrations_needed = []
    
    if 'start_date' not in columns:
        migrations_needed.append("Adding 'start_date' column")
        cursor.execute("ALTER TABLE quote ADD COLUMN start_date DATETIME")
    
    if 'end_date' not in columns:
        migrations_needed.append("Adding 'end_date' column")
        cursor.execute("ALTER TABLE quote ADD COLUMN end_date DATETIME")
    
    if migrations_needed:
        conn.commit()
        print("✓ Migration completed successfully!")
        for migration in migrations_needed:
            print(f"  - {migration}")
    else:
        print("✓ Database is already up to date!")
    
    # Verify the changes
    cursor.execute("PRAGMA table_info(quote)")
    print("\nCurrent quote table structure:")
    for col in cursor.fetchall():
        print(f"  {col[1]}: {col[2]}")
    
except Exception as e:
    conn.rollback()
    print(f"✗ Error during migration: {e}")
    exit(1)
finally:
    conn.close()

print("\nMigration complete! You can now run the application.")
