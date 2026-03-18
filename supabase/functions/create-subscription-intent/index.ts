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
            targetPriceId = billingCycle === 'yearly' ? pkg.stripe_price_id_base_yearly : pkg.stripe_price_id_base_monthly;
        } else {
            targetPriceId = PRICE_IDS[plan];
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
                    country: address.country || normalizedCountry,
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
        // Wir erstellen einen SetupIntent, damit der Nutzer seine Zahlungsmethode sicher hinterlegen kann
        const setupIntent = await stripe.setupIntents.create({
            customer: customerId,
            usage: 'off_session',
            metadata: { tenant_id: tenant.id, plan }
        });

        return new Response(JSON.stringify({
            clientSecret: setupIntent.client_secret,
            intentType: 'setup',
            success: true
        }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });

    } catch (error: any) {
        console.error('Error in create-subscription-intent:', error);
        return new Response(JSON.stringify({ error: error.message }), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
    }
});
