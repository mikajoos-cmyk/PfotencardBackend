/**
 * Create/Update Stripe Subscription Intent for Pfotencard
 */
import Stripe from "https://esm.sh/stripe@14.14.0?target=deno";
import { createAuthClient, createAdminClient } from "../_shared/supabase-client.ts";
import { corsHeaders } from "../_shared/cors.ts";
import { getCountryCode } from "../_shared/country-mapping.ts";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
    apiVersion: "2023-10-16",
    httpClient: Stripe.createFetchHttpClient(),
});

const PRICE_IDS: Record<string, string> = {
    starter: "price_1Qu98fK7M6uS7n6T7u8X3Y1Z", // Beispiel IDs, sollten aus der DB kommen oder angepasst werden
    pro: "price_1Qu98fK7M6uS7n6T7u8X3Y2A",
    enterprise: "price_1Qu98fK7M6uS7n6T7u8X3Y3B",
};

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

Deno.serve(async (req: Request): Promise<Response> => {
    if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders });

    try {
        const supabaseAuth = createAuthClient(req);
        const supabaseAdmin = createAdminClient();

        // Optional: User aus dem Auth-Context laden (Supabase JWT)
        // Wenn JWT Verifikation aus ist, kann user null sein
        const { data: { user } } = await supabaseAuth.auth.getUser();

        const body = await req.json();
        const { plan, action, paymentMethodId, vatId: frontendVatId, address, billingCycle, promoCodeId } = body;
        
        console.log(`[SUBSCRIPTION_INTENT] Action: ${action}, Plan: ${plan}, Cycle: ${billingCycle}`);
        if (address) console.log(`[SUBSCRIPTION_INTENT] Address updated: ${address.city}, ${address.countryCode || address.country}`);

        // Fetch tenant details
        // In Pfotencard, we use the 'x-tenant-subdomain' header or search by user
        const subdomain = req.headers.get('x-tenant-subdomain');
        let tenantId = body.tenantId;

        if (!tenantId && subdomain) {
            const { data: t } = await supabaseAdmin.from('tenants').select('id').eq('subdomain', subdomain).maybeSingle();
            if (t) tenantId = t.id;
        }
        
        if (!tenantId && user) {
            // Fallback: search for tenant where this user is owner
            // Wir prüfen 'owner_id' in der tenants Tabelle
            const { data: tenantData } = await supabaseAdmin
                .from('tenants')
                .select('id')
                .eq('owner_id', user.id)
                .maybeSingle();
            
            if (tenantData) tenantId = tenantData.id;
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
            const email = user?.email || tenant.support_email || `support@${tenant.subdomain}.pfotencard.de`;
            
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

        // Get Price ID from DB or Map
        let targetPriceId = "";
        const { data: pkg } = await supabaseAdmin
            .from('subscription_packages')
            .select('*')
            .ilike('plan_name', plan)
            .eq('package_type', 'base')
            .single();
        
        if (pkg) {
            console.log(`[SUBSCRIPTION_INTENT] Package found in DB: ${JSON.stringify(pkg)}`);
            targetPriceId = billingCycle === 'yearly' ? pkg.stripe_price_id_base_yearly : pkg.stripe_price_id_base_monthly;
            
            // Fallback auf die alte Spalte, falls vorhanden und die neuen leer sind
            if (!targetPriceId && pkg.stripe_price_id_base) {
                targetPriceId = pkg.stripe_price_id_base;
                console.log(`[SUBSCRIPTION_INTENT] Using legacy stripe_price_id_base: ${targetPriceId}`);
            }

            if (targetPriceId) {
                console.log(`[SUBSCRIPTION_INTENT] Using price ID from DB: ${targetPriceId}`);
            } else {
                targetPriceId = PRICE_IDS[plan];
                console.log(`[SUBSCRIPTION_INTENT] Package in DB has no Stripe Price ID, using fallback: ${targetPriceId}`);
            }
        } else {
            targetPriceId = PRICE_IDS[plan];
            console.log(`[SUBSCRIPTION_INTENT] WARNING: Plan '${plan}' not found in DB, using fallback price ID: ${targetPriceId}`);
        }

        if (!targetPriceId && action !== 'cancel_downgrade') {
            return new Response(JSON.stringify({ error: `Ungültiger Plan: ${plan}` }), { status: 400, headers: corsHeaders });
        }

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
                const updateParams: Stripe.SubscriptionUpdateParams = {
                    items: [{ id: activeSub.items.data[0].id, price: targetPriceId }],
                    default_payment_method: defaultPaymentMethod,
                    proration_behavior: 'always_invoice',
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
                    items: [{ price: targetPriceId }],
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
                previewParams.subscription_items = [
                    { id: activeSub.items.data[0].id, price: targetPriceId }
                ];
            } else {
                previewParams.subscription_items = [
                    { price: targetPriceId }
                ];
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

            console.log(`[SUBSCRIPTION_INTENT] Retrieving upcoming invoice for Customer: ${customerId} with Price: ${targetPriceId}`);
            const upcoming = await stripe.invoices.retrieveUpcoming(previewParams);
            
            console.log(`[SUBSCRIPTION_INTENT] Upcoming Invoice Subtotal: ${upcoming.subtotal / 100}, Total: ${upcoming.total / 100}, Amount Due: ${upcoming.amount_due / 100}`);
            
            // Log line items for debugging
            upcoming.lines.data.forEach((line: any, index: number) => {
                console.log(`[SUBSCRIPTION_INTENT] Line ${index}: ${line.description} - Amount: ${line.amount / 100} ${line.currency}`);
            });

            preview = {
                total: upcoming.total,
                subtotal: upcoming.subtotal,
                tax: upcoming.tax,
                amount_due: upcoming.amount_due,
                currency: upcoming.currency,
                period_start: upcoming.next_payment_attempt || upcoming.period_start,
                proration_date: upcoming.subscription_proration_date,
                lines: upcoming.lines.data.map((l: any) => ({
                    description: l.description,
                    amount: l.amount,
                    quantity: l.quantity,
                    type: l.type,
                    proration: l.proration
                }))
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
