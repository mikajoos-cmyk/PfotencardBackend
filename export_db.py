import json
import datetime
from sqlalchemy import create_engine, MetaData, select
from decimal import Decimal

# ==========================================
# KONFIGURATION
# ==========================================
# Hier Ihren Connection String einfügen (achten Sie auf das 'db.' im Host!)
# Format: postgresql://USER:PASSWORD@HOST:PORT/DATABASE
DATABASE_URL = "postgresql://postgres.hddfrvbtvmfiivlwrmij:N0Ha4HZonNORxj2N@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"

# Name der Ausgabedatei
OUTPUT_FILE = "alte_daten_export.json"


# ==========================================
# HELFER: Datentypen für JSON konvertieren
# ==========================================
def custom_serializer(obj):
    """Hilft JSON, Datumsangaben und Dezimalzahlen zu speichern."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


def export_data():
    print(f"🔌 Verbinde mit Datenbank: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else '...'}")

    try:
        # 1. Engine erstellen (wie in Ihrer app/database.py)
        engine = create_engine(DATABASE_URL)
        connection = engine.connect()

        # 2. Datenbank-Struktur automatisch erkennen ("Reflection")
        metadata = MetaData()
        metadata.reflect(bind=engine)

        full_dump = {}

        # 3. Alle Tabellen durchgehen
        # sorted_tables sorgt dafür, dass wir die richtige Reihenfolge haben (wegen Foreign Keys)
        for table in metadata.sorted_tables:
            table_name = table.name
            print(f"   Sammle Daten aus Tabelle: {table_name}...", end="")

            # Select * from table
            query = select(table)
            result = connection.execute(query)

            # Zeilen in Liste von Dictionaries umwandeln
            # result.keys() gibt die Spaltennamen zurück
            rows = [dict(row._mapping) for row in result]

            full_dump[table_name] = rows
            print(f" {len(rows)} Zeilen gefunden.")

        # 4. In Datei speichern
        print(f"💾 Speichere Daten in {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(full_dump, f, default=custom_serializer, indent=4, ensure_ascii=False)

        print("✅ Export erfolgreich abgeschlossen!")

    except Exception as e:
        print("\n❌ FEHLER:")
        print(e)
        print("\nTipp: Prüfen Sie, ob 'db.' vor der Supabase-URL steht und das Passwort stimmt.")
    finally:
        if 'connection' in locals():
            connection.close()


if __name__ == "__main__":
    export_data()