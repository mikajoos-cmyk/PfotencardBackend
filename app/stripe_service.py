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

def extract_client_secret(invoice):
    """Hilfsfunktion zum sicheren Extrahieren des Client Secrets aus einer Invoice"""
    if not invoice:
        return None
        
    # Versuche payment_intent zu holen (Dictionary-Zugriff für Stripe Objekte)
    payment_intent = None
    try:
        # Prio 1: Dictionary Access (Standard für neue Stripe Libs)
        payment_intent = invoice['payment_intent']
    except (TypeError, KeyError, AttributeError):
        try:
            # Prio 2: Attribut Access (Fallback)
            payment_intent = invoice.payment_intent
        except AttributeError:
            pass
            
    if not payment_intent:
        return None
        
    # Client Secret aus Payment Intent holen
    try:
        return payment_intent['client_secret']
    except (TypeError, KeyError, AttributeError):
        try:
            return payment_intent.client_secret
        except AttributeError:
            return None

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

    try:
        if not tenant.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user_email,
                name=tenant.name,
                metadata={"tenant_id": tenant.id}
            )
            tenant.stripe_customer_id = customer.id
            db.commit()
        
        customer_id = tenant.stripe_customer_id

        # FALL A: Update
        if tenant.stripe_subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
                
                if subscription.status in ['active', 'trialing', 'past_due']:
                    item_id = subscription['items']['data'][0].id
                    
                    updated_sub = stripe.Subscription.modify(
                        tenant.stripe_subscription_id,
                        items=[{
                            'id': item_id,
                            'price': price_id,
                        }],
                        cancel_at_period_end=False,
                        metadata={
                            "tenant_id": tenant.id,
                            "plan_name": plan
                        },
                        proration_behavior='create_prorations',
                        payment_behavior='default_incomplete',
                        expand=['latest_invoice.payment_intent']
                    )
                    
                    client_secret = extract_client_secret(updated_sub.latest_invoice)
                    
                    return {
                        "subscriptionId": updated_sub.id,
                        "clientSecret": client_secret,
                        "status": "updated",
                        "message": "Dein Plan wurde erfolgreich geändert."
                    }
            except stripe.error.InvalidRequestError:
                pass

        # FALL B: Neu
        subscription_data = {
            'customer': customer_id,
            'items': [{"price": price_id}],
            'payment_behavior': 'default_incomplete',
            'payment_settings': {'save_default_payment_method': 'on_subscription'},
            'expand': ['latest_invoice.payment_intent', 'pending_setup_intent'],
            'metadata': {
                "tenant_id": tenant.id,
                "plan_name": plan
            }
        }
        
        if trial_days > 0:
            subscription_data['trial_period_days'] = trial_days

        subscription = stripe.Subscription.create(**subscription_data)

        tenant.stripe_subscription_id = subscription.id
        db.commit()

        client_secret = None
        if subscription.pending_setup_intent:
            # Setup Intent hat auch dict access
            try:
                client_secret = subscription.pending_setup_intent['client_secret']
            except (TypeError, KeyError):
                client_secret = subscription.pending_setup_intent.client_secret
        else:
            client_secret = extract_client_secret(subscription.latest_invoice)

        return {
            "subscriptionId": subscription.id,
            "clientSecret": client_secret,
            "status": "created"
        }

    except Exception as e:
        print(f"Stripe Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

def cancel_subscription(db: Session, tenant_id: int):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    try:
        stripe.Subscription.modify(
            tenant.stripe_subscription_id,
            cancel_at_period_end=True
        )
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
        
        # Sicherer Zugriff
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
                else:
                    details["next_payment_date"] = datetime.fromtimestamp(current_period_end, tz=timezone.utc)
            except stripe.error.InvalidRequestError:
                pass
        
        return details

    except Exception as e:
        print(f"Stripe Error in details: {e}")
        return None