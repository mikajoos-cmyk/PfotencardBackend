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
    """
    branding = tenant.config.get("branding", {}) if tenant and tenant.config else {}
    primary_color = branding.get("primary_color", "#22C55E")
    logo_url = branding.get("logo_url", "https://pfotencard.de/logo.png")
    school_name = tenant.name if tenant else "Pfotencard"

    base_url = f"https://{tenant.subdomain}.pfotencard.de" if tenant else "https://pfotencard.de"
    
    full_action_url = action_url
    if action_url and action_url.startswith("/"):
        full_action_url = f"{base_url}{action_url}"

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

def send_push(db: Session, subscription: models.PushSubscription, title: str, message: str, url: str):
    """Sendet VAPID Push und loggt detailliert."""
    print(f"DEBUG [Push]: Versuch an Endpoint ...{subscription.endpoint[-20:] if subscription.endpoint else 'Unknown'}")
    
    try:
        payload = json.dumps({
            "title": title,
            "body": message,
            "url": url,
            "icon": "/paw.png"
        })
        
        # Test: Keys prüfen
        if not subscription.p256dh or not subscription.auth:
            print(f"DEBUG [Push]: FEHLER - Fehlende Keys für Subscription ID {subscription.id}")
            return

        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}
            },
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
        print("DEBUG [Push]: ✅ Erfolgreich an Push-Service übergeben.")
        
    except WebPushException as ex:
        print(f"DEBUG [Push]: ❌ WebPushException: {ex}")
        
        # Check auf Status 410 (Gone) -> Abo löschen
        if ex.response is not None and ex.response.status_code == 410:
            print(f"DEBUG [Push]: Abo ist abgelaufen (410). Lösche Subscription ID {subscription.id} aus DB.")
            try:
                db.delete(subscription)
                db.commit()
            except Exception as db_err:
                print(f"DEBUG [Push]: DB Fehler beim Löschen: {db_err}")
                db.rollback()
                
    except Exception as e:
        print(f"DEBUG [Push]: ❌ Generischer Fehler: {str(e)}")

def notify_user(db: Session, user_id: int, type: str, title: str, message: str, url: str = "/", details: dict = None):
    """
    Zentrale Funktion mit Debugging.
    """
    print(f"DEBUG [Notify]: Starte für User ID {user_id}, Typ '{type}'")
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        print(f"DEBUG [Notify]: User ID {user_id} nicht gefunden.")
        return
    if not user.is_active:
        print(f"DEBUG [Notify]: User {user.email} ist inaktiv.")
        return

    channels = []
    
    # 1. Check Push Settings
    push_overall = getattr(user, "notif_push_overall", True)
    pref_field_push = f"notif_push_{type}"
    push_specific = getattr(user, pref_field_push, True) if hasattr(user, pref_field_push) else True
    
    print(f"DEBUG [Notify]: Push Check -> Overall: {push_overall}, Specific ({pref_field_push}): {push_specific}")
    
    if push_overall and push_specific:
        channels.append("push")
            
    # 2. Check Email Settings
    email_overall = getattr(user, "notif_email_overall", True)
    pref_field_email = f"notif_email_{type}"
    email_specific = getattr(user, pref_field_email, True) if hasattr(user, pref_field_email) else True
    
    print(f"DEBUG [Notify]: Email Check -> Overall: {email_overall}, Specific ({pref_field_email}): {email_specific}")

    if email_overall and email_specific and type != "chat":
        channels.append("email")

    print(f"DEBUG [Notify]: Gewählte Kanäle: {channels}")

    # --- PUSH SENDEN ---
    if "push" in channels:
        subscriptions = db.query(models.PushSubscription).filter(
            models.PushSubscription.user_id == user.id
        ).all()
        
        print(f"DEBUG [Notify]: Gefundene Push-Abos in DB: {len(subscriptions)}")
        
        if len(subscriptions) == 0:
            print("DEBUG [Notify]: User hat Push aktiviert, aber keine Geräte registriert (Tabelle push_subscriptions leer für diesen User).")
        
        for sub in subscriptions:
            send_push(db, sub, title, message, url)

    # --- EMAIL SENDEN ---
    if "email" in channels:
        tenant = user.tenant
        sender_email = "notifications@pfotencard.de"
        sender_name = tenant.name if tenant else "Pfotencard"
            
        html_content = get_html_template(
            tenant=tenant, 
            title=title, 
            body_content=message, 
            action_url=url, 
            details=details
        )
        
        try:
            resend.Emails.send({
                "from": f"{sender_name} <{sender_email}>",
                "to": user.email,
                "subject": title,
                "html": html_content
            })
            print(f"DEBUG [Notify]: Email an {user.email} gesendet.")
        except Exception as e:
            print(f"DEBUG [Notify]: Email failed: {e}")