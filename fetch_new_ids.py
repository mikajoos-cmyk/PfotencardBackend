from sqlalchemy import create_engine, text

# ==============================================================================
# KONFIGURATION
# ==============================================================================
# Fügen Sie hier den Connection-String Ihrer NEUEN Supabase-Datenbank ein.
# Wichtig: Nutzen Sie wieder 'db.' am Anfang des Hosts (Session Mode, Port 5432)
DATABASE_URL = "postgresql://postgres.hddfrvbtvmfiivlwrmij:N0Ha4HZonNORxj2N@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"


# ==============================================================================

def fetch_ids():
    print(f"🔌 Verbinde mit neuer Datenbank...")

    try:
        engine = create_engine(DATABASE_URL)
        with engine.connect() as conn:

            # 1. TENANTS (Hundeschulen)
            print("\n" + "=" * 50)
            print("🏢 VORHANDENE TENANTS (Hundeschulen)")
            print("=" * 50)
            tenants = conn.execute(text("SELECT id, name FROM tenants ORDER BY id")).fetchall()
            if not tenants:
                print("⚠️  Keine Tenants gefunden.")
            for t in tenants:
                print(f"ID: {t.id}  |  Name: {t.name}")

            # 2. LEVEL
            print("\n" + "=" * 50)
            print("📊 VORHANDENE LEVELS (für LEVEL_MAP)")
            print("=" * 50)
            levels = conn.execute(
                text("SELECT id, name, tenant_id, rank_order FROM levels ORDER BY tenant_id, rank_order")).fetchall()

            if not levels:
                print("⚠️  Keine Levels gefunden. (Haben Sie das SQL-Skript zum Erstellen der Level ausgeführt?)")

            current_tenant = None
            for l in levels:
                if l.tenant_id != current_tenant:
                    print(f"\n--- Für Tenant ID {l.tenant_id} ---")
                    current_tenant = l.tenant_id

                # Ausgabe im Format für einfaches Kopieren
                print(f"Name: '{l.name:<15}' --> Neue ID: {l.id}")

            # 3. TRAINING TYPES (Leistungen)
            print("\n" + "=" * 50)
            print("🎓 VORHANDENE LEISTUNGEN (für ACHIEVEMENT_MAP)")
            print("=" * 50)
            # Wir filtern nach 'training' oder holen alle, je nach Bedarf
            types = conn.execute(
                text("SELECT id, name, tenant_id FROM training_types ORDER BY tenant_id, name")).fetchall()

            if not types:
                print("⚠️  Keine Training Types gefunden.")

            current_tenant = None
            for t in types:
                if t.tenant_id != current_tenant:
                    print(f"\n--- Für Tenant ID {t.tenant_id} ---")
                    current_tenant = t.tenant_id

                print(f"Name: '{t.name:<25}' --> Neue ID: {t.id}")

    except Exception as e:
        print(f"\n❌ FEHLER: {e}")
        print("Tipp: Prüfen Sie Passwort und Host (beginnt er mit 'db.'?)")


if __name__ == "__main__":
    fetch_ids()