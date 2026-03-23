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
    const supabaseAdmin = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )

    // Auth Check: Wir brauchen den User aus dem Authorization Header
    const authHeader = req.headers.get('Authorization')
    if (!authHeader) throw new Error('Kein Authorization Header')

    const supabaseClient = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_ANON_KEY') ?? '',
      { global: { headers: { Authorization: authHeader } } }
    )

    const { data: { user }, error: authError } = await supabaseClient.auth.getUser()
    if (authError || !user) throw new Error('Nicht authentifiziert')

    // Admin Check in der 'users' Tabelle
    const { data: dbUser, error: dbUserError } = await supabaseAdmin
      .from('users')
      .select('is_superadmin')
      .eq('email', user.email?.toLowerCase())
      .maybeSingle()

    if (dbUserError || !dbUser || !dbUser.is_superadmin) {
        throw new Error('Nicht autorisiert: Nur Superadmins können Promo-Codes erstellen.')
    }

    const { code, name, duration_months, max_uses, expires_at, applicable_plans } = await req.json()

    if (!code || !duration_months) {
        throw new Error('Code und Dauer (Monate) sind erforderlich.')
    }

    // 1. Stripe Product IDs für applicable_plans holen
    let stripeProductIds: string[] | undefined = undefined
    if (applicable_plans && applicable_plans.length > 0) {
      const { data: packages } = await supabaseAdmin
        .from('subscription_packages')
        .select('stripe_product_id')
        .in('plan_name', applicable_plans)
        .not('stripe_product_id', 'is', null)

      if (packages && packages.length > 0) {
        stripeProductIds = packages.map(p => p.stripe_product_id)
      }
    }

    // 2. Stripe Coupon erstellen
    const coupon = await stripe.coupons.create({
      percent_off: 100,
      duration: 'repeating',
      duration_in_months: parseInt(duration_months),
      applies_to: stripeProductIds ? { products: stripeProductIds } : undefined,
      name: name || code,
    })

    // 3. Stripe Promotion Code erstellen
    const promotionCode = await stripe.promotionCodes.create({
      coupon: coupon.id,
      code: code.toUpperCase(),
      max_redemptions: max_uses ? parseInt(max_uses) : undefined,
      expires_at: expires_at ? Math.floor(new Date(expires_at).getTime() / 1000) : undefined,
    })

    // 4. In Supabase speichern
    const { data, error } = await supabaseAdmin
      .from('promo_codes')
      .insert({
        code: code.toUpperCase(),
        name,
        duration_months: parseInt(duration_months),
        max_uses: max_uses ? parseInt(max_uses) : null,
        expires_at: expires_at || null,
        stripe_coupon_id: coupon.id,
        stripe_promotion_code_id: promotionCode.id,
        applicable_plans: applicable_plans || [],
        created_by: user.id
      })
      .select()
      .single()

    if (error) {
      // Rollback Stripe (so gut es geht - Coupons können nicht gelöscht werden, wenn sie benutzt werden könnten, aber Promotion Codes können deaktiviert werden)
      await stripe.promotionCodes.update(promotionCode.id, { active: false })
      throw error
    }

    return jsonResponse(data)

  } catch (error) {
    return errorResponse(error.message, 400)
  }
})
