import uuid
import datetime
from sqlalchemy import create_engine, text
# Für das Hashing eines Standard-Passworts, falls das alte nicht geht
# pip install bcrypt
import bcrypt

# ==========================================
# KONFIGURATION
# ==========================================
DATABASE_URL = "postgresql://postgres.ctsoisfxbhaynonnudua:U0Pn6bzjxYqNSFKy@aws-1-eu-west-1.pooler.supabase.com:6543/postgres"

# Wenn True, wird versucht, den alten Hash zu nutzen.
# Wenn False (oder wenn der Hash fehlschlägt), wird das TEMP_PASSWORD gesetzt.
TRY_KEEP_OLD_PASSWORD = True

# Standard-Passwort für User, deren alter Hash nicht kompatibel ist
TEMP_PASSWORD = "Hundeschule2025!"


# ==========================================
# SKRIPT
# ==========================================

def sync_auth_users():
    engine = create_engine(DATABASE_URL)

    # Bcrypt Hash für das Temp-Passwort vorbereiten
    temp_pw_bytes = TEMP_PASSWORD.encode('utf-8')
    salt = bcrypt.gensalt()
    temp_pw_hash = bcrypt.hashpw(temp_pw_bytes, salt).decode('utf-8')

    print("🚀 Starte Auth-Synchronisation...")

    with engine.connect() as conn:
        # 1. Hole alle User aus public.users, die noch keine auth_user_id haben
        # Wir holen auch den alten Hash (hashed_password)
        users = conn.execute(text("""
                                  SELECT id, email, hashed_password, name, role
                                  FROM public.users
                                  WHERE auth_id IS NULL
                                  """)).fetchall()

        print(f"🔍 Gefunden: {len(users)} User ohne Login-Verknüpfung.")

        success_count = 0

        for u in users:
            user_id = u[0]
            email = u[1]
            old_hash = u[2]
            name = u[3]
            role = u[4]  # z.B. 'admin' oder 'user'

            # Neue UUID für auth generieren
            new_uuid = str(uuid.uuid4())

            # Entscheidung: Welches Passwort nehmen wir?
            final_hash = temp_pw_hash
            password_source = "TEMP"

            if TRY_KEEP_OLD_PASSWORD and old_hash:
                # Einfacher Check: Sieht es aus wie Bcrypt? ($2a$, $2b$, $2y$)
                if old_hash.startswith('$2'):
                    final_hash = old_hash
                    password_source = "OLD"

            try:
                # 2. In auth.users einfügen
                # Wir setzen email_confirmed_at, damit sie sich sofort einloggen können
                insert_auth_sql = text("""
                                       INSERT INTO auth.users (instance_id,
                                                               id,
                                                               aud,
                                                               role,
                                                               email,
                                                               encrypted_password,
                                                               email_confirmed_at,
                                                               raw_app_meta_data,
                                                               raw_user_meta_data,
                                                               created_at,
                                                               updated_at,
                                                               is_super_admin)
                                       VALUES ('00000000-0000-0000-0000-000000000000',
                                               :uuid,
                                               'authenticated',
                                               'authenticated',
                                               :email,
                                               :password_hash,
                                               NOW(),
                                               :app_meta,
                                               :user_meta,
                                               NOW(),
                                               NOW(),
                                               FALSE)
                                       """)

                # App Meta Data: Hier können wir Rollen speichern, die Supabase Row Level Security (RLS) nutzen kann
                app_meta = '{"provider": "email", "providers": ["email"]}'

                # User Meta Data: Name speichern (wird oft im Frontend genutzt)
                user_meta = f'{{"name": "{name}"}}'

                conn.execute(insert_auth_sql, {
                    "uuid": new_uuid,
                    "email": email,
                    "password_hash": final_hash,
                    "app_meta": app_meta,
                    "user_meta": user_meta
                })

                # 3. Verknüpfung in public.users speichern
                update_public_sql = text("""
                                         UPDATE public.users
                                         SET auth_id = :uuid
                                         WHERE id = :id
                                         """)
                conn.execute(update_public_sql, {"uuid": new_uuid, "id": user_id})

                print(f"✅ User {email} angelegt (Passwort: {password_source}) -> UUID verknüpft.")
                success_count += 1

                conn.commit()  # Wichtig: Sofort speichern

            except Exception as e:
                print(f"❌ Fehler bei User {email}: {e}")
                conn.rollback()

    print(f"\n🎉 Fertig! {success_count} User erfolgreich mit Auth verknüpft.")


if __name__ == "__main__":
    # Sicherheitsabfrage
    print(f"Dieses Skript legt Auth-User an.")
    print(f"Standard-Passwort für nicht-kompatible Hashes: {TEMP_PASSWORD}")
    x = input("Starten? (y/n): ")
    if x.lower() == 'y':
        sync_auth_users()