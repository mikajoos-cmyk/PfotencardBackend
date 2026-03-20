# app/notification_service.py
import requests
from sqlalchemy.orm import Session
from . import models
from .config import settings

def notify_user(db: Session, user_id: int = None, title: str = None, message: str = None, type: str = "news", details: dict = None, url: str = None, user: models.User = None):
    if not user:
        if user_id:
            user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            print(f"ERROR [Notify]: User nicht gefunden (ID: {user_id})")
            return
    return send_notification(db, user, type, title, message, url, details)

def send_notification(db: Session, user: models.User, type: str, title: str, message: str, url: str = None, details: dict = None):
    """
    Prüft die Berechtigungen des Users und delegiert den tatsächlichen Versand
    an die Supabase Edge Functions (send-email / send-push).
    """
    channels = []
    print(f"DEBUG [Notify]: Starte Prüfung für Typ '{type}' an User '{user.email}'")

    # --- BERECHTIGUNGEN PRÜFEN (E-Mail) ---
    if user.notif_email_overall:
        if type == "chat" and user.notif_email_chat: channels.append("email")
        elif type == "news" and user.notif_email_news: channels.append("email")
        elif type == "booking" and user.notif_email_booking: channels.append("email")
        elif type == "waitinglist_move": channels.append("email") # Immer E-Mail bei Wartelisten-Nachrücken
        elif type == "reminder" and user.notif_email_reminder: channels.append("email")
        elif type == "alert" and user.notif_email_alert: channels.append("email")
        elif type == "homework" and user.notif_email_news: channels.append("email") # Hausaufgaben nutzen News-Einstellung als Fallback

    # --- BERECHTIGUNGEN PRÜFEN (Push) ---
    if user.notif_push_overall:
        if type == "chat" and user.notif_push_chat: channels.append("push")
        elif type == "news" and user.notif_push_news: channels.append("push")
        elif type == "booking" and user.notif_push_booking: channels.append("push")
        elif type == "waitinglist_move": channels.append("push") # Auch Push bei Warteliste
        elif type == "reminder" and user.notif_push_reminder: channels.append("push")
        elif type == "alert" and user.notif_push_alert: channels.append("push")
        elif type == "homework" and user.notif_push_news: channels.append("push") # Hausaufgaben nutzen News-Einstellung als Fallback

    print(f"DEBUG [Notify]: Gewählte Kanäle nach Berechtigungs-Prüfung: {channels}")

    tenant_name = user.tenant.name if user.tenant else "Pfotencard"

    # Headers für den sicheren Aufruf der Edge Functions
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json"
    }

    # --- E-MAIL VIA EDGE FUNCTION SENDEN ---
    if "email" in channels:
        email_payload = {
            "to": user.email,
            "userName": user.vorname or user.name,
            "tenantName": tenant_name,
            "type": type,
            "title": title,
            "message": message,
            "url": url,
            "details": details
        }
        try:
            # Sende asynchron oder synchron an die Edge Function
            res = requests.post(
                f"{settings.SUPABASE_URL}/functions/v1/send-email",
                json=email_payload,
                headers=headers,
                timeout=5
            )
            print(f"DEBUG [Notify]: E-Mail Edge Function Status: {res.status_code}")
        except Exception as e:
            print(f"Fehler beim Aufruf der E-Mail Edge Function: {e}")

    # --- PUSH VIA EDGE FUNCTION SENDEN ---
    if "push" in channels:
        push_payload = {
            "user_id": user.id,
            "title": title,
            "body": message,
            "url": url
        }
        try:
            res = requests.post(
                f"{settings.SUPABASE_URL}/functions/v1/send-push",
                json=push_payload,
                headers=headers,
                timeout=5
            )
            print(f"DEBUG [Notify]: Push Edge Function Status: {res.status_code}")
        except Exception as e:
            print(f"Fehler beim Aufruf der Push Edge Function: {e}")