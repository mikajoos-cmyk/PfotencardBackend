# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models
import json

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
        # Sicherer Zugriff auf verschachtelte Objekte
        if isinstance(obj, dict):
            if obj.get('client_secret'): return obj['client_secret']
            pi = obj.get('payment_intent')
            if isinstance(pi, dict): return pi.get('client_secret')
            if hasattr(pi, 'client_secret'): return pi.client_secret
        else:
            if hasattr(obj, 'client_secret') and obj.client_secret: return obj.client_secret
            if hasattr(obj, 'payment_intent') and obj.payment_intent:
                pi = obj.payment_intent
                if hasattr(pi, 'client_secret'): return pi.client_secret
    except Exception as e:
        print(f"DEBUG: Error extracting client secret: {e}")
    return None

def safe_get(obj, key, default=None):
    """Sicherer Zugriff auf Attribute, vermeidet Methoden-Konflikte (z.B. .items)"""
    try:
        # 1. Dictionary Access (bevorzugt für Stripe Objects)
        if hasattr(obj, 'get'):
            val = obj.get(key, default)
            # Schutz: Falls .get() eine Methode zurückgibt (unwahrscheinlich aber möglich bei falschem Key)
            if callable(val) and key != 'get': 
                return default
            return val
        
        # 2. Attribute Access
        val = getattr(obj, key, default)
        if callable(val): 
            return default
        return val
    except Exception:
        return default

# --- CORE SYNC ---

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """
    Synchronisiert DB mit Stripe.
    Nutzt 'safe_get', um Abstürze bei fehlenden Feldern zu vermeiden.
    """
    try:
        print(f"DEBUG: Syncing Tenant {tenant.id} with Sub {safe_get(subscription, 'id')}")
        
        sub_id = safe_get(subscription, 'id')
        status = safe_get(subscription, 'status')
        metadata = safe_get(subscription, 'metadata') or {}
        
        # 1. ENDDATUM
        current_period_end = safe_get(subscription, 'current_period_end')
        
        # Fallback: Items prüfen (Sicherer Zugriff!)
        if not current_period_end:
            items_container = safe_get(subscription, 'items')
            # items könnte eine Liste oder ein ListObject sein
            items_data = []
            if items_container:
                if isinstance(items_container, dict):
                    items_data = items_container.get('data', [])
                elif hasattr(items_container, 'data'):
                    items_data = items_container.data
            
            if items_data:
                # Nimm das Ende des ersten Items
                first_item = items_data[0]
                current_period_end = safe_get(first_item, 'current_period_end')

        trial_end = safe_get(subscription, 'trial_end')
        cancel_at = safe_get(subscription, 'cancel_at')

        # DB Update
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
        if metadata.get('plan_name'):
            tenant.plan = metadata.get('plan_name')

        if status in ['active', 'trialing', 'incomplete']:
            tenant.is_active = True

        # 3. PREIS VORSCHAU (Hardcoded für Stabilität)
        target_plan_name = tenant.upcoming_plan if tenant.upcoming_plan else tenant.plan
        
        # Zyklus raten
        current_cycle = "monthly"
        try:
            items_container = safe_get(subscription, 'items')
            items_data = items_container.get('data') if hasattr(items_container, 'get') else getattr(items_container, 'data', [])
            if items_data:
                plan_obj = safe_get(items_data[0], 'plan')
                interval = safe_get(plan_obj, 'interval')
                if interval == 'year': current_cycle = "yearly"
        except Exception as e:
            print(f"DEBUG: Could not detect cycle: {e}")

        # Preis setzen
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
        import traceback
        traceback.print_exc()

