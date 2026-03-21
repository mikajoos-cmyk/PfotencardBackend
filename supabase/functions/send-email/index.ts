import { Resend } from "https://esm.sh/resend@2.0.0"

const resend = new Resend(Deno.env.get("RESEND_API_KEY"))

// Cors Headers für Aufruf aus dem Browser (falls nötig)
const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') {
    return new Response('ok', { headers: corsHeaders })
  }

  try {
    const payload = await req.json()
    const { to, userName, tenantName, type, title, message, url, details, logoUrl, primaryColor, supportEmail } = payload
    
    // Branding-Farben und Support-Kontakt dynamisch setzen
    const brandColor = primaryColor || "#22c55e";
    const brandSupportEmail = supportEmail || "support@pfotencard.de";

    // --- NEU: Keine E-Mails für Chat-Nachrichten versenden ---
    if (type === "chat") {
      return new Response(JSON.stringify({ message: "Skipping email for chat message" }), {
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        status: 200,
      })
    }

    let subject = title;
    let content = "";

    // Basis-Styling für Text-Absätze, damit es einheitlich bleibt
    const pStyle = "color: #64748B; font-size: 16px; line-height: 1.6; text-align: center; margin-bottom: 16px;";

    // --- Switch-Case für verschiedene E-Mail Typen ---
    switch (type) {
      case "chat":
        subject = `Neue Nachricht von ${tenantName}`;
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="${pStyle}">Du hast eine neue Nachricht in der App erhalten:</p>
          <div style="background-color: #F1F5F9; border-left: 4px solid ${brandColor}; padding: 16px; color: #475569; font-style: italic; margin: 20px 0; text-align: left; border-radius: 4px;">
            "${message}"
          </div>
        `;
        break;

      case "waitinglist_move":
        subject = `Gute Nachrichten: Platz bestätigt bei ${tenantName}`;
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="${pStyle}">du bist soeben von der <strong>Warteliste nachgerückt</strong>!</p>
          <div style="margin: 20px 0; padding: 16px; background-color: #F1F5F9; border: 1px solid ${brandColor}; border-radius: 8px; color: #334155; text-align: center;">
            Dein Platz für den Termin <strong>${details?.Kurs || ""}</strong> ist nun fest bestätigt.
          </div>
        `;
        break;

      case "booking":
        subject = `Termin-Update: ${title}`;
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="${pStyle}">Es gibt Neuigkeiten zu deinem Termin bei <strong>${tenantName}</strong>:</p>
          <div style="margin: 20px 0; padding: 16px; background-color: #F1F5F9; border-radius: 8px; color: #334155; text-align: center;">
            ${message}
          </div>
        `;
        break;

      case "news":
        subject = `Neuigkeiten von ${tenantName}: ${title}`;
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="${pStyle}"><strong>${tenantName}</strong> hat etwas Neues gepostet:</p>
          <p style="${pStyle}">${message}</p>
        `;
        break;

      case "alert":
        subject = `⚠️ Wichtige Info: ${title}`;
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="color: #dc2626; font-size: 16px; line-height: 1.6; text-align: center; font-weight: 600;">
            ${message}
          </p>
        `;
        break;

      default:
        content = `
          <p style="${pStyle}">Hallo <strong>${userName}</strong>,</p>
          <p style="${pStyle}">${message}</p>
        `;
        break;
    }

    // --- Details auflisten (angepasst an das neue Design) ---
    let detailsHtml = "";
    if (details && Object.keys(details).length > 0) {
      detailsHtml = `
        <div style="margin-top: 30px; padding: 20px; background-color: #F8FAFC; border-radius: 8px; border: 1px solid #E2E8F0; text-align: left;">
          <h4 style="margin-top: 0; margin-bottom: 12px; color: #0F172A; font-size: 14px;">Details zum Termin:</h4>
          <ul style="color: #64748B; font-size: 14px; margin: 0; padding-left: 20px; line-height: 1.6;">`;
      for (const [key, value] of Object.entries(details)) {
        detailsHtml += `<li style="margin-bottom: 6px;"><strong>${key}:</strong> ${value}</li>`;
      }
      detailsHtml += `</ul></div>`;
    }

    // --- Button & Fallback-Link (angepasst an das neue Design) ---
    let buttonHtml = "";
    if (url) {
      buttonHtml = `
        <div style="text-align: center; margin: 30px 0;">
          <a href="${url}" style="background-color: ${brandColor}; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: 600; display: inline-block;">
            In der App ansehen
          </a>
        </div>
        <p style="color: #94A3B8; font-size: 12px; text-align: center; margin-top: 20px;">
          Falls der Button nicht funktioniert, nutze diesen Link:<br>
          <a href="${url}" style="color: ${brandColor}; word-break: break-all;">${url}</a>
        </p>
      `;
    }

    // --- Logo oder Text-Header vorbereiten ---
    let headerContent = "";
    if (logoUrl) {
      headerContent = `<img src="${logoUrl}" alt="${tenantName}" style="height: 60px; object-fit: contain;">`;
    } else {
      headerContent = `<h1 style="margin: 0; color: ${brandColor}; font-size: 28px; font-weight: 800; letter-spacing: -0.5px;">${tenantName}</h1>`;
    }

    const emailHtml = `
      <div style="background-color: #F8FAFC; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; padding: 40px 20px;">
        <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; padding: 40px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #E2E8F0;">
          
          <div style="text-align: center; margin-bottom: 30px;">
            ${headerContent}
          </div>

          <h2 style="color: #0F172A; text-align: center; margin-bottom: 24px; font-size: 24px;">${title}</h2>
          
          ${content}
          
          ${detailsHtml}
          
          ${buttonHtml}

          <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #E2E8F0; text-align: center;">
            <p style="color: #94A3B8; font-size: 12px; margin: 0; line-height: 1.5;">
              Diese E-Mail wurde im Auftrag von <strong>${tenantName}</strong> über Pfotencard gesendet.<br>
              Du kannst deine Benachrichtigungseinstellungen jederzeit in der App anpassen.
            </p>
          </div>

        </div>
      </div>
    `;

    const { data, error } = await resend.emails.send({
      from: `${tenantName} <benachrichtigungen@pfotencard.de>`, // Passe die Domain an
      reply_to: brandSupportEmail,
      to: [to],
      subject: subject,
      html: emailHtml,
    });

    if (error) {
      throw error;
    }

    return new Response(JSON.stringify(data), {
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      status: 200,
    })

  } catch (error) {
    console.error("Resend API error:", error);
    return new Response(JSON.stringify({ error: error.message }), {
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      status: 500,
    })
  }
})