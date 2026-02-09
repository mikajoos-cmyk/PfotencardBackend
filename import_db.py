import json
import datetime
from sqlalchemy import create_engine, text

# ==========================================
# 1. KONFIGURATION & MAPPINGS
# ==========================================

DATABASE_URL = "postgresql://postgres.ctsoisfxbhaynonnudua:U0Pn6bzjxYqNSFKy@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"
TARGET_TENANT_ID = 4
INPUT_FILE = "alte_daten_export.json"

# !!! WICHTIG !!!
# Geben Sie hier die ID eines existierenden Admins/Trainers in der NEUEN DB an.
# Diese ID wird verwendet, wenn 'booked_by_id' in den alten Daten leer ist.
FALLBACK_ADMIN_ID = 6  # <--- BITTE HIER EINE GÜLTIGE USER-ID AUS DER NEUEN DB EINTRAGEN

# LEVEL MAPPING (Alt -> Neu)
LEVEL_MAP = {
    1: 11,
    2: 16,
    3: 17,
    4: 18,
    5: 19
}

# ACHIEVEMENT TYPEN MAPPING (Text/Alt-ID -> Neu-ID)
ACHIEVEMENT_MAP = {
    "group_class": 6,
    # Fügen Sie hier weitere Mappings hinzu, falls nötig
}


# ==========================================
# 2. HILFSFUNKTIONEN
# ==========================================

def load_json(filename):
    with open(filename, 'r', encoding='utf-8') as f:
        return json.load(f)


