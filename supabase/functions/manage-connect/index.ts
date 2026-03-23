import { createClient } from 'npm:@supabase/supabase-js@^2.40.0'
import Stripe from 'npm:stripe@^17.0.0'
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

    // User über das Frontend-Token verifizieren
    let userEmail: string | undefined;
    const { data: { user }, error: userError } = await supabaseClient.auth.getUser()

    if (user && !userError) {
      userEmail = user.email;
    } else {
      // Fallback: Wenn kein Supabase-User gefunden wurde, könnte es ein FastAPI-Token sein.
      const authHeader = req.headers.get('Authorization');
      if (authHeader && authHeader.startsWith('Bearer ')) {
        try {
          const token = authHeader.split(' ')[1];
          const base64Url = token.split('.')[1];
          const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
          const payload = JSON.parse(atob(base64));
          userEmail = payload.email || payload.sub;
        } catch (e) {
          console.error('Fehler beim Dekodieren des Custom-JWT:', e);
        }
      }
    }

    if (!userEmail) throw new Error('Nicht authentifiziert')

    const body = await req.json()
    const { action, tenantId, returnUrl, refreshUrl } = body

    console.log(`[manage-connect] Action: ${action}, TenantId: ${tenantId}, User: ${userEmail}`);

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

    if (tenantError || !tenant) {
      throw new Error('Tenant nicht gefunden');
    }

    // Sicherheit: Prüfen ob die E-Mail ein Admin für diesen Tenant ist
    const { data: dbUser } = await supabaseAdmin
        .from('users')
        .select('role')
        .eq('tenant_id', tenantId)
        .eq('email', userEmail.toLowerCase())
        .maybeSingle();

    if (!dbUser || dbUser.role !== 'admin') {
      throw new Error('Nicht autorisiert: Nur Mandanten-Administratoren können diese Aktion ausführen.')
    }

    if (action === 'create_connect_link') {
      let stripeAccountId = tenant.stripe_account_id

      // 1. Account erstellen, falls noch nicht vorhanden
      if (!stripeAccountId) {
        console.log(`[manage-connect] Erstelle neuen Stripe Express Account für Tenant: ${tenant.name}`);
        const account = await stripe.accounts.create({
          type: 'express',
          country: 'DE',
          email: tenant.support_email || userEmail,
          capabilities: {
            transfers: { requested: true },
          },
          metadata: {
            tenant_id: tenant.id.toString(),
            tenant_name: tenant.name
          }
        });
        stripeAccountId = account.id

        // In DB speichern
        const { error: updateError } = await supabaseAdmin
          .from('tenants')
          .update({ stripe_account_id: stripeAccountId })
          .eq('id', tenant.id)

        if (updateError) {
          console.error('[manage-connect] Fehler beim Speichern der Stripe Account ID:', updateError);
          throw new Error('Fehler beim Speichern der Stripe Account ID');
        }
      }

      // 2. Onboarding-Link generieren
      console.log(`[manage-connect] Generiere Onboarding-Link für Account: ${stripeAccountId}`);
      const accountLink = await stripe.accountLinks.create({
        account: stripeAccountId,
        refresh_url: refreshUrl || returnUrl,
        return_url: returnUrl,
        type: 'account_onboarding',
      });

      return new Response(JSON.stringify({ url: accountLink.url }), { 
        headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
      });
    }

    throw new Error('Unbekannte Aktion')

  } catch (error) {
    console.error('Connect error:', error)
    return new Response(JSON.stringify({ error: error.message }), { 
      status: 400, 
      headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
    })
  }
})
