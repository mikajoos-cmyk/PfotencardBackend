# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models

stripe.api_key = settings.STRIPE_SECRET_KEY

# --- HELPER FUNKTIONEN ---

def get_price_id(plan_name: str, cycle: str):
    plan = plan_name.lower()
    cycle = cycle.lower()
    
    prices = {
        "starter": {
            "monthly": settings.STRIPE_PRICE_ID_STARTER_MONTHLY,
            "yearly": settings.STRIPE_PRICE_ID_STARTER_YEARLY
        },
        "pro": {
            "monthly": settings.STRIPE_PRICE_ID_PRO_MONTHLY,
            "yearly": settings.STRIPE_PRICE_ID_PRO_YEARLY
        },
        "enterprise": { 
            "monthly": settings.STRIPE_PRICE_ID_ENTERPRISE_MONTHLY,
            "yearly": settings.STRIPE_PRICE_ID_ENTERPRISE_YEARLY
        }
    }
    if plan == 'verband': plan = 'enterprise'
    
    if plan in prices and cycle in prices[plan]:
        return prices[plan][cycle]
    return None

def extract_client_secret(obj):
    """Extrahiert sicher das Client Secret aus Invoice oder Subscription Objekten"""
    if not obj: return None
    try:
        # Direkter Zugriff
        if hasattr(obj, 'client_secret') and obj.client_secret: return obj.client_secret
        if isinstance(obj, dict) and obj.get('client_secret'): return obj['client_secret']
        
        # Via Payment Intent
        pi = obj.get('payment_intent') if isinstance(obj, dict) else getattr(obj, 'payment_intent', None)
        if pi:
            return pi.get('client_secret') if isinstance(pi, dict) else getattr(pi, 'client_secret', None)
    except Exception: 
        pass
    return None

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """
    Synchronisiert DB mit Stripe Subscription Objekt.
    Berechnet next_payment_amount live via Stripe API für maximale Genauigkeit.
    """
    try:
        is_dict = isinstance(subscription, dict)
        get = lambda k: subscription.get(k) if is_dict else getattr(subscription, k, None)
        
        sub_id = get('id')
        status = get('status')
        metadata = get('metadata') or {}
        
        # 1. ENDDATUM & KÜNDIGUNGSDATUM
        current_period_end = get('current_period_end')
        trial_end = get('trial_end')
        cancel_at = get('cancel_at')

        # Fallback: Datum aus Items (für flexible Billing)
        if not current_period_end:
            items = get('items')
            items_data = items.get('data') if isinstance(items, dict) else (items.data if items else [])
            max_end = 0
            if items_data:
                for item in items_data:
                    i_get = lambda k: item.get(k) if isinstance(item, dict) else getattr(item, k, None)
                    end = i_get('current_period_end')
                    if end and end > max_end:
                        max_end = end
            if max_end > 0:
                current_period_end = max_end

        # 2. STATUS & KÜNDIGUNGS-LOGIK
        tenant.stripe_subscription_id = sub_id
        tenant.stripe_subscription_status = status
        
        stripe_cancel_flag = get('cancel_at_period_end')
        
        # Wir setzen 'cancel_at_period_end' auf True, wenn gekündigt (Flag oder Datum)
        if stripe_cancel_flag or (cancel_at is not None):
            tenant.cancel_at_period_end = True
            final_end_date = cancel_at if cancel_at else current_period_end
            if final_end_date:
                tenant.subscription_ends_at = datetime.fromtimestamp(final_end_date, tz=timezone.utc)
        else:
            tenant.cancel_at_period_end = False
            # Reguläres Ende
            if status == 'trialing' and trial_end:
                 tenant.subscription_ends_at = datetime.fromtimestamp(trial_end, tz=timezone.utc)
            elif current_period_end:
                 tenant.subscription_ends_at = datetime.fromtimestamp(current_period_end, tz=timezone.utc)

        # 3. METADATEN (Plan Name)
        if metadata.get('plan_name'):
            tenant.plan = metadata.get('plan_name')

        # 4. AKTIV-STATUS
        if status in ['active', 'trialing', 'incomplete']:
            tenant.is_active = True
        elif status in ['canceled', 'unpaid', 'incomplete_expired']:
            # Hier könnte man den Zugriff sperren
            pass

        # 5. PREVIEW NÄCHSTE ZAHLUNG (Via Upcoming Invoice)
        tenant.next_payment_amount = 0.0
        tenant.next_payment_date = tenant.subscription_ends_at
        tenant.upcoming_plan = None
        
        # Nur berechnen, wenn Abo nicht komplett beendet ist
        if status not in ['canceled', 'incomplete_expired', 'unpaid']:
            try:
                # WICHTIG: Wir fragen Stripe, was als nächstes passiert.
                # Das berücksichtigt Guthaben, Schedules (Downgrades) und Prorations.
                upcoming = stripe.Invoice.upcoming(
                    customer=tenant.stripe_customer_id, 
                    subscription=sub_id
                )
                
                # amount_due: Was der Kunde tatsächlich zahlen muss (nach Guthaben)
                tenant.next_payment_amount = upcoming.amount_due / 100.0
                
                # Datum der nächsten Rechnung
                if upcoming.next_payment_attempt:
                    tenant.next_payment_date = datetime.fromtimestamp(upcoming.next_payment_attempt, tz=timezone.utc)
                
                # Prüfen auf Plan-Wechsel (z.B. durch Schedule)
                if upcoming.lines and upcoming.lines.data:
                    next_price = upcoming.lines.data[0].price
                    if next_price and next_price.id:
                        # Hier nutzen wir eine Umkehr-Suche durch unsere Config
                        from .main import get_plan_name_from_price_id 
                        plan_name = get_plan_name_from_price_id(next_price.id)
                        if plan_name and plan_name != tenant.plan:
                            tenant.upcoming_plan = plan_name

            except Exception:
                # Upcoming Invoice call kann fehlschlagen, z.B. wenn gerade gekündigt wurde
                pass

        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(f"✅ Tenant {tenant.id} synced. Status: {status}, Next Amount: {tenant.next_payment_amount}")

    except Exception as e:
        print(f"❌ Error syncing tenant DB: {e}")
        # db.rollback()

