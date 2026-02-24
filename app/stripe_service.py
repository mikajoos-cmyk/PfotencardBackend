# app/stripe_service.py
from datetime import datetime, timezone, timedelta
import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
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
    """Extrahiert zuverlässig das Client-Secret, auch wenn es nicht fully expanded ist."""
    if not obj: return None
    try:
        secret = safe_get(obj, 'client_secret')
        if secret: return secret

        pi = safe_get(obj, 'payment_intent')
        if pi:
            if isinstance(pi, str):
                pi_obj = stripe.PaymentIntent.retrieve(pi)
                return pi_obj.client_secret
            return safe_get(pi, 'client_secret')

        si = safe_get(obj, 'pending_setup_intent')
        if si:
            if isinstance(si, str):
                si_obj = stripe.SetupIntent.retrieve(si)
                return si_obj.client_secret
            return safe_get(si, 'client_secret')
    except Exception as e:
        print(f"Error extracting client secret: {e}")
    return None

# --- CORE SYNC ---

def update_tenant_from_subscription(db: Session, tenant: models.Tenant, subscription):
    """Synchronisiert DB mit Stripe."""
    try:
        sub_id = safe_get(subscription, 'id')
        status = safe_get(subscription, 'status')
        metadata = safe_get(subscription, 'metadata') or {}

        # --- Abgebrochene oder abgelaufene Abos direkt sperren ---
        if status in ['canceled', 'incomplete_expired', 'unpaid']:
            ended_at = safe_get(subscription, 'ended_at') or safe_get(subscription, 'canceled_at')

            tenant.stripe_subscription_id = sub_id
            tenant.stripe_subscription_status = status
            tenant.plan = 'starter'
            tenant.cancel_at_period_end = False

            if ended_at:
                tenant.subscription_ends_at = datetime.fromtimestamp(ended_at, tz=timezone.utc)
            else:
                tenant.subscription_ends_at = datetime.now(timezone.utc)

            tenant.next_payment_amount = 0.0
            tenant.next_payment_date = None
            tenant.upcoming_plan = None

            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            print(f"✅ Tenant synced (CANCELED/EXPIRED). Reverted to starter.")
            return

        # 1. ENDDATUM
        current_period_end = safe_get(subscription, 'current_period_end')
        if not current_period_end:
            items_data = get_nested(subscription, 'items', 'data')
            if items_data and len(items_data) > 0:
                current_period_end = safe_get(items_data[0], 'current_period_end')

        trial_end = safe_get(subscription, 'trial_end')
        cancel_at = safe_get(subscription, 'cancel_at')

        tenant.stripe_subscription_id = sub_id
        tenant.stripe_subscription_status = status

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
            elif status == 'active' and current_period_end:
                tenant.subscription_ends_at = datetime.fromtimestamp(current_period_end, tz=timezone.utc)

        # 2. PLAN & UPCOMING (Jetzt aus den Metadaten!)
        if status in ['active', 'trialing']:
            if safe_get(metadata, 'plan_name'):
                tenant.plan = safe_get(metadata, 'plan_name')

            upcoming_plan = safe_get(metadata, 'upcoming_plan')
            if upcoming_plan:
                tenant.upcoming_plan = upcoming_plan
            else:
                tenant.upcoming_plan = None

            tenant.is_active = True

        # 3. PREIS VORSCHAU (Ziel-Plan Preis)
        target_plan_name = tenant.upcoming_plan if tenant.upcoming_plan else tenant.plan

        # --- FIX: Cycle aus Metadata statt raten ---
        target_cycle = safe_get(metadata, 'upcoming_cycle') if tenant.upcoming_plan else safe_get(metadata, 'cycle')

        # Fallback, falls Metadaten fehlen (z.B. bei alten Abos)
        if not target_cycle:
            target_cycle = "monthly"
            try:
                items_data = get_nested(subscription, 'items', 'data')
                if items_data:
                    interval = get_nested(items_data[0], 'plan', 'interval')
                    if interval == 'year': target_cycle = "yearly"
            except:
                pass

        if not tenant.cancel_at_period_end and status not in ['canceled', 'incomplete_expired', 'unpaid', 'incomplete']:
            price_conf = get_plan_config(target_plan_name, target_cycle)
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

# --- CHECKOUT & UPDATE ---

