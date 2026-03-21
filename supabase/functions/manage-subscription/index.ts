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

    const hasActiveSub = !!(tenant.stripe_subscription_id && 
                           tenant.stripe_subscription_status && 
                           !['canceled', 'incomplete_expired'].includes(tenant.stripe_subscription_status));

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

        const upcomingInvoice = await stripe.invoices.retrieveUpcoming({
          customer: tenant.stripe_customer_id,
          automatic_tax: { enabled: true },
        });

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

      const newPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);

      const currentPkg = allPackages?.find(p => p.plan_name === tenant.plan && p.package_type === 'base');
      const newPkg = allPackages?.find(p => p.plan_name === newPlan && p.package_type === 'base');
      const currentPrice = cycle === 'yearly' ? (currentPkg?.price_yearly || 0) : (currentPkg?.price_monthly || 0);
      const newPrice = cycle === 'yearly' ? (newPkg?.price_yearly || 0) : (newPkg?.price_monthly || 0);
      const isBaseUpgrade = newPrice > currentPrice;

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

        const prorationDetails = calculateLineDetails(prorationLines, true); // Filtert Addon- (und ggf. Base-) Gutschriften aus
        const regularDetails = calculateLineDetails(regularLines);

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

        const linesWithMetadata = upcomingInvoice.lines.data.map(line => ({
          ...line,
          package_type: priceTypeMap[line.price?.id] || 'unknown'
        })).filter(line => {
           // Wir filtern die Zeilen raus, die wir in calculateLineDetails ignoriert haben, 
           // damit die Anzeige im Frontend mit der Summe übereinstimmt.
           if (line.proration) {
              if (line.amount < 0) {
                 if (line.package_type === 'addon') return false;
                 if (line.package_type === 'base' && !isBaseUpgrade) return false;
              }
              if (line.amount > 0 && line.package_type === 'base' && !isBaseUpgrade) return false;
           }
           return true;
        });

        return new Response(JSON.stringify({
          amountDueToday,
          taxDueToday,
          netDueToday,
          nextBillingDate: upcomingInvoice.next_payment_attempt || upcomingInvoice.period_end,
          amountDueNextMonth: regularDetails.total,
          taxDueNextMonth: regularDetails.tax,
          netDueNextMonth: regularDetails.net,
          lines: linesWithMetadata,
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

      console.log("Fetching price IDs for:", { newPlan, newAddons, cycle });
      const newPriceIds = await getStripePriceIdsForPlanAndAddons(newPlan, newAddons, cycle);
      console.log("Got price IDs:", newPriceIds);
      const itemsArray = newPriceIds.map(priceId => ({ price: priceId }));

      // Aktuelle Subscription abrufen, um die Item-IDs für den Austausch zu finden (Stripe empfiehlt das für Updates)
      const currentSub = await stripe.subscriptions.retrieve(tenant.stripe_subscription_id);

      // Lösche alle alten Items und füge neue hinzu
      const itemsToUpdate = currentSub.items.data.map(item => ({
        id: item.id,
        deleted: true
      })).concat(itemsArray.map(item => ({ price: item.price })));

      // Prüfen ob es ein Upgrade oder Downgrade ist (inkl. Add-ons)
      // Wir müssen Basis-Paket UND Add-ons berücksichtigen
      let currentAddons: string[] = [];
      let addonsRemoved = false;
      let behavior: 'always_invoice' | 'none' = 'always_invoice';

      try {
        // Aktuelle Paket-Preise ermitteln
        const { data: allPackages } = await supabaseAdmin
          .from('subscription_packages')
          .select('plan_name, price_monthly, price_yearly, package_type');

        const currentPlan = tenant.plan;

        // WICHTIG: tenant.config könnte ein JSON-String sein oder undefined
        if (tenant.config) {
          // Falls config ein Objekt ist
          if (typeof tenant.config === 'object') {
            currentAddons = tenant.config.active_addons || [];
          }
          // Falls config ein String ist (manchmal passiert das bei JSON-Feldern)
          else if (typeof tenant.config === 'string') {
            try {
              const parsed = JSON.parse(tenant.config);
              currentAddons = parsed.active_addons || [];
            } catch (e) {
              console.warn("Could not parse tenant.config:", e);
            }
          }
        }

        const currentCycleFromMeta = currentSub.metadata?.cycle || 'monthly';

        console.log("=== DOWNGRADE CHECK START ===");
        console.log("Tenant config raw:", tenant.config);
        console.log("Current state:", { currentPlan, currentAddons, currentCycle: currentCycleFromMeta });
        console.log("New state:", { newPlan, newAddons, newCycle: cycle });

        const currentBasePkg = allPackages?.find(p => p.plan_name === currentPlan && p.package_type === 'base');
        const newBasePkg = allPackages?.find(p => p.plan_name === newPlan && p.package_type === 'base');

        if (!currentBasePkg) {
          console.warn("WARNING: Current base package not found:", currentPlan);
        }
        if (!newBasePkg) {
          console.warn("WARNING: New base package not found:", newPlan);
        }

        const currentBasePrice = currentCycleFromMeta === 'yearly' ? (currentBasePkg?.price_yearly || 0) : (currentBasePkg?.price_monthly || 0);
        const newBasePrice = cycle === 'yearly' ? (newBasePkg?.price_yearly || 0) : (newBasePkg?.price_monthly || 0);

        // Addon-Preise berechnen
        let currentAddonsPrice = 0;
        if (currentAddons.length > 0) {
          const currentAddonPkgs = allPackages?.filter(p => currentAddons.includes(p.plan_name) && p.package_type === 'addon') || [];
          console.log("Current addon packages found:", currentAddonPkgs.map(p => p.plan_name));
          currentAddonsPrice = currentAddonPkgs.reduce((sum, pkg) => {
            const price = currentCycleFromMeta === 'yearly' ? (pkg.price_yearly || 0) : (pkg.price_monthly || 0);
            console.log(`  ${pkg.plan_name}: ${price}€`);
            return sum + price;
          }, 0);
        }

        let newAddonsPrice = 0;
        if (newAddons && newAddons.length > 0) {
          const newAddonPkgs = allPackages?.filter(p => newAddons.includes(p.plan_name) && p.package_type === 'addon') || [];
          console.log("New addon packages found:", newAddonPkgs.map(p => p.plan_name));
          newAddonsPrice = newAddonPkgs.reduce((sum, pkg) => {
            const price = cycle === 'yearly' ? (pkg.price_yearly || 0) : (pkg.price_monthly || 0);
            console.log(`  ${pkg.plan_name}: ${price}€`);
            return sum + price;
          }, 0);
        }

        const currentTotalPrice = currentBasePrice + currentAddonsPrice;
        const newTotalPrice = newBasePrice + newAddonsPrice;

        console.log("Price comparison:", {
          currentBase: currentBasePrice,
          currentAddons: currentAddonsPrice,
          currentTotal: currentTotalPrice,
          newBase: newBasePrice,
          newAddons: newAddonsPrice,
          newTotal: newTotalPrice
        });

        // Wenn der neue Gesamtpreis niedriger ist -> Downgrade (vormerken)
        // ODER wenn Add-ons entfernt werden (auch wenn Basis gleich bleibt) -> Downgrade (vormerken)
        const isDowngrade = newTotalPrice < currentTotalPrice;

        // Prüfen ob Add-ons entfernt wurden (nicht nur Anzahl, sondern tatsächliche Änderung)
        const normalizedNewAddons = (newAddons || []).sort();
        const normalizedCurrentAddons = currentAddons.sort();
        const addonsChanged = JSON.stringify(normalizedNewAddons) !== JSON.stringify(normalizedCurrentAddons);
        addonsRemoved = currentAddons.some(addon => !normalizedNewAddons.includes(addon));

        console.log("Decision factors:", {
          isDowngrade,
          addonsChanged,
          addonsRemoved,
          priceDifference: newTotalPrice - currentTotalPrice
        });

        // WICHTIG: Bei gleichem Preis aber geänderten Add-ons ist es KEIN Downgrade
        // Nur wenn Preis sinkt ODER Add-ons entfernt werden (bei gleichem/höherem Basispaket)
        if (isDowngrade || (addonsRemoved && !isDowngrade && newBasePrice >= currentBasePrice)) {
          behavior = 'none';
          console.log("✅ DOWNGRADE ERKANNT - Wird zum Periodenende vorgemerkt");
        } else {
          console.log("✅ UPGRADE ERKANNT - Wird sofort durchgeführt");
        }
        console.log("=== DOWNGRADE CHECK END ===");
      } catch (e) {
        console.error("FEHLER bei Upgrade/Downgrade-Prüfung:", e);
        console.warn("Nutze Fallback Standard-Verhalten (always_invoice)");
      }

      if (behavior === 'always_invoice') {
        // UPGRADE: Stripe die Subscription überschreiben lassen (Sofortiges Upgrade)
        const updatedSub = await stripe.subscriptions.update(tenant.stripe_subscription_id, {
          items: itemsToUpdate,
          proration_behavior: behavior,
          metadata: { 
            plan_name: newPlan, 
            addons: JSON.stringify(newAddons), 
            cycle: cycle, 
            tenant_id: tenant.id.toString(),
            upcoming_plan: "",
            upcoming_addons: ""
          }
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
      } else {
        // DOWNGRADE via Schedule (Wirksam zum nächsten Abrechnungszeitraum)
        const subId = currentSub.id;
        let schedId = typeof currentSub.schedule === 'string' ? currentSub.schedule : currentSub.schedule?.id;

        if (!schedId) {
          const schedule = await stripe.subscriptionSchedules.create({ from_subscription: subId });
          schedId = schedule.id;
        }

        const scheduleObj = await stripe.subscriptionSchedules.retrieve(schedId);
        const periodEndTs = currentSub.current_period_end;

        // Für metered prices dürfen wir keine quantity setzen
        const currentPhaseItems = currentSub.items.data.map((item: any) => {
          const baseItem: any = { price: item.price.id };
          if (item.price.recurring?.usage_type !== 'metered') {
            baseItem.quantity = 1;
          }
          return baseItem;
        });

        // Neue Items: Prices abrufen um usage_type zu prüfen
        const newPhaseItems = await Promise.all(
          itemsArray.map(async (item: any) => {
            const priceObj = await stripe.prices.retrieve(item.price);
            const baseItem: any = { price: item.price };
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
              // Aktuelle Phase bleibt bis zum Ende der Laufzeit unverändert
              start_date: scheduleObj.phases[0].start_date,
              end_date: periodEndTs,
              items: currentPhaseItems,
            },
            {
              // Neue Phase (Downgrade) startet am Ende der aktuellen Laufzeit
              start_date: periodEndTs,
              items: newPhaseItems,
              metadata: {
                plan_name: newPlan,
                addons: JSON.stringify(newAddons),
                cycle: cycle,
                tenant_id: tenant.id.toString(),
                upcoming_plan: "",
                upcoming_addons: ""
              }
            }
          ]
        });

        // Metadaten an der aktuellen Subscription anpassen, damit das Backend / Webhooks wissen, was ansteht
        await stripe.subscriptions.update(subId, {
          metadata: {
            ...currentSub.metadata,
            upcoming_plan: newPlan,
            upcoming_addons: JSON.stringify(newAddons),
            upcoming_cycle: cycle
          }
        });
      }

      // Update Pfotencard DB (Tenant)
      const config = tenant.config || {};
      const updateData: any = { config };

      const isReturningToCurrent = newPlan === tenant.plan &&
                                   JSON.stringify([...(newAddons || [])].sort()) === JSON.stringify([...(tenant.config?.active_addons || [])].sort());

      if (behavior === 'none' && !isReturningToCurrent) {
        // Downgrade: Nur vormerken, aktueller Plan und Add-ons bleiben aktiv bis Periodenende
        updateData.upcoming_plan = newPlan;
        updateData.upcoming_addons = newAddons || [];
        updateData.upcoming_cycle = cycle;
        console.log("Vorgemerkt für nächste Periode:", { upcoming_plan: newPlan, upcoming_addons: newAddons });

        // NEU: Markiere Addons, die entfernt werden sollen, mit removes_at_period_end = true
        if (addonsRemoved) {
          const removedAddons = currentAddons.filter(a => !(newAddons || []).includes(a));
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
      } else {
        // Upgrade oder Rückkehr zum aktuellen Plan: Sofort aktivieren
        updateData.plan = newPlan;
        updateData.upcoming_plan = null;
        updateData.upcoming_cycle = null;
        updateData.upcoming_addons = null;
        console.log("Sofort aktiviert:", { plan: newPlan, active_addons: newAddons });
      }

      await supabaseAdmin.from('tenants').update(updateData).eq('id', tenant.id);

      // Synchronize addons in tenant_addons table
      // NUR bei Upgrades sofort synchronisieren, bei Downgrades erst beim Webhook
      if (behavior !== 'none' || isReturningToCurrent) {
          await syncTenantAddons(tenant.id, newAddons || []);
          console.log("tenant_addons sofort aktualisiert");
      } else {
          console.log("tenant_addons Synchronisation (Löschen/Hinzufügen) verschoben auf Periodenende (via Webhook)");
      }

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

      if (hasActiveSub) {
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