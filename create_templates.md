Das ist ein sehr kluger Gedanke! Wenn du dir diese Regeln abspeicherst, kannst du in Zukunft (entweder selbst oder durch eine KI wie mich) in wenigen Sekunden perfekt funktionierende, neue Zertifikats-Layouts generieren lassen.

Hier ist das **"Master-Regelwerk"** für die Erstellung von HTML-Zertifikatsvorlagen in eurem System:

---

### 📜 Leitfaden: Erstellung von Zertifikats-Templates (Pfotencard)

Jedes neue Layout besteht immer aus zwei Dateien, die denselben Namen tragen müssen (z. B. `layout_neu.html` und `layout_neu.json`).

#### Regel 1: Das unveränderliche Grundgerüst (CSS & A4-Format)

Das System nutzt **WeasyPrint** zur PDF-Generierung und ein skaliertes **iFrame** im React-Frontend. Damit beides auf den Millimeter genau identisch aussieht, muss das HTML zwingend feste A4-Maße in Millimetern haben.

* **Zwingendes CSS im `<head>`:**
```css
@page { size: A4 portrait; margin: 0; } /* Für WeasyPrint PDF */
* { box-sizing: border-box; }
html, body {
    width: 210mm;
    height: 297mm;
    margin: 0;
    padding: 0;
    overflow: hidden; /* Verhindert Scrollbalken in der Vorschau */
    font-family: 'Helvetica', 'Arial', sans-serif; /* Standard-Schriften nutzen! */
    background-color: #fff;
    color: #333;
    position: relative; /* Wichtigste Regel für das Layout! */
}

```



#### Regel 2: Absolute Positionierung ist Goldstandard

Vermeide komplexe CSS-Grids oder stark verschachtelte Flexboxen für das grobe Layout. Chrome (Frontend) und WeasyPrint (Backend) berechnen Restabstände minimal anders.

* **Best Practice:** Platziere alle Hauptblöcke absolut in Millimetern vom Rand aus.
* *Beispiel:*
```css
.footer-area {
    position: absolute;
    bottom: 20mm;
    left: 20mm;
    right: 20mm;
}

```



#### Regel 3: Text-Variablen (Jinja2-Syntax)

Texte werden vom Backend via Jinja2 übergeben. Sie müssen in doppelten geschweiften Klammern stehen.

* **Standard-Variablen, die immer zur Verfügung stehen:**
  `{{ title }}`, `{{ kundenname }}`, `{{ hundename }}`, `{{ kursname }}`, `{{ hundeschule_name }}`, `{{ ort }}`, `{{ datum }}`, `{{ kursleiter }}`, `{{ footer_text }}`, `{{ sidebar_color }}`.
* *Tipp für CSS-Farben:* Variablen können auch im `<style>`-Block für Farben genutzt werden! (z. B. `background-color: {{ sidebar_color | default('#8b9370') }};`)

#### Regel 4: Bilder und Unterschriften (Sicherheits-Check)

Bilder (Logos, Unterschriften, Siegel) sind optional. Das HTML **muss** abfangen, wenn ein Bild nicht hochgeladen wurde, da sonst ein unschönes "Bild fehlt"-Icon (Broken Image) auftaucht.

* **Immer in eine If-Abfrage packen:**
```html
{% if images.mein_logo_slot %}
    <img src="{{ images.mein_logo_slot }}" class="logo-img">
{% else %}
    <div style="height: 20mm;"></div> 
{% endif %}

```


* **CSS für Bilder:** Nutze immer `max-width`, `max-height` und `object-fit: contain;`, damit hochgeladene Logos das Layout nicht sprengen.

#### Regel 5: Dynamische Bild-Variablen & Unterschriften

Seit dem neuesten Update können Bild-Slots dynamisch mit Text-Variablen verknüpft werden. Dies ist der empfohlene Weg für Unterschriften des jeweiligen Kursleiters oder für Logos der Hundeschule.