def create_checkout_session(db: Session, tenant_id: int, plan: str, cycle: str, user_email: str, billing_details=None):
    from sqlalchemy.orm.attributes import flag_modified

    print(f"DEBUG: Starting Checkout/Update for Tenant {tenant_id} -> {plan} ({cycle})")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")

    conf = get_plan_config(plan, cycle)
    if not conf: raise HTTPException(400, "Invalid plan")
    target_price_id = conf["id"]
    target_amount = conf["amount"]

    if not tenant.stripe_customer_id:
        try:
            c = stripe.Customer.create(email=user_email, name=tenant.name, metadata={"tenant_id": tenant.id})
            tenant.stripe_customer_id = c.id
            db.commit()
        except Exception as e:
            raise HTTPException(400, f"Stripe Error: {e}")

    customer_id = tenant.stripe_customer_id

    if billing_details:
        try:
            customer_name = billing_details.company_name if billing_details.company_name else billing_details.name
            stripe.Customer.modify(
                customer_id,
                name=customer_name,
                address={
                    "line1": billing_details.address_line1,
                    "postal_code": billing_details.postal_code,
                    "city": billing_details.city,
                    "country": billing_details.country,
                }
            )

            if billing_details.vat_id:
                existing_tax_ids = stripe.Customer.list_tax_ids(customer_id)
                for tax_id_obj in existing_tax_ids.data:
                    stripe.Customer.delete_tax_id(customer_id, tax_id_obj.id)
                stripe.Customer.create_tax_id(customer_id, type="eu_vat", value=billing_details.vat_id)

            current_config = dict(tenant.config) if tenant.config else {}
            inv_settings = current_config.get("invoice_settings", {})
            inv_settings["company_name"] = billing_details.company_name
            inv_settings["account_holder"] = billing_details.name
            inv_settings["address_line1"] = billing_details.address_line1
            inv_settings["address_line2"] = f"{billing_details.postal_code} {billing_details.city}"
            if billing_details.vat_id:
                inv_settings["vat_id"] = billing_details.vat_id

            current_config["invoice_settings"] = inv_settings
            tenant.config = current_config
            flag_modified(tenant, "config")
            db.commit()
        except Exception as e:
            raise HTTPException(400, f"Fehler bei Rechnungsdaten: {str(e)}")

    active_subscription = None
    if tenant.stripe_subscription_id:
        try:
            active_subscription = stripe.Subscription.retrieve(
                tenant.stripe_subscription_id,
                expand=['items.data', 'schedule', 'latest_invoice.payment_intent', 'pending_setup_intent']
            )
            sub_status = safe_get(active_subscription, 'status')

            if sub_status in ['canceled', 'incomplete_expired']:
                update_tenant_from_subscription(db, tenant, active_subscription)
                active_subscription = None

            elif sub_status in ['incomplete', 'unpaid'] and get_nested(active_subscription, 'items', 'data', 0, 'price',
                                                                       'id') != target_price_id:
                try:
                    stripe.Subscription.cancel(safe_get(active_subscription, 'id'))
                except:
                    pass
                active_subscription = None

            elif sub_status not in ['active', 'trialing', 'past_due', 'incomplete', 'unpaid']:
                active_subscription = None

        except Exception as e:
            active_subscription = None

    if active_subscription:
        try:
            items_data = get_nested(active_subscription, 'items', 'data')
            if not items_data: raise HTTPException(400, "Subscription has no items.")

            current_item = items_data[0]
            current_price_id = get_nested(current_item, 'price', 'id')
            current_price_val = get_nested(current_item, 'price', 'unit_amount') or 0
            current_stripe_price = current_price_val / 100.0

            if current_price_id == target_price_id:
                sub_status = safe_get(active_subscription, 'status')
                if sub_status in ['incomplete', 'unpaid']:
                    inv = safe_get(active_subscription, 'latest_invoice')
                    amount_due = safe_get(inv, 'amount_due', 0) / 100.0

                    secret = extract_client_secret(inv) or extract_client_secret(
                        safe_get(active_subscription, 'pending_setup_intent'))
                    if not secret:
                        si = stripe.SetupIntent.create(customer=customer_id, usage='off_session', metadata={
                            "subscription_id": safe_get(active_subscription, 'id')})
                        secret = si.client_secret

                    return {
                        "subscriptionId": safe_get(active_subscription, 'id'),
                        "clientSecret": secret,
                        "status": "payment_needed",
                        "nextPaymentAmount": target_amount,
                        "amountDue": amount_due
                    }

                sched_id = safe_get(active_subscription, 'schedule')
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')
                if sched_id:
                    try:
                        stripe.SubscriptionSchedule.release(sched_id)
                    except:
                        pass
                    tenant.upcoming_plan = None
                    tenant.next_payment_amount = target_amount
                    db.commit()
                    return {"status": "updated", "message": "Wechsel abgebrochen, Plan beibehalten."}
                return {"status": "updated", "message": "Plan already active"}

            is_upgrade = target_amount > current_stripe_price
            is_trial = safe_get(active_subscription, 'status') == 'trialing'

            # A) UPGRADE (Sofort)
            if is_upgrade or is_trial:
                sched_id = safe_get(active_subscription, 'schedule')
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')
                if sched_id:
                    try:
                        stripe.SubscriptionSchedule.release(sched_id)
                    except:
                        pass

                updated_sub_step1 = stripe.Subscription.modify(
                    safe_get(active_subscription, 'id'),
                    items=[{'id': safe_get(current_item, 'id'), 'price': target_price_id}],
                    proration_behavior='always_invoice',
                    payment_behavior='default_incomplete',
                    expand=['latest_invoice.payment_intent']
                )

                stripe.Subscription.modify(
                    safe_get(active_subscription, 'id'),
                    metadata={"tenant_id": tenant.id, "plan_name": plan, "cycle": cycle, "upcoming_plan": "",
                              "upcoming_cycle": ""},
                    cancel_at_period_end=False
                )

                tenant.upcoming_plan = None
                tenant.plan = plan
                update_tenant_from_subscription(db, tenant, updated_sub_step1)

                tenant.next_payment_amount = target_amount
                db.commit()

                inv = safe_get(updated_sub_step1, 'latest_invoice')
                amount_due = safe_get(inv, 'amount_due', 0) / 100.0

                # --- IMMER EIN PAYMENT/SETUP ELEMENT ERZWINGEN ---
                secret = extract_client_secret(inv)
                if not secret:
                    si = stripe.SetupIntent.create(customer=customer_id, usage='off_session',
                                                   metadata={"subscription_id": safe_get(updated_sub_step1, 'id')})
                    secret = si.client_secret

                return {
                    "subscriptionId": safe_get(updated_sub_step1, 'id'),
                    "clientSecret": secret,
                    "status": "updated",
                    "nextPaymentAmount": target_amount,
                    "amountDue": amount_due
                }

            # B) DOWNGRADE (Schedule)
            else:
                sub_id = safe_get(active_subscription, 'id')
                sched_id = safe_get(active_subscription, 'schedule')
                if isinstance(sched_id, dict): sched_id = sched_id.get('id')

                schedule_obj = None
                if sched_id:
                    schedule_obj = stripe.SubscriptionSchedule.retrieve(sched_id)
                else:
                    try:
                        schedule_obj = stripe.SubscriptionSchedule.create(from_subscription=sub_id)
                        sched_id = schedule_obj.id
                    except stripe.error.InvalidRequestError:
                        refreshed = stripe.Subscription.retrieve(sub_id)
                        sched_id = safe_get(refreshed, 'schedule')
                        schedule_obj = stripe.SubscriptionSchedule.retrieve(sched_id)

                period_end_ts = safe_get(active_subscription, 'current_period_end') or safe_get(current_item,
                                                                                                'current_period_end')
                period_end_ts = int(period_end_ts)
                current_phase_start = schedule_obj.phases[0].start_date

                current_interval = get_nested(current_item, 'plan', 'interval')
                current_cycle = "yearly" if current_interval == "year" else "monthly"

                stripe.SubscriptionSchedule.modify(
                    sched_id,
                    end_behavior='release',
                    phases=[
                        {
                            'start_date': current_phase_start,
                            'end_date': period_end_ts,
                            'items': [{'price': get_nested(current_item, 'price', 'id'), 'quantity': 1}],
                            'metadata': safe_get(active_subscription, 'metadata') or {}
                        },
                        {
                            'start_date': period_end_ts,
                            'items': [{'price': target_price_id, 'quantity': 1}],
                            'metadata': {"tenant_id": tenant.id, "plan_name": plan, "cycle": cycle}
                        }
                    ]
                )

                updated_sub = stripe.Subscription.modify(
                    sub_id,
                    metadata={"tenant_id": tenant.id, "plan_name": tenant.plan, "cycle": current_cycle,
                              "upcoming_plan": plan, "upcoming_cycle": cycle}
                )

                tenant.upcoming_plan = plan
                # FIX: Wir synchronisieren den Tenant aus der aktualisierten Subscription,
                # um sicherzustellen, dass alle Metadaten korrekt verarbeitet werden.
                update_tenant_from_subscription(db, tenant, updated_sub)
                db.commit()

                return {"subscriptionId": sub_id, "status": "success", "message": "Downgrade vorgemerkt."}

        except Exception as e:
            print(f"FATAL ERROR in create_checkout_session: {e}")
            import traceback
            traceback.print_exc()
            raise HTTPException(400, f"Update failed: {str(e)}")

    # --- NEW SUBSCRIPTION ---
    else:
        print("✨ Creating NEW Subscription")
        trial_days = 0
        now = datetime.now(timezone.utc)
        trial_end_absolute = tenant.created_at + timedelta(days=14)
        if trial_end_absolute > now:
            delta = trial_end_absolute - now
            trial_days = delta.days

        sub_data = {
            'customer': customer_id,
            'items': [{"price": target_price_id}],
            'payment_behavior': 'default_incomplete',
            'payment_settings': {'save_default_payment_method': 'on_subscription'},
            'expand': ['latest_invoice.payment_intent', 'pending_setup_intent'],
            'metadata': {"tenant_id": tenant.id, "plan_name": plan, "cycle": cycle, "upcoming_plan": "",
                         "upcoming_cycle": ""}
        }
        if trial_days > 0: sub_data['trial_period_days'] = trial_days

        try:
            sub = stripe.Subscription.create(**sub_data)

            tenant.plan = plan
            tenant.upcoming_plan = None
            update_tenant_from_subscription(db, tenant, sub)

            inv = safe_get(sub, 'latest_invoice')
            amount_due = safe_get(inv, 'amount_due', 0) / 100.0

            # --- IMMER EIN PAYMENT/SETUP ELEMENT ERZWINGEN ---
            secret = extract_client_secret(inv) or extract_client_secret(safe_get(sub, 'pending_setup_intent'))
            if not secret:
                si = stripe.SetupIntent.create(customer=customer_id, usage='off_session',
                                               metadata={"subscription_id": safe_get(sub, 'id')})
                secret = si.client_secret

            return {
                "subscriptionId": safe_get(sub, 'id'),
                "clientSecret": secret,
                "status": "created",
                "nextPaymentAmount": target_amount,
                "amountDue": amount_due
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
        except:
            pass

        sub = stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=True)
        tenant.upcoming_plan = None
        tenant.next_payment_amount = 0.0
        update_tenant_from_subscription(db, tenant, sub)
        return {"message": "Cancelled"}
    except Exception as e:
        raise HTTPException(400, str(e))


