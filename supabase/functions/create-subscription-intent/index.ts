/**
 * Create/Update Stripe Subscription Intent for Pfotencard
 */
import Stripe from "npm:stripe@^17.0.0";
import { createClient } from "npm:@supabase/supabase-js@^2.40.0";
import { corsHeaders } from "../_shared/cors.ts";
import { getCountryCode } from "../_shared/country-mapping.ts";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
    apiVersion: "2023-10-16",
    httpClient: Stripe.createFetchHttpClient(),
});

        // Helfer: Umsatzsteuer-ID sicher prüfen
async function syncTaxId(stripeClient: Stripe, customerId: string, vatId: string) {
    if (!vatId) return;
    const taxIds = await stripeClient.customers.listTaxIds(customerId);
    const hasVatId = taxIds.data.some(t => t.value === vatId);

    if (!hasVatId) {
        try {
            await stripeClient.customers.createTaxId(customerId, { type: 'eu_vat', value: vatId });
        } catch (e: any) {
            console.error("Stripe Tax ID Error", e);
            if (e.code === 'tax_id_invalid') {
                throw new Error(`Stripe lehnt die USt-IdNr. '${vatId}' ab.`);
            }
            throw new Error("Fehler beim Validieren der USt-IdNr. bei Stripe.");
        }
    }
}

// Hilfsfunktion zum Berechnen der Zeilen-Details (Netto, Steuern, Gesamt)
function calculateLineDetails(lines: any[], priceTypeMap: Record<string, string>, filterAddonCredits = false) {
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
}

