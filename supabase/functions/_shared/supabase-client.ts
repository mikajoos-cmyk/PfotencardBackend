import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

// Für Aktionen, die der User selbst ausführt (Auth-Kontext)
export const createAuthClient = (req: Request) => {
    return createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_ANON_KEY') ?? '',
        { global: { headers: { Authorization: req.headers.get('Authorization')! } } }
    )
}

// Für Aktionen, die RLS umgehen müssen (z.B. Webhooks, Paket-Preis-Abfragen)
export const createAdminClient = () => {
    return createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )
}