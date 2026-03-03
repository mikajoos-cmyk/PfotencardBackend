import stripe
import time
from sqlalchemy.orm import Session
from app.models import Tenant, User, Transaction, SubscriptionPackage

def report_stripe_usage(db: Session):
    print("Starte Stripe Usage Reporting (Billing V2)...")
    
    # Wir brauchen nur noch Tenants, die eine stripe_customer_id haben
    tenants = db.query(Tenant).filter(Tenant.stripe_customer_id.isnot(None)).all()
    
    for tenant in tenants:
        package = db.query(SubscriptionPackage).filter_by(plan_name=tenant.plan).first()
        if not package:
            continue
            
        # ---------------------------------------------------------
        # 1. ZUSATZKUNDEN MELDEN
        # ---------------------------------------------------------
        # Zähle alle aktiven Endkunden
        active_customers = db.query(User).filter(
            User.tenant_id == tenant.id,
            User.role.in_(['customer', 'kunde']),
            User.is_active == True
        ).count()
        
        # Berechne, wie viele über dem Limit sind
        overage = max(0, active_customers - (package.included_customers or 0))
        
        try:
            # Sende das Event an den User-Meter
            # Stripe Billing V2: Event-basiertes Reporting
            stripe.billing.MeterEvent.create(
                event_name="pfotencard_extra_users", 
                payload={
                    "stripe_customer_id": tenant.stripe_customer_id,
                    "value": str(overage)
                }
            )
            print(f"Tenant {tenant.name}: {overage} Zusatzkunden gemeldet.")
        except Exception as e:
            print(f"Fehler bei User-Meldung für Tenant {tenant.name}: {e}")

        # ---------------------------------------------------------
        # 2. TRANSAKTIONSGEBÜHREN MELDEN
        # ---------------------------------------------------------
        # Hole alle noch nicht gemeldeten Aufladungen
        unreported_txs = db.query(Transaction).filter(
            Transaction.tenant_id == tenant.id,
            Transaction.type == 'Aufladung',
            Transaction.reported_to_stripe == False
        ).all()
        
        if unreported_txs:
            total_fee_cents = 0
            for tx in unreported_txs:
                # Nutze die gespeicherte top_up_fee falls vorhanden, sonst 1.5%
                if tx.top_up_fee and tx.top_up_fee > 0:
                    fee = tx.top_up_fee
                else:
                    fee = tx.amount * 0.015  # 1.5% Gebühr Fallback
                
                total_fee_cents += int(round(fee * 100))
            
            if total_fee_cents > 0:
                try:
                    # Sende das Event an den Gebühren-Meter
                    stripe.billing.MeterEvent.create(
                        event_name="pfotencard_tx_fees",
                        payload={
                            "stripe_customer_id": tenant.stripe_customer_id,
                            "value": str(total_fee_cents)
                        }
                    )
                    
                    # Transaktionen als gemeldet markieren
                    for tx in unreported_txs:
                        tx.reported_to_stripe = True
                    db.commit()
                    print(f"Tenant {tenant.name}: {total_fee_cents} Cent Gebühren gemeldet.")
                except Exception as e:
                    print(f"Fehler bei Gebühren-Meldung für Tenant {tenant.name}: {e}")
                    db.rollback()

    print("Stripe Usage Reporting beendet.")
