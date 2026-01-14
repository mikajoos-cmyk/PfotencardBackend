# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models
import traceback

stripe.api_key = settings.STRIPE_SECRET_KEY

# --- CONFIG ---
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

# --- HELPERS ---

def safe_get(obj, key, default=None):
    """Universeller Getter für Objekte, Dicts und Stripe-Responses."""
    try:
        if obj is None: return default
        if hasattr(obj, 'get'):
            val = obj.get(key)
            if callable(val) and key != 'get': return default
            return val if val is not None else default
        val = getattr(obj, key, default)
        if callable(val): return default
        return val
    except Exception:
        return default

def get_nested(obj, *keys):
    current = obj
    for key in keys:
        current = safe_get(current, key)
        if current is None: return None
    return current

def get_plan_config(plan_name: str, cycle: str):
    if not plan_name or not cycle: return None
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
        secret = safe_get(obj, 'client_secret')
        if secret: return secret
        pi = safe_get(obj, 'payment_intent')
        return safe_get(pi, 'client_secret')
    except Exception: return None

# --- CORE SYNC ---

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """Synchronisiert DB mit Stripe."""
    try:
        sub_id = safe_get(subscription, 'id')
        status = safe_get(subscription, 'status')
        metadata = safe_get(subscription, 'metadata') or {}
        
        # 1. ENDDATUM
        current_period_end = safe_get(subscription, 'current_period_end')
        if not current_period_end:
            items_data = get_nested(subscription, 'items', 'data')
            if items_data and len(items_data) > 0:
                current_period_end = safe_get(items_data[0], 'current_period_end')

        trial_end = safe_get(subscription, 'trial_end')
        cancel_at = safe_get(subscription, 'cancel_at')

        # Status Update
        tenant.stripe_subscription_id = sub_id
        tenant.stripe_subscription_status = status
        
        # Kündigungsstatus
        stripe_cancel_flag = safe_get(subscription, 'cancel_at_period_end')
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

        # 2. PLAN & UPCOMING
        if safe_get(metadata, 'plan_name'):
            tenant.plan = safe_get(metadata, 'plan_name')

        if status in ['active', 'trialing', 'incomplete']:
            tenant.is_active = True

        # 3. PREIS VORSCHAU (Ziel-Plan Preis)
        target_plan_name = tenant.upcoming_plan if tenant.upcoming_plan else tenant.plan
        
        # Zyklus raten
        current_cycle = "monthly"
        try:
            items_data = get_nested(subscription, 'items', 'data')
            if items_data:
                interval = get_nested(items_data[0], 'plan', 'interval')
                if interval == 'year': current_cycle = "yearly"
        except: pass

        if not tenant.cancel_at_period_end and status not in ['canceled', 'incomplete_expired', 'unpaid']:
            price_conf = get_plan_config(target_plan_name, current_cycle)
            if price_conf:
                tenant.next_payment_amount = price_conf["amount"]
            else:
                tenant.next_payment_amount = 0.0
            tenant.next_payment_date = tenant.subscription_ends_at
        else:
            tenant.next_payment_amount = 0.0
            tenant.next_payment_date = None
            tenant.upcoming_plan = None

        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(f"✅ Tenant synced. Plan: {tenant.plan}, Next: {tenant.next_payment_amount}€")

    except Exception as e:
        print(f"❌ Error syncing tenant DB: {e}")
        traceback.print_exc()

