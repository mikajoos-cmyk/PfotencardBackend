# app/notification_service.py
import os
import json
import resend
from pywebpush import webpush, WebPushException
from sqlalchemy.orm import Session
from . import models

from .config import settings

# Config laden
resend.api_key = settings.RESEND_API_KEY
# VAPID Keys müssen generiert werden (z.B. https://vapidkeys.com/)
VAPID_PRIVATE_KEY = settings.VAPID_PRIVATE_KEY
VAPID_CLAIMS = {"sub": "mailto:support@pfotencard.de"}

def get_html_template(tenant, title: str, body_content: str, action_url: str = None, action_text: str = "Anzeigen"):
    """
    Erstellt ein schönes HTML-Template im Supabase-Stil mit Tenant-Branding.
    """
    # Branding aus Tenant-Config holen
    branding = tenant.config.get("branding", {})
    primary_color = branding.get("primary_color", "#22C55E")
    logo_url = branding.get("logo_url", "https://pfotencard.de/logo.png")
    school_name = tenant.name

    # Basis URL für Links (z.B. https://bello.pfotencard.de)
    base_url = f"https://{tenant.subdomain}.pfotencard.de"
    
    # Falls action_url relativ ist (startet mit /), Subdomain davor hängen
    full_action_url = action_url
    if action_url and action_url.startswith("/"):
        full_action_url = f"{base_url}{action_url}"

    # Einfaches, responsives HTML Template
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f9fafb; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 40px auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border: 1px solid #e5e7eb; }}
            .header {{ padding: 32px; text-align: center; border-bottom: 1px solid #f3f4f6; }}
            .logo {{ width: 64px; height: 64px; object-fit: contain; }}
            .content {{ padding: 32px; color: #374151; line-height: 1.6; }}
            .h1 {{ font-size: 20px; font-weight: 600; color: #111827; margin-bottom: 16px; }}
            .button {{ display: inline-block; background-color: {primary_color}; color: #ffffff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 500; margin-top: 24px; }}
            .footer {{ padding: 24px; text-align: center; font-size: 12px; color: #9ca3af; background-color: #f9fafb; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <img src="{logo_url}" alt="{school_name}" class="logo" />
            </div>
            <div class="content">
                <div class="h1">{title}</div>
                <div>{body_content}</div>
                {f'<a href="{full_action_url}" class="button">{action_text}</a>' if full_action_url else ''}
            </div>
            <div class="footer">
                &copy; {school_name} via PfotenCard
            </div>
        </div>
    </body>
    </html>
    """

def send_push(subscription_info: dict, title: str, message: str, url: str):
    """Sendet VAPID Push"""
    try:
        payload = json.dumps({
            "title": title,
            "body": message,
            "url": url,
            "icon": "/paw.png" # Optional: Tenant Logo URL hier rein
        })
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
    except WebPushException as ex:
        # User hat Subscription widerrufen -> Sollte in DB gelöscht werden (TODO)
        print(f"Push failed: {ex}")

def notify_user(db: Session, user_id: int, type: str, title: str, message: str, url: str = "/"):
    """
    Zentrale Funktion: Entscheidet über Kanäle basierend auf 'type' und User-Präferenzen.
    type: 'chat', 'system', 'booking', 'alert'
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        return

    # LOGIK: Kanäle bestimmen basierend auf User-Einstellungen
    channels = []
    
    # Check Push Settings
    if getattr(user, "notif_push_overall", True):
        # Mappe den Typ auf das entsprechende Datenbank-Feld
        pref_field = f"notif_push_{type}"
        if getattr(user, pref_field, True):
            channels.append("push")
            
    # Check Email Settings
    if getattr(user, "notif_email_overall", True) and type != "chat":
        # Mappe den Typ auf das entsprechende Datenbank-Feld
        pref_field = f"notif_email_{type}"
        if getattr(user, pref_field, True):
            channels.append("email")

    # 1. PUSH SENDEN
    if "push" in channels:
        # Alle Geräte des Users laden
        subscriptions = db.query(models.PushSubscription).filter(
            models.PushSubscription.user_id == user.id
        ).all()
        
        for sub in subscriptions:
            sub_info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth}
            }
            # URL muss für Push relativ oder absolut sein, Web Push öffnet sie im Browser
            # Wir nutzen hier den relativen Pfad, Service Worker macht den Rest
            send_push(sub_info, title, message, url)

    # 2. EMAIL SENDEN
    if "email" in channels:
        tenant = user.tenant
        if tenant and tenant.support_email:
            sender_name = tenant.name
        else:
            sender_name = "Pfotencard"
            
        html_content = get_html_template(tenant, title, message, url)
        
        try:
            resend.Emails.send({
                "from": f"{sender_name} <notifications@pfotencard.de>",
                "to": user.email,
                "subject": title,
                "html": html_content
            })
        except Exception as e:
            print(f"Email failed: {e}")