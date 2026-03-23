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

    console.log(`[manage-subscription] Action: ${action}, TenantId: ${tenantId}, User: ${userEmail}`);

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
      console.error('[manage-subscription] Tenant nicht gefunden:', { tenantId, error: tenantError?.message });
      throw new Error('Tenant nicht gefunden');
    }

    console.log('[manage-subscription] Tenant gefunden:', { id: tenant.id, plan: tenant.plan, active_addons: tenant.config?.active_addons });

    // Sicherheit: Prüfen ob die E-Mail ein Admin für diesen Tenant ist (Sync mit Pfotencard DB)
    const { data: dbUser } = await supabaseAdmin
        .from('users')
        .select('role')
        .eq('tenant_id', tenantId)
        .eq('email', userEmail.toLowerCase())
        .maybeSingle();

    if (!dbUser || dbUser.role !== 'admin') {
      console.error('[manage-subscription] Nicht autorisiert:', { userEmail, dbUser, tenant_id: tenantId });
      throw new Error('Nicht autorisiert: Nur Mandanten-Administratoren können diese Aktion ausführen.')
    }

    console.log('[manage-subscription] Authorization OK, proceeding with action:', action);

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

    async function syncTenantAddons(tenantId: number, addonNames: string[]) {
      console.log("Syncing addons for tenant", { tenantId, addonNames });
      
      // 1. Bestehende löschen
      await supabaseAdmin.from('tenant_addons').delete().eq('tenant_id', tenantId);
      
      if (!addonNames || addonNames.length === 0) return;
      
      // 2. IDs der Addons holen
      const { data: addonPackages } = await supabaseAdmin
        .from('subscription_packages')
        .select('id')
        .in('plan_name', addonNames)
        .eq('package_type', 'addon');
        
      if (!addonPackages || addonPackages.length === 0) return;
      
      // 3. Neue einfügen
      const inserts = addonPackages.map((p: any) => ({
        tenant_id: tenantId,
        addon_id: p.id,
        removes_at_period_end: false
      }));
      
      await supabaseAdmin.from('tenant_addons').insert(inserts);
    }

    async function redeemPromoCode(promoCodeId: string, tenantId: number) {
      if (!promoCodeId) return;

      console.log('[manage-subscription] Löse Promo-Code ein:', promoCodeId);

      // Finde den internen Promo Code anhand der Stripe Promotion Code ID
      const { data: promo, error: promoError } = await supabaseAdmin
        .from('promo_codes')
        .select('*')
        .eq('stripe_promotion_code_id', promoCodeId)
        .single();

      if (promoError || !promo) {
        console.error('[manage-subscription] Promo Code nicht in DB gefunden für Stripe ID:', promoCodeId);
        return;
      }

      // Eintrag in redemptions (Unique Constraint auf promo_code_id, tenant_id verhindert Mehrfachnutzung)
      const { error: redeemError } = await supabaseAdmin.from('promo_code_redemptions').insert({
        promo_code_id: promo.id,
        tenant_id: tenantId,
        applied_months: promo.duration_months
      });

      if (redeemError) {
          console.warn('[manage-subscription] Fehler beim Erstellen des Redemption-Eintrags (vielleicht schon eingelöst):', redeemError.message);
          return;
      }

      // Zähler erhöhen
      await supabaseAdmin
        .from('promo_codes')
        .update({ current_uses: (promo.current_uses || 0) + 1 })
        .eq('id', promo.id);
    }

    const hasActiveSub = !!(tenant.stripe_subscription_id && 
                           tenant.stripe_subscription_status && 
                           !['canceled', 'incomplete_expired'].includes(tenant.stripe_subscription_status));

    // ==========================================
    // ACTION: GET_PAYMENT_METHODS
    // ==========================================
    if (action === 'get_payment_methods') {
      if (!tenant.stripe_customer_id) {
        return new Response(JSON.stringify({ paymentMethods: [] }), { 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      }

      try {
        const paymentMethods = await stripe.paymentMethods.list({
          customer: tenant.stripe_customer_id,
          type: 'card',
        });

        return new Response(JSON.stringify({ 
          paymentMethods: paymentMethods.data.map(pm => ({
            id: pm.id,
            type: pm.type,
            card: pm.card ? {
              brand: pm.card.brand,
              last4: pm.card.last4,
              exp_month: pm.card.exp_month,
              exp_year: pm.card.exp_year,
            } : null,
          }))
        }), { 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      } catch (e: any) {
        console.error("Fehler beim Abrufen der Zahlungsmethoden:", e.message);
        return new Response(JSON.stringify({ error: e.message }), { 
          status: 400, 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      }
    }

    // ==========================================
    // ACTION: GET STATUS
    // ==========================================
    if (action === 'get_status') {
      const { data: cancelledAddonsData } = await supabaseAdmin
        .from('tenant_addons')
        .select('subscription_packages(plan_name)')
        .eq('tenant_id', tenant.id)
        .eq('removes_at_period_end', true);
      
      const cancelled_addons = (cancelledAddonsData || []).map((a: any) => a.subscription_packages.plan_name);

      return new Response(JSON.stringify({
        tenant_id: tenant.id,
        plan: tenant.plan,
        upcoming_plan: tenant.upcoming_plan,
        active_addons: tenant.config?.active_addons || [],
        upcoming_addons: tenant.upcoming_addons || [],
        cancelled_addons: cancelled_addons,
        stripe_subscription_id: tenant.stripe_subscription_id,
        stripe_subscription_status: tenant.stripe_subscription_status,
        hasActiveSub
      }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
    }

    // ==========================================
    // ACTION: GET UPCOMING INVOICE (Nächste Zahlung)
    // ==========================================
    if (action === 'get_upcoming_invoice') {
      if (!tenant.stripe_customer_id) {
        return new Response(JSON.stringify({ error: 'Kein Stripe-Kunde gefunden' }), { 
          status: 200, 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      }

      try {
        // Alle Packages abrufen für Zuordnung von Price IDs zu Typ (Base vs Addon)
        const { data: allPackages } = await supabaseAdmin
          .from('subscription_packages')
          .select('plan_name, stripe_price_id_base_monthly, stripe_price_id_base_yearly, stripe_price_id_users, stripe_price_id_fees, package_type');

        const priceTypeMap: Record<string, string> = {};
        allPackages?.forEach(p => {
          if (p.stripe_price_id_base_monthly) priceTypeMap[p.stripe_price_id_base_monthly] = p.package_type;
          if (p.stripe_price_id_base_yearly) priceTypeMap[p.stripe_price_id_base_yearly] = p.package_type;
          if (p.stripe_price_id_users) priceTypeMap[p.stripe_price_id_users] = 'base';
          if (p.stripe_price_id_fees) priceTypeMap[p.stripe_price_id_fees] = 'base';
        });

        let upcomingInvoice;
        
        if (tenant.upcoming_plan) {
           // Workaround for Stripe Schedule "release" bug:
           // When a Schedule's final phase releases, Stripe's Upcoming Invoice generator
           // natively evaluates it as having a 0-duration, dropping advance charges.
           // To get the TRUE upcoming invoice cost, we bypass the Schedule evaluation
           // and manually pass the exact items. 
           let upcomingAddons: string[] = [];
           if (typeof tenant.upcoming_addons === 'object') {
             upcomingAddons = tenant.upcoming_addons || [];
           } else if (typeof tenant.upcoming_addons === 'string') {
             try { upcomingAddons = JSON.parse(tenant.upcoming_addons); } catch(e) {}
           }
           const finalPriceIds = await getStripePriceIdsForPlanAndAddons(tenant.upcoming_plan, upcomingAddons, tenant.upcoming_cycle || 'monthly');
           
           upcomingInvoice = await stripe.invoices.retrieveUpcoming({
             customer: tenant.stripe_customer_id,
             subscription_items: finalPriceIds.map(id => ({ price: id })),
             automatic_tax: { enabled: true }
           });
        } else {
           upcomingInvoice = await stripe.invoices.retrieveUpcoming({
             customer: tenant.stripe_customer_id,
             automatic_tax: { enabled: true },
           });
        }

        const linesWithMetadata = upcomingInvoice.lines.data.map(line => ({
          ...line,
          package_type: priceTypeMap[line.price?.id] || 'unknown'
        }));

        return new Response(JSON.stringify({
          total: upcomingInvoice.total / 100,
          subtotal: upcomingInvoice.subtotal / 100,
          tax: (upcomingInvoice.tax || 0) / 100,
          next_payment_attempt: upcomingInvoice.next_payment_attempt || upcomingInvoice.period_end,
          currency: upcomingInvoice.currency,
          lines: linesWithMetadata
        }), { 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      } catch (e: any) {
        console.error("Fehler beim Abrufen der nächsten Rechnung:", e.message);
        return new Response(JSON.stringify({ error: e.message }), { 
          status: 200, 
          headers: { ...corsHeaders, 'Content-Type': 'application/json' } 
        });
      }
    }

    // ==========================================
    // ACTION: PREVIEW UPGRADE (Preisanzeige)
    // ==========================================
    if (action === 'preview_upgrade') {
      const { newPlan, newAddons, cycle = 'monthly' } = body;

      // Alle Packages abrufen für Zuordnung von Price IDs zu Typ (Base vs Addon)
      const { data: allPackages } = await supabaseAdmin
        .from('subscription_packages')
        .select('plan_name, stripe_price_id_base_monthly, stripe_price_id_base_yearly, stripe_price_id_users, stripe_price_id_fees, package_type, price_monthly, price_yearly');

      const priceTypeMap: Record<string, string> = {};
      allPackages?.forEach(p => {
        if (p.stripe_price_id_base_monthly) priceTypeMap[p.stripe_price_id_base_monthly] = p.package_type;
        if (p.stripe_price_id_base_yearly) priceTypeMap[p.stripe_price_id_base_yearly] = p.package_type;
        if (p.stripe_price_id_users) priceTypeMap[p.stripe_price_id_users] = 'base';
        if (p.stripe_price_id_fees) priceTypeMap[p.stripe_price_id_fees] = 'base';
      });

      let currentSub: any = null;
      let currentCycleFromMeta = 'monthly';
      
      if (hasActiveSub) {
        try {
          currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);
          currentCycleFromMeta = currentSub.metadata?.cycle || 'monthly';
        } catch (e: any) {
          console.error("Fehler beim Abrufen der Subscription für Vorschau:", e.message);
        }
      }

      const currentPkg = allPackages?.find(p => p.plan_name === tenant.plan && p.package_type === 'base');
      const newPkg = allPackages?.find(p => p.plan_name === newPlan && p.package_type === 'base');
      
      const currentBasePrice = currentCycleFromMeta === 'yearly' ? (currentPkg?.price_yearly || 0) : (currentPkg?.price_monthly || 0);
      const newBasePrice = cycle === 'yearly' ? (newPkg?.price_yearly || 0) : (newPkg?.price_monthly || 0);
      
      const isBaseDowngrade = newBasePrice < currentBasePrice;
      const isBaseUpgrade = newBasePrice > currentBasePrice;

      // Berechne immediate (Sofort-) Status analog zu update_subscription
      const immediatePlan = isBaseDowngrade ? tenant.plan : newPlan;
      const immediateCycle = isBaseDowngrade ? currentCycleFromMeta : cycle;
      
      let currentAddons: string[] = [];
      if (tenant.config) {
        if (typeof tenant.config === 'object') {
          currentAddons = tenant.config.active_addons || [];
        } else if (typeof tenant.config === 'string') {
          try {
            const parsed = JSON.parse(tenant.config);
            currentAddons = parsed.active_addons || [];
          } catch (e) {}
        }
      }
      
      const immediateAddons = Array.from(new Set([...currentAddons, ...(newAddons || [])]));
      const immediatePriceIds = await getStripePriceIdsForPlanAndAddons(immediatePlan, immediateAddons, immediateCycle);

      const prorationDate = Math.floor(Date.now() / 1000); // Genau jetzt

      const previewParams: any = {
        automatic_tax: { enabled: true },
      };

      if (tenant.stripe_customer_id) {
        previewParams.customer = tenant.stripe_customer_id;
      } else {
        // Fallback für Vorschau ohne registrierten Stripe-Kunden
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
      if (hasActiveSub) {
        try {
          // --- UPGRADE-VORSCHAU ---
          if (currentSub?.status === 'canceled' || !currentSub) {
            // Falls es doch canceled ist (Sync-Gap), neues Abo Vorschau
            previewParams.subscription_items = immediatePriceIds.map((priceId: string) => ({ price: priceId }));
          } else {
            // Alte Items virtuell löschen und neue hinzufügen
            const deletedItems = currentSub.items.data.map((item: any) => ({
              id: item.id,
              deleted: true
            }));
            const addedItems = immediatePriceIds.map((priceId: string) => ({ price: priceId }));

            previewParams.subscription = tenant.stripe_subscription_id;
            previewParams.subscription_items = [...deletedItems, ...addedItems];
            previewParams.subscription_proration_date = prorationDate;
          }
        } catch (e: any) {
          console.error("Fehler bei Vorschau-Params:", e.message);
          previewParams.subscription_items = immediatePriceIds.map((priceId: string) => ({ price: priceId }));
        }
      } else {
        // --- NEUES ABO VORSCHAU ---
        const finalPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);
        previewParams.subscription_items = finalPriceIds.map((priceId: string) => ({ price: priceId }));
      }

      try {
        const upcomingInvoice = await stripe.invoices.retrieveUpcoming(previewParams);

        // Wir sortieren die Positionen aus der Stripe-Vorschau:
        // prorationLines = Das was anteilig für den aktuellen Restmonat berechnet wird
        // regularLines = Das was ab dem nächsten regulären Rechnungsdatum gilt
        const prorationLines = upcomingInvoice.lines.data.filter(line => line.proration);
        const regularLines = upcomingInvoice.lines.data.filter(line => !line.proration);

        // Helper: Berechnet Details inkl. Steuern
        const calculateLineDetails = (lines: any[], filterCredits = false) => {
          let netCents = 0;
          let taxCents = 0;
          lines.forEach(line => {
            // Logik für Gutschriften (negative Beträge):
            // 1. Bei Modulen (Addons) ignorieren wir Gutschriften heute (da Vormerkung).
            // 2. Bei Basis-Paketen erlauben wir sie NUR bei echten Upgrades.
            if (line.amount < 0 && filterCredits) {
              const priceId = line.price?.id;
              const type = priceTypeMap[priceId];
              if (type === 'addon') return;
              if (type === 'base' && !isBaseUpgrade) return;
            }

            // NEU: Bei Basis-Downgrade ignorieren wir heute auch die anteiligen KOSTEN des neuen kleinen Plans,
            // weil wir den Wechsel nur "vormerken" (proration_behavior: 'none').
            if (line.amount > 0 && line.proration && filterCredits && !isBaseUpgrade) {
              const priceId = line.price?.id;
              const type = priceTypeMap[priceId];
              if (type === 'base') return;
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

        const prorationDetails = calculateLineDetails(prorationLines, true);

        // --- FINAL-STATE VORSCHAU (Für den Preis im nächsten Monat) ---
        // Da 'previewParams' nur den Immediate State enthält, müssen wir den 
        // zukünftigen (Final) Zustand separat evaluieren, um den genauen
        // Preis für den nächsten Abrechnungszyklus zu erhalten.
        const finalPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);
        const finalPreviewParams: any = {
          automatic_tax: { enabled: true },
          subscription_items: finalPriceIds.map((priceId: string) => ({ price: priceId }))
        };

        if (tenant.stripe_customer_id) {
          finalPreviewParams.customer = tenant.stripe_customer_id;
        } else if (previewParams.customer_details) {
          finalPreviewParams.customer_details = previewParams.customer_details;
        }

        const finalInvoice = await stripe.invoices.retrieveUpcoming(finalPreviewParams);
        const finalRegularLines = finalInvoice.lines.data.filter(line => !line.proration);
        const regularDetails = calculateLineDetails(finalRegularLines);

        let amountDueToday = 0;
        let taxDueToday = 0;
        let netDueToday = 0;

        if (hasActiveSub) {
          // Beim Upgrade zahlt er heute NUR die Prorations (anteilige Kosten)
          amountDueToday = Math.max(0, prorationDetails.total);
          taxDueToday = Math.max(0, prorationDetails.tax);
          netDueToday = Math.max(0, prorationDetails.net);
        } else {
          // Neues Abo: Er zahlt heute den vollen Paketpreis (da keine Prorationen für den Start anfallen)
          // Wir nutzen hier regularDetails, da Stripe bei upcoming_invoice für neue Abos 
          // manchmal 0.00 als amount_due zurückgibt (z.B. wenn es nur eine Vorschau ist).
          amountDueToday = regularDetails.total;
          taxDueToday = regularDetails.tax;
          netDueToday = regularDetails.net;
        }

        const upcomingLinesWithMetadata = upcomingInvoice.lines.data.map(line => ({
          ...line,
          package_type: priceTypeMap[line.price?.id] || 'unknown'
        })).filter(line => {
           if (line.proration) {
              if (line.amount < 0) {
                 if (line.package_type === 'addon') return false;
                 if (line.package_type === 'base' && !isBaseUpgrade) return false;
              }
              if (line.amount > 0 && line.package_type === 'base' && !isBaseUpgrade) return false;
           }
           return true;
        });

        const finalLinesWithMetadata = finalInvoice.lines.data.map(line => ({
          ...line,
          package_type: priceTypeMap[line.price?.id] || 'unknown'
        }));

        // Wir kombinieren die Prorations der aktuellen Vorschau (für "Heute fällig")
        // mit den regulären Zeilen der finalen Vorschau (für "Nächster Monat")
        const prorationLinesWithMetadata = upcomingLinesWithMetadata.filter(l => l.proration);
        const finalRegularLinesWithMetadata = finalLinesWithMetadata.filter(l => !l.proration);
        const combinedLines = [...prorationLinesWithMetadata, ...finalRegularLinesWithMetadata];

        return new Response(JSON.stringify({
          amountDueToday,
          taxDueToday,
          netDueToday,
          nextBillingDate: upcomingInvoice.next_payment_attempt || upcomingInvoice.period_end,
          amountDueNextMonth: regularDetails.total,
          taxDueNextMonth: regularDetails.tax,
          netDueNextMonth: regularDetails.net,
          lines: combinedLines,
          currency: upcomingInvoice.currency,
          isBaseUpgrade
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
      console.log("🔄 UPDATE SUBSCRIPTION ACTION CALLED");
      const { newPlan, newAddons, cycle = 'monthly' } = body;
      console.log("Request body:", { newPlan, newAddons, cycle, tenantId });

      console.log("Active subscription check:", {
        stripe_subscription_id: tenant.stripe_subscription_id,
        stripe_subscription_status: tenant.stripe_subscription_status,
        hasActiveSub
      });

      if (!hasActiveSub) throw new Error("Kein aktives Abo für Update gefunden. Bitte nutze den Checkout.");

      // Aktuelle Subscription abrufen, um die Item-IDs für den Austausch zu finden
      const currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);

      let currentAddons: string[] = [];
      if (tenant.config) {
        if (typeof tenant.config === 'object') {
          currentAddons = tenant.config.active_addons || [];
        } else if (typeof tenant.config === 'string') {
          try {
            const parsed = JSON.parse(tenant.config);
            currentAddons = parsed.active_addons || [];
          } catch (e) {
            console.warn("Could not parse tenant.config:", e);
          }
        }
      }

      const currentPlan = tenant.plan;
      const currentCycleFromMeta = currentSub.metadata?.cycle || 'monthly';
      const normalizedCurrentAddons = [...currentAddons].sort();
      const normalizedNewAddons = [...(newAddons || [])].sort();

      // Aktuelle Paket-Preise ermitteln, um Downgrades zu erkennen
      const { data: allPackages } = await supabaseAdmin
        .from('subscription_packages')
        .select('plan_name, price_monthly, price_yearly, package_type');

      const currentBasePkg = allPackages?.find(p => p.plan_name === currentPlan && p.package_type === 'base');
      const newBasePkg = allPackages?.find(p => p.plan_name === newPlan && p.package_type === 'base');

      const currentBasePrice = currentCycleFromMeta === 'yearly' ? (currentBasePkg?.price_yearly || 0) : (currentBasePkg?.price_monthly || 0);
      const newBasePrice = cycle === 'yearly' ? (newBasePkg?.price_yearly || 0) : (newBasePkg?.price_monthly || 0);

      const isBaseDowngrade = newBasePrice < currentBasePrice;

      // 1. BESTIMME SOFORTIGE / ULTIMATIVE ZIEL-ITEMS
      // Immediate Plan (Alles was teurer oder gleich ist, wird sofort aktiv. Downgrades werden "verschoben" -> dh. der alte Plan bleibt aktiv)
      const immediatePlan = isBaseDowngrade ? currentPlan : newPlan;
      const immediateCycle = isBaseDowngrade ? currentCycleFromMeta : cycle;

      // Immediate Addons: Alle NEUEN Addons sollen sofort aktiv sein (Upgrades). 
      // Alle ABGEWÄHLTEN Addons sollen auch erstmal aktiv bleiben (da Downgrade).
      // Daher ist die Menge an sofort aktiven Addons = currentAddons UNION newAddons
      const immediateAddons = Array.from(new Set([...normalizedCurrentAddons, ...normalizedNewAddons]));

      const finalPlan = newPlan;
      const finalCycle = cycle;
      const finalAddons = normalizedNewAddons;

      console.log("=== MIXED UPGRADE/DOWNGRADE CALCULATION ===");
      console.log("Current state:", { plan: currentPlan, addons: normalizedCurrentAddons, cycle: currentCycleFromMeta });
      console.log("Immediate state:", { plan: immediatePlan, addons: immediateAddons, cycle: immediateCycle });
      console.log("Final state:", { plan: finalPlan, addons: finalAddons, cycle: finalCycle });

      const needsSchedule = immediatePlan !== finalPlan || 
                            immediateCycle !== finalCycle ||
                            JSON.stringify([...immediateAddons].sort()) !== JSON.stringify([...finalAddons].sort());

      // 2. SOFORTIGE PRORATION (Sofortiges Update)
      const immediatePriceIds = await getStripePriceIdsForPlanAndAddons(immediatePlan, immediateAddons, immediateCycle);
      const finalPriceIds = await getStripePriceIdsForPlanAndAddons(finalPlan, finalAddons, finalCycle);

      const immediateItemsToUpdate = currentSub.items.data.map(item => ({
        id: item.id,
        deleted: true
      })).concat(immediatePriceIds.map(price => ({ price })));

      const isReturningToCurrent = finalPlan === tenant.plan &&
                                   JSON.stringify(finalAddons) === JSON.stringify(normalizedCurrentAddons);

      // Metadaten für das anstehende Abo
      const upcomingPlanMeta = (needsSchedule && !isReturningToCurrent) ? finalPlan : "";
      const upcomingAddonsMeta = (needsSchedule && !isReturningToCurrent) ? JSON.stringify(finalAddons) : "";
      const upcomingCycleMeta = (needsSchedule && !isReturningToCurrent) ? finalCycle : "";

      const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
        items: immediateItemsToUpdate,
        proration_behavior: 'always_invoice',
        metadata: { 
          plan_name: immediatePlan, 
          addons: JSON.stringify(immediateAddons), 
          cycle: immediateCycle, 
          tenant_id: tenant.id.toString(),
          upcoming_plan: upcomingPlanMeta,
          upcoming_addons: upcomingAddonsMeta,
          upcoming_cycle: upcomingCycleMeta
        }
      });

      // Sofort-Rechnung ggf. abbuchen
      const latestInvoiceId = updatedSub.latest_invoice;
      if (latestInvoiceId) {
        const invoice = await stripe.invoices.retrieve(latestInvoiceId as string);
        if (invoice.status === 'open' && invoice.amount_due > 0) {
          try {
             await stripe.invoices.pay(invoice.id);
          } catch(e) { console.warn("Auto-pay failed", e); }
        }
      }

      // 3. SCHEDULE ERSTELLEN FALLS NÖTIG
      if (needsSchedule && !isReturningToCurrent) {
        console.log("✅ ÄNDERUNG ENTHÄLT DOWNGRADE KOMPONENTEN - Erstelle Schedule für das Periodenende");
        const subId = updatedSub.id;
        let schedId = typeof updatedSub.schedule === 'string' ? updatedSub.schedule : updatedSub.schedule?.id;

        if (!schedId) {
          const schedule = await stripe.subscriptionSchedules.create({ from_subscription: subId });
          schedId = schedule.id;
        }

        const scheduleObj = await stripe.subscriptionSchedules.retrieve(schedId);
        const periodEndTs = updatedSub.current_period_end;

        const currentPhaseItems = scheduleObj.phases[0].items.map((i: any) => {
          const baseItem: any = { price: typeof i.price === 'string' ? i.price : i.price.id };
          if (i.quantity !== undefined && i.quantity !== null) {
            baseItem.quantity = i.quantity;
          }
          if (i.billing_thresholds) {
            baseItem.billing_thresholds = i.billing_thresholds;
          }
          return baseItem;
        });

        const newPhaseItems = await Promise.all(
          finalPriceIds.map(async (priceId: string) => {
            const priceObj = await stripe.prices.retrieve(priceId);
            const baseItem: any = { price: priceId };
            if (priceObj.recurring?.usage_type !== 'metered') {
              baseItem.quantity = 1;
            }
            return baseItem;
          })
        );

        await stripe.subscriptionSchedules.update(schedId, {
          end_behavior: 'release',
          default_settings: { automatic_tax: { enabled: true } },
          phases: [
            {
              start_date: scheduleObj.phases[0].start_date,
              end_date: periodEndTs,
              items: currentPhaseItems,
            },
            {
              start_date: periodEndTs,
              iterations: 1, // Zwingt Stripe dazu, die Phase für genau 1 Rechnungszyklus zu evaluieren, wodurch wiederkehrende Kosten für die nächste Rechnung ordnungsgemäß projektiert werden
              items: newPhaseItems,
              metadata: {
                plan_name: finalPlan,
                addons: JSON.stringify(finalAddons),
                cycle: finalCycle,
                tenant_id: tenant.id.toString(),
                upcoming_plan: "",
                upcoming_addons: "",
                upcoming_cycle: ""
              }
            }
          ]
        });
      } else if (isReturningToCurrent && typeof updatedSub.schedule === 'string') {
         await stripe.subscriptionSchedules.release(updatedSub.schedule);
      } else if (isReturningToCurrent && updatedSub.schedule?.id) {
         await stripe.subscriptionSchedules.release(updatedSub.schedule.id);
      }

      // 4. Update Pfotencard DB (Tenant)
      const config = tenant.config || {};
      const updateData: any = { config };

      if (needsSchedule && !isReturningToCurrent) {
        updateData.plan = immediatePlan;
        updateData.upcoming_plan = finalPlan;
        updateData.upcoming_addons = finalAddons;
        updateData.upcoming_cycle = finalCycle;
      } else {
        updateData.plan = finalPlan;
        updateData.upcoming_plan = null;
        updateData.upcoming_cycle = null;
        updateData.upcoming_addons = null;
      }

      await supabaseAdmin.from('tenants').update(updateData).eq('id', tenant.id);

      // tenant_addons synchronisieren (sofort aktualisieren auf den Immediate Zustand)
      await syncTenantAddons(tenant.id, immediateAddons);

      // Falls es Downgrades bei Addons gab, diese nun nachträglich auf removes_at_period_end setzen
      if (needsSchedule && !isReturningToCurrent) {
        const removedAddons = immediateAddons.filter(a => !finalAddons.includes(a));
        if (removedAddons.length > 0) {
          const { data: removedPkgs } = await supabaseAdmin
            .from('subscription_packages')
            .select('id')
            .in('plan_name', removedAddons)
            .eq('package_type', 'addon');
          
          if (removedPkgs && removedPkgs.length > 0) {
            const removedIds = removedPkgs.map((p: any) => p.id);
            await supabaseAdmin.from('tenant_addons')
              .update({ removes_at_period_end: true })
              .eq('tenant_id', tenant.id)
              .in('addon_id', removedIds);
            console.log("Addons marked for removal at period end:", removedAddons);
          }
        }
      }

      return new Response(JSON.stringify({ status: 'success' }), { headers: corsHeaders });
    }

    // ==========================================
    // ACTION: UPCOMING CHANGES VERWERFEN
    // ==========================================
    if (action === 'cancel_pending_changes') {
      if (!tenant.stripe_subscription_id) throw new Error('Kein aktives Abo');

      const currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);
      
      // 1. Metadaten auf der Subscription bereinigen, BEVOR der Schedule released wird 
      //    (damit Webhooks nicht die alten Metadaten in die DB zurückschreiben)
      await stripe.subscriptions.update(tenant.stripe_subscription_id, {
        metadata: {
          ...currentSub.metadata,
          upcoming_plan: "",
          upcoming_addons: "",
          upcoming_cycle: ""
        }
      });

      // 2. Wenn es einen Schedule gibt, diesen releasen
      let schedId = typeof currentSub.schedule === 'string' ? currentSub.schedule : currentSub.schedule?.id;
      if (schedId) {
        await stripe.subscriptionSchedules.release(schedId);
      }

      // 3. Datenbank aktualisieren (Tenants)
      await supabaseAdmin.from('tenants').update({
        upcoming_plan: null,
        upcoming_addons: null,
        upcoming_cycle: null
      }).eq('id', tenant.id);

      // 4. Datenbank aktualisieren (Tenant Addons) - removes_at_period_end zurücksetzen
      await supabaseAdmin.from('tenant_addons').update({
        removes_at_period_end: false
      }).eq('tenant_id', tenant.id);

      return new Response(JSON.stringify({ status: 'success', message: 'Änderungen verworfen' }), { headers: corsHeaders });
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
      const { plan, cycle, billingDetails, addons = [], promoCodeId } = body

      if (promoCodeId) {
          console.log('[manage-subscription] Validiere Promo-Code vor Checkout:', promoCodeId);
          const { data: promoCheck, error: promoCheckError } = await supabaseAdmin
            .from('promo_codes')
            .select('id, is_active, max_uses, current_uses, expires_at')
            .eq('stripe_promotion_code_id', promoCodeId)
            .single();

          if (promoCheckError || !promoCheck) {
              throw new Error('Gutscheincode nicht gefunden.');
          }
          if (!promoCheck.is_active) {
              throw new Error('Gutscheincode ist nicht aktiv.');
          }
          if (promoCheck.max_uses && promoCheck.current_uses >= promoCheck.max_uses) {
              throw new Error('Gutscheincode Nutzungslimit erreicht.');
          }
          if (promoCheck.expires_at && new Date(promoCheck.expires_at) < new Date()) {
              throw new Error('Gutscheincode abgelaufen.');
          }
          
          // Prüfung auf Mehrfachnutzung
          const { data: redemptionCheck } = await supabaseAdmin
            .from('promo_code_redemptions')
            .select('id')
            .eq('promo_code_id', promoCheck.id)
            .eq('tenant_id', tenant.id)
            .maybeSingle();
            
          if (redemptionCheck) {
              throw new Error('Gutscheincode wurde bereits eingelöst.');
          }
      }

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

      if (hasActiveSub) {
        const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
          items: lineItems.map(item => ({ price: item.price })),
          proration_behavior: 'always_invoice',
          automatic_tax: { enabled: true },
          payment_settings: { save_default_payment_method: 'on_subscription' },
          discounts: promoCodeId ? [{ promotion_code: promoCodeId }] : undefined,
          metadata: { plan_name: plan, cycle: cycle, tenant_id: tenant.id.toString() }
        })

        if (promoCodeId) {
            await redeemPromoCode(promoCodeId, tenant.id)
        }

        return new Response(JSON.stringify({ status: 'updated', subscriptionId: updatedSub.id }), { headers: corsHeaders })
      } else {
        const sub = await stripe.subscriptions.create({
          customer: customerId,
          items: lineItems,
          payment_behavior: 'default_incomplete',
          automatic_tax: { enabled: true },
          payment_settings: { save_default_payment_method: 'on_subscription' },
          expand: ['latest_invoice.payment_intent'],
          discounts: promoCodeId ? [{ promotion_code: promoCodeId }] : undefined,
          metadata: { plan_name: plan, cycle: cycle, tenant_id: tenant.id.toString() }
        })

        if (promoCodeId) {
            await redeemPromoCode(promoCodeId, tenant.id)
        }

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