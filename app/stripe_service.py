import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .config import settings
from . import models

stripe.api_key = settings.STRIPE_SECRET_KEY

def get_price_id(plan_name: str, cycle: str):
    plan = plan_name.lower()
    cycle = cycle.lower() # 'monthly' oder 'yearly'

    # Mapping Struktur: plan -> cycle -> ID
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
    
    # Fallback für 'verband' auf 'enterprise' mappen, falls nötig
    if plan == 'verband': plan = 'enterprise'

    if plan in prices and cycle in prices[plan]:
        return prices[plan][cycle]
    
    return None

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    """
    Erstellt eine Subscription für Stripe Elements (Embedded Formular).
    Handhabt sowohl sofortige Zahlungen als auch Testphasen (SetupIntent).
    """
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    price_id = get_price_id(plan, cycle)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    try:
        # 1. Customer erstellen/holen
        if not tenant.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user_email,
                name=tenant.name,
                metadata={"tenant_id": tenant.id}
            )
            tenant.stripe_customer_id = customer.id
            db.commit()
        
        customer_id = tenant.stripe_customer_id

        # 2. Subscription erstellen
        # WICHTIG: Wir fragen hier AUCH nach 'pending_setup_intent' für Testphasen!
        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            trial_period_days=14,
            payment_behavior='default_incomplete',
            payment_settings={'save_default_payment_method': 'on_subscription'},
            expand=['latest_invoice.payment_intent', 'pending_setup_intent'],
            metadata={
                "tenant_id": tenant.id,
                "plan_name": plan
            }
        )

        tenant.stripe_subscription_id = subscription.id
        db.commit()

        # 3. Client Secret ermitteln (Das Herzstück für dein Frontend)
        client_secret = None
        
        # Fall A: Testphase (Geld wird später eingezogen -> SetupIntent)
        if subscription.pending_setup_intent:
            client_secret = subscription.pending_setup_intent.client_secret
            
        # Fall B: Sofortzahlung (Keine Testphase oder Testphase vorbei -> PaymentIntent)
        elif subscription.latest_invoice and subscription.latest_invoice.payment_intent:
            client_secret = subscription.latest_invoice.payment_intent.client_secret

        if not client_secret:
            raise HTTPException(status_code=500, detail="Konnte kein Client Secret von Stripe generieren.")

        # Antwort für dein Frontend (Embedded Form)
        return {
            "subscriptionId": subscription.id,
            "clientSecret": client_secret
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
        return {"message": "Subscription will be cancelled at the end of the period"}
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
