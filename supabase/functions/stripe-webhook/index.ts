import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.39.3'
import Stripe from 'https://esm.sh/stripe@14.14.0?target=deno'
import { corsHeaders } from '../_shared/cors.ts'

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') || '', {
  httpClient: Stripe.createFetchHttpClient(),
})


Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  const signature = req.headers.get('Stripe-Signature')
  if (!signature) return new Response('No signature', { status: 400 })

  try {
    const body = await req.text()
    const webhookSecret = Deno.env.get('STRIPE_WEBHOOK_SECRET')
    let event;

    try {
      event = stripe.webhooks.constructEvent(body, signature, webhookSecret!)
    } catch (err) {
      console.error(`Webhook signature verification failed: ${err.message}`)
      return new Response(`Webhook Error: ${err.message}`, { status: 400 })
    }

    // Supabase Admin Client initialisieren (um RLS zu umgehen)
    const supabaseAdmin = createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )

    console.log(`Verarbeite Event: ${event.type}`)

    if (event.type === 'customer.subscription.created' ||
        event.type === 'customer.subscription.updated' ||
        event.type === 'customer.subscription.deleted') {

      const subscription = event.data.object
      const tenantId = subscription.metadata.tenant_id

      if (!tenantId) {
        console.log("Ignoriere Subscription ohne tenant_id")
        return new Response('ok', { status: 200 })
      }

      // Daten für Pfotencard 'tenants' Tabelle extrahieren
      const status = subscription.status
      const cancelAtPeriodEnd = subscription.cancel_at_period_end

      let endsAt = null
      if (status === 'canceled' || status === 'incomplete_expired') {
        endsAt = subscription.ended_at || subscription.canceled_at
      } else if (cancelAtPeriodEnd && subscription.cancel_at) {
        endsAt = subscription.cancel_at
      } else {
        endsAt = subscription.current_period_end
      }

      const updateData: any = {
        stripe_subscription_id: subscription.id,
        stripe_subscription_status: status,
        cancel_at_period_end: cancelAtPeriodEnd,
        subscription_ends_at: endsAt ? new Date(endsAt * 1000).toISOString() : null,
      }

      // Bei aktiven Abos den Plan updaten
      if (status === 'active' || status === 'trialing') {
        updateData.is_active = true
        if (subscription.metadata.plan_name) updateData.plan = subscription.metadata.plan_name
        updateData.upcoming_plan = subscription.metadata.upcoming_plan || null
      }

      // Bei Abbruch auf Starter zurückfallen
      if (status === 'canceled' || status === 'incomplete_expired' || status === 'unpaid') {
        updateData.plan = 'starter'
        updateData.upcoming_plan = null
        updateData.next_payment_amount = 0
      }

      // Update in der Datenbank ausführen
      const { error } = await supabaseAdmin
          .from('tenants')
          .update(updateData)
          .eq('id', tenantId)

      if (error) throw error
      console.log(`Tenant ${tenantId} erfolgreich aus Webhook aktualisiert.`)
    }

    return new Response(JSON.stringify({ received: true }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } })

  } catch (error) {
    console.error('Webhook processing failed:', error)
    return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: corsHeaders })
  }
})