Deno.serve(async (req: Request): Promise<Response> => {
    if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders });

    try {
        const supabaseClient = createClient(
            Deno.env.get('SUPABASE_URL') ?? '',
            Deno.env.get('SUPABASE_ANON_KEY') ?? '',
            { global: { headers: { Authorization: req.headers.get('Authorization')! } } }
        )
        const supabaseAdmin = createClient(
            Deno.env.get('SUPABASE_URL') ?? '',
            Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
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
              console.log('Using FastAPI decoded user email:', userEmail);
            } catch (e) {
              console.error('Error decoding custom JWT:', e);
            }
          }
        }

        const body = await req.json();
        const { plan, addons = [], action, paymentMethodId, vatId: frontendVatId, address, billingCycle, promoCodeId } = body;
        
        console.log(`[SUBSCRIPTION_INTENT] Action: ${action}, Plan: ${plan}, Addons: ${JSON.stringify(addons)}, Cycle: ${billingCycle}`);
        if (address) console.log(`[SUBSCRIPTION_INTENT] Address updated: ${address.city}, ${address.countryCode || address.country}`);

        // Fetch tenant details
        // In Pfotencard, we use the 'x-tenant-subdomain' header or search by user
        const subdomain = req.headers.get('x-tenant-subdomain');
        let tenantId = body.tenantId;

        if (!tenantId && subdomain) {
            const { data: t } = await supabaseAdmin.from('tenants').select('id').eq('subdomain', subdomain).maybeSingle();
            if (t) tenantId = t.id;
        }
        
        if (!tenantId && userEmail) {
            // Fallback: search for tenant where this user is owner or has email
            // In Pfotencard Backend, we search in the users table
            const { data: dbUser } = await supabaseAdmin
                .from('users')
                .select('tenant_id')
                .eq('email', userEmail.toLowerCase())
                .maybeSingle();
            
            if (dbUser) tenantId = dbUser.tenant_id;
        }

        if (!tenantId) {
            return new Response(JSON.stringify({ error: "Tenant-ID konnte nicht ermittelt werden. Bitte x-tenant-subdomain Header mitsenden." }), { status: 400, headers: corsHeaders });
        }

        // ==========================================
        // ACTION: SAVE_BILLING_DETAILS (Only save, no stripe action)
        // ==========================================
        if (action === 'save_billing_details') {
            if (address) {
                const normalizedCountry = getCountryCode(address.country || address.countryCode);
                const updateData: any = {
                    street: address.street,
                    city: address.city,
                    postcode: address.postcode,
                    country: address.countryCode || normalizedCountry,
                };
                if (frontendVatId !== undefined) {
                    updateData.vat_id = frontendVatId;
                }
                await supabaseAdmin.from('tenants').update(updateData).eq('id', tenantId);
                return new Response(JSON.stringify({ success: true }), { headers: corsHeaders });
            }
            return new Response(JSON.stringify({ error: "Address data missing" }), { status: 400, headers: corsHeaders });
        }

        const { data: tenant, error: tenantError } = await supabaseAdmin
            .from("tenants")
            .select("*")
            .eq("id", tenantId)
            .single();

        if (tenantError || !tenant) {
            return new Response(JSON.stringify({ error: "Tenant nicht gefunden" }), { status: 404, headers: corsHeaders });
        }

        const customerName = tenant.name || "Kunde";
        let customerId = tenant.stripe_customer_id;

        // Ensure customer exists in Stripe
        if (!customerId) {
            // Wenn kein User da ist, nutzen wir die Support-Email des Tenants
            const email = userEmail || tenant.support_email || `support@${tenant.subdomain}.pfotencard.de`;
            
            const customers = await stripe.customers.list({ email: email, limit: 1 });
            if (customers.data.length > 0) {
                customerId = customers.data[0].id;
            } else {
                const newCustomer = await stripe.customers.create({
                    email: email,
                    name: customerName,
                    metadata: { tenant_id: tenant.id }
                });
                customerId = newCustomer.id;
            }
            await supabaseAdmin.from('tenants').update({ stripe_customer_id: customerId }).eq('id', tenant.id);
        }

        // Get Price IDs from DB
        const allPriceIds: string[] = [];
        const { data: pkg } = await supabaseAdmin
            .from('subscription_packages')
            .select('*')
            .ilike('plan_name', plan)
            .eq('package_type', 'base')
            .single();
        
        if (pkg) {
            console.log(`[SUBSCRIPTION_INTENT] Base package found: ${pkg.plan_name}`);
            const basePriceId = billingCycle === 'yearly' ? pkg.stripe_price_id_base_yearly : pkg.stripe_price_id_base_monthly;
            if (basePriceId) allPriceIds.push(basePriceId);
            else if (pkg.stripe_price_id_base) allPriceIds.push(pkg.stripe_price_id_base);
            
            if (pkg.stripe_price_id_users) allPriceIds.push(pkg.stripe_price_id_users);
            if (pkg.stripe_price_id_fees) allPriceIds.push(pkg.stripe_price_id_fees);
        }

        if (addons.length > 0) {
            const { data: addonPkgs } = await supabaseAdmin
                .from('subscription_packages')
                .select('*')
                .in('plan_name', addons)
                .eq('package_type', 'addon');
            
            if (addonPkgs) {
                addonPkgs.forEach(a => {
                    const pId = billingCycle === 'yearly' ? a.stripe_price_id_base_yearly : a.stripe_price_id_base_monthly;
                    if (pId) allPriceIds.push(pId);
                });
            }
        }

        if (allPriceIds.length === 0 && action !== 'cancel_downgrade' && action !== 'save_billing_details') {
            return new Response(JSON.stringify({ error: `Keine gültigen Stripe-Preise für Plan '${plan}' gefunden.` }), { status: 400, headers: corsHeaders });
        }

        const subscriptionItems = allPriceIds.map(id => ({ price: id }));

        // ==========================================
        // ACTION: FINALIZE (After SetupIntent or with saved PM)
        // ==========================================
        if (action === 'finalize_subscription') {
            let defaultPaymentMethod = paymentMethodId;
            if (!defaultPaymentMethod) {
                const paymentMethods = await stripe.paymentMethods.list({ customer: customerId, limit: 1 });
                if (paymentMethods.data.length === 0) return new Response(JSON.stringify({ error: "Keine Zahlungsmethode hinterlegt" }), { headers: corsHeaders });
                defaultPaymentMethod = paymentMethods.data[0].id;
            }

            await stripe.customers.update(customerId, {
                invoice_settings: { default_payment_method: defaultPaymentMethod },
            });

            // Adress-Update und Supabase Sync
            if (address) {
                const normalizedCountry = getCountryCode(address.country || address.countryCode);
                
                // Update Stripe Customer Address
                await stripe.customers.update(customerId, {
                    name: customerName, // Sicherstellen dass Name auch gesetzt ist
                    address: {
                        line1: address.street,
                        city: address.city,
                        postal_code: address.postcode,
                        country: normalizedCountry,
                    },
                });

                // Update Tenant in Supabase
                const updateData: any = {
                    street: address.street,
                    city: address.city,
                    postcode: address.postcode,
                    country: address.countryCode || normalizedCountry,
                };
                
                // Falls vatId mitgekommen ist, auch im Tenant speichern
                if (frontendVatId) {
                    updateData.vat_id = frontendVatId;
                }

                await supabaseAdmin.from('tenants').update(updateData).eq('id', tenant.id);
            }

            const customerVatId = frontendVatId || (tenant.config?.invoice_settings?.vat_id);
            if (customerVatId) {
                await syncTaxId(stripe, customerId, customerVatId);
            }

            const allSubs = await stripe.subscriptions.list({ customer: customerId, status: 'active', limit: 1 });
            const activeSub = allSubs.data[0];

            let finalSubscriptionId;
            let finalStatus;

            if (activeSub) {
                // Alle alten Items löschen und neue hinzufügen
                const itemsToUpdate = activeSub.items.data.map((item: any) => ({
                    id: item.id,
                    deleted: true
                })).concat(subscriptionItems as any);

                const updateParams: Stripe.SubscriptionUpdateParams = {
                    items: itemsToUpdate,
                    default_payment_method: defaultPaymentMethod,
                    proration_behavior: 'always_invoice',
                    automatic_tax: { enabled: true },
                    metadata: { tenant_id: tenant.id, plan }
                };
                if (promoCodeId) {
                    updateParams.discounts = [{ promotion_code: promoCodeId }];
                }
                const updatedSub = await stripe.subscriptions.update(activeSub.id, updateParams);
                finalSubscriptionId = updatedSub.id;
                finalStatus = updatedSub.status;
            } else {
                const subOptions: Stripe.SubscriptionCreateParams = {
                    customer: customerId,
                    items: subscriptionItems,
                    default_payment_method: defaultPaymentMethod,
                    automatic_tax: { enabled: true },
                    payment_settings: { save_default_payment_method: 'on_subscription' },
                    metadata: { tenant_id: tenant.id, plan }
                };
                if (promoCodeId) {
                    subOptions.discounts = [{ promotion_code: promoCodeId }];
                }
                const subscription = await stripe.subscriptions.create(subOptions);
                finalSubscriptionId = subscription.id;
                finalStatus = subscription.status;
            }

            return new Response(JSON.stringify({ success: true, subscriptionId: finalSubscriptionId }), { headers: corsHeaders });
        }

        // ==========================================
        // ACTION: CREATE INTENT (Vorbereitung Checkout)
        // ==========================================
        
        // Speichere die Adresse auch beim Erstellen des Intents, damit sie beim nächsten Mal da ist
        if (address) {
            const normalizedCountry = getCountryCode(address.country || address.countryCode);
            const updateData: any = {
                street: address.street,
                city: address.city,
                postcode: address.postcode,
                country: address.countryCode || normalizedCountry,
            };
            if (frontendVatId !== undefined) {
                updateData.vat_id = frontendVatId;
            }
            await supabaseAdmin.from('tenants').update(updateData).eq('id', tenant.id);
        }

        // Wir erstellen einen SetupIntent, damit der Nutzer seine Zahlungsmethode sicher hinterlegen kann
        const setupIntent = await stripe.setupIntents.create({
            customer: customerId,
            usage: 'off_session',
            metadata: { tenant_id: tenant.id, plan }
        });

        // VORSCHAU DER RECHNUNG ERSTELLEN
        let preview: any = null;
        try {
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

            // Aktuelle Adresse/VAT an Stripe senden für korrekte Steuern
            if (address || frontendVatId) {
                const normalizedCountry = getCountryCode(address?.country || address?.countryCode || tenant.country);
                const updateParams: Stripe.CustomerUpdateParams = {};
                
                if (address) {
                    updateParams.address = {
                        line1: address.street,
                        city: address.city,
                        postal_code: address.postcode,
                        country: normalizedCountry,
                    };
                }
                
                if (frontendVatId) {
                    await syncTaxId(stripe, customerId, frontendVatId);
                }
                
                if (Object.keys(updateParams).length > 0) {
                    await stripe.customers.update(customerId, updateParams);
                }
            }

            const allSubs = await stripe.subscriptions.list({ customer: customerId, status: 'active', limit: 1 });
            const activeSub = allSubs.data[0];

            const previewParams: any = {
                customer: customerId,
                automatic_tax: { enabled: true },
            };

            if (activeSub) {
                previewParams.subscription = activeSub.id;
                previewParams.subscription_items = activeSub.items.data.map((item: any) => ({
                    id: item.id,
                    deleted: true
                })).concat(subscriptionItems as any);
            } else {
                previewParams.subscription_items = subscriptionItems;
            }

            if (promoCodeId) {
                // Stripe retrieveUpcoming erwartet 'discounts' oder 'coupon' oder 'promotion_code'
                // Wenn promoCodeId ein Promotion Code ID ist (z.B. promo_...), müssen wir ihn anders handhaben
                if (promoCodeId.startsWith('promo_')) {
                   // Leider unterstützt retrieveUpcoming direkt keine Promotion Code IDs in manchen Versionen,
                   // oft nutzt man 'coupon' von dem Promo Code.
                   const promo = await stripe.promotionCodes.retrieve(promoCodeId);
                   previewParams.coupon = promo.coupon.id;
                   console.log(`[SUBSCRIPTION_INTENT] Applied Promotion Code: ${promoCodeId} -> Coupon: ${promo.coupon.id}`);
                } else {
                   previewParams.coupon = promoCodeId;
                   console.log(`[SUBSCRIPTION_INTENT] Applied Coupon: ${promoCodeId}`);
                }
            }

            console.log(`[SUBSCRIPTION_INTENT] Retrieving upcoming invoice for Customer: ${customerId} with Prices: ${allPriceIds.join(', ')}`);
            const upcoming = await stripe.invoices.retrieveUpcoming(previewParams);
            
            console.log(`[SUBSCRIPTION_INTENT] Upcoming Invoice Subtotal: ${upcoming.subtotal / 100}, Total: ${upcoming.total / 100}, Amount Due: ${upcoming.amount_due / 100}`);
            
            // Wir sortieren die Positionen aus der Stripe-Vorschau:
            // prorationLines = Das was anteilig für den aktuellen Restmonat berechnet wird
            // regularLines = Das was ab dem nächsten regulären Rechnungsdatum gilt
            const prorationLines = upcoming.lines.data.filter(line => line.proration);
            const regularLines = upcoming.lines.data.filter(line => !line.proration);

            const prorationDetails = calculateLineDetails(prorationLines, priceTypeMap, true); // Filtert Addon-Gutschriften aus
            const regularDetails = calculateLineDetails(regularLines, priceTypeMap);

            let amountDueToday = 0;
            let taxDueToday = 0;
            let netDueToday = 0;

            if (activeSub) {
                // Beim Upgrade zahlt er heute NUR die Prorations (anteilige Kosten)
                amountDueToday = Math.max(0, prorationDetails.total);
                taxDueToday = Math.max(0, prorationDetails.tax);
                netDueToday = Math.max(0, prorationDetails.net);
            } else {
                // Neues Abo: Er zahlt heute den vollen Vorschau-Betrag
                amountDueToday = upcoming.amount_due / 100;
                taxDueToday = (upcoming.tax || 0) / 100;
                netDueToday = (upcoming.subtotal || 0) / 100;
            }

            const linesWithMetadata = upcoming.lines.data.map(line => ({
                description: line.description,
                amount: line.amount,
                quantity: line.quantity,
                type: line.type,
                proration: line.proration,
                tax_amounts: line.tax_amounts,
                package_type: priceTypeMap[line.price?.id] || 'unknown'
            }));

            preview = {
                amountDueToday,
                taxDueToday,
                netDueToday,
                nextBillingDate: upcoming.next_payment_attempt || upcoming.period_end,
                amountDueNextMonth: regularDetails.total,
                taxDueNextMonth: regularDetails.tax,
                netDueNextMonth: regularDetails.net,
                lines: linesWithMetadata,
                currency: upcoming.currency
            };
        } catch (previewError) {
            console.error("Error creating preview invoice:", previewError);
            // Wir lassen den Checkout trotzdem zu, auch wenn die Vorschau fehlschlägt
        }

        return new Response(JSON.stringify({
            clientSecret: setupIntent.client_secret,
            intentType: 'setup',
            preview: preview,
            success: true
        }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });

    } catch (error: any) {
        console.error('Error in create-subscription-intent:', error);
        return new Response(JSON.stringify({ error: error.message }), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
    }
});
