import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.39.3'
// Wir importieren das Standard web-push Paket via esm.sh
import webpush from 'https://esm.sh/web-push@3.6.7'

// Deine VAPID Keys aus den Supabase Secrets
const publicVapidKey = Deno.env.get('VAPID_PUBLIC_KEY')!
const privateVapidKey = Deno.env.get('VAPID_PRIVATE_KEY')!
const mailto = 'mailto:dein-support@pfotencard.de'

webpush.setVapidDetails(mailto, publicVapidKey, privateVapidKey)

serve(async (req) => {
  try {
    const payload = await req.json()
    // Wenn über Database Webhook getriggert, stecken die Daten in payload.record
    const { user_id, title, body, url } = payload

    // Supabase Admin Client initialisieren
    const supabaseAdmin = createClient(
        Deno.env.get('SUPABASE_URL') ?? '',
        Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    )

    // 1. Hole alle aktiven Push-Subscriptions für diesen User
    const { data: subscriptions, error } = await supabaseAdmin
        .from('push_subscriptions')
        .select('*')
        .eq('user_id', user_id)

    if (error || !subscriptions || subscriptions.length === 0) {
      return new Response(JSON.stringify({ message: "Keine aktiven Subscriptions" }), { status: 200 })
    }

    const notificationPayload = JSON.stringify({
      title: title,
      body: body,
      // WICHTIG: Hier wird die URL aus dem Payload in das "data"-Feld verpackt
      data: { 
        url: url || '/' 
      },
      icon: '/paw_icon.png',
      badge: '/paw_icon.png'
    })

    // 2. Sende an alle Geräte des Users gleichzeitig
    const pushPromises = subscriptions.map(async (sub) => {
      const pushSubscription = {
        endpoint: sub.endpoint,
        keys: {
          p256dh: sub.p256dh,
          auth: sub.auth
        }
      }

      try {
        await webpush.sendNotification(pushSubscription, notificationPayload)
        return { success: true }
      } catch (err: any) {
        // 3. WICHTIG: Tote Subscriptions sofort löschen!
        if (err.statusCode === 410 || err.statusCode === 404) {
          console.log(`Lösche tote Subscription: ${sub.id}`)
          await supabaseAdmin.from('push_subscriptions').delete().eq('id', sub.id)
        } else {
          console.error('Push Error:', err)
        }
        return { success: false, error: err }
      }
    })

    await Promise.all(pushPromises)

    return new Response(JSON.stringify({ success: true }), { status: 200 })
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), { status: 500 })
  }
})