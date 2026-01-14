# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models

stripe.api_key = settings.STRIPE_SECRET_KEY

# --- HELPER: PREISE & CONFIG ---

def get_plan_config(plan_name: str, cycle: str):
    """Liefert Price-ID und Betrag (in EUR) für einen Plan zurück"""
    plan = plan_name.lower()
    cycle = cycle.lower()
    
    # Preise hardcodiert (für stabile 'next_payment' Berechnung ohne Stripe API Calls)
    # Beträge in Euro (Float) für DB, IDs für Stripe
    configs = {
        "starter": {
            "monthly": {"id": settings.STRIPE_PRICE_ID_STARTER_MONTHLY, "amount": 29.00},
            "yearly":  {"id": settings.STRIPE_PRICE_ID_STARTER_YEARLY, "amount": 290.00}
        },
        "pro": {
            "monthly": {"id": settings.STRIPE_PRICE_ID_PRO_MONTHLY, "amount": 79.00},
            "yearly":  {"id": settings.STRIPE_PRICE_ID_PRO_YEARLY, "amount": 790.00}
        },
        "enterprise": { 
            "monthly": {"id": settings.STRIPE_PRICE_ID_ENTERPRISE_MONTHLY, "amount": 199.00},
            "yearly":  {"id": settings.STRIPE_PRICE_ID_ENTERPRISE_YEARLY, "amount": 1990.00}
        }
    }
    # Alias
    if plan == 'verband': plan = 'enterprise'
    
    if plan in configs and cycle in configs[plan]:
        return configs[plan][cycle]
    return None

def get_price_id(plan_name: str, cycle: str):
    conf = get_plan_config(plan_name, cycle)
    return conf["id"] if conf else None

def extract_client_secret(obj):
    if not obj: return None
    try:
        if hasattr(obj, 'client_secret') and obj.client_secret: return obj.client_secret
        if isinstance(obj, dict) and obj.get('client_secret'): return obj['client_secret']
        
        pi = obj.get('payment_intent') if isinstance(obj, dict) else getattr(obj, 'payment_intent', None)
        if pi:
            return pi.get('client_secret') if isinstance(pi, dict) else getattr(pi, 'client_secret', None)
    except Exception: pass
    return None

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """
    Synchronisiert DB mit Stripe.
    Setzt 'next_payment_amount' strikt auf den Listenpreis des (kommenden) Plans.
    """
    try:
        is_dict = isinstance(subscription, dict)
        get = lambda k: subscription.get(k) if is_dict else getattr(subscription, k, None)
        
        sub_id = get('id')
        status = get('status')
        metadata = get('metadata') or {}
        
        # 1. ENDDATUM & KÜNDIGUNG
        current_period_end = get('current_period_end')
        trial_end = get('trial_end')
        cancel_at = get('cancel_at')

        # Fallback: Datum aus Items
        if not current_period_end:
            items = get('items')
            items_data = items.get('data') if isinstance(items, dict) else (items.data if items else [])
            max_end = 0
            if items_data:
                for item in items_data:
                    i_get = lambda k: item.get(k) if isinstance(item, dict) else getattr(item, k, None)
                    end = i_get('current_period_end')
                    if end and end > max_end: max_end = end
            if max_end > 0: current_period_end = max_end

        # Basis-Daten
        tenant.stripe_subscription_id = sub_id
        tenant.stripe_subscription_status = status
        
        # Kündigungsstatus (Flag oder Datum)
        stripe_cancel_flag = get('cancel_at_period_end')
        if stripe_cancel_flag or (cancel_at is not None):
            tenant.cancel_at_period_end = True
            final_end_date = cancel_at if cancel_at else current_period_end
            if final_end_date:
                tenant.subscription_ends_at = datetime.fromtimestamp(final_end_date, tz=timezone.utc)
        else:
            tenant.cancel_at_period_end = False
            if status == 'trialing' and trial_end:
                 tenant.subscription_ends_at = datetime.fromtimestamp(trial_end, tz=timezone.utc)
            elif current_period_end:
                 tenant.subscription_ends_at = datetime.fromtimestamp(current_period_end, tz=timezone.utc)

        # 2. PLAN UPDATE
        # Wir aktualisieren den Plan nur, wenn er explizit im Stripe-Objekt steht
        if metadata.get('plan_name'):
            tenant.plan = metadata.get('plan_name')

        if status in ['active', 'trialing', 'incomplete']:
            tenant.is_active = True

        # 3. NEXT PAYMENT & UPCOMING PLAN
        # Hier implementieren wir die gewünschte Logik: Immer den Preis des ZIEL-Plans anzeigen.
        
        # Welcher Plan gilt ab nächster Rechnung?
        target_plan_name = tenant.upcoming_plan if tenant.upcoming_plan else tenant.plan
        
        # Welcher Zyklus? (monthly/yearly)
        current_cycle = "monthly"
        try:
            items = get('items')
            data = items.get('data') if isinstance(items, dict) else (items.data if items else [])
            if data:
                item0 = data[0]
                i_get = lambda k: item0.get(k) if isinstance(item0, dict) else getattr(item0, k, None)
                plan_obj = i_get('plan')
                if plan_obj:
                    interval = plan_obj.get('interval') if isinstance(plan_obj, dict) else plan_obj.interval
                    if interval == 'year': current_cycle = "yearly"
        except: pass

        # Preis festlegen
        if not tenant.cancel_at_period_end and status not in ['canceled', 'incomplete_expired', 'unpaid']:
            price_conf = get_plan_config(target_plan_name, current_cycle)
            if price_conf:
                tenant.next_payment_amount = price_conf["amount"]
            else:
                tenant.next_payment_amount = 0.0 # Fallback
            
            tenant.next_payment_date = tenant.subscription_ends_at
        else:
            # Bei Kündigung keine weitere Zahlung
            tenant.next_payment_amount = 0.0
            tenant.next_payment_date = None
            tenant.upcoming_plan = None

        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(f"✅ Tenant synced. Plan: {tenant.plan}, Upcoming: {tenant.upcoming_plan}, Next: {tenant.next_payment_amount}€")

    except Exception as e:
        print(f"❌ Error syncing tenant DB: {e}")
        # db.rollback()