# --- CHECKOUT ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
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
            # WICHTIG: expand=['items.data'] um Items sicher zu haben
            active_subscription = stripe.Subscription.retrieve(
                tenant.stripe_subscription_id,
                expand=['items.data']
            )
            if active_subscription.status not in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = None
        except: pass

    # --- UPDATE ---
    if active_subscription:
        print(f"🔄 Modifying Subscription {active_subscription.id}")
        try:
            # SICHERER ZUGRIFF auf Items
            items_container = active_subscription.get('items') # .get() ist sicher bei StripeObjects
            if not items_container or not hasattr(items_container, 'data') or not items_container.data:
                raise HTTPException(400, "Subscription has no items. Cannot update.")
                
            current_item = items_container.data[0]
            current_stripe_price = current_item.price.unit_amount / 100.0
            
            is_upgrade = target_amount > current_stripe_price
            is_trial = active_subscription.status == 'trialing'
            
            print(f"DEBUG: Upgrade? {is_upgrade} (Old: {current_stripe_price}, New: {target_amount})")

            # A) UPGRADE (Sofort)
            if is_upgrade or is_trial:
                # Schedule auflösen
                if active_subscription.schedule:
                    print("DEBUG: Releasing schedule...")
                    try: stripe.SubscriptionSchedule.release(active_subscription.schedule)
                    except: pass

                updated_sub_step1 = stripe.Subscription.modify(
                    active_subscription.id,
                    items=[{'id': current_item.id, 'price': target_price_id}],
                    proration_behavior='always_invoice',
                    payment_behavior='pending_if_incomplete',
                    expand=['latest_invoice.payment_intent']
                )
                
                # Metadata Update
                stripe.Subscription.modify(
                    active_subscription.id,
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False
                )
                
                tenant.upcoming_plan = None
                tenant.plan = plan 
                update_tenant_from_subscription(db, tenant, updated_sub_step1)
                
                return {
                    "subscriptionId": updated_sub_step1.id,
                    "clientSecret": extract_client_secret(updated_sub_step1.latest_invoice),
                    "status": "updated",
                    "nextPaymentAmount": target_amount
                }
            
            # B) DOWNGRADE (Schedule)
            else:
                sched_id = active_subscription.schedule
                
                # Schedule erstellen wenn nötig
                if not sched_id:
                    try:
                        print("DEBUG: Creating schedule...")
                        sched = stripe.SubscriptionSchedule.create(from_subscription=active_subscription.id)
                        sched_id = sched.id
                    except stripe.error.InvalidRequestError as e:
                        print(f"DEBUG: Schedule error ({e}), retrying refresh...")
                        refreshed = stripe.Subscription.retrieve(active_subscription.id)
                        if refreshed.schedule: sched_id = refreshed.schedule
                        else: raise e

                # Datum Robust
                period_end_timestamp = active_subscription.current_period_end
                if not period_end_timestamp:
                    period_end_timestamp = current_item.get('current_period_end')
                
                if not period_end_timestamp:
                    raise HTTPException(400, "Could not determine period end.")
                
                period_end_timestamp = int(period_end_timestamp)
                print(f"DEBUG: Scheduling downgrade for {period_end_timestamp}")

                stripe.SubscriptionSchedule.modify(
                    sched_id,
                    end_behavior='release', 
                    phases=[
                        {
                            'start_date': 'now', 
                            'end_date': period_end_timestamp, 
                            'items': [{'price': current_item.price.id, 'quantity': 1}],
                            'metadata': active_subscription.metadata 
                        },
                        {
                            'start_date': period_end_timestamp,
                            'items': [{'price': target_price_id, 'quantity': 1}],
                            'metadata': {"tenant_id": tenant.id, "plan_name": plan} 
                        }
                    ]
                )
                
                tenant.upcoming_plan = plan
                tenant.next_payment_amount = target_amount
                db.commit()
                
                return {
                    "subscriptionId": active_subscription.id,
                    "status": "success", 
                    "message": "Downgrade vorgemerkt."
                }

        except Exception as e:
            print(f"FATAL ERROR in create_checkout_session: {e}")
            import traceback
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
        try:
            sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            if sub.schedule: stripe.SubscriptionSchedule.release(sub.schedule)
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