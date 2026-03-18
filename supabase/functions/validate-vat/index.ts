/**
 * Validate EU VAT ID via official VIES REST API
 * Inklusive Bypass für Stripe Test-Nummern
 */
import { handleCors, jsonResponse } from "../_shared/cors.ts";

Deno.serve(async (req: Request) => {
  const corsResponse = handleCors(req);
  if (corsResponse) return corsResponse;

  try {
    const { vatId } = await req.json();

    if (!vatId || vatId.length < 4) {
      return jsonResponse({ valid: false, error: "Zu kurz" });
    }

    // Format bereinigen (Leerzeichen entfernen, alles Großbuchstaben)
    const cleanVat = vatId.replace(/\s+/g, '').toUpperCase();

    // =========================================
    // FIX 1: STRIPE TEST-NUMMERN DURCHWINKEN
    // =========================================
    const testNumbers = ['DE123456789', 'ATU12345678'];
    if (testNumbers.includes(cleanVat)) {
      console.log(`[VAT] Testnummer erkannt (${cleanVat}), VIES-Abfrage übersprungen.`);
      return jsonResponse({ valid: true, isTest: true });
    }

    const countryCode = cleanVat.substring(0, 2);
    const vatNumber = cleanVat.substring(2);

    console.log(`[VAT] Prüfe echte Nummer: Land=${countryCode}, Nummer=${vatNumber}`);

    // =========================================
    // FIX 2: SAUBERER FETCH MIT USER-AGENT
    // =========================================
    const response = await fetch('https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'User-Agent': 'Pfotencard-App/1.0', // Wichtig, damit die EU uns nicht blockiert!
        'Accept': 'application/json'
      },
      body: JSON.stringify({ countryCode, vatNumber })
    });

    if (!response.ok) {
      console.error(`[VAT] EU VIES API offline oder blockiert: HTTP ${response.status}`);
      // WICHTIG: Wenn die EU-Server down sind, geben wir "true" zurück!
      // Wir wollen keine zahlungswilligen Kunden blockieren, nur weil die Behörde Serverprobleme hat.
      return jsonResponse({ valid: true, warning: "EU API offline, bypass" });
    }

    const data = await response.json();
    console.log(`[VAT] EU VIES Antwort:`, data);

    // =========================================
    // FIX 3: FEHLERBEHANDLUNG FÜR VIES-API
    // =========================================
    // Die neue REST-API nutzt "valid" direkt am Top-Level.
    // Falls das Feld fehlt, gab es vermutlich einen Fehler.
    if (data.valid === undefined) {
      const errorMsg = data.errorWrappers?.[0]?.error || "Unbekannter Fehler oder Format-Fehler";
      console.warn(`[VAT] EU VIES Antwort ohne 'valid' Feld: ${errorMsg}. Bypass aktiv.`);
      return jsonResponse({
        valid: true,
        warning: `API Fehler/Format: ${errorMsg}`,
        error: errorMsg
      });
    }

    // data.valid ist das finale Urteil der EU
    // WICHTIG: Fängt sowohl Boolean (true) als auch String ('true') ab
    const isValid = data.valid === true || data.valid === 'true';
    return jsonResponse({ valid: isValid });

  } catch (error: any) {
    console.error("[VAT] Interner Fehler bei der Validierung:", error);
    // Bei einem Crash unsererseits immer durchwinken
    return jsonResponse({ valid: true, error: error.message });
  }
});