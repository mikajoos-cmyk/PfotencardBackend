# app/stripe_service.py
from datetime import datetime, timezone
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models

stripe.api_key = settings.STRIPE_SECRET_KEY

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
    """
    Extrahiert client_secret robust aus Invoice oder SetupIntent.
    """
    if not obj:
        return None
    
    try:
        if hasattr(obj, 'client_secret') and obj.client_secret:
            return obj.client_secret
        if isinstance(obj, dict) and obj.get('client_secret'):
            return obj['client_secret']
    except: pass

    payment_intent = None
    try:
        payment_intent = obj.get('payment_intent') if isinstance(obj, dict) else obj.payment_intent
    except AttributeError: pass
            
    if payment_intent:
        try:
            return payment_intent.get('client_secret') if isinstance(payment_intent, dict) else payment_intent.client_secret
        except AttributeError: pass
            
    return None

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """
    Aktualisiert den Tenant SOFORT mit den Daten von Stripe.
    """
    try:
        is_dict = isinstance(subscription, dict)
        get = lambda k: subscription.get(k) if is_dict else getattr(subscription, k, None)
        
        status = get('status')
        cancel_at_period_end = get('cancel_at_period_end')
        current_period_end = get('current_period_end')
        metadata = get('metadata') or {}
        
        if current_period_end:
            tenant.subscription_ends_at = datetime.fromtimestamp(current_period_end, tz=timezone.utc)
        
        tenant.stripe_subscription_id = get('id')
        tenant.stripe_subscription_status = status
        tenant.cancel_at_period_end = cancel_at_period_end
        
        plan_name = metadata.get('plan_name')
        if plan_name:
            tenant.plan = plan_name

        if status in ['active', 'trialing']:
            tenant.is_active = True

        # Vorschau auf nächste Rechnung laden
        if not cancel_at_period_end and status in ['active', 'trialing'] and tenant.stripe_customer_id:
            try:
                upcoming = stripe.Invoice.upcoming(customer=tenant.stripe_customer_id)
                tenant.next_payment_amount = upcoming.amount_due / 100.0
                
                if upcoming.next_payment_attempt:
                    tenant.next_payment_date = datetime.fromtimestamp(upcoming.next_payment_attempt, tz=timezone.utc)
                
                if upcoming.lines and upcoming.lines.data:
                    tenant.upcoming_plan = None 
            except Exception:
                tenant.next_payment_amount = 0.0
                tenant.next_payment_date = None
                tenant.upcoming_plan = None
        else:
            tenant.next_payment_amount = 0.0
            tenant.next_payment_date = None
            tenant.upcoming_plan = None

        db.commit()
        db.refresh(tenant)
    except Exception as e:
        print(f"Error updating tenant from sub: {e}")

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    price_id = get_price_id(plan, cycle)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    trial_days = 0
    now = datetime.now(timezone.utc)
    if tenant.subscription_ends_at and tenant.subscription_ends_at > now:
        delta = tenant.subscription_ends_at - now
        trial_days = delta.days + 1
        if trial_days > 14: trial_days = 14
    else:
        trial_days = 0

    if not tenant.stripe_customer_id:
        try:
            customer = stripe.Customer.create(
                email=user_email,
                name=tenant.name,
                metadata={"tenant_id": tenant.id}
            )
            tenant.stripe_customer_id = customer.id
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Customer creation failed: {str(e)}")
            
    customer_id = tenant.stripe_customer_id

    try:
        # --- FALL A: Update existierendes Abo ---
        if tenant.stripe_subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
                if subscription.status in ['active', 'trialing', 'past_due']:
                    item_id = subscription['items']['data'][0].id
                    
                    updated_sub = stripe.Subscription.modify(
                        tenant.stripe_subscription_id,
                        items=[{'id': item_id, 'price': price_id}],
                        cancel_at_period_end=False,
                        metadata={"tenant_id": tenant.id, "plan_name": plan},
                        proration_behavior='create_prorations',
                        payment_behavior='default_incomplete',
                        expand=['latest_invoice.payment_intent']
                    )
                    
                    # SOFORT DB UPDATE
                    update_tenant_from_subscription(db, tenant, updated_sub)
                    
                    client_secret = extract_client_secret(updated_sub.latest_invoice)
                    
                    # ÄNDERUNG: Nächste Zahlungsdaten zurückgeben
                    return {
                        "subscriptionId": updated_sub.id,
                        "clientSecret": client_secret,
                        "status": "updated",
                        "message": "Plan geändert.",
                        "nextPaymentAmount": tenant.next_payment_amount,
                        "nextPaymentDate": tenant.next_payment_date
                    }
            except stripe.error.InvalidRequestError:
                pass 

        # --- FALL B: Neues Abo anlegen ---
        subscription_data = {
            'customer': customer_id,
            'items': [{"price": price_id}],
            'payment_behavior': 'default_incomplete',
            'payment_settings': {'save_default_payment_method': 'on_subscription'},
            'expand': ['latest_invoice.payment_intent', 'pending_setup_intent'],
            'metadata': {"tenant_id": tenant.id, "plan_name": plan}
        }
        
        if trial_days > 0:
            subscription_data['trial_period_days'] = trial_days

        subscription = stripe.Subscription.create(**subscription_data)
        
        # SOFORT DB UPDATE
        update_tenant_from_subscription(db, tenant, subscription)

        client_secret = None
        if subscription.pending_setup_intent:
            client_secret = extract_client_secret(subscription.pending_setup_intent)
        elif subscription.latest_invoice:
            client_secret = extract_client_secret(subscription.latest_invoice)

        # ÄNDERUNG: Nächste Zahlungsdaten zurückgeben
        return {
            "subscriptionId": subscription.id,
            "clientSecret": client_secret,
            "status": "created",
            "nextPaymentAmount": tenant.next_payment_amount,
            "nextPaymentDate": tenant.next_payment_date
        }

    except Exception as e:
        print(f"Stripe Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# ... (Rest bleibt gleich)
def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    try:
        sub = stripe.Subscription.modify(
            tenant.stripe_subscription_id,
            cancel_at_period_end=True
        )
        update_tenant_from_subscription(db, tenant, sub)
        return {"message": "Subscription cancelled at period end"}
    except Exception as e:
         raise HTTPException(status_code=400, detail=str(e))

def get_billing_portal_url(db: Session, tenant_id: int, return_url: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id:
         raise HTTPException(status_code=400, detail="No stripe customer")
    session = stripe.billing_portal.Session.create(
        customer=tenant.stripe_customer_id,
        return_url=return_url,
    )
    return {"url": session.url}

def get_subscription_details(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
        cancel_at_period_end = sub.get('cancel_at_period_end') if isinstance(sub, dict) else sub.cancel_at_period_end
        status = sub.get('status') if isinstance(sub, dict) else sub.status
        current_period_end = sub.get('current_period_end') if isinstance(sub, dict) else sub.current_period_end
        metadata = sub.get('metadata', {}) if isinstance(sub, dict) else sub.metadata

        details = {
            "plan": metadata.get("plan_name", tenant.plan),
            "status": status,
            "cancel_at_period_end": cancel_at_period_end,
            "current_period_end": datetime.fromtimestamp(current_period_end, tz=timezone.utc),
            "next_payment_amount": 0.0,
            "next_payment_date": None
        }
        if not cancel_at_period_end and status in ['active', 'trialing']:
            try:
                invoice = stripe.Invoice.upcoming(customer=tenant.stripe_customer_id)
                details["next_payment_amount"] = invoice.amount_due / 100.0 
                if invoice.next_payment_attempt:
                    details["next_payment_date"] = datetime.fromtimestamp(invoice.next_payment_attempt, tz=timezone.utc)
            except: pass
        return details
    except Exception: return None