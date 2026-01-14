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
    # Legacy Support
    if plan == 'verband': plan = 'enterprise'
    
    if plan in prices and cycle in prices[plan]:
        return prices[plan][cycle]
    return None

def get_plan_name_from_price_id(price_id: str):
    s = settings
    if price_id in [s.STRIPE_PRICE_ID_STARTER_MONTHLY, s.STRIPE_PRICE_ID_STARTER_YEARLY]: return "starter"
    if price_id in [s.STRIPE_PRICE_ID_PRO_MONTHLY, s.STRIPE_PRICE_ID_PRO_YEARLY]: return "pro"
    if price_id in [s.STRIPE_PRICE_ID_ENTERPRISE_MONTHLY, s.STRIPE_PRICE_ID_ENTERPRISE_YEARLY]: return "enterprise"
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
    Behandelt 'cancel_at' als Kündigung und nutzt Items-Fallback für Enddaten.
    """
    try:
        is_dict = isinstance(subscription, dict)
        get = lambda k: subscription.get(k) if is_dict else getattr(subscription, k, None)
        
        sub_id = get('id')
        status = get('status')
        metadata = get('metadata') or {}
        
        # 1. ENDDATUM & KÜNDIGUNGSDATUM
        # Wir holen alle möglichen Datums-Quellen
        current_period_end = get('current_period_end')
        trial_end = get('trial_end')
        cancel_at = get('cancel_at') # Das hier ist entscheidend!

        # Fallback: Datum aus Items, wenn im Root nicht vorhanden (siehe Flexible Billing)
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
        
        # WICHTIG: Wir setzen 'cancel_at_period_end' in unserer DB auf True, wenn:
        # A) Das Flag in Stripe True ist
        # B) ODER wenn ein konkretes Kündigungsdatum (cancel_at) gesetzt ist
        stripe_cancel_flag = get('cancel_at_period_end')
        
        if stripe_cancel_flag or (cancel_at is not None):
            tenant.cancel_at_period_end = True
            # Wenn gekündigt ist, ist 'cancel_at' das definitivste Enddatum
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
        if status in ['active', 'trialing']:
            tenant.is_active = True
        # Wenn Status 'canceled' ist (Zeitraum abgelaufen), setzen wir is_active auf False
        elif status in ['canceled', 'unpaid', 'incomplete_expired']:
            # Optional: Hier könnte man is_active auf False setzen
            pass

        # 5. PREVIEW NÄCHSTE ZAHLUNG (Nur wenn NICHT gekündigt)
        tenant.next_payment_amount = 0.0
        tenant.next_payment_date = tenant.subscription_ends_at
        tenant.upcoming_plan = None
        
        if not tenant.cancel_at_period_end and status in ['active', 'trialing']:
            try:
                items = get('items')
                data = items.get('data') if isinstance(items, dict) else (items.data if items else [])
                if data:
                    # Preis aus erstem Item extrahieren
                    item0 = data[0]
                    i_get = lambda k: item0.get(k) if isinstance(item0, dict) else getattr(item0, k, None)
                    price = i_get('price')
                    if price:
                        p_get = lambda k: price.get(k) if isinstance(price, dict) else getattr(price, k, None)
                        amount = p_get('unit_amount')
                        if amount: tenant.next_payment_amount = amount / 100.0
            except: pass

        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        print(f"✅ Tenant {tenant.id} synced. Status: {status}, Gekündigt: {tenant.cancel_at_period_end}")

    except Exception as e:
        print(f"❌ Error syncing tenant DB: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
# --- HAUPTFUNKTION: CHECKOUT / UPGRADE / DOWNGRADE ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    target_price_id = get_price_id(plan, cycle)
    if not target_price_id:
        raise HTTPException(status_code=400, detail="Invalid plan configuration")

    # 1. Customer Check
    if not tenant.stripe_customer_id:
        try:
            customer = stripe.Customer.create(email=user_email, name=tenant.name, metadata={"tenant_id": tenant.id})
            tenant.stripe_customer_id = customer.id
            db.commit()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Stripe Customer Error: {e}")
            
    customer_id = tenant.stripe_customer_id

    # 2. Prüfen: Hat der Kunde ein Abo?
    active_subscription = None
    if tenant.stripe_subscription_id:
        try:
            sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
            # Wir aktualisieren fast alle Status, außer es ist wirklich 'canceled'
            if sub.status in ['active', 'trialing', 'past_due', 'incomplete', 'incomplete_expired', 'unpaid']:
                active_subscription = sub
        except stripe.error.InvalidRequestError:
            pass # ID existiert nicht bei Stripe -> Neu anlegen

    # --- WEICHE: UPDATE (MODIFY) vs NEU (CREATE) ---
    
    if active_subscription:
        # A) UPDATE / UPGRADE / DOWNGRADE
        print(f"🔄 Modifying Subscription {active_subscription.id}")
        
        try:
            # Preise vergleichen
            current_item = active_subscription['items']['data'][0]
            current_amount = current_item.price.unit_amount or 0
            
            new_price_obj = stripe.Price.retrieve(target_price_id)
            new_amount = new_price_obj.unit_amount or 0
            
            is_upgrade = new_amount > current_amount
            is_trial = active_subscription.status == 'trialing'
            
            # --- INTELLIGENTE LOGIK ---
            proration_behavior = 'create_prorations'
            payment_behavior = 'pending_if_incomplete'
            
            if is_trial:
                proration_behavior = 'none' # Keine Berechnung im Trial
                payment_behavior = 'pending_if_incomplete'
            elif is_upgrade:
                proration_behavior = 'always_invoice' # Sofort zahlen
                payment_behavior = 'default_incomplete'

            # SCHRITT 1: Tarif ändern
            # WICHTIG: cancel_at_period_end und metadata HIER WEGLASSEN, um Konflikte zu vermeiden!
            updated_sub = stripe.Subscription.modify(
                active_subscription.id,
                items=[{
                    'id': current_item.id,
                    'price': target_price_id, 
                }],
                # cancel_at_period_end=False,  <-- HIER ENTFERNT!
                proration_behavior=proration_behavior,
                payment_behavior=payment_behavior,
                expand=['latest_invoice.payment_intent']
            )
            
            # Invoice sichern (da Schritt 2 sie evtl. nicht zurückgibt)
            saved_latest_invoice = updated_sub.latest_invoice

            # SCHRITT 2: Metadaten und Kündigungs-Status aktualisieren
            # Hier nutzen wir Standard-Verhalten, daher sind diese Params erlaubt.
            try:
                updated_sub = stripe.Subscription.modify(
                    active_subscription.id,
                    metadata={"tenant_id": tenant.id, "plan_name": plan},
                    cancel_at_period_end=False # <-- HIER HINZUFÜGEN!
                )
            except Exception as e:
                print(f"⚠️ Metadata update warning: {e}")
                updated_sub['metadata'] = {"tenant_id": tenant.id, "plan_name": plan}
                updated_sub['cancel_at_period_end'] = False

            update_tenant_from_subscription(db, tenant, updated_sub)
            
            return {
                "subscriptionId": updated_sub.id,
                "clientSecret": extract_client_secret(saved_latest_invoice),
                "status": "updated",
                "nextPaymentAmount": tenant.next_payment_amount
            }

        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Update failed: {str(e)}")

    else:
        # B) NEUES ABO ERSTELLEN
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
        
        if trial_days > 0:
            sub_data['trial_period_days'] = trial_days

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

# ... (Restliche Funktionen bleiben gleich) ...

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
    if not tenant or not tenant.stripe_subscription_id: return None
    try:
        sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
        return {"status": sub.status, "plan": sub.metadata.get("plan_name", tenant.plan)}
    except: return None

def get_invoices(db: Session, tenant_id: int, limit: int = 12):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id: return []
    try:
        invoices = stripe.Invoice.list(customer=tenant.stripe_customer_id, limit=limit, status='paid')
        return [{"id": i.id, "number": i.number, "created": datetime.fromtimestamp(i.created, tz=timezone.utc), "amount": i.total/100.0, "status": i.status, "pdf_url": i.invoice_pdf, "hosted_url": i.hosted_invoice_url} for i in invoices.data]
    except: return []