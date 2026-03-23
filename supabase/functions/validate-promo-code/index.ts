import { createClient } from 'npm:@supabase/supabase-js@^2.40.0'
import Stripe from 'npm:stripe@^17.0.0'
import { corsHeaders, handleCors, jsonResponse, errorResponse } from '../_shared/cors.ts'

const stripe = new Stripe(Deno.env.get('STRIPE_SECRET_KEY') || '', {
  apiVersion: '2023-10-16',
})

Deno.serve(async (req) => {
  const corsRes = handleCors(req)
  if (corsRes) return corsRes

  try {
    const { code, plan, tenant_id } = await req.json()

    if (!code) throw new Error('Code ist erforderlich')

    const supabaseAdmin = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )

    // 1. Supabase Check
    const { data: promo, error: promoError } = await supabaseAdmin
      .from('promo_codes')
      .select('*')
      .eq('code', code.toUpperCase())
      .single()

    if (promoError || !promo) {
      throw new Error('Gutscheincode ungültig oder nicht gefunden')
    }

    if (!promo.is_active) {
      throw new Error('Gutscheincode ist nicht mehr aktiv')
    }

    if (promo.max_uses && promo.current_uses >= promo.max_uses) {
      throw new Error('Gutscheincode hat das Nutzungslimit erreicht')
    }

    if (promo.expires_at && new Date(promo.expires_at) < new Date()) {
      throw new Error('Gutscheincode ist abgelaufen')
    }

    if (plan && promo.applicable_plans && promo.applicable_plans.length > 0) {
      if (!promo.applicable_plans.includes(plan)) {
        throw new Error(`Gutscheincode ist nicht für den Plan '${plan}' gültig`)
      }
    }

    // 2. Prüfung, ob Tenant den Code bereits genutzt hat
    if (tenant_id) {
        const { data: redemption } = await supabaseAdmin
            .from('promo_code_redemptions')
            .select('id')
            .eq('promo_code_id', promo.id)
            .eq('tenant_id', tenant_id)
            .maybeSingle()

        if (redemption) {
            throw new Error('Sie haben diesen Gutscheincode bereits eingelöst')
        }
    }

    // 3. Stripe Check
    const stripeCodes = await stripe.promotionCodes.list({
      code: code.toUpperCase(),
      active: true,
      expand: ['data.coupon'],
    })

    if (stripeCodes.data.length === 0) {
      throw new Error('Gutscheincode in Stripe nicht aktiv')
    }

    const stripePromo = stripeCodes.data[0]
    
    // Optional: Stripe Product Check (wurde schon in Supabase gemacht, aber doppelt hält besser)
    if (plan) {
        const { data: pkg } = await supabaseAdmin
            .from('subscription_packages')
            .select('stripe_product_id')
            .eq('plan_name', plan)
            .single()
        
        if (pkg?.stripe_product_id && stripePromo.coupon.applies_to?.products) {
            if (!stripePromo.coupon.applies_to.products.includes(pkg.stripe_product_id)) {
                throw new Error('Gutscheincode gilt nicht für dieses Produkt (Stripe Check)')
            }
        }
    }

    return jsonResponse({
      valid: true,
      promoCodeId: promo.id,
      stripePromotionCodeId: stripePromo.id,
      percentOff: stripePromo.coupon.percent_off,
      durationMonths: promo.duration_months,
      name: promo.name
    })

  } catch (error) {
    return errorResponse(error.message, 400)
  }
})
