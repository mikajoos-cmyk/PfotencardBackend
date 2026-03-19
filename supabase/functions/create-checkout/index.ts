import { createAuthClient, createAdminClient } from '../_shared/supabase-client.ts'
import Stripe from 'https://esm.sh/stripe@14.14.0'
import { corsHeaders } from '../_shared/cors.ts'

import { getCountryCode } from '../_shared/country-mapping.ts'
const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') ?? '', {
  apiVersion: '2023-10-16',
  httpClient: Stripe.createFetchHttpClient(),
})

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })

  try {
    const supabaseAuth = createAuthClient(req)
    const supabaseAdmin = createAdminClient()

    const { data: { user }, error: userError } = await supabaseAuth.auth.getUser()
    if (userError || !user) throw new Error('Unauthorized')

    const payload = await req.json()
    const tenantId = payload.tenant_id
    const plan = payload.plan
    const cycle = payload.cycle
    let selectedAddons = payload.selected_addons || []
    const billingDetails = payload.billing_details
    const trialAllowed = payload.trial_allowed !== false

    // 1. Tenant laden
    const { data: tenant, error: tenantError } = await supabaseAdmin
        .from('tenants').select('*').eq('id', tenantId).single()
    if (tenantError || !tenant) throw new Error("Tenant not found")

    // 2. Paket und Addons bestimmen (Exakte Python Logik)
    let targetPlanName = plan
    const { data: checkPackage } = await supabaseAdmin
        .from('subscription_packages').select('*').ilike('plan_name', plan).single()

    if (checkPackage && checkPackage.package_type === 'addon') {
      if (!selectedAddons.includes(plan)) selectedAddons.push(plan)
      targetPlanName = tenant.plan || 'starter'
    }

    const { data: basePackage } = await supabaseAdmin
        .from('subscription_packages').select('*')
        .ilike('plan_name', targetPlanName).eq('package_type', 'base').single()

    if (!basePackage) throw new Error(`Basis-Paket '${targetPlanName}' nicht gefunden`)

    const targetPriceId = cycle === 'yearly' ? basePackage.stripe_price_id_base_yearly : basePackage.stripe_price_id_base_monthly
    if (!targetPriceId) throw new Error(`Kein Stripe-Preis für Zyklus '${cycle}' gefunden`)

    let targetAmount = parseFloat(cycle === 'yearly' ? basePackage.price_yearly : basePackage.price_monthly) || 0
    const subscriptionItems: any[] = [{ price: targetPriceId }]

    if (basePackage.stripe_price_id_users) subscriptionItems.push({ price: basePackage.stripe_price_id_users })
    if (basePackage.stripe_price_id_fees) subscriptionItems.push({ price: basePackage.stripe_price_id_fees })

    // Addons laden
    if (selectedAddons.length > 0) {
      const { data: addons } = await supabaseAdmin
          .from('subscription_packages').select('*')
          .in('plan_name', selectedAddons).eq('package_type', 'addon')

      for (const addon of addons || []) {
        const addonPriceId = cycle === 'yearly' ? addon.stripe_price_id_base_yearly : addon.stripe_price_id_base_monthly
        if (addonPriceId) {
          subscriptionItems.push({ price: addonPriceId })
          targetAmount += parseFloat(cycle === 'yearly' ? addon.price_yearly : addon.price_monthly) || 0
        }
      }
    }

    // 3. Stripe Customer & Billing Details Update
    let customerId = tenant.stripe_customer_id
    if (!customerId) {
      const customer = await stripe.customers.create({
        email: user.email,
        name: tenant.name,
        metadata: { tenant_id: tenant.id }
      })
      customerId = customer.id
      await supabaseAdmin.from('tenants').update({ stripe_customer_id: customerId }).eq('id', tenant.id)
    }

    if (billingDetails) {
      const customerName = billingDetails.company_name || billingDetails.name
      const normalizedCountry = getCountryCode(billingDetails.country)
      await stripe.customers.update(customerId, {
        name: customerName,
        address: {
          line1: billingDetails.address_line1,
          postal_code: billingDetails.postal_code,
          city: billingDetails.city,
          country: normalizedCountry,
        }
      })

      // Update Tenant Config JSON
      const currentConfig = tenant.config || {}
      currentConfig.invoice_settings = {
        ...(currentConfig.invoice_settings || {}),
        company_name: billingDetails.company_name,
        account_holder: billingDetails.name,
        address_line1: billingDetails.address_line1,
        address_line2: `${billingDetails.postal_code} ${billingDetails.city}`,
        vat_id: billingDetails.vat_id || null
      }
      await supabaseAdmin.from('tenants').update({ config: currentConfig }).eq('id', tenant.id)
    }

    // 4. Abo Logik (Update vs Create)
    let activeSubscription: any = null
    if (tenant.stripe_subscription_id) {
      try {
        activeSubscription = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id, {
          expand: ['items.data', 'schedule', 'latest_invoice.payment_intent']
        })
        if (['canceled', 'incomplete_expired'].includes(activeSubscription.status)) {
          activeSubscription = null
        }
      } catch (e) { activeSubscription = null }
    }

    const metadata = {
      tenant_id: tenant.id, plan_name: targetPlanName, cycle: cycle,
      upcoming_plan: "", upcoming_cycle: ""
    }

    if (activeSubscription) {
      const currentItem = activeSubscription.items.data[0]
      const currentPriceId = currentItem.price.id
      const currentPriceVal = (currentItem.price.unit_amount || 0) / 100.0

      if (currentPriceId === targetPriceId) {
        // Gleicher Plan, ggf. unbezahlt
        if (['incomplete', 'unpaid'].includes(activeSubscription.status)) {
          const inv = activeSubscription.latest_invoice as Stripe.Invoice
          const pi = inv?.payment_intent as Stripe.PaymentIntent
          return new Response(JSON.stringify({
            subscriptionId: activeSubscription.id, clientSecret: pi?.client_secret, status: "payment_needed"
          }), { headers: corsHeaders })
        }
        return new Response(JSON.stringify({ status: "updated", message: "Plan already active" }), { headers: corsHeaders })
      }

      const isUpgrade = targetAmount > currentPriceVal
      const isTrial = activeSubscription.status === 'trialing'

      // A) UPGRADE (Sofort)
      if (isUpgrade || isTrial) {
        if (activeSubscription.schedule) {
          const schedId = typeof activeSubscription.schedule === 'string' ? activeSubscription.schedule : activeSubscription.schedule.id
          try { await stripe.subscriptionSchedules.release(schedId) } catch(e){}
        }

        const newItems = activeSubscription.items.data.map((item: any) => ({ id: item.id, deleted: true }))
        subscriptionItems.forEach(si => newItems.push({ price: si.price }))

        const updatedSub = await stripe.subscriptions.update(activeSubscription.id, {
          items: newItems,
          proration_behavior: 'always_invoice',
          payment_behavior: 'allow_incomplete',
          metadata: metadata,
          cancel_at_period_end: false,
          expand: ['latest_invoice.payment_intent']
        })

        const inv = updatedSub.latest_invoice as Stripe.Invoice
        const amountDue = (inv?.amount_due || 0) / 100

        if (updatedSub.status === 'active' && amountDue === 0) {
          return new Response(JSON.stringify({ status: "success", subscriptionId: updatedSub.id }), { headers: corsHeaders })
        } else {
          const pi = inv?.payment_intent as Stripe.PaymentIntent
          return new Response(JSON.stringify({ status: "updated", clientSecret: pi?.client_secret, amountDue }), { headers: corsHeaders })
        }
      }
      // B) DOWNGRADE (Schedule)
      else {
        const subId = activeSubscription.id
        let schedId = typeof activeSubscription.schedule === 'string' ? activeSubscription.schedule : activeSubscription.schedule?.id

        if (!schedId) {
          const schedule = await stripe.subscriptionSchedules.create({ from_subscription: subId })
          schedId = schedule.id
        }

        const scheduleObj = await stripe.subscriptionSchedules.retrieve(schedId)
        const periodEndTs = activeSubscription.current_period_end

        await stripe.subscriptionSchedules.update(schedId, {
          end_behavior: 'release',
          phases: [
            {
              start_date: scheduleObj.phases[0].start_date,
              end_date: periodEndTs,
              items: activeSubscription.items.data.map((item: any) => ({ price: item.price.id, quantity: 1 })),
            },
            {
              start_date: periodEndTs,
              items: subscriptionItems.map(si => ({ price: si.price, quantity: 1 })),
              metadata: metadata
            }
          ]
        })

        await stripe.subscriptions.update(subId, {
          metadata: { ...metadata, upcoming_plan: plan, upcoming_cycle: cycle }
        })

        return new Response(JSON.stringify({ status: "success", subscriptionId: subId, message: "Downgrade vorgemerkt" }), { headers: corsHeaders })
      }
    }
    // C) NEUES ABO
    else {
      let trialDays = 0
      if (trialAllowed) {
        const createdDate = new Date(tenant.created_at)
        const trialEndAbsolute = new Date(createdDate.getTime() + 14 * 24 * 60 * 60 * 1000)
        const now = new Date()
        if (trialEndAbsolute > now) {
          trialDays = Math.floor((trialEndAbsolute.getTime() - now.getTime()) / (1000 * 3600 * 24))
        }
      }

      const subData: any = {
        customer: customerId,
        items: subscriptionItems,
        payment_behavior: 'allow_incomplete',
        payment_settings: { save_default_payment_method: 'on_subscription' },
        expand: ['latest_invoice.payment_intent', 'pending_setup_intent'],
        metadata: metadata
      }
      if (trialDays > 0) subData.trial_period_days = trialDays

      const sub = await stripe.subscriptions.create(subData)
      const inv = sub.latest_invoice as Stripe.Invoice
      const amountDue = (inv?.amount_due || 0) / 100

      if (sub.status === 'active' && amountDue === 0) {
        return new Response(JSON.stringify({ status: "success", subscriptionId: sub.id }), { headers: corsHeaders })
      }

      let secret = (inv?.payment_intent as Stripe.PaymentIntent)?.client_secret
      if (!secret && sub.pending_setup_intent) {
        secret = (sub.pending_setup_intent as Stripe.SetupIntent).client_secret
      }

      return new Response(JSON.stringify({ status: "created", subscriptionId: sub.id, clientSecret: secret, amountDue }), { headers: corsHeaders })
    }

  } catch (error) {
    console.error(error)
    return new Response(JSON.stringify({ error: error.message }), { headers: corsHeaders, status: 400 })
  }
})