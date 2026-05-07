import datetime
import os
import psycopg2

print(f"Current UTC time: {datetime.datetime.utcnow()}")
print(f"Current Local time: {datetime.datetime.now()}")

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    print("DATABASE_URL not set")
else:
    try:
        conn = psycopg2.connect(database_url)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;")
            rows = cur.fetchall()
            print("\nRecent orders:")
            for row in rows:
                print(row)
    except Exception as e:
        print(f"DB Error: {e}")
