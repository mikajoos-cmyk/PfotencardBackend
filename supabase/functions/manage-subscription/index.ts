import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.39.3'
import Stripe from 'https://esm.sh/stripe@14.14.0?target=deno'
import { corsHeaders } from '../_shared/cors.ts'

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') || '', {
  httpClient: Stripe.createFetchHttpClient(),
})


Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })

  try {
    const supabaseClient = createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_ANON_KEY') ?? '',
        { global: { headers: { Authorization: req.headers.get('Authorization')! } } }
    )

    // User über das Frontend-Token verifizieren (Sicherheit!)
    const { data: { user }, error: userError } = await supabaseClient.auth.getUser()
    if (userError || !user) throw new Error('Nicht authentifiziert')

    const body = await req.json()
    const { action, tenantId } = body

    // Supabase Admin Client für sichere DB-Abfragen
    const supabaseAdmin = createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )

    // Hole den Tenant
    const { data: tenant, error: tenantError } = await supabaseAdmin
        .from('tenants')
        .select('*')
        .eq('id', tenantId)
        .single()

    if (tenantError || !tenant) throw new Error('Tenant nicht gefunden')

    // ==========================================
    // ACTION: ABO KÜNDIGEN
    // ==========================================
    if (action === 'cancel') {
      if (!tenant.stripe_subscription_id) throw new Error('Kein aktives Abo')

      const sub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
        cancel_at_period_end: true
      })

      return new Response(JSON.stringify({ status: 'success', message: 'Gekündigt' }), { headers: corsHeaders })
    }

    // ==========================================
    // ACTION: ABO REAKTIVIEREN (Undo Kündigung)
    // ==========================================
    if (action === 'reactivate') {
      if (!tenant.stripe_subscription_id) throw new Error('Kein aktives Abo')

      const sub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
        cancel_at_period_end: false
      })

      return new Response(JSON.stringify({ status: 'success', message: 'Reaktiviert' }), { headers: corsHeaders })
    }

    // ==========================================
    // ACTION: CHECKOUT / UPGRADE / NEUES ABO
    // ==========================================
    if (action === 'create_checkout') {
      const { plan, cycle, billingDetails, addons = [] } = body

      // 1. Hole das Paket aus der Pfotencard DB (Preise müssen in Supabase DB liegen)
      const { data: packageData } = await supabaseAdmin
          .from('subscription_packages')
          .select('*')
          .eq('plan_name', plan)
          .eq('package_type', 'base')
          .single()

      if (!packageData) throw new Error(`Plan ${plan} nicht gefunden`)

      // 2. Stripe Customer erstellen/updaten
      let customerId = tenant.stripe_customer_id
      const customerPayload = {
        name: billingDetails?.company_name || tenant.name,
        email: user.email,
        address: {
          line1: billingDetails?.address_line1,
          postal_code: billingDetails?.postal_code,
          city: billingDetails?.city,
          country: billingDetails?.country, // z.B. "DE"
        },
        metadata: { tenant_id: tenant.id.toString() }
      }

      if (!customerId) {
        const newCustomer = await stripe.customers.create(customerPayload)
        customerId = newCustomer.id
        await supabaseAdmin.from('tenants').update({ stripe_customer_id: customerId }).eq('id', tenant.id)
      } else {
        await stripe.customers.update(customerId, customerPayload)
      }

      // 3. Tax ID (VAT) setzen
      if (billingDetails?.vat_id) {
        const existingTaxIds = await stripe.customers.listTaxIds(customerId)
        for (const tax of existingTaxIds.data) {
          await stripe.customers.deleteTaxId(customerId, tax.id)
        }
        await stripe.customers.createTaxId(customerId, {
          type: 'eu_vat',
          value: billingDetails.vat_id
        })
      }

      // 4. Line Items für den Checkout zusammenstellen
      const priceId = cycle === 'yearly' ? packageData.stripe_price_id_base_yearly : packageData.stripe_price_id_base_monthly
      const lineItems = [{ price: priceId, quantity: 1 }]

      // Metered Billing anheften (Zusatzkunden & Gebühren)
      if (packageData.stripe_price_id_users) lineItems.push({ price: packageData.stripe_price_id_users })
      if (packageData.stripe_price_id_fees) lineItems.push({ price: packageData.stripe_price_id_fees })

      // 5. Subscription erstellen oder updaten (Payment Intent Logic)
      if (tenant.stripe_subscription_id) {
        // UPGRADE LOGIK (Proration, wie in deinem Python Code)
        // Hier würdest du stripe.subscriptions.update aufrufen
        // Zur Vereinfachung hier angedeutet:
        const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
          items: lineItems.map(item => ({ price: item.price })),
          proration_behavior: 'always_invoice',
          payment_settings: { save_default_payment_method: 'on_subscription' },
          metadata: { plan_name: plan, cycle: cycle, tenant_id: tenant.id.toString() }
        })

        return new Response(JSON.stringify({ status: 'updated', subscriptionId: updatedSub.id }), { headers: corsHeaders })
      } else {
        // NEUES ABO
        const sub = await stripe.subscriptions.create({
          customer: customerId,
          items: lineItems,
          payment_behavior: 'default_incomplete',
          payment_settings: { save_default_payment_method: 'on_subscription' },
          expand: ['latest_invoice.payment_intent'],
          metadata: { plan_name: plan, cycle: cycle, tenant_id: tenant.id.toString() }
        })

        const invoice = sub.latest_invoice as Stripe.Invoice
        const paymentIntent = invoice.payment_intent as Stripe.PaymentIntent

        return new Response(
            JSON.stringify({
              clientSecret: paymentIntent.client_secret,
              subscriptionId: sub.id,
              amountDue: invoice.amount_due / 100
            }),
            { headers: { ...corsHeaders, 'Content-Type': 'application/json' } }
        )
      }
    }

    throw new Error('Unbekannte Aktion')

  } catch (error) {
    console.error('Subscription error:', error)
    return new Response(JSON.stringify({ error: error.message }), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
  }
})