*   **Funktionsweise:** Im Frontend-Builder kann jeder Bild-Slot entweder mit einem festen Upload befüllt oder mit einer Variable verknüpft werden.
*   **Verfügbare Variablen:** `kursleiter` (Unterschrift), `hundeschule_name` (Schullogo), `kundenname`, `hundename`, `kursname`, `ort`, `datum`.
*   **Syntax für Verknüpfungen:** Falls du ein Zertifikats-Template manuell anlegst oder via API konfigurierst, schreibst du statt einer URL einfach `ref:` gefolgt vom Variablennamen in den Slot:
    ```json
    "images": {
        "unterschrift_links": "ref:kursleiter"
    }
    ```
*   **Wichtig für das HTML:** Im HTML-Code änderst du nichts! Du greifst weiterhin ganz normal über die Slot-ID auf das Bild zu. Das Backend löst die Referenz automatisch auf, bevor das HTML gerendert wird:
    ```html
    <img src="{{ images.unterschrift_links }}">
    ```
*   **Backend-Logik:** Verknüpfte Slots werden intern mit `ref:variablename` gespeichert. Das Backend nimmt dann den aktuellen Wert dieser Variable (z. B. den Namen des Trainers) und sucht in den Mandanten-Einstellungen unter "Unterschriften" nach dem passenden Bild.
*   **Vorteil:** Ein einziges Layout funktioniert für alle Mitarbeiter automatisch – die Unterschrift wechselt je nachdem, wer das Zertifikat ausstellt.
*   **Abwärtskompatibilität:** Falls keine manuelle Verknüpfung im Builder vorgenommen wurde, sucht das Backend weiterhin automatisch nach Unterschriften für die Keys **`images.signature`** oder **`images.signature_2`**.

---

#### Regel 6: Die unverzichtbare JSON-Datei

Damit dein React-Frontend (der Modal-Builder) weiß, welche Upload-Felder und Platzhalter-Eingaben für das Layout angezeigt werden müssen, muss parallel zur `.html`-Datei eine `.json`-Datei angelegt werden.

*Struktur der `.json`-Datei:*

```json
{
    "name": "Mein Neues Layout",
    "image_slots": [
        {
            "id": "mein_logo_slot", 
            "label": "Hauptlogo (Oben)",
            "allow_variables": true,
            "default_ref": "hundeschule_name"
        },
        {
            "id": "signature", 
            "label": "Unterschrift",
            "allow_variables": true,
            "default_ref": "kursleiter"
        },
        {
            "id": "festes_siegel", 
            "label": "Siegel (nur Upload)",
            "allow_variables": false
        }
    ],
    "placeholders": [
        "hundename", "kundenname", "datum", "hundeschule_name", 
        "kursname", "ort", "kursleiter", "sidebar_color"
    ]
}

```

*   **`image_slots`**: Definiert die IDs, die im HTML als `images.ID` aufgerufen werden.
    *   `allow_variables`: (Boolean) Wenn `false`, wird im Frontend nur das Upload-Feld angezeigt (keine Variablen-Verknüpfung möglich).
    *   `default_ref`: (String) Name einer Variable (z.B. `kursleiter`), die standardmäßig für diesen Slot vorausgewählt wird, wenn ein neues Zertifikat erstellt wird.
*   **`placeholders`**: Definiert, welche Testdaten-Eingabefelder das Frontend links in der Sidebar anzeigen soll.

---

### 💡 Dein Prompt-Muster für die Zukunft:

Wenn du ChatGPT oder mich in Zukunft bittest, ein neues Zertifikat zu bauen, kannst du einfach diesen Text kopieren:

> *"Erstelle mir ein neues Zertifikats-Template namens `layout_xyz`. Nutze absolut positionierte Blöcke auf einer festen 210x297mm Seite (A4). Verwende die Jinja2-Variablen `{{ kundenname }}`, `{{ hundename }}`, etc. Das Layout soll [BESCHREIBE DEIN DESIGN, z.B. einen blauen Rahmen haben und das Logo zentriert anzeigen]. Baue If-Abfragen für die Bilder ein und gib mir am Ende auch die dazugehörige JSON-Datei mit den image_slots und placeholders."*