# --- HAUPTFUNKTIONEN ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    target_price_id = get_price_id(plan, cycle)
    if not target_price_id:
        raise HTTPException(status_code=400, detail="Invalid plan configuration")

    # Customer erstellen/holen
    if not tenant.stripe_customer_id:
        try:
            customer = stripe.Customer.create(email=user_email, name=tenant.name, metadata={"tenant_id": tenant.id})
            tenant.stripe_customer_id = customer.id
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Stripe Customer Error: {e}")
            
    customer_id = tenant.stripe_customer_id
    active_subscription = None
    
    # Aktives Abo suchen
    if tenant.stripe_subscription_id:
        try:
            # Wir laden das Abo neu, um sicherzustellen, dass 'schedule' aktuell ist
            active_subscription = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            if active_subscription.status not in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = None
        except: 
            pass

    # --- UPDATE / UPGRADE / DOWNGRADE ---
    if active_subscription:
        print(f"🔄 Modifying Subscription {active_subscription.id}")
        try:
            current_item = active_subscription['items']['data'][0]
            current_price_id = current_item.price.id
            
            if current_price_id == target_price_id:
                return {"status": "updated", "message": "Plan already active"}

            new_price_obj = stripe.Price.retrieve(target_price_id)
            new_amount = new_price_obj.unit_amount or 0
            current_amount = current_item.price.unit_amount or 0
            
            is_upgrade = new_amount > current_amount
            is_trial = active_subscription.status == 'trialing'
            
            # --- FALL 1: UPGRADE (Sofort) ---
            if is_upgrade or is_trial:
                # FIX: Wenn bereits ein Schedule existiert (z.B. geplanter Downgrade),
                # muss dieser aufgelöst werden, da wir sonst das Abo nicht modifizieren können.
                if active_subscription.schedule:
                    print(f"Releasing existing schedule {active_subscription.schedule} before upgrade...")
                    stripe.SubscriptionSchedule.release(active_subscription.schedule)
                    # Kurz warten oder Objekt neu laden ist meist nicht nötig, da release synchron ist

                # SCHRITT 1: Preisänderung & Zahlung (ohne Metadata!)
                updated_sub_step1 = stripe.Subscription.modify(
                    active_subscription.id,
                    items=[{'id': current_item.id, 'price': target_price_id}],
                    proration_behavior='always_invoice', # Differenz berechnen
                    payment_behavior='pending_if_incomplete', # Sicherstellen, dass gezahlt wird
                    expand=['latest_invoice.payment_intent']
                    # WICHTIG: Metadata und cancel_at_period_end hier NICHT setzen!
                )
                
                # SCHRITT 2: Metadaten & Status nachziehen (separater Call)
                updated_sub_step2 = stripe.Subscription.modify(
                    active_subscription.id,
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False
                )
                
                # DB Update
                update_tenant_from_subscription(db, tenant, updated_sub_step2)
                
                client_secret = extract_client_secret(updated_sub_step1.latest_invoice)
                
                return {
                    "subscriptionId": updated_sub_step1.id,
                    "clientSecret": client_secret,
                    "status": "updated",
                    "nextPaymentAmount": tenant.next_payment_amount
                }
            
            # --- FALL 2: DOWNGRADE (Zum Ende) ---
            else:
                # 1. Schedule holen oder erstellen
                sched_id = active_subscription.schedule
                
                # Wir stellen sicher, dass wir auf einem sauberen Stand sind
                if not sched_id:
                    # Erstellt Schedule mit aktueller Phase (bis Period End)
                    sched = stripe.SubscriptionSchedule.create(from_subscription=active_subscription.id)
                    sched_id = sched.id
                
                # 2. Schedule updaten
                stripe.SubscriptionSchedule.modify(
                    sched_id,
                    end_behavior='release', 
                    phases=[
                        {
                            # Phase 1: Aktueller Plan bis zum Ende der Periode
                            'start_date': 'now', 
                            'end_date': active_subscription.current_period_end,
                            'items': [{'price': current_price_id, 'quantity': 1}],
                            'metadata': active_subscription.metadata 
                        },
                        {
                            # Phase 2: Neuer Plan (Downgrade) ab nächster Periode
                            'start_date': active_subscription.current_period_end,
                            'items': [{'price': target_price_id, 'quantity': 1}],
                            'metadata': {"tenant_id": tenant.id, "plan_name": plan} 
                        }
                    ]
                )
                
                # Manuelles Update für Frontend-Feedback ("Wechsel vorgemerkt")
                tenant.upcoming_plan = plan
                db.commit()
                
                # FIX: DB sofort aktualisieren, damit 'next_payment_amount' den neuen (niedrigeren) Preis anzeigt
                # Da der Schedule jetzt bei Stripe aktiv ist, liefert 'upcoming invoice' die korrekten Daten für Phase 2.
                active_subscription = stripe.Subscription.retrieve(active_subscription.id)
                update_tenant_from_subscription(db, tenant, active_subscription)
                
                return {
                    "subscriptionId": active_subscription.id,
                    "status": "success", 
                    "message": "Downgrade zum Ende des Zeitraums vorgemerkt."
                }

        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Update failed: {str(e)}")

    # --- NEUANLAGE (Checkout) ---
    else:
        print("✨ Creating NEW Subscription")
        trial_days = 0
        if tenant.subscription_ends_at and tenant.subscription_ends_at > datetime.now(timezone.utc):
            delta = tenant.subscription_ends_at - datetime.now(timezone.utc)
            trial_days = min(delta.days + 1, 14)

        sub_data = {
            'customer': customer_id,
            'items': [{"price": target_price_id}],
            'payment_behavior': 'default_incomplete',
            'payment_settings': {'save_default_payment_method': 'on_subscription'},
            'expand': ['latest_invoice.payment_intent', 'pending_setup_intent'],
            'metadata': {"tenant_id": tenant.id, "plan_name": plan}
        }
        if trial_days > 0: sub_data['trial_period_days'] = trial_days

        try:
            sub = stripe.Subscription.create(**sub_data)
            update_tenant_from_subscription(db, tenant, sub)
            client_secret = extract_client_secret(sub.latest_invoice) or extract_client_secret(sub.pending_setup_intent)
            
            return {
                "subscriptionId": sub.id,
                "clientSecret": client_secret,
                "status": "created",
                "nextPaymentAmount": tenant.next_payment_amount
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Create failed: {str(e)}")

# ... (restliche Funktionen bleiben gleich) ...

def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    try:
        sub = stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=True)
        update_tenant_from_subscription(db, tenant, sub)
        return {"message": "Cancelled"}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

def get_billing_portal_url(db: Session, tenant_id: int, return_url: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id: raise HTTPException(status_code=400, detail="No customer")
    session = stripe.billing_portal.Session.create(customer=tenant.stripe_customer_id, return_url=return_url)
    return {"url": session.url}

def get_subscription_details(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: return None
    return {
        "plan": tenant.plan,
        "status": tenant.stripe_subscription_status,
        "cancel_at_period_end": tenant.cancel_at_period_end,
        "current_period_end": tenant.subscription_ends_at,
        "next_payment_amount": tenant.next_payment_amount,
        "next_payment_date": tenant.next_payment_date,
        "upcoming_plan": tenant.upcoming_plan
    }

def get_invoices(db: Session, tenant_id: int, limit: int = 12):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id: return []
    try:
        invoices = stripe.Invoice.list(customer=tenant.stripe_customer_id, limit=limit)
        results = []
        for i in invoices.data:
            results.append({
                "id": i.id,
                "number": i.number,
                "created": datetime.fromtimestamp(i.created, tz=timezone.utc),
                "amount": i.total / 100.0,
                "status": i.status,
                "pdf_url": i.invoice_pdf,
                "hosted_url": i.hosted_invoice_url 
            })
        return results
    except: return []