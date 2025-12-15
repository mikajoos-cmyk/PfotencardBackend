# Technische Spezifikation: Migration zu Multi-Tenant SaaS

## 1. Datenbank-Migration & Strategie

Die Migration erfolgt in mehreren Phasen, um die Datenintegrität für den bestehenden Betrieb (Single-Tenant) zu wahren und gleichzeitig die neue Struktur einzuführen.

### Ziel-Schema Analyse
Das Zielschema führt eine strikte Trennung der Mandanten durch eine `tenant_id` in fast allen Tabellen ein. Konfigurationen (Level, Leistungen), die bisher hardcodiert waren, werden nun in `TrainingType`, `Level` und `LevelRequirement` gespeichert.

### Migrationsstrategie "Default Tenant"

Da wir bereits aktive Nutzer haben, können wir nicht einfach mit einer leeren Datenbank starten. Wir definieren den aktuellen Betrieb als den ersten Mandanten.

1.  **Default Tenant erstellen**: Wir legen einen Tenant "Pfotencard Original" (oder den Namen der aktuellen Hundeschule) an.
2.  **Daten zuordnen**: Alle bestehenden Zeilen in `users`, `dogs`, etc. erhalten die ID dieses Default-Tenants.
3.  **Konfiguration migrieren**: Die hardcodierten Dictionaries aus `crud.py` (`LEVEL_REQUIREMENTS`, `DOGLICENSE_PREREQS`) werden in die neuen Tabellen `training_types` und `levels` für den Default-Tenant insertiert.

### Anpassungen im Migrations-Skript (alembic oder SQL)

Die Migration sollte folgende Schritte umfassen:

1.  **Neue Tabellen erstellen**: `tenants`, `training_types`, `levels`, `level_requirements`.
2.  **Tenant-Record anlegen**: Insert 'Default School' -> ID merken (z.B. 1).
3.  **Schema-Updates bestehender Tabellen**:
    *   Spalte `tenant_id` zu `users`, `dogs`, `transactions`, `achievements`, `documents` hinzufügen (nullable vorerst).
    *   **Data Migration**: `UPDATE users SET tenant_id = 1 WHERE tenant_id IS NULL;` (und für alle anderen Tabellen).
    *   **Constraints**: `ALTER TABLE ... ALTER COLUMN tenant_id SET NOT NULL;` + Foreign Keys hinzufügen.
4.  **Seed-Script für Konfiguration**:
    *   Ein Python-Script, das die `LEVEL_REQUIREMENTS` aus `crud.py` liest und entsprechende `TrainingType`- und `LevelRequirement`-Einträge für Tenant 1 in der DB erstellt.

---

## 2. Backend-Architektur (FastAPI)

### Tenant-Resolution (Erkennung der Hundeschule)

Wie erkennt das Backend, welche Schule gemeint ist?
Wir nutzen primär den **Host-Header** (Subdomain) für die Erkennung, unterstützen aber fallback-weise einen Header `X-Tenant-ID` für einfacheres Testen/Entwicklung.

*   **Middleware**: Eine Middleware extrahiert die Subdomain (z.B. `bello` aus `bello.pfotencard.de`).
*   **Dependency `get_current_tenant`**:
    *   Prüft, ob der Tenant in der DB existiert.
    *   Speichert das Tenant-Objekt im `request.state`.
    *   Wirft 404, wenn die Subdomain unbekannt ist.

### Dependency Injection & Scoping

Jeder Endpoint, der Daten manipuliert, benötigt ab jetzt `current_tenant`.

```python
# Pseudo-Code
def get_current_tenant(request: Request, db: Session = Depends(get_db)) -> models.Tenant:
    # 1. Check Header X-Tenant-ID (Dev mode)
    # 2. Check Subdomain
    # 3. Query DB
    tenant = db.query(models.Tenant).filter(...).first()
    if not tenant: raise HTTPException(404, "School not found")
    return tenant
```

### Authentifizierung & User-Scope

Ein User gehört nun strikt zu **einem** Tenant.
*   **Unique Constraint**: `email` ist nicht mehr global unique, sondern `(email, tenant_id)` muss unique sein. Das erfordert eine Anpassung des `User`-Models und der DB-Constraints.
*   **Login**:
    *   Der Login-Endpoint `/api/login` muss wissen, für welchen Tenant der Login versucht wird (via Subdomain/Header).
    *   `get_user_by_email` sucht dann: `SELECT * FROM users WHERE email = ? AND tenant_id = ?`.
    *   Das Token sollte die `tenant_id` im Payload (Claims) enthalten, um bei nachfolgenden Requests sicherzustellen, dass das Token zum angefragten Tenant passt.

### Dynamische Konfiguration (Refactoring `crud.py`)

Die Logik in `crud.py` muss massiv angepasst werden.
*   **Wegfall**: `LEVEL_REQUIREMENTS` Dictionary.
*   **Neu**: `check_level_requirements(db, user, target_level)` Funktion.
    *   Lädt `user.tenant.levels`.
    *   Prüft `LevelRequirement` DB-Einträge gegen die `user.achievements`.
    *   Die IDs für Achievements ("group_class") müssen nun Matches auf `training_types.id` (oder einem Code-Feld) sein. *Empfehlung*: Wir fügen `TrainingType.code` (String) hinzu oder nutzen die generierte ID. Da die Migration bestehende String-IDs in der `achievements` Tabelle hat ("group_class"), sollten wir `TrainingType` um ein Feld `internal_id` oder `code` erweitern, oder die Migration mappt die alten Strings auf die neuen IDs.
    *   *Strategie*: Bestehende Achievements verweisen auf Strings. Wir migrieren diese Strings idealerweise auf ForeignKeys, oder wir behalten Strings bei, aber validieren sie gegen `TrainingType.name` oder `code`. Der Plan im Prompt nutzt `training_type_id` (Integer) in `Achievement`. Das ist sauberer.
    *   **Achtung**: Das bedeutet, wir müssen auch die bestehende `achievements` Tabelle migrieren: `UPDATE achievements SET training_type_id = (SELECT id FROM training_types WHERE code = achievements.requirement_id ...)`.

---

## 3. Marketing-Website (Frontend)

Die Marketing-Seite (`pfotencard-marketing-website`) hat nun einen neuen Zweck: B2B-Verkauf an Hundeschulen.

1.  **Registrierungs-Flow (Neue Schule)**:
    *   Formular: Name der Schule, gewünschte Subdomain, Admin-Email, Passwort.
    *   API-Call: `POST /api/tenants/register`
        *   Erstellt Tenant.
        *   Erstellt Admin-User für diesen Tenant.
        *   Erstellt Standard-Konfiguration (Default TrainingTypes/Levels), damit die Schule nicht leer startet.
2.  **Login-Flow**:
    *   Seite fragt nach "Ihre Hundeschul-URL" oder bietet Login per E-Mail (Global Lookup -> Weiterleitung zur Subdomain).
    *   Einfachste Lösung: Der User muss auf `seine-schule.pfotencard.de` gehen, um sich einzuloggen. Die Marketing-Seite hat nur einen "Login"-Button, der nach der Subdomain fragt und dann dorthin weiterleitet.

---

## 4. App Frontend

Die App muss "Tenant-Aware" werden.

1.  **Initial Load**:
    *   Beim Laden von `app.tsx` wird `GET /api/config` aufgerufen.
    *   Response: `{ tenant_name: "Bello Akademie", branding: {...}, training_types: [...] }`.
    *   Das Frontend setzt CSS-Variablen (Farben) und Texte (Schulname) basierend auf dieser Config.
2.  **API Context**:
    *   Der Axios/Fetch-Client muss keine explizite Tenant-ID senden, wenn wir Subdomains nutzen (der Browser sendet den Host-Header automatisch).
    *   Falls wir Headers nutzen, muss der Interceptor den Header setzen.
3.  **Dynamische UI**:
    *   Statt harter Level-Logik ("Du brauchst noch 5 Gruppenstunden") muss die UI die Anforderungen aus der API rendern: "Du brauchst noch {count} x {training_name}".

---

## 5. Schritt-für-Schritt Umsetzungsplan (Tasks)

### Phase 1: Datenbank & Backend Core (Das Fundament)

1.  [ ] **DB-Modelle erstellen**: `models.py` aktualisieren mit `Tenant`, `TrainingType`, `Level`, `LevelRequirement`. Relationen definieren.
2.  [ ] **Alembic/Migration Script erstellen**:
    *   Tabellen anlegen.
    *   Default-Tenant "Original" seeden.
    *   Bestehende Daten auf Default-Tenant migrieren (SQL UPDATEs).
    *   Neue Spalten `training_type_id` in Achievements füllen (Mapping von alten String-IDs).
    *   Foreign Keys und NOT NULL Constraints setzen.
3.  [ ] **Backend Models Refactoring**: `User`, `Dog`, etc. in `models.py` aktualisieren (wie in der Vorgabe).
4.  [ ] **Config API**: Endpoint `GET /api/config` erstellen, der Public-Tenant-Infos liefert (Name, Logo, Levels).

### Phase 2: Refactoring Logik (Weg vom Hardcoding)

5.  [ ] **Dependency Injection**: `get_current_tenant` Dependency implementieren.
6.  [ ] **Auth Anpassung**: `login` Endpoint und `get_current_user` so anpassen, dass sie tenant-scoped suchen. `User.email` Constraint prüfen.
7.  [ ] **CRUD Refactoring**:
    *   `LEVEL_REQUIREMENTS` Konstante löschen.
    *   Logik `are_prerequisites_met_for_exam` umschreiben: DB-Abfrage statt Dictionary-Lookup.
    *   Achievement-Erstellung: Validierung gegen `TrainingTypes` des Tenants.
    *   Transaction Bonus-Logik: Optional konfigurierbar machen oder vorerst Standard lassen (aber tenant-isoliert).

### Phase 3: Tenant-Management (Admin Funktionen)

8.  [ ] **Tenant-Registration Endpoint**: `POST /api/register-school`. Erstellt Tenant + Admin + Default-Config.
9.  [ ] **Schul-Admin API**: Endpoints, damit der Schul-Admin seine Levels und Leistungen bearbeiten kann (`CRUD TrainingType`, `CRUD Level`).

### Phase 4: Frontend Integration

10. [ ] **App Frontend Config-Vorbereitung**: `App.tsx` so umbauen, dass erst die Config geladen wird, bevor gerendert wird.
11. [ ] **Login Redirect**: Wenn ein User nicht eingeloggt ist -> Login Page. Nach Login -> Token speichern.
12. [ ] **Dynamische Level-Anzeige**: Das User-Profil "Nächstes Level" Widget generisch machen, basierend auf API-Daten.
13. [ ] **Migration Marketing-Seite**: Registrierungsformular für neue Schulen bauen.

### Phase 5: Cleanup & Testing

14. [ ] **Verify Default Tenant**: Prüfen, ob die alte App (Original-Daten) weiterhin 1:1 funktioniert wie vorher.
15. [ ] **Verify New Tenant**: Neue Schule anlegen, User anlegen, Leistungen buchen – prüfen ob Daten strikt getrennt bleiben.