def reactivate_subscription(db: Session, tenant_id: int):
    """
    Reaktiviert ein zum Zeitraumende gekündigtes Abo, indem `cancel_at_period_end` wieder auf False gesetzt wird.
    """
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")
    try:
        # Aktuelle Subscription holen
        sub = stripe.Subscription.retrieve(tenant.stripe_subscription_id)
        # Falls bereits vollständig gekündigt, kein Reaktivieren möglich
        status = safe_get(sub, 'status')
        if status == 'canceled':
            raise HTTPException(400, "Subscription already canceled")
        # Reaktivieren
        sub = stripe.Subscription.modify(tenant.stripe_subscription_id, cancel_at_period_end=False)
        update_tenant_from_subscription(db, tenant, sub)
        return {"message": "Reactivated"}
    except Exception as e:
        raise HTTPException(400, str(e))

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

def get_invoices(db: Session, tenant_id: int, limit: int = 100):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant or not tenant.stripe_customer_id: return []
    try:
        # Load all invoices (Stripe limit is 100 per call, if we need more we would need pagination, but 100 is better than 12)
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

def create_topup_intent(db: Session, user_id: int, tenant_id: int, amount: float, bonus: float):
    """
    Erstellt einen Stripe PaymentIntent für eine Guthaben-Aufladung.
    Die Metadaten enthalten alle Infos, um das Guthaben im Webhook-Handler gutzuschreiben.
    """
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant: raise HTTPException(404, "Tenant not found")
    
    user = db.query(models.User).filter(models.User.id == user_id, models.User.tenant_id == tenant_id).first()
    if not user: raise HTTPException(404, "User not found")

    try:
        intent = stripe.PaymentIntent.create(
            amount=int(amount * 100), # Stripe erwartet Cents
            currency="eur",
            metadata={
                "type": "balance_topup",
                "user_id": user_id,
                "tenant_id": tenant_id,
                "base_amount": amount,
                "bonus_amount": bonus,
                "description": f"Guthaben-Aufladung ({amount}€ + {bonus}€ Bonus)"
            },
            description=f"Guthaben aufladen für {user.name}"
        )
        return {"clientSecret": intent.client_secret}
    except Exception as e:
        print(f"Stripe Error creating PaymentIntent: {e}")
        raise HTTPException(400, f"Stripe Error: {str(e)}")