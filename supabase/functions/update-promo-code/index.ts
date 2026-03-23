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

    // Auth Check
    const authHeader = req.headers.get('Authorization')
    if (!authHeader) throw new Error('Kein Authorization Header')

    const supabaseClient = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_ANON_KEY') ?? '',
      { global: { headers: { Authorization: authHeader } } }
    )

    const { data: { user }, error: authError } = await supabaseClient.auth.getUser()
    if (authError || !user) throw new Error('Nicht authentifiziert')

    // Admin Check
    const { data: dbUser, error: dbUserError } = await supabaseAdmin
      .from('users')
      .select('is_superadmin')
      .eq('email', user.email?.toLowerCase())
      .maybeSingle()

    if (dbUserError || !dbUser || !dbUser.is_superadmin) {
        throw new Error('Nicht autorisiert: Nur Superadmins können Promo-Codes bearbeiten.')
    }

    const { id, is_active } = await req.json()

    if (!id || is_active === undefined) {
        throw new Error('ID und Status (is_active) sind erforderlich.')
    }

    // 1. Promo Code aus Supabase holen
    const { data: promoCode, error: fetchError } = await supabaseAdmin
      .from('promo_codes')
      .select('stripe_promotion_code_id')
      .eq('id', id)
      .single()

    if (fetchError || !promoCode) {
        throw new Error('Promo-Code nicht gefunden.')
    }

    // 2. Stripe Promotion Code aktualisieren
    await stripe.promotionCodes.update(promoCode.stripe_promotion_code_id, {
        active: is_active
    })

    // 3. In Supabase aktualisieren
    const { data, error: updateError } = await supabaseAdmin
      .from('promo_codes')
      .update({ is_active })
      .eq('id', id)
      .select()
      .single()

    if (updateError) throw updateError

    return jsonResponse(data)

  } catch (error) {
    return errorResponse(error.message, 400)
  }
})