# --- CHECKOUT & UPDATE ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    print(f"DEBUG: Starting Checkout/Update for Tenant {tenant_id} -> {plan} ({cycle})")
    
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")

    conf = get_plan_config(plan, cycle)
    if not conf: raise HTTPException(400, "Invalid plan")
    target_price_id = conf["id"]
    target_amount = conf["amount"]

    # Customer
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
            active_subscription = stripe.Subscription.retrieve(
                tenant.stripe_subscription_id,
                expand=['items.data', 'schedule'] # Schedule gleich mitladen wenn möglich
            )
            sub_status = safe_get(active_subscription, 'status')
            if sub_status not in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = None
        except Exception as e: 
            print(f"DEBUG: Could not fetch subscription: {e}")
            active_subscription = None

    # --- UPDATE ---
    if active_subscription:
        print(f"🔄 Modifying Subscription {safe_get(active_subscription, 'id')}")
        try:
            items_data = get_nested(active_subscription, 'items', 'data')
            if not items_data:
                raise HTTPException(400, "Subscription has no items.")
                
            current_item = items_data[0]
            current_price_id = get_nested(current_item, 'price', 'id')
            current_price_val = get_nested(current_item, 'price', 'unit_amount') or 0
            current_stripe_price = current_price_val / 100.0
            
            # --- SPEZIALFALL: Downgrade abbrechen / Beim aktuellen Plan bleiben ---
            if current_price_id == target_price_id:
                # Wenn ein Schedule existiert (z.B. geplanter Downgrade), diesen löschen!
                sched_id = safe_get(active_subscription, 'schedule')
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')
                
                if sched_id:
                    print(f"DEBUG: Releasing schedule {sched_id} to stay on current plan...")
                    try:
                        stripe.SubscriptionSchedule.release(sched_id)
                    except Exception as e:
                        print(f"DEBUG: Could not release schedule: {e}")
                    
                    # DB Bereinigen
                    tenant.upcoming_plan = None
                    # Next Payment wieder auf aktuellen Preis setzen
                    tenant.next_payment_amount = target_amount
                    db.commit()
                    
                    return {"status": "updated", "message": "Wechsel abgebrochen, Plan beibehalten."}
                
                return {"status": "updated", "message": "Plan already active"}
            
            is_upgrade = target_amount > current_stripe_price
            is_trial = safe_get(active_subscription, 'status') == 'trialing'
            
            print(f"DEBUG: Upgrade? {is_upgrade} (Old: {current_stripe_price}, New: {target_amount})")

            # A) UPGRADE (Sofort)
            if is_upgrade or is_trial:
                # Schedule auflösen falls vorhanden
                sched_id = safe_get(active_subscription, 'schedule')
                # 'schedule' kann ID-String oder Objekt sein, je nach Expand
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')
                
                if sched_id:
                    print("DEBUG: Releasing schedule...")
                    try: stripe.SubscriptionSchedule.release(sched_id)
                    except: pass

                updated_sub_step1 = stripe.Subscription.modify(
                    safe_get(active_subscription, 'id'),
                    items=[{'id': safe_get(current_item, 'id'), 'price': target_price_id}],
                    proration_behavior='always_invoice',
                    payment_behavior='pending_if_incomplete',
                    expand=['latest_invoice.payment_intent']
                )
                
                stripe.Subscription.modify(
                    safe_get(active_subscription, 'id'),
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False
                )
                
                tenant.upcoming_plan = None
                tenant.plan = plan 
                update_tenant_from_subscription(db, tenant, updated_sub_step1)
                
                # Preis manuell setzen
                tenant.next_payment_amount = target_amount
                db.commit()
                
                inv = safe_get(updated_sub_step1, 'latest_invoice')
                return {
                    "subscriptionId": safe_get(updated_sub_step1, 'id'),
                    "clientSecret": extract_client_secret(inv),
                    "status": "updated",
                    "nextPaymentAmount": target_amount
                }
            
            # B) DOWNGRADE (Schedule)
            else:
                sub_id = safe_get(active_subscription, 'id')
                sched_id = safe_get(active_subscription, 'schedule')
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')
                
                schedule_obj = None

                # 1. Schedule holen oder erstellen
                if sched_id:
                    try:
                        schedule_obj = stripe.SubscriptionSchedule.retrieve(sched_id)
                    except Exception as e:
                        print(f"DEBUG: Could not retrieve schedule {sched_id}: {e}")
                        raise e
                else:
                    try:
                        print("DEBUG: Creating schedule...")
                        schedule_obj = stripe.SubscriptionSchedule.create(from_subscription=sub_id)
                        sched_id = schedule_obj.id
                    except stripe.error.InvalidRequestError as e:
                        print(f"DEBUG: Schedule create failed ({e}), checking refresh...")
                        refreshed = stripe.Subscription.retrieve(sub_id)
                        sched_id = safe_get(refreshed, 'schedule')
                        if sched_id:
                            schedule_obj = stripe.SubscriptionSchedule.retrieve(sched_id)
                        else:
                            raise e

                # 2. Aktuelles Perioden-Ende robust finden
                period_end_ts = safe_get(active_subscription, 'current_period_end')
                if not period_end_ts:
                    period_end_ts = safe_get(current_item, 'current_period_end')
                
                if not period_end_ts:
                    raise HTTPException(400, "Could not determine period end date.")
                period_end_ts = int(period_end_ts)

                # 3. Start-Zeit der AKTUELLEN Phase finden (für Phase 1)
                # schedule_obj.phases enthält die Phasen. Wir nehmen die erste (aktuelle).
                # Wenn wir 'start_date' nicht originalgetreu übergeben, meckert Stripe.
                current_phase_start = schedule_obj.phases[0].start_date

                print(f"DEBUG: Modifying schedule {sched_id}. Phase 1 start: {current_phase_start}, end: {period_end_ts}")

                current_price_id = get_nested(current_item, 'price', 'id')
                current_metadata = safe_get(active_subscription, 'metadata') or {}

                stripe.SubscriptionSchedule.modify(
                    sched_id,
                    end_behavior='release', 
                    phases=[
                        {
                            # Phase 1: Aktuell (Startdatum muss exakt stimmen!)
                            'start_date': current_phase_start, 
                            'end_date': period_end_ts, 
                            'items': [{'price': current_price_id, 'quantity': 1}],
                            'metadata': current_metadata 
                        },
                        {
                            # Phase 2: Neuer Plan
                            'start_date': period_end_ts,
                            'items': [{'price': target_price_id, 'quantity': 1}],
                            'metadata': {"tenant_id": tenant.id, "plan_name": plan} 
                        }
                    ]
                )
                
                tenant.upcoming_plan = plan
                tenant.next_payment_amount = target_amount
                db.commit()
                
                return {
                    "subscriptionId": sub_id,
                    "status": "success", 
                    "message": "Downgrade vorgemerkt."
                }

        except Exception as e:
            print(f"FATAL ERROR in create_checkout_session: {e}")
            traceback.print_exc()
            raise HTTPException(400, f"Update failed: {str(e)}")

    # --- NEU ---
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
            
            inv = safe_get(sub, 'latest_invoice')
            setup = safe_get(sub, 'pending_setup_intent')
            
            return {
                "subscriptionId": safe_get(sub, 'id'),
                "clientSecret": extract_client_secret(inv) or extract_client_secret(setup),
                "status": "created",
                "nextPaymentAmount": target_amount
            }
        except Exception as e:
            raise HTTPException(400, f"Create failed: {str(e)}")

# ... (Restliche Funktionen) ...

def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")
    try:
        try:
            sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            sched_id = safe_get(sub, 'schedule')
            if sched_id: stripe.SubscriptionSchedule.release(sched_id)
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