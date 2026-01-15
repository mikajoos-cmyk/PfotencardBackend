# app/notification_service.py
import json
import resend
from pywebpush import webpush, WebPushException
from sqlalchemy.orm import Session
from . import models
from .config import settings

# Config laden
resend.api_key = settings.RESEND_API_KEY
VAPID_PRIVATE_KEY = settings.VAPID_PRIVATE_KEY
VAPID_CLAIMS = {"sub": "mailto:support@pfotencard.de"}

def get_html_template(tenant, title: str, body_content: str, action_url: str = None, action_text: str = "Anzeigen", details: dict = None):
    """
    Erstellt ein HTML-Template im Design der Supabase-Auth-Emails.
    Unterstützt jetzt auch strukturierte Details.
    """
    # Branding aus Tenant-Config holen
    branding = tenant.config.get("branding", {}) if tenant and tenant.config else {}
    primary_color = branding.get("primary_color", "#22C55E")
    logo_url = branding.get("logo_url", "https://pfotencard.de/logo.png")
    school_name = tenant.name if tenant else "Pfotencard"

    # Basis URL für Links
    base_url = f"https://{tenant.subdomain}.pfotencard.de" if tenant else "https://pfotencard.de"
    
    # Full Action URL berechnen
    full_action_url = action_url
    if action_url and action_url.startswith("/"):
        full_action_url = f"{base_url}{action_url}"

    # --- Details Block generieren (für "Mehr Infos") ---
    details_html = ""
    if details and isinstance(details, dict):
        rows = ""
        for label, value in details.items():
            rows += f"""
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #F1F5F9;">
                <span style="font-weight: 600; color: #475569;">{label}:</span>
                <span style="color: #0F172A;">{value}</span>
            </div>
            """
        
        details_html = f"""
        <div style="background-color: #F8FAFC; border-radius: 8px; padding: 20px; margin: 25px 0; text-align: left;">
            {rows}
        </div>
        """

    # --- Button Block generieren ---
    button_html = ""
    if full_action_url:
        button_html = f"""
        <div style="text-align: center; margin: 30px 0;">
          <a href="{full_action_url}" 
             style="background-color: {primary_color}; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 600; display: inline-block; font-family: 'Segoe UI', sans-serif;">
            {action_text}
          </a>
        </div>
        <p style="color: #94A3B8; font-size: 12px; text-align: center; margin-top: 30px;">
          Falls der Button nicht funktioniert, nutze diesen Link:<br>
          <a href="{full_action_url}" style="color: {primary_color}; word-break: break-all;">{full_action_url}</a>
        </p>
        """

    # --- Das finale Template ---
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #F8FAFC; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">
        <div style="background-color: #F8FAFC; padding: 40px 20px;">
          <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; padding: 40px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #E2E8F0;">
            
            <div style="text-align: center; margin-bottom: 30px;">
              <img src="{logo_url}" alt="{school_name}" style="height: 80px; object-fit: contain;">
            </div>

            <h2 style="color: #0F172A; text-align: center; margin-bottom: 20px; font-size: 24px; font-weight: 700;">{title}</h2>
            
            <div style="color: #64748B; font-size: 16px; line-height: 1.6; text-align: center;">
              {body_content}
            </div>

            {details_html}

            {button_html}

            <div style="border-top: 1px solid #E2E8F0; margin-top: 40px; padding-top: 20px; text-align: center;">
                <p style="color: #94A3B8; font-size: 12px; margin: 0;">
                  Diese Nachricht wurde von <strong>{school_name}</strong> gesendet.
                </p>
            </div>

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
            "icon": "/paw.png"
        })
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
    except WebPushException as ex:
        print(f"Push failed: {ex}")

def notify_user(db: Session, user_id: int, type: str, title: str, message: str, url: str = "/", details: dict = None):
    """
    Zentrale Funktion für Benachrichtigungen.
    
    :param details: Optionales Dictionary für strukturierte Daten (z.B. {"Datum": "12.10.2025", "Kurs": "Welpen"})
                    Wird in der E-Mail als schöne Liste angezeigt.
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        return

    # LOGIK: Kanäle bestimmen
    channels = []
    
    # Check Push Settings
    if getattr(user, "notif_push_overall", True):
        pref_field = f"notif_push_{type}"
        # Fallback falls der genaue Typ nicht existiert -> True
        if hasattr(user, pref_field) and getattr(user, pref_field, True):
            channels.append("push")
        elif not hasattr(user, pref_field):
            channels.append("push") # Default erlauben wenn Typ unbekannt
            
    # Check Email Settings
    if getattr(user, "notif_email_overall", True) and type != "chat":
        pref_field = f"notif_email_{type}"
        if hasattr(user, pref_field) and getattr(user, pref_field, True):
            channels.append("email")
        elif not hasattr(user, pref_field):
            channels.append("email")

    # 1. PUSH SENDEN
    if "push" in channels:
        subscriptions = db.query(models.PushSubscription).filter(
            models.PushSubscription.user_id == user.id
        ).all()
        
        for sub in subscriptions:
            sub_info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth}
            }
            send_push(sub_info, title, message, url)

    # 2. EMAIL SENDEN
    if "email" in channels:
        tenant = user.tenant
        if tenant and tenant.support_email:
            sender_email = "notifications@pfotencard.de" 
            # Hinweis: Wir müssen über verifizierte Domain senden, daher sender_email meist fix
            sender_name = tenant.name
        else:
            sender_email = "notifications@pfotencard.de"
            sender_name = "Pfotencard"
            
        html_content = get_html_template(
            tenant=tenant, 
            title=title, 
            body_content=message, 
            action_url=url, 
            details=details  # Hier übergeben wir die neuen Infos
        )
        
        try:
            resend.Emails.send({
                "from": f"{sender_name} <{sender_email}>",
                "to": user.email,
                "subject": title,
                "html": html_content
            })
        except Exception as e:
            print(f"Email failed: {e}")