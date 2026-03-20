import { Resend } from "https://esm.sh/resend@2.0.0"

const resend = new Resend(Deno.env.get("RESEND_API_KEY"))

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
    const { to, userName, tenantName, type, title, message, url, details } = payload

    let subject = title;
    let content = message;
    let buttonHtml = "";

    if (url) {
      buttonHtml = `
        <div style="margin-top: 24px;">
          <a href="${url}" style="background-color: #22c55e; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
            In der App ansehen
          </a>
        </div>
      `;
    }

    // --- LOGIK AUS CREATORSTAY: Switch-Case für verschiedene E-Mail Typen ---
    switch (type) {
      case "chat":
        subject = `Neue Nachricht von ${tenantName}`;
        content = `
          <p>Hallo ${userName},</p>
          <p>Du hast eine neue Nachricht in der App erhalten:</p>
          <blockquote style="border-left: 4px solid #e5e7eb; padding-left: 16px; color: #4b5563; font-style: italic; margin: 16px 0;">
            "${message}"
          </blockquote>
        `;
        break;

      case "booking":
        subject = `Termin-Update: ${title}`;
        content = `
          <p>Hallo ${userName},</p>
          <p>Es gibt Neuigkeiten zu deinem Termin bei <strong>${tenantName}</strong>:</p>
          <p style="font-size: 16px; padding: 12px; background-color: #f3f4f6; border-radius: 6px;">${message}</p>
        `;
        break;

      case "news":
        subject = `Neuigkeiten von ${tenantName}: ${title}`;
        content = `
          <p>Hallo ${userName},</p>
          <p><strong>${tenantName}</strong> hat etwas Neues gepostet:</p>
          <p>${message}</p>
        `;
        break;

      case "alert":
        subject = `⚠️ Wichtige Info: ${title}`;
        content = `
          <p>Hallo ${userName},</p>
          <p style="color: #dc2626; font-weight: bold;">${message}</p>
        `;
        break;

      default:
        // Fallback für 'reminder' oder generische Benachrichtigungen
        content = `
          <p>Hallo ${userName},</p>
          <p>${message}</p>
        `;
        break;
    }

    // Details auflisten (z.B. Datum, Zeit bei Terminen)
    let detailsHtml = "";
    if (details && Object.keys(details).length > 0) {
      detailsHtml = `<div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #e5e7eb;">
        <h4 style="margin-bottom: 8px; color: #374151;">Details:</h4>
        <ul style="color: #4b5563; font-size: 14px;">`;
      for (const [key, value] of Object.entries(details)) {
        detailsHtml += `<li><strong>${key}:</strong> ${value}</li>`;
      }
      detailsHtml += `</ul></div>`;
    }

    // E-Mail Template zusammenbauen
    const emailHtml = `
      <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #111827;">
        <h2 style="color: #111827; border-bottom: 2px solid #22c55e; padding-bottom: 10px;">${title}</h2>
        ${content}
        ${detailsHtml}
        ${buttonHtml}
        <p style="margin-top: 40px; font-size: 12px; color: #6b7280; border-top: 1px solid #e5e7eb; padding-top: 16px;">
          Diese E-Mail wurde im Auftrag von ${tenantName} über Pfotencard gesendet.<br>
          Du kannst deine Benachrichtigungseinstellungen jederzeit in der App anpassen.
        </p>
      </div>
    `;

    const { data, error } = await resend.emails.send({
      from: `${tenantName} <benachrichtigungen@pfotencard.de>`, // Passe die Domain an
      reply_to: "support@pfotencard.de",
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