def run_import():
    print(f"📂 Lade Daten aus {INPUT_FILE}...")
    data = load_json(INPUT_FILE)

    print(f"🔌 Verbinde mit Datenbank...")
    engine = create_engine(DATABASE_URL)

    # --- MAPPING SPEICHER (Alte ID -> Neue ID) ---
    # Diese Dictionaries sind das Herzstück der Migration.
    MAP_USERS = {}  # old_user_id -> new_user_id
    MAP_DOGS = {}  # old_dog_id -> new_dog_id
    MAP_TRANSACTIONS = {}  # old_tx_id -> new_tx_id

    skipped_items = {
        "users": [],
        "dogs": [],
        "transactions": [],
        "achievements": [],
        "documents": []
    }

    with engine.begin() as conn:  # Eine große Transaktion

        # ---------------------------------------------------------
        # SCHRITT A: USERS
        # ---------------------------------------------------------
        if 'users' in data:
            print(f"👤 Importiere {len(data['users'])} User (Neue IDs werden generiert)...")

            for u in data['users']:
                old_id = u['id']
                old_level = u.get('level_id')
                new_level = LEVEL_MAP.get(old_level)  # Kann None sein, DB sollte Default haben oder wir setzen fallback

                # Wir lassen die 'id' Spalte im INSERT weg, damit Postgres sie generiert
                user_sql = text("""
                                INSERT INTO users (tenant_id, name, email, hashed_password, role, is_active,
                                                   current_level_id, balance, customer_since, is_vip, is_expert,
                                                   permissions, notifications_email, notifications_push)
                                VALUES (:tenant_id, :name, :email, :hashed_password, :role, :is_active,
                                        :current_level_id, :balance, :customer_since, :is_vip, :is_expert,
                                        :permissions, :notifications_email, :notifications_push) RETURNING id
                                """)

                user_params = {
                    "tenant_id": TARGET_TENANT_ID,
                    "name": u['name'],
                    "email": u['email'],
                    "hashed_password": u['hashed_password'],
                    "role": u['role'],
                    "is_active": u.get('is_active', True),
                    "current_level_id": new_level,
                    "balance": u.get('balance', 0.0),
                    "customer_since": u.get('created_at', datetime.datetime.now()),
                    "is_vip": False,
                    "is_expert": False,
                    "permissions": '{}',
                    "notifications_email": True,
                    "notifications_push": True
                }

                try:
                    # Nested Transaction für jeden User, falls Email-Duplikat auftritt
                    with conn.begin_nested():
                        result = conn.execute(user_sql, user_params)
                        new_id = result.fetchone()[0]

                        # MAPPING SPEICHERN
                        MAP_USERS[old_id] = new_id

                except Exception as e:
                    # Falls der User schon existiert (Email Unique Constraint),
                    # versuchen wir seine ID zu finden, um das Mapping trotzdem zu füllen.
                    if "unique constraint" in str(e).lower() and "email" in str(e).lower():
                        existing_id_sql = text("SELECT id FROM users WHERE email = :email AND tenant_id = :tenant_id")
                        existing = conn.execute(existing_id_sql,
                                                {"email": u['email'], "tenant_id": TARGET_TENANT_ID}).fetchone()
                        if existing:
                            MAP_USERS[old_id] = existing[0]
                            # Info: Wir überspringen das Insert, aber merken uns das Mapping für die Hunde/Transaktionen
                        else:
                            skipped_items["users"].append(
                                f"User {u['email']} (Alt-ID {old_id}): Email-Konflikt, aber ID nicht gefunden.")
                    else:
                        skipped_items["users"].append(f"User {u['email']} (Alt-ID {old_id}) Fehler: {e}")

        # ---------------------------------------------------------
        # SCHRITT B: DOGS
        # ---------------------------------------------------------
        if 'dogs' in data:
            print(f"🐕 Importiere {len(data['dogs'])} Hunde...")
            for d in data['dogs']:
                old_dog_id = d['id']
                old_owner_id = d['owner_id']

                # 1. Neuen Besitzer finden
                new_owner_id = MAP_USERS.get(old_owner_id)

                if not new_owner_id:
                    skipped_items["dogs"].append(
                        f"Hund {d['name']} (Alt-ID {old_dog_id}): Besitzer (Alt-ID {old_owner_id}) nicht gefunden.")
                    continue

                # Level ermitteln (Hund erbt oft vom User, wenn nicht explizit)
                # Hier holen wir das aktuelle Level des NEUEN Users aus der DB oder nehmen None
                # Der Einfachheit halber lassen wir es hier im Insert, wenn es im Dict 'd' ist,
                # sonst sollte die DB Logik greifen.

                dog_sql = text("""
                               INSERT INTO dogs (tenant_id, owner_id, name, breed, birth_date, chip, current_level_id)
                               VALUES (:tenant_id, :owner_id, :name, :breed, :birth_date, :chip,
                                       :current_level_id) RETURNING id
                               """)

                # Wir müssen das Level des Besitzers wissen für 'current_level_id',
                # falls im Hunde-Export keines steht.
                # Vereinfachung: Wir setzen None, wenn im Export keines ist.

                dog_params = {
                    "tenant_id": TARGET_TENANT_ID,
                    "owner_id": new_owner_id,
                    "name": d['name'],
                    "breed": d.get('breed'),
                    "birth_date": d.get('birth_date'),
                    "chip": d.get('chip'),
                    "current_level_id": LEVEL_MAP.get(d.get('level_id'))  # Oder None
                }

                try:
                    result = conn.execute(dog_sql, dog_params)
                    new_dog_id = result.fetchone()[0]
                    MAP_DOGS[old_dog_id] = new_dog_id
                except Exception as e:
                    skipped_items["dogs"].append(f"Hund {d['name']} Fehler: {e}")

        # ---------------------------------------------------------
        # SCHRITT C: TRANSACTIONS
        # ---------------------------------------------------------
        if 'transactions' in data:
            print(f"💰 Importiere {len(data['transactions'])} Transaktionen...")
            for t in data['transactions']:
                old_tx_id = t['id']
                old_user_id = t['user_id']
                old_booked_by = t.get('booked_by_id')

                # 1. Neuen User finden
                new_user_id = MAP_USERS.get(old_user_id)
                if not new_user_id:
                    skipped_items["transactions"].append(f"TX {old_tx_id}: User {old_user_id} nicht gefunden.")
                    continue

                # 2. Booked By Logik (Fix für NOT NULL Violation)
                new_booked_by_id = None
                if old_booked_by:
                    new_booked_by_id = MAP_USERS.get(old_booked_by)

                # FALLBACK: Wenn kein 'booked_by' da ist oder der User nicht gefunden wurde
                if not new_booked_by_id:
                    # Option A: Wir nehmen den FALLBACK_ADMIN_ID
                    new_booked_by_id = FALLBACK_ADMIN_ID

                    # Option B (Alternativ): Wenn es eine Selbstbuchung war, könnte man new_user_id nehmen
                    # new_booked_by_id = new_user_id

                tx_sql = text("""
                              INSERT INTO transactions (tenant_id, user_id, booked_by_id, date, type, description,
                                                        amount, balance_after, bonus, invoice_number)
                              VALUES (:tenant_id, :user_id, :booked_by_id, :date, :type, :description,
                                      :amount, :balance_after, :bonus, :invoice_number) RETURNING id
                              """)

                tx_params = {
                    "tenant_id": TARGET_TENANT_ID,
                    "user_id": new_user_id,
                    "booked_by_id": new_booked_by_id,  # Jetzt sicher nicht NULL
                    "date": t['date'],
                    "type": t['type'],
                    "description": t.get('description'),
                    "amount": t['amount'],
                    "balance_after": t['balance_after'],
                    "bonus": 0.0,
                    "invoice_number": None
                }

                try:
                    result = conn.execute(tx_sql, tx_params)
                    new_tx_id = result.fetchone()[0]
                    MAP_TRANSACTIONS[old_tx_id] = new_tx_id
                except Exception as e:
                    skipped_items["transactions"].append(f"TX {old_tx_id} Fehler: {e}")

        # ---------------------------------------------------------
        # SCHRITT D: ACHIEVEMENTS
        # ---------------------------------------------------------
        if 'achievements' in data:
            print(f"🏆 Importiere Achievements...")
            for a in data['achievements']:
                # Mappings auflösen
                old_user_id = a['user_id']
                old_dog_id = a.get('dog_id')
                old_tx_id = a.get('transaction_id')
                old_req_id = a.get('requirement_id')

                new_user_id = MAP_USERS.get(old_user_id)
                new_dog_id = MAP_DOGS.get(old_dog_id)  # Kann None sein, wenn dog_id null war
                new_tx_id = MAP_TRANSACTIONS.get(old_tx_id)  # Fix für Foreign Key Error
                new_type_id = ACHIEVEMENT_MAP.get(old_req_id)

                if not new_user_id:
                    skipped_items["achievements"].append(f"Achievement User {old_user_id} fehlt.")
                    continue

                if not new_type_id:
                    # Wenn wir den Typ nicht kennen, überspringen wir es oft besser als Fehler zu werfen
                    skipped_items["achievements"].append(f"Achievement Typ '{old_req_id}' unbekannt.")
                    continue

                # Wenn Transaction ID im Original da war, muss sie jetzt auch da sein.
                # Falls 'transaction_id' in DB nullable ist, ist None okay.
                # Falls NOT NULL, müssen wir aufpassen. Meistens sind Achievements optional mit TX verknüpft.
                # Im Fehlerlog stand aber FK Violation -> Also war sie gesetzt.

                if old_tx_id and not new_tx_id:
                    skipped_items["achievements"].append(f"Achievement verweist auf fehlende TX {old_tx_id}.")
                    continue

                ach_sql = text("""
                               INSERT INTO achievements (tenant_id, user_id, training_type_id, transaction_id,
                                                         date_achieved, is_consumed, dog_id)
                               VALUES (:tenant_id, :user_id, :training_type_id, :transaction_id,
                                       :date_achieved, :is_consumed, :dog_id)
                               """)

                ach_params = {
                    "tenant_id": TARGET_TENANT_ID,
                    "user_id": new_user_id,
                    "training_type_id": new_type_id,
                    "transaction_id": new_tx_id,  # Hier nutzen wir die neue ID!
                    "date_achieved": a['date_achieved'],
                    "is_consumed": a.get('is_consumed', False),
                    "dog_id": new_dog_id
                }

                try:
                    conn.execute(ach_sql, ach_params)
                except Exception as e:
                    skipped_items["achievements"].append(f"Achievement Fehler: {e}")

        # ---------------------------------------------------------
        # SCHRITT E: DOKUMENTE
        # ---------------------------------------------------------
        if 'documents' in data:
            print(f"📄 Importiere Dokumente...")
            for d in data['documents']:
                new_user_id = MAP_USERS.get(d['user_id'])
                if not new_user_id:
                    continue

                doc_sql = text("""
                               INSERT INTO documents (tenant_id, user_id, file_name, file_type, upload_date, file_path)
                               VALUES (:tenant_id, :user_id, :file_name, :file_type, :upload_date, :file_path)
                               """)

                try:
                    conn.execute(doc_sql, {
                        "tenant_id": TARGET_TENANT_ID,
                        "user_id": new_user_id,
                        "file_name": d['file_name'],
                        "file_type": d['file_type'],
                        "upload_date": d['upload_date'],
                        "file_path": d['file_path']
                    })
                except Exception as e:
                    skipped_items["documents"].append(f"Doc {d['file_name']} Fehler: {e}")

        print("\n✅ DATENBANK COMMIT...")
        # Das 'with engine.begin()' führt am Ende automatisch ein commit() aus, wenn kein Fehler auftrat.
        # Wenn wir hier sind, wird alles gespeichert.

    # --- BERICHT ---
    print("\n--- ZUSAMMENFASSUNG ÜBERSPRUNGENE DATEN ---")
    for category, items in skipped_items.items():
        if items:
            print(f"\n📂 {category.upper()} ({len(items)} Fehler/Übersprungen):")
            for item in items[:10]:  # Nur die ersten 10 zeigen, damit Konsole nicht explodiert
                print(f"  - {item}")
            if len(items) > 10:
                print(f"  ... und {len(items) - 10} weitere.")
        else:
            print(f"\n✅ {category.upper()}: Sauber durchgelaufen.")


if __name__ == "__main__":
    run_import()