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

    // User über das Frontend-Token verifizieren (Sicherheit!)
    let userEmail: string | undefined;
    const { data: { user }, error: userError } = await supabaseClient.auth.getUser()

    if (user && !userError) {
      userEmail = user.email;
    } else {
      // Fallback: Wenn kein Supabase-User gefunden wurde, könnte es ein FastAPI-Token von der Marketing-Seite sein.
      const authHeader = req.headers.get('Authorization');
      if (authHeader && authHeader.startsWith('Bearer ')) {
        try {
          const token = authHeader.split(' ')[1];
          const base64Url = token.split('.')[1];
          const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
          const payload = JSON.parse(atob(base64));
          userEmail = payload.email || payload.sub;
          console.log('Nutze dekodierte E-Mail aus FastAPI-Token:', userEmail);
        } catch (e) {
          console.error('Fehler beim Dekodieren des Custom-JWT:', e);
        }
      }
    }

    if (!userEmail) throw new Error('Nicht authentifiziert')

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

    // Sicherheit: Prüfen ob die E-Mail ein Admin für diesen Tenant ist (Sync mit Pfotencard DB)
    const { data: dbUser } = await supabaseAdmin
        .from('users')
        .select('role')
        .eq('tenant_id', tenantId)
        .eq('email', userEmail.toLowerCase())
        .maybeSingle();

    if (!dbUser || dbUser.role !== 'admin') {
      throw new Error('Nicht autorisiert: Nur Mandanten-Administratoren können diese Aktion ausführen.')
    }

    // ==========================================
    // HELPER: GET PRICE IDs
    // ==========================================
    async function getStripePriceIdsForPlanAndAddons(plan: string, addons: string[], cycle: string = 'monthly') {
      const priceIds: string[] = [];

      // 1. Basis-Paket
      const { data: basePackage } = await supabaseAdmin
          .from('subscription_packages')
          .select('*')
          .eq('plan_name', plan)
          .eq('package_type', 'base')
          .single();

      if (basePackage) {
        const basePriceId = cycle === 'yearly' ? basePackage.stripe_price_id_base_yearly : basePackage.stripe_price_id_base_monthly;
        if (basePriceId) priceIds.push(basePriceId);

        // Metered Billing (Zusatzkunden & Gebühren)
        if (basePackage.stripe_price_id_users) priceIds.push(basePackage.stripe_price_id_users);
        if (basePackage.stripe_price_id_fees) priceIds.push(basePackage.stripe_price_id_fees);
      }

      // 2. Addons
      if (addons && addons.length > 0) {
        const { data: addonPackages } = await supabaseAdmin
            .from('subscription_packages')
            .select('*')
            .in('plan_name', addons)
            .eq('package_type', 'addon');

        if (addonPackages) {
          for (const addon of addonPackages) {
            const addonPriceId = cycle === 'yearly' ? addon.stripe_price_id_base_yearly : addon.stripe_price_id_base_monthly;
            if (addonPriceId) priceIds.push(addonPriceId);
          }
        }
      }

      return priceIds;
    }

    // ==========================================
    // ACTION: GET STATUS
    // ==========================================
    if (action === 'get_status') {
      return new Response(JSON.stringify({
        tenant_id: tenant.id,
        plan: tenant.plan,
        active_addons: tenant.config?.active_addons || [],
        stripe_subscription_id: tenant.stripe_subscription_id,
        stripe_subscription_status: tenant.stripe_subscription_status
      }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
    }

    // ==========================================
    // ACTION: PREVIEW UPGRADE (Preisanzeige)
    // ==========================================
    if (action === 'preview_upgrade') {
      const { newPlan, newAddons, cycle = 'monthly' } = body;

      // Alle Packages abrufen für Zuordnung von Price IDs zu Typ (Base vs Addon)
      const { data: allPackages } = await supabaseAdmin
        .from('subscription_packages')
        .select('stripe_price_id_base_monthly, stripe_price_id_base_yearly, stripe_price_id_users, stripe_price_id_fees, package_type');

      const priceTypeMap: Record<string, string> = {};
      allPackages?.forEach(p => {
        if (p.stripe_price_id_base_monthly) priceTypeMap[p.stripe_price_id_base_monthly] = p.package_type;
        if (p.stripe_price_id_base_yearly) priceTypeMap[p.stripe_price_id_base_yearly] = p.package_type;
        if (p.stripe_price_id_users) priceTypeMap[p.stripe_price_id_users] = 'base';
        if (p.stripe_price_id_fees) priceTypeMap[p.stripe_price_id_fees] = 'base';
      });

      const newPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);
      const prorationDate = Math.floor(Date.now() / 1000); // Genau jetzt

      const previewParams: any = {
        automatic_tax: { enabled: true },
      };

      if (tenant.stripe_customer_id) {
        previewParams.customer = tenant.stripe_customer_id;
      } else {
        // Fallback für Vorschau ohne registrierten Stripe-Kunden
        // Versuchen wir, Adressdaten aus dem Tenant-Profil zu nutzen für die Steuer-Vorschau
        previewParams.customer_details = {
          address: { 
            line1: tenant.street || undefined,
            city: tenant.city || undefined,
            postal_code: tenant.postcode || tenant.postal_code || undefined,
            country: tenant.country_code || tenant.country || 'DE' 
          }
        };
      }

      // Nur wenn wir ein aktives Abo haben, machen wir eine Upgrade-Vorschau (Prorations)
      // Ansonsten machen wir eine Vorschau für ein NEUES Abo.
      const hasActiveSub = tenant.stripe_subscription_id && 
                           tenant.stripe_subscription_status && 
                           !['canceled', 'incomplete_expired'].includes(tenant.stripe_subscription_status);

      if (hasActiveSub) {
        try {
          // --- UPGRADE-VORSCHAU ---
          // 1. Aktuelle Subscription abrufen
          const currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);
          
          if (currentSub.status === 'canceled') {
            // Falls es doch canceled ist (Sync-Gap), neues Abo Vorschau
            previewParams.subscription_items = newPriceIds.map(priceId => ({ price: priceId }));
          } else {
            // Alte Items virtuell löschen und neue hinzufügen
            const deletedItems = currentSub.items.data.map(item => ({
              id: item.id,
              deleted: true
            }));
            const addedItems = newPriceIds.map(priceId => ({ price: priceId }));

            previewParams.subscription = tenant.stripe_subscription_id;
            previewParams.subscription_items = [...deletedItems, ...addedItems];
            previewParams.subscription_proration_date = prorationDate;
          }
        } catch (e) {
          console.error("Fehler beim Abrufen der Subscription für Vorschau:", e.message);
          // Fallback auf neues Abo Vorschau
          previewParams.subscription_items = newPriceIds.map(priceId => ({ price: priceId }));
        }
      } else {
        // --- NEUES ABO VORSCHAU ---
        previewParams.subscription_items = newPriceIds.map(priceId => ({ price: priceId }));
      }

      try {
        const upcomingInvoice = await stripe.invoices.retrieveUpcoming(previewParams);

        // Wir sortieren die Positionen aus der Stripe-Vorschau:
        // prorationLines = Das was anteilig für den aktuellen Restmonat berechnet wird
        // regularLines = Das was ab dem nächsten regulären Rechnungsdatum gilt
        const prorationLines = upcomingInvoice.lines.data.filter(line => line.proration);
        const regularLines = upcomingInvoice.lines.data.filter(line => !line.proration);

        // Helper: Berechnet Details inkl. Steuern
        const calculateLineDetails = (lines: any[], filterAddonCredits = false) => {
          let netCents = 0;
          let taxCents = 0;
          lines.forEach(line => {
            // Logik für Gutschriften (negative Beträge):
            // Bei Modulen (Addons) ignorieren wir Gutschriften heute (da Vormerkung).
            // Bei Basis-Paketen erlauben wir sie, um Upgrades korrekt zu verrechnen.
            if (line.amount < 0 && filterAddonCredits) {
              const priceId = line.price?.id;
              const type = priceTypeMap[priceId];
              if (type === 'addon') return;
            }

            let lineNet = line.amount;
            if (line.tax_amounts) {
              line.tax_amounts.forEach((tax: any) => {
                if (!tax.inclusive) {
                  taxCents += tax.amount;
                } else {
                  // Bei inklusiven Steuern ist die Steuer bereits in line.amount enthalten.
                  // Wir ziehen sie hier ab, um den reinen Netto-Betrag zu erhalten.
                  lineNet -= tax.amount;
                  taxCents += tax.amount;
                }
              });
            }
            netCents += lineNet;
          });
          return { 
            net: netCents / 100, 
            tax: taxCents / 100, 
            total: (netCents + taxCents) / 100 
          };
        };

        const prorationDetails = calculateLineDetails(prorationLines, true); // Filtert Addon-Gutschriften aus
        const regularDetails = calculateLineDetails(regularLines);

        let amountDueToday = 0;
        let taxDueToday = 0;
        let netDueToday = 0;

        if (tenant.stripe_subscription_id) {
          // Beim Upgrade zahlt er heute NUR die Prorations (anteilige Kosten)
          amountDueToday = Math.max(0, prorationDetails.total);
          taxDueToday = Math.max(0, prorationDetails.tax);
          netDueToday = Math.max(0, prorationDetails.net);
        } else {
          // Neues Abo: Er zahlt heute den vollen Vorschau-Betrag
          amountDueToday = upcomingInvoice.amount_due / 100;
          taxDueToday = (upcomingInvoice.tax || 0) / 100;
          netDueToday = (upcomingInvoice.subtotal || 0) / 100;
        }

        const linesWithMetadata = upcomingInvoice.lines.data.map(line => ({
          ...line,
          package_type: priceTypeMap[line.price?.id] || 'unknown'
        }));

        return new Response(JSON.stringify({
          amountDueToday,
          taxDueToday,
          netDueToday,
          nextBillingDate: upcomingInvoice.next_payment_attempt || upcomingInvoice.period_end,
          amountDueNextMonth: regularDetails.total,
          taxDueNextMonth: regularDetails.tax,
          netDueNextMonth: regularDetails.net,
          lines: linesWithMetadata,
          currency: upcomingInvoice.currency
        }), { 
          headers: { 
            ...corsHeaders, 
            'Content-Type': 'application/json' 
          } 
        });

      } catch (e: any) {
        console.error("Stripe Vorschau fehlgeschlagen:", e.message);
        // Wir werfen keinen harten Fehler, damit das Frontend auf Fallback-Werte zurückgreifen kann
        return new Response(JSON.stringify({
          error: e.message,
          amountDueToday: 0,
          taxDueToday: 0,
          netDueToday: 0,
          amountDueNextMonth: 0,
          taxDueNextMonth: 0,
          netDueNextMonth: 0,
          lines: []
        }), { 
          status: 200, // Wir geben 200 zurück, damit das Frontend die "error" Property im JSON lesen kann
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      }
    }

    // ==========================================
    // ACTION: UPDATE SUBSCRIPTION (Der Kauf)
    // ==========================================
    if (action === 'update_subscription') {
      const { newPlan, newAddons, cycle = 'monthly' } = body;

      const hasActiveSub = tenant.stripe_subscription_id && 
                           tenant.stripe_subscription_status && 
                           !['canceled', 'incomplete_expired'].includes(tenant.stripe_subscription_status);
      
      if (!hasActiveSub) throw new Error("Kein aktives Abo für Update gefunden. Bitte nutze den Checkout.");

      const newPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);
      const itemsArray = newPriceIds.map(priceId => ({ price: priceId }));

      // Aktuelle Subscription abrufen, um die Item-IDs für den Austausch zu finden (Stripe empfiehlt das für Updates)
      const currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);

      // Lösche alle alten Items und füge neue hinzu
      const itemsToUpdate = currentSub.items.data.map(item => ({
        id: item.id,
        deleted: true
      })).concat(itemsArray.map(item => ({ price: item.price })));

      // Vorschau generieren um zu sehen ob es ein Downgrade (Gutschrift) oder Upgrade (Zahlung) ist
      let behavior: 'always_invoice' | 'none' = 'always_invoice';
      try {
        const previewInvoice = await stripe.invoices.retrieveUpcoming({
          customer: tenant.stripe_customer_id,
          subscription: tenant.stripe_subscription_id,
          subscription_items: itemsToUpdate,
          subscription_proration_date: Math.floor(Date.now() / 1000),
        });
        // Wenn der Betrag <= 0 ist, ist es ein Downgrade oder keine Änderung -> keine sofortige Rechnung (vormerken)
        if (previewInvoice.amount_due <= 0) {
          behavior = 'none';
        }
      } catch (e) {
        console.warn("Konnte Vorschau für Update nicht laden, nutze Standard-Verhalten:", e.message);
      }

      // Stripe die Subscription überschreiben lassen
      const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
        items: itemsToUpdate,
        proration_behavior: behavior,
        metadata: { plan_name: newPlan, addons: JSON.stringify(newAddons), cycle: cycle, tenant_id: tenant.id.toString() }
      });

      // Die erzeugte anteilige Rechnung direkt bezahlen lassen (falls Karte hinterlegt ist)
      const latestInvoiceId = updatedSub.latest_invoice;
      if (latestInvoiceId) {
        const invoice = await stripe.invoices.retrieve(latestInvoiceId as string);
        if (invoice.status === 'open' && invoice.amount_due > 0) {
          // Versuchen abzubuchen
          await stripe.invoices.pay(invoice.id);
        }
      }

      // Update Pfotencard DB (Tenant)
      const config = tenant.config || {};
      // Addons in Config synchronisieren
      config['active_addons'] = newAddons;
      
      await supabaseAdmin.from('tenants').update({ 
        plan: newPlan,
        config: config
      }).eq('id', tenant.id);

      return new Response(JSON.stringify({ status: 'success' }), { headers: corsHeaders });
    }

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
      // ... (Dein existierender Code bleibt hier unangetastet)
      const { plan, cycle, billingDetails, addons = [] } = body

      const { data: packageData } = await supabaseAdmin
          .from('subscription_packages')
          .select('*')
          .eq('plan_name', plan)
          .eq('package_type', 'base')
          .single()

      if (!packageData) throw new Error(`Plan ${plan} nicht gefunden`)

      let customerId = tenant.stripe_customer_id
      const customerPayload = {
        name: billingDetails?.company_name || tenant.name,
        email: userEmail,
        address: {
          line1: billingDetails?.address_line1,
          postal_code: billingDetails?.postal_code,
          city: billingDetails?.city,
          country: billingDetails?.country,
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

      const priceId = cycle === 'yearly' ? packageData.stripe_price_id_base_yearly : packageData.stripe_price_id_base_monthly
      const lineItems = [{ price: priceId, quantity: 1 }]

      if (packageData.stripe_price_id_users) lineItems.push({ price: packageData.stripe_price_id_users })
      if (packageData.stripe_price_id_fees) lineItems.push({ price: packageData.stripe_price_id_fees })

      if (tenant.stripe_subscription_id) {
        const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
          items: lineItems.map(item => ({ price: item.price })),
          proration_behavior: 'always_invoice',
          automatic_tax: { enabled: true },
          payment_settings: { save_default_payment_method: 'on_subscription' },
          metadata: { plan_name: plan, cycle: cycle, tenant_id: tenant.id.toString() }
        })

        return new Response(JSON.stringify({ status: 'updated', subscriptionId: updatedSub.id }), { headers: corsHeaders })
      } else {
        const sub = await stripe.subscriptions.create({
          customer: customerId,
          items: lineItems,
          payment_behavior: 'default_incomplete',
          automatic_tax: { enabled: true },
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