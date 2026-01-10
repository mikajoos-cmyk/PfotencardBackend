import logging
from supabase import Client
from .config import settings
from supabase import create_client

logger = logging.getLogger("pfotencard")

# Initialize client (same as in main.py but avoiding circular imports)
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

def delete_file_from_storage(supabase: Client, bucket_name: str, file_path: str):
    """
    Löscht eine Datei physisch aus dem Supabase Storage.
    Wird aufgerufen, bevor der DB-Eintrag gelöscht wird.
    """
    try:
        # Prüfen ob Pfad existiert, um Fehler zu vermeiden
        if not file_path:
            return

        # Supabase storage.remove erwartet eine Liste von Pfaden
        response = supabase.storage.from_(bucket_name).remove([file_path])
        
        # Einfache Prüfung ob erfolgreich (Supabase wirft nicht immer Errors bei 'nicht gefunden')
        if response:
            logger.info(f"Storage Cleanup: File {file_path} deleted from {bucket_name}.")
        else:
            logger.warning(f"Storage Cleanup: Could not delete {file_path}.")
            
    except Exception as e:
        # Wir loggen den Fehler, brechen aber den Löschvorgang der DB nicht ab,
        # damit der User nicht blockiert wird. Die Datei wird zur "Waisendatei" (Edge Case).
        logger.error(f"Storage Cleanup Error for {file_path}: {e}")

def delete_folder_from_storage(supabase: Client, bucket_name: str, folder_path: str):
    """
    Löscht alle Dateien in einem Ordner (z.B. alle Bilder eines Tenants).
    """
    try:
        files = supabase.storage.from_(bucket_name).list(folder_path)
        if files:
            file_paths = [f"{folder_path}/{f['name']}" for f in files]
            supabase.storage.from_(bucket_name).remove(file_paths)
            logger.info(f"Storage Cleanup: Folder {folder_path} cleared.")
    except Exception as e:
        logger.error(f"Storage Cleanup Error for folder {folder_path}: {e}")
