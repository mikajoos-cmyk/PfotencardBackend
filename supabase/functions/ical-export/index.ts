import { createClient } from 'npm:@supabase/supabase-js@^2.40.0'

/**
 * iCal-Export für Pfotencard
 * 
 * Diese Edge Function liefert einen dynamischen iCalendar-Feed (.ics) für einen Nutzer zurück.
 * Authentifizierung erfolgt über einen geheimen Token in der URL (?token=...).
 */

Deno.serve(async (req) => {
  try {
    const url = new URL(req.url)
    const token = url.searchParams.get('token')

    if (!token) {
      return new Response(JSON.stringify({ error: 'Token fehlt' }), { status: 400 })
    }

    const supabaseUrl = Deno.env.get('SUPABASE_URL') ?? ''
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''

    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    // 1. Nutzer anhand des iCal-Tokens finden
    const { data: user, error: userError } = await supabase
      .from('users')
      .select('id, name, tenant_id')
      .eq('ical_token', token)
      .single()

    if (userError || !user) {
      console.error('User not found or error:', userError)
      return new Response(JSON.stringify({ error: 'Ungültiger Token' }), { status: 401 })
    }

    // 2. Termine abrufen (alle Buchungen des Nutzers)
    // Wir holen nur Termine, die nicht älter als 30 Tage sind, um den Feed kompakt zu halten
    const thirtyDaysAgo = new Date()
    thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30)

    const { data: bookings, error: bookingsError } = await supabase
      .from('bookings')
      .select(`
        id,
        status,
        appointment:appointments (
          id,
          title,
          description,
          start_time,
          end_time,
          location,
          created_at
        )
      `)
      .eq('user_id', user.id)
      .gte('appointment.start_time', thirtyDaysAgo.toISOString())

    if (bookingsError) {
      console.error('Bookings error:', bookingsError)
      throw new Error('Fehler beim Laden der Termine')
    }

    // 3. iCal-String generieren (RFC 5545)
    let ical = [
      'BEGIN:VCALENDAR',
      'VERSION:2.0',
      'PRODID:-//Pfotencard//NONSGML v1.0//DE',
      'CALSCALE:GREGORIAN',
      'METHOD:PUBLISH',
      'X-WR-CALNAME:Pfotencard Termine',
      'X-WR-TIMEZONE:UTC'
    ].join('\r\n') + '\r\n'

    const formatICalDate = (dateStr: string) => {
      const date = new Date(dateStr)
      return date.toISOString().replace(/[-:]/g, '').split('.')[0] + 'Z'
    }

    for (const booking of (bookings || [])) {
      const event = booking.appointment
      if (!event) continue

      const uid = `${event.id}@pfotencard.de`
      const dtstamp = formatICalDate(event.created_at || new Date().toISOString())
      const dtstart = formatICalDate(event.start_time)
      const dtend = formatICalDate(event.end_time)
      const summary = event.title
      const description = event.description || ''
      const location = event.location || ''
      const status = booking.status === 'confirmed' ? 'CONFIRMED' : 'CANCELLED'

      ical += [
        'BEGIN:VEVENT',
        `UID:${uid}`,
        `DTSTAMP:${dtstamp}`,
        `DTSTART:${dtstart}`,
        `DTEND:${dtend}`,
        `SUMMARY:${summary}`,
        `DESCRIPTION:${description.replace(/\n/g, '\\n')}`,
        `LOCATION:${location}`,
        `STATUS:${status}`,
        'SEQUENCE:0',
        'END:VEVENT'
      ].join('\r\n') + '\r\n'
    }

    ical += 'END:VCALENDAR'

    // 4. Response mit korrektem Header zurückgeben
    return new Response(ical, {
      headers: {
        'Content-Type': 'text/calendar; charset=utf-8',
        'Content-Disposition': 'attachment; filename="pfotencard.ics"'
      }
    })

  } catch (error) {
    console.error('iCal Export Error:', error)
    return new Response(JSON.stringify({ error: error.message }), { status: 500 })
  }
})

/**
 * HINWEIS FÜR FRONTEND:
 * 
 * Abruf-URL generieren:
 * const icalUrl = `https://[PROJECT_REF].supabase.co/functions/v1/ical-export?token=${user.ical_token}`
 * 
 * Für Apple/iOS (One-Click Abo):
 * const webcalUrl = icalUrl.replace('https://', 'webcal://')
 */
