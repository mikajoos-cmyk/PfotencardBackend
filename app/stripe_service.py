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

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    price_id = get_price_id(plan, cycle)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    # --- NEU: Dynamische Berechnung der Trial-Tage ---
    trial_days = 0
    now = datetime.now(timezone.utc)
    
    if tenant.subscription_ends_at and tenant.subscription_ends_at > now:
        # Berechne verbleibende Tage
        delta = tenant.subscription_ends_at - now
        trial_days = delta.days + 1 # +1 Puffer, damit es nicht heute endet
        if trial_days > 14: trial_days = 14 # Max 14 Tage (Sicherheit)
    else:
        # Abo abgelaufen -> Sofort zahlen (kein Trial)
        trial_days = 0
    # ------------------------------------------------

    try:
        # 1. Customer sicherstellen
        if not tenant.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user_email,
                name=tenant.name,
                metadata={"tenant_id": tenant.id}
            )
            tenant.stripe_customer_id = customer.id
            db.commit()
        
        customer_id = tenant.stripe_customer_id

        # -------------------------------------------------------
        # FALL A: ÄNDERUNG (Upgrade/Downgrade eines bestehenden Abos)
        # -------------------------------------------------------
        if tenant.stripe_subscription_id:
            try:
                # Altes Abo laden
                subscription = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
                
                # Wenn das Abo aktiv oder in Trial ist -> Updaten
                if subscription.status in ['active', 'trialing', 'past_due']:
                    # Wir brauchen die ID des Items, das wir austauschen wollen (z.B. Starter -> Pro)
                    item_id = subscription['items']['data'][0].id
                    
                    updated_sub = stripe.Subscription.modify(
                        tenant.stripe_subscription_id,
                        items=[{
                            'id': item_id,
                            'price': price_id, # Neuer Preis
                        }],
                        metadata={
                            "tenant_id": tenant.id,
                            "plan_name": plan # Wichtig für Webhook
                        },
                        proration_behavior='create_prorations', # Verrechnung Restbetrag
                        payment_behavior='default_incomplete', # Erlaubt Payment Element Flow
                        expand=['latest_invoice.payment_intent']
                    )
                    
                    # Client Secret für eventuelle Nachzahlung zurückgeben
                    client_secret = None
                    if updated_sub.latest_invoice and updated_sub.latest_invoice.payment_intent:
                        client_secret = updated_sub.latest_invoice.payment_intent.client_secret
                    
                    return {
                        "subscriptionId": updated_sub.id,
                        "clientSecret": client_secret, # Kann null sein bei reinem Swap ohne Kosten
                        "status": "updated"
                    }
            except stripe.error.InvalidRequestError:
                # Abo ID war in DB, existiert aber bei Stripe nicht mehr -> Fallthrough zu Neu erstellen
                pass

        # -------------------------------------------------------
        # FALL B: NEUABSCHLUSS (Erstes Abo)
        # -------------------------------------------------------
        
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
        
        # Nur Trial setzen, wenn > 0
        if trial_days > 0:
            subscription_data['trial_period_days'] = trial_days

        subscription = stripe.Subscription.create(**subscription_data)

        tenant.stripe_subscription_id = subscription.id
        db.commit()

        client_secret = None
        if subscription.pending_setup_intent:
            client_secret = subscription.pending_setup_intent.client_secret
        elif subscription.latest_invoice and subscription.latest_invoice.payment_intent:
            client_secret = subscription.latest_invoice.payment_intent.client_secret

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
