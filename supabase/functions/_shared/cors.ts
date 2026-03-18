/**
 * CORS Headers - Standardisierte CORS-Konfiguration für alle Edge Functions
 * 
 * @module _shared/cors
 */

export const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-tenant-subdomain, x-supabase-client-platform, x-supabase-client-platform-version, x-supabase-client-runtime, x-supabase-client-runtime-version',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS, PUT, DELETE',
}

/**
 * Handles CORS preflight requests
 * @returns Response for OPTIONS request, null for other methods
 */
export function handleCors(req: Request): Response | null {
  if (req.method === "OPTIONS") {
    return new Response("ok", {
      status: 200,
      headers: corsHeaders
    });
  }
  return null;
}

/**
 * Creates a JSON response with CORS headers
 */
export function jsonResponse(
  data: unknown,
  status: number = 200
): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

/**
 * Creates an error response with CORS headers
 */
export function errorResponse(
  message: string,
  status: number = 500
): Response {
  console.error(`[ERROR] ${message}`);
  return new Response(
    JSON.stringify({ error: message }),
    {
      status,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    }
  );
}