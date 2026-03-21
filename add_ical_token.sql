ALTER TABLE users ADD COLUMN IF NOT EXISTS ical_token TEXT UNIQUE;

-- Initialisiere Token für bestehende Nutzer, falls gewünscht. 
-- Da es ein geheimes Token sein soll, generieren wir hier UUIDs.
UPDATE users SET ical_token = gen_random_uuid()::text WHERE ical_token IS NULL;