# --- CHECKOUT & SUBSCRIPTION MANAGEMENT ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")

    conf = get_plan_config(plan, cycle)
    if not conf: raise HTTPException(400, "Invalid plan")
    target_price_id = conf["id"]
    target_amount = conf["amount"]

    # Customer sicherstellen
    if not tenant.stripe_customer_id:
        try:
            c = stripe.Customer.create(email=user_email, name=tenant.name, metadata={"tenant_id": tenant.id})
            tenant.stripe_customer_id = c.id
            db.commit()
        except Exception as e: raise HTTPException(400, f"Stripe Error: {e}")
            
    customer_id = tenant.stripe_customer_id
    active_subscription = None
    
    # Abo laden
    if tenant.stripe_subscription_id:
        try:
            # WICHTIG: Abo immer frisch laden, damit wir 'schedule' field aktuell haben
            active_subscription = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            if active_subscription.status not in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = None
        except: 
            pass

    # --- FALL A: BESTEHENDES ABO ÄNDERN ---
    if active_subscription:
        print(f"🔄 Modifying Subscription {active_subscription.id}")
        try:
            current_item = active_subscription['items']['data'][0]
            
            # Aktuellen Preis prüfen
            current_stripe_price = current_item.price.unit_amount / 100.0
            
            # Entscheidung: Upgrade (teurer) oder Downgrade (billiger)?
            is_upgrade = target_amount > current_stripe_price
            is_trial = active_subscription.status == 'trialing'
            
            # --- UPGRADE: SOFORT ---
            if is_upgrade or is_trial:
                # 1. Bestehenden Schedule auflösen (falls vorhanden), um Konflikte zu vermeiden
                if active_subscription.schedule:
                    print(f"Releasing schedule {active_subscription.schedule} before upgrade...")
                    try:
                        stripe.SubscriptionSchedule.release(active_subscription.schedule)
                    except Exception as e:
                        print(f"Warning releasing schedule: {e}")

                # 2. Upgrade durchführen (Preisänderung)
                updated_sub_step1 = stripe.Subscription.modify(
                    active_subscription.id,
                    items=[{'id': current_item.id, 'price': target_price_id}],
                    proration_behavior='always_invoice',
                    payment_behavior='pending_if_incomplete',
                    expand=['latest_invoice.payment_intent']
                )
                
                # 3. Metadaten nachziehen (Plan Name aktualisieren)
                stripe.Subscription.modify(
                    active_subscription.id,
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False
                )
                
                # DB Update: Upcoming löschen (Upgrade ist sofort), Plan setzen
                tenant.upcoming_plan = None
                tenant.plan = plan 
                tenant.next_payment_amount = target_amount # Neuer Preis
                update_tenant_from_subscription(db, tenant, updated_sub_step1)
                
                return {
                    "subscriptionId": updated_sub_step1.id,
                    "clientSecret": extract_client_secret(updated_sub_step1.latest_invoice),
                    "status": "updated",
                    "nextPaymentAmount": target_amount
                }
            
            # --- DOWNGRADE: ZUM ENDE DER LAUFZEIT (SCHEDULE) ---
            else:
                sched_id = active_subscription.schedule
                
                # Versuch 1: Schedule erstellen
                if not sched_id:
                    try:
                        print("Creating new schedule from subscription...")
                        sched = stripe.SubscriptionSchedule.create(from_subscription=active_subscription.id)
                        sched_id = sched.id
                    except stripe.error.InvalidRequestError as e:
                        # Fallback: "You cannot migrate a subscription that is already attached..."
                        # Das passiert, wenn 'sub.schedule' lokal None war, aber Stripe einen hat.
                        # Wir laden das Abo neu und versuchen es erneut.
                        print(f"Schedule create failed ({e}), checking existing...")
                        refreshed_sub = stripe.Subscription.retrieve(active_subscription.id)
                        if refreshed_sub.schedule:
                            sched_id = refreshed_sub.schedule
                        else:
                            raise e # Echter Fehler

                # 2. Schedule updaten mit Phasen (Safe Update)
                # Wir definieren exakt 2 Phasen:
                # Phase 1: Aktuell bis Periodenende
                # Phase 2: Neuer Plan ab Periodenende
                
                period_end = int(active_subscription.current_period_end)
                
                stripe.SubscriptionSchedule.modify(
                    sched_id,
                    end_behavior='release', 
                    phases=[
                        {
                            'start_date': 'now', # Startet die Phase sofort (übernimmt laufende Periode)
                            'end_date': period_end, 
                            'items': [{'price': current_item.price.id, 'quantity': 1}],
                            'metadata': active_subscription.metadata 
                        },
                        {
                            'start_date': period_end,
                            'items': [{'price': target_price_id, 'quantity': 1}],
                            'metadata': {"tenant_id": tenant.id, "plan_name": plan} 
                        }
                    ]
                )
                
                # DB Manuell updaten für sofortiges Feedback
                tenant.upcoming_plan = plan
                tenant.next_payment_amount = target_amount # Neuer Preis für die Zukunft anzeigen
                db.commit()
                
                return {
                    "subscriptionId": active_subscription.id,
                    "status": "success", 
                    "message": "Downgrade vorgemerkt."
                }

        except Exception as e:
            raise HTTPException(400, f"Update failed: {str(e)}")

    # --- NEUANLAGE ---
    else:
        print("✨ Creating NEW Subscription")
        trial_days = 0
        now = datetime.now(timezone.utc)
        if tenant.subscription_ends_at and tenant.subscription_ends_at > now:
            delta = tenant.subscription_ends_at - now
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
            
            tenant.plan = plan
            tenant.upcoming_plan = None
            tenant.next_payment_amount = target_amount
            update_tenant_from_subscription(db, tenant, sub)
            
            client_secret = extract_client_secret(sub.latest_invoice) or extract_client_secret(sub.pending_setup_intent)
            
            return {
                "subscriptionId": sub.id,
                "clientSecret": client_secret,
                "status": "created",
                "nextPaymentAmount": target_amount
            }
        except Exception as e:
            raise HTTPException(400, f"Create failed: {str(e)}")

# ... (Restliche Funktionen wie cancel_subscription, get_billing_portal_url etc. bleiben identisch) ...

def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")
    try:
        try:
            sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            if sub.schedule:
                stripe.SubscriptionSchedule.release(sub.schedule)
        except: pass

        sub = stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=True)
        
        tenant.upcoming_plan = None
        tenant.next_payment_amount = 0.0
        
        update_tenant_from_subscription(db, tenant, sub)
        return {"message": "Cancelled"}
    except Exception as e: raise HTTPException(400, str(e))

def get_billing_portal_url(db: Session, tenant_id: int, return_url: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id: raise HTTPException(400, "No customer")
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