
import oracledb

DSN = "localhost:1521/FREEPDB1"
USER = "AUTO_TIMETABLE"
PASSWORD = "macro123"

try:
    connection = oracledb.connect(
        user=USER,
        password=PASSWORD,
        dsn=DSN
    )

    print("Oracle connected successfully!")

    cursor = connection.cursor()
    cursor.execute("SELECT USER, SYS_CONTEXT('USERENV', 'CON_NAME') FROM DUAL")

    print("Connected user and container:", cursor.fetchone())

    connection.close()

except Exception as e:
    print("Connection Error:", e)
