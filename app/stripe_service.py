# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models

stripe.api_key = settings.STRIPE_SECRET_KEY

# --- PREIS-KONFIGURATION ---
# Wir nutzen diese Config, um den 'next_payment_amount' stabil vorherzusagen
PLAN_CONFIGS = {
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

def get_plan_config(plan_name: str, cycle: str):
    plan = plan_name.lower()
    if plan == 'verband': plan = 'enterprise'
    cycle = cycle.lower()
    
    if plan in PLAN_CONFIGS and cycle in PLAN_CONFIGS[plan]:
        return PLAN_CONFIGS[plan][cycle]
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
        # Wir aktualisieren den aktuellen Plan nur, wenn er explizit im Stripe-Objekt steht
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
    
    # Abo laden (FRISCH!)
    if tenant.stripe_subscription_id:
        try:
            sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            if sub.status in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = sub
        except: pass

    # --- FALL A: BESTEHENDES ABO ÄNDERN ---
    if active_subscription:
        print(f"🔄 Modifying Subscription {active_subscription.id}")
        try:
            current_item = active_subscription['items']['data'][0]
            
            # Aktuellen Preis prüfen
            current_stripe_price = current_item.price.unit_amount / 100.0
            is_upgrade = target_amount > current_stripe_price
            is_trial = active_subscription.status == 'trialing'
            
            # --- UPGRADE: SOFORT ---
            if is_upgrade or is_trial:
                # 1. Bestehenden Schedule auflösen (falls vorhanden)
                if active_subscription.schedule:
                    print("Releasing existing schedule before upgrade...")
                    stripe.SubscriptionSchedule.release(active_subscription.schedule)
                    # Kurz warten oder Objekt neu laden ist hier nicht zwingend, da release synchron ist

                # 2. Upgrade durchführen (Preisänderung)
                updated_sub_step1 = stripe.Subscription.modify(
                    active_subscription.id,
                    items=[{'id': current_item.id, 'price': target_price_id}],
                    proration_behavior='always_invoice',
                    payment_behavior='pending_if_incomplete',
                    expand=['latest_invoice.payment_intent']
                )
                
                # 3. Metadaten nachziehen (separat, da payment_behavior gesetzt war)
                stripe.Subscription.modify(
                    active_subscription.id,
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False
                )
                
                # DB Update: Upcoming löschen (Upgrade ist sofort), neuer Preis setzen
                tenant.upcoming_plan = None
                tenant.plan = plan 
                # next_payment_amount wird durch update_tenant_from_subscription gesetzt
                update_tenant_from_subscription(db, tenant, updated_sub_step1)
                
                return {
                    "subscriptionId": updated_sub_step1.id,
                    "clientSecret": extract_client_secret(updated_sub_step1.latest_invoice),
                    "status": "updated",
                    "nextPaymentAmount": target_amount
                }
            
            # --- DOWNGRADE: ZUM ENDE DER LAUFZEIT (SCHEDULE) ---
            else:
                try:
                    # 1. Schedule ID robust ermitteln
                    sched_id = active_subscription.schedule
                    
                    # Wenn kein Schedule existiert -> Erstellen
                    if not sched_id:
                        print("Creating new schedule from subscription...")
                        sched = stripe.SubscriptionSchedule.create(from_subscription=active_subscription.id)
                        sched_id = sched.id
                        
                        # Subscription neu laden, um aktuelle current_period_end zu bekommen
                        active_subscription = stripe.Subscription.retrieve(active_subscription.id)
                        print(f"Created schedule {sched_id}, reloaded subscription")
                    else:
                        print(f"Using existing schedule {sched_id}")

                    # 2. current_period_end als Integer sicherstellen
                    period_end_timestamp = int(active_subscription.current_period_end)
                    
                    # 3. Schedule updaten mit Phasen
                    stripe.SubscriptionSchedule.modify(
                        sched_id,
                        end_behavior='release',
                        proration_behavior='none',  # Keine Zwischenabrechnung bei Downgrade
                        phases=[
                            {
                                # Phase 1: Aktuell bis Periodenende
                                'end_date': period_end_timestamp,
                                'items': [{'price': current_item.price.id, 'quantity': 1}],
                                'proration_behavior': 'none',
                                'metadata': active_subscription.metadata 
                            },
                            {
                                # Phase 2: Neuer Plan ab Periodenende
                                'items': [{'price': target_price_id, 'quantity': 1}],
                                'proration_behavior': 'none',
                                'metadata': {"tenant_id": tenant.id, "plan_name": plan} 
                            }
                        ]
                    )
                    
                    # DB Update
                    tenant.upcoming_plan = plan
                    # next_payment_amount setzen wir auf den neuen (günstigeren) Betrag
                    tenant.next_payment_amount = target_amount
                    db.commit()
                    
                    return {
                        "subscriptionId": active_subscription.id,
                        "status": "success", 
                        "message": "Downgrade vorgemerkt."
                    }
                    
                except stripe.error.InvalidRequestError as e:
                    error_msg = str(e)
                    print(f"❌ Stripe InvalidRequestError during downgrade: {error_msg}")
                    raise HTTPException(400, f"Downgrade schedule failed: {error_msg}")
                except Exception as e:
                    error_msg = str(e)
                    print(f"❌ Unexpected error during downgrade: {error_msg}")
                    raise HTTPException(400, f"Downgrade failed: {error_msg}")

        except Exception as e:
            raise HTTPException(400, f"Update failed: {str(e)}")

    # --- FALL B: NEUES ABO ---
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

def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")
    try:
        # Falls Schedule aktiv -> Release, damit wir sauber kündigen können
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