import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.39.3'
import Stripe from 'https://esm.sh/stripe@17.0.0?target=deno'
import { corsHeaders } from '../_shared/cors.ts'

const log = (step: string, details?: Record<string, unknown>) => {
  const d = details ? ` - ${JSON.stringify(details)}` : '';
  console.log(`[STRIPE-WEBHOOK] ${step}${d}`);
};

const jsonResponse = (data: any) => new Response(JSON.stringify(data), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
const errorResponse = (message: string, status = 400) => new Response(JSON.stringify({ error: message }), { status, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });

/**
 * Bestimmt den Plan-Namen basierend auf der Stripe Price-ID oder Product-ID aus der DB.
 * Berücksichtigt nur Basis-Pakete, um zu verhindern, dass Add-ons den Hauptplan überschreiben.
 */
async function determinePlan(supabase: any, priceId?: string, productId?: string): Promise<string | null> {
  if (priceId) {
    const { data } = await supabase
      .from('subscription_packages')
      .select('plan_name, package_type')
      .or(`stripe_price_id_base_monthly.eq.${priceId},stripe_price_id_base_yearly.eq.${priceId}`)
      .maybeSingle();
    
    if (data && data.package_type === 'base') return data.plan_name;
  }
  
  if (productId) {
    const { data } = await supabase
      .from('subscription_packages')
      .select('plan_name, package_type')
      .eq('stripe_product_id', productId)
      .maybeSingle();
    
    if (data && data.package_type === 'base') return data.plan_name;
  }
  
  return null;
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  const signature = req.headers.get('Stripe-Signature')
  if (!signature) return errorResponse("No signature", 400);

  try {
    const stripeKey = Deno.env.get("STRIPE_SECRET_KEY");
    const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET");
    if (!stripeKey) throw new Error("STRIPE_SECRET_KEY is not set");
    if (!webhookSecret) throw new Error("STRIPE_WEBHOOK_SECRET is not set");

    const stripe = new Stripe(stripeKey, {
      httpClient: Stripe.createFetchHttpClient(),
    });

    const body = await req.text()
    let event: Stripe.Event;

    try {
      event = await stripe.webhooks.constructEventAsync(body, signature, webhookSecret)
      log("Event verified", { type: event.type, id: event.id });
    } catch (err) {
      log("Signature verification failed", { error: String(err) });
      return errorResponse(`Webhook Error: ${err.message}`, 400);
    }

    const supabaseAdmin = createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? '',
        { auth: { persistSession: false } }
    )

    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        log("checkout.session.completed", { mode: session.mode, customerId: session.customer, subscriptionId: session.subscription });

        if (session.mode !== 'subscription') {
          log("Not a subscription checkout, skipping");
          break;
        }

        const customerId = session.customer as string;
        const subscriptionId = session.subscription as string;
        const tenantId = session.metadata?.tenant_id;
        
        // Load subscription from Stripe for full details
        const sub = await stripe.subscriptions.retrieve(subscriptionId);
        
        // Den Plan bestimmen: Alle Items durchsuchen, ob eines ein Basis-Paket in unserer DB ist.
        let plan: string | null = null;
        for (const item of sub.items.data) {
          const detectedPlan = await determinePlan(supabaseAdmin, item.price.id, item.price.product as string);
          if (detectedPlan) {
            plan = detectedPlan;
            break;
          }
        }
        
        // Fallback auf Metadaten, falls kein Basis-Paket via ID gefunden wurde
        if (!plan) {
          plan = session.metadata?.plan || session.metadata?.plan_name || null;
        }
        
        const isTrialing = sub.status === 'trialing';
        const trialEnd = sub.trial_end ? new Date(sub.trial_end * 1000).toISOString() : null;
        const periodEnd = sub.current_period_end ? new Date(sub.current_period_end * 1000).toISOString() : null;

        const updateData: any = {
          stripe_customer_id: customerId,
          stripe_subscription_id: subscriptionId,
          stripe_subscription_status: sub.status,
          subscription_ends_at: periodEnd,
          trial_end: trialEnd,
          cancel_at_period_end: sub.cancel_at_period_end,
          cancelled_at: null,
          next_payment_date: periodEnd,
        };

        if (plan) {
            updateData.plan = plan;
        }

        // Find tenant and update
        let dbTenant: any = null;
        if (tenantId) {
          const { data, error } = await supabaseAdmin.from('tenants').update(updateData).eq('id', tenantId).select('id, name, plan, stripe_subscription_status').maybeSingle();
          if (error) log("ERROR updating tenant", { error: error.message });
          dbTenant = data;
        } else {
          const { data, error } = await supabaseAdmin.from('tenants').update(updateData).eq('stripe_customer_id', customerId).select('id, name, plan, stripe_subscription_status').maybeSingle();
          if (error) log("ERROR updating tenant via customerId", { error: error.message });
          dbTenant = data;
        }

        if (dbTenant) {
          log("Tenant updated via checkout", { tenantId: dbTenant.id });
          // Log to history
          await supabaseAdmin.from('subscription_history').insert({
            tenant_id: dbTenant.id,
            event_type: 'checkout_completed',
            source: 'stripe',
            description: `Checkout abgeschlossen – Plan: ${plan || 'unbekannt'}${isTrialing ? ' (Trial)' : ''}`,
            previous_plan: dbTenant.plan,
            new_plan: plan,
            previous_status: dbTenant.stripe_subscription_status,
            new_status: sub.status,
            details: { stripe_customer_id: customerId, stripe_subscription_id: subscriptionId },
          });

          // Track promo codes
          try {
            const fullSession = await stripe.checkout.sessions.retrieve(session.id, { expand: ['total_details.breakdown'] });
            const discounts = (fullSession as any).total_details?.breakdown?.discounts;
            if (discounts?.length > 0) {
              const promoId = discounts[0].discount?.promotion_code;
              if (promoId) {
                const { data: promoData } = await supabaseAdmin.from('promo_codes').select('id, current_uses, duration_months').eq('stripe_promotion_code_id', promoId).maybeSingle();
                if (promoData) {
                  const { error: redemptionError } = await supabaseAdmin.from('promo_code_redemptions').insert({
                    promo_code_id: promoData.id,
                    tenant_id: dbTenant.id,
                    applied_months: promoData.duration_months
                  });
                  if (!redemptionError) {
                    await supabaseAdmin.from('promo_codes').update({ current_uses: (promoData.current_uses || 0) + 1 }).eq('id', promoData.id);
                  }
                }
              }
            }
          } catch (e) { log("Promo tracking error", { error: String(e) }); }
        }
        break;
      }

      case "customer.subscription.created":
      case "customer.subscription.updated": {
        const sub = event.data.object as Stripe.Subscription;
        const customerId = typeof sub.customer === 'string' ? sub.customer : sub.customer.id;
        const tenantId = sub.metadata?.tenant_id;
        
        // Den Plan bestimmen: Alle Items durchsuchen, ob eines ein Basis-Paket in unserer DB ist.
        let plan: string | null = null;
        for (const item of sub.items.data) {
          const detectedPlan = await determinePlan(supabaseAdmin, item.price.id, item.price.product as string);
          if (detectedPlan) {
            plan = detectedPlan;
            break;
          }
        }
        
        // Fallback auf Metadaten, falls kein Basis-Paket via ID gefunden wurde
        if (!plan) {
          plan = sub.metadata?.plan || sub.metadata?.plan_name || null;
        }

        log(event.type, { customerId, subscriptionId: sub.id, status: sub.status });

        const periodEnd = sub.current_period_end ? new Date(sub.current_period_end * 1000).toISOString() : null;
        const trialEnd = sub.trial_end ? new Date(sub.trial_end * 1000).toISOString() : null;

        const updateData: any = {
          stripe_customer_id: customerId,
          stripe_subscription_id: sub.id,
          stripe_subscription_status: sub.status,
          cancel_at_period_end: sub.cancel_at_period_end,
          subscription_ends_at: periodEnd,
          trial_end: trialEnd,
          next_payment_date: periodEnd,
        };
        
        if (sub.cancel_at_period_end) {
          const finalDate = sub.cancel_at ? new Date(sub.cancel_at * 1000).toISOString() : periodEnd;
          updateData.subscription_ends_at = finalDate;
          updateData.next_payment_date = null;
        }

        if (plan) {
          updateData.plan = plan;
        }
        if (sub.metadata?.upcoming_plan) {
          updateData.upcoming_plan = sub.metadata.upcoming_plan;
        }

        // Handle cancellation state
        if (sub.cancel_at_period_end) {
          const { data: existing } = await supabaseAdmin.from('tenants').select('cancelled_at').eq('stripe_customer_id', customerId).maybeSingle();
          if (!existing?.cancelled_at) {
            updateData.cancelled_at = new Date().toISOString();
          }
        } else {
          updateData.cancelled_at = null;
        }

        const { data: tenantBefore } = await supabaseAdmin.from('tenants').select('id, plan, stripe_subscription_status, upcoming_plan').eq('stripe_customer_id', customerId).maybeSingle();

        const query = tenantId 
          ? supabaseAdmin.from('tenants').update(updateData).eq('id', tenantId)
          : supabaseAdmin.from('tenants').update(updateData).eq('stripe_customer_id', customerId);

        const { error } = await query;
        if (error) log("ERROR updating tenant on sub update", { error: error.message });

        if (tenantBefore && (tenantBefore.stripe_subscription_status !== sub.status || (plan && tenantBefore.plan !== plan))) {
          // If plan changed real, clear upcoming_plan
          if (plan && tenantBefore.plan !== plan) {
             await supabaseAdmin.from('tenants').update({ upcoming_plan: null }).eq('id', tenantBefore.id);
          }

          await supabaseAdmin.from('subscription_history').insert({
            tenant_id: tenantBefore.id,
            event_type: 'subscription_updated',
            source: 'stripe',
            description: `Abo aktualisiert – Status: ${sub.status}${plan ? `, Plan: ${plan}` : ''}`,
            previous_plan: tenantBefore.plan,
            new_plan: plan || tenantBefore.plan,
            previous_status: tenantBefore.stripe_subscription_status,
            new_status: sub.status,
          });
        }
        break;
      }

      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        const customerId = typeof sub.customer === 'string' ? sub.customer : sub.customer.id;
        const tenantId = sub.metadata?.tenant_id;
        
        log("subscription.deleted", { customerId, subscriptionId: sub.id });

        const { data: tenantBefore } = await supabaseAdmin.from('tenants').select('id, plan, stripe_subscription_status').eq('stripe_customer_id', customerId).maybeSingle();

        const updateData = {
          stripe_subscription_status: 'cancelled',
          plan: 'starter',
          upcoming_plan: null,
          subscription_ends_at: sub.ended_at ? new Date(sub.ended_at * 1000).toISOString() : new Date().toISOString(),
          cancelled_at: sub.canceled_at ? new Date(sub.canceled_at * 1000).toISOString() : new Date().toISOString(),
        };

        const query = tenantId 
          ? supabaseAdmin.from('tenants').update(updateData).eq('id', tenantId)
          : supabaseAdmin.from('tenants').update(updateData).eq('stripe_customer_id', customerId);

        const { error } = await query;
        if (error) log("ERROR on sub deletion", { error: error.message });

        if (tenantBefore) {
          await supabaseAdmin.from('subscription_history').insert({
            tenant_id: tenantBefore.id,
            event_type: 'subscription_deleted',
            source: 'stripe',
            description: 'Abo bei Stripe endgültig beendet. Zurück auf Starter-Plan.',
            previous_plan: tenantBefore.plan,
            new_plan: 'starter',
            previous_status: tenantBefore.stripe_subscription_status,
            new_status: 'cancelled',
          });
        }
        break;
      }

      case "invoice.payment_succeeded": {
        const invoice = event.data.object as Stripe.Invoice;
        if (!invoice.subscription) break;
        const customerId = typeof invoice.customer === 'string' ? invoice.customer : invoice.customer?.id;
        if (!customerId) break;

        const amountPaid = invoice.amount_paid ?? 0;
        log("invoice.payment_succeeded", { customerId, amountPaid, subscriptionId: invoice.subscription });

        // Immer die Subscription laden, um Metadaten (tenant_id) und periodEnd zu bekommen
        const sub = await stripe.subscriptions.retrieve(invoice.subscription as string);
        const tenantId = sub.metadata?.tenant_id;
        const periodEnd = sub.current_period_end ? new Date(sub.current_period_end * 1000).toISOString() : null;

        const updateData: any = {
          stripe_subscription_status: 'active',
          subscription_ends_at: periodEnd,
          next_payment_amount: 0,
          stripe_customer_id: customerId,
          stripe_subscription_id: sub.id
        };

        let query = tenantId 
          ? supabaseAdmin.from('tenants').update(updateData).eq('id', tenantId)
          : supabaseAdmin.from('tenants').update(updateData).eq('stripe_customer_id', customerId);
        
        const { data: updatedTenant, error: updateError } = await query.select('id, plan').maybeSingle();
        
        if (updateError) {
          log("ERROR updating tenant on payment_succeeded", { error: updateError.message });
        } else if (updatedTenant) {
          log("Tenant updated on payment_succeeded", { tenantId: updatedTenant.id });
          
          if (amountPaid > 0) {
            await supabaseAdmin.from('subscription_history').insert({
              tenant_id: updatedTenant.id,
              event_type: 'payment_succeeded',
              source: 'stripe',
              description: `Zahlung erfolgreich – ${(amountPaid / 100).toFixed(2)}€`,
              new_plan: updatedTenant.plan,
              new_status: 'active',
              details: { amount_paid: amountPaid, invoice_id: invoice.id },
            });
          }
        } else {
          log("Tenant not found for update on payment_succeeded", { customerId, tenantId });
        }
        break;
      }

      case "invoice.payment_failed": {
        const invoice = event.data.object as Stripe.Invoice;
        if (!invoice.subscription) break;
        const customerId = typeof invoice.customer === 'string' ? invoice.customer : invoice.customer?.id;
        if (!customerId) break;
        
        log("invoice.payment_failed", { customerId });

        const { data: tenant } = await supabaseAdmin.from('tenants').select('id, plan, stripe_subscription_status').eq('stripe_customer_id', customerId).maybeSingle();
        
        await supabaseAdmin.from('tenants').update({ stripe_subscription_status: 'past_due' }).eq('stripe_customer_id', customerId);
        
        if (tenant) {
          await supabaseAdmin.from('subscription_history').insert({
            tenant_id: tenant.id,
            event_type: 'payment_failed',
            source: 'stripe',
            description: 'Zahlung fehlgeschlagen – Status: Überfällig',
            previous_status: tenant.stripe_subscription_status,
            new_status: 'past_due',
            new_plan: tenant.plan,
          });
        }
        break;
      }

      default:
        log("Unhandled event type", { type: event.type });
    }

    return jsonResponse({ received: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log("ERROR", { message });
    return errorResponse(message, 500);
  }
});