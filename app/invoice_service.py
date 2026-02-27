import io
import locale
import logging
import os
from datetime import datetime
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Table, TableStyle
import requests # To fetch logo
from reportlab.lib.utils import ImageReader

from . import models

logger = logging.getLogger(__name__)

def underline_text(c, x, y, text):
    text_width = c.stringWidth(text)
    c.line(x, y - 2, x + text_width, y - 2)

def generate_invoice_pdf(transaction: models.Transaction, tenant: models.Tenant, user: models.User) -> io.BytesIO:
    """
    Generates a PDF invoice for the given transaction.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    
    # --- CONFIG ---
    # Tenant Invoice Settings
    inv_settings = tenant.config.get("invoice_settings", {})
    # If inv_settings is a Pydantic model (depending on how it's loaded), convert to dict
    if hasattr(inv_settings, "dict"):
        inv_settings = inv_settings.dict()
    # Fallback if empty (should prevent download in frontend if crucial data missing, but good to be safe)
    company_name = inv_settings.get("company_name") or tenant.name
    address_line1 = inv_settings.get("address_line1") or ""
    address_line2 = inv_settings.get("address_line2") or ""
    
    # Styles
    style = getSampleStyleSheet()
    normal_style = style['Normal']
    normal_style.fontSize = 10
    normal_style.leading = 14
    
    # --- LOGO & SENDER ADDRESS ---
    try:
        logo_url = inv_settings.get("logo_url")
        if not logo_url:
            branding = tenant.config.get("branding", {})
            logo_url = branding.get("logo_url")
            
        if logo_url:
            img = None
            # If it's a remote URL, fetch it
            if logo_url.startswith("http"):
                try:
                    resp = requests.get(logo_url, timeout=5)
                    if resp.status_code == 200:
                        img_data = io.BytesIO(resp.content)
                        img = ImageReader(img_data)
                except Exception as e:
                    logger.warning(f"Could not load logo from {logo_url}: {e}")
            else:
                # Handle relative path (local file)
                # Remove leading slash if present
                clean_path = logo_url.lstrip('/')
                # Check probable locations
                locations = [
                    clean_path,
                    os.path.join("app", clean_path),
                    os.path.join(".", clean_path),
                    os.path.join("public_uploads", clean_path.split('/')[-1]),
                ]
                for loc in locations:
                    if os.path.exists(loc):
                        try:
                            img = ImageReader(loc)
                            break
                        except:
                            continue
            
            if img:
                c.drawImage(img, 50, A4[1] - inch - 70, width=200, height=80, preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logger.error(f"Logo error: {e}")

    # Sender Address (Absenderzeile klein)
    is_small_business = inv_settings.get("is_small_business", False)
    
    if is_small_business:
        # Kleingewerbe: "Fantasiename – Inh. Vorname Nachname" oder nur "Vorname Nachname"
        owner_name = inv_settings.get("owner_name") or ""
        fantasie_name = inv_settings.get("fantasie_name") or ""
        if fantasie_name and owner_name:
            sender_company = f"{fantasie_name} – Inh. {owner_name}"
        else:
            sender_company = owner_name or fantasie_name or company_name
    else:
        # GmbH: Nur offizieller Firmenname
        sender_company = company_name

    sender_line = f"{sender_company}, {address_line1}, {address_line2}"
    c.setFont("Helvetica", 8)
    # Positioning adapted to A4 standard window envelope
    c.drawString(50, A4[1] - inch - 120, sender_line)
    underline_text(c, 50, A4[1] - inch - 120, sender_line)
    
    # --- RECIPIENT ---
    c.setFont("Helvetica", 10)
    customer_addr_start = 150
    
    # Name
    recipient_name = user.name
    if hasattr(user, 'first_name') and user.first_name and user.last_name:
        recipient_name = f"{user.first_name} {user.last_name}"
        
    c.drawString(50, A4[1] - inch - customer_addr_start, recipient_name)
    # Temporary fallback for address since User model might not have full address split
    # If user has address fields, use them. Assuming user might just have basic fields.
    # We will just print empty lines if data is missing, or "Adresse unbekannt"
    # c.drawString(50, A4[1] - inch - (customer_addr_start + 14), "Musterstraße 1")
    # c.drawString(50, A4[1] - inch - (customer_addr_start + 28), "12345 Musterstadt")
    
    # --- INFO BLOCK (Right side) ---
    right_x = A4[0] - inch - 135
    info_start_y = A4[1] - inch - 110
    
    c.drawString(right_x, info_start_y + 25, f"{sender_company}") # Wiederholung Firmenname oben rechts
    c.drawString(right_x, info_start_y, f"{address_line1}")
    c.drawString(right_x, info_start_y - 14, f"{address_line2}")
    
    c.drawString(right_x, info_start_y - 42, "Datum:")
    date_str = transaction.date.strftime("%d.%m.%Y")
    c.drawRightString(A4[0] - 50, info_start_y - 42, date_str)
    
    c.drawString(right_x, info_start_y - 56, "Rechnungs-Nr.:")
    invoice_nr = transaction.invoice_number or "ENTWURF"
    c.drawRightString(A4[0] - 50, info_start_y - 56, invoice_nr)
    
    # --- HEADING ---
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, A4[1] - inch - 250, f"Rechnung {invoice_nr}")
    
    # --- TABLE ---
    # Data: [Pos, Description, Amount, Price, Total]
    
    # Format currency
    amount_str = f"{abs(transaction.amount):.2f}".replace('.', ',') + " €"
    
    vat_rate = inv_settings.get("vat_rate", 19.0)
    is_small_business = inv_settings.get("is_small_business", False)

    if is_small_business:
        table_data = [
            ["Pos.", "Beschreibung", "Menge", "Einzelpreis", "Gesamtpreis"],
            ["1", transaction.description or "Leistung", "1", amount_str, amount_str]
        ]
        col_widths = [40, 250, 50, 80, 80]
    else:
        # Bei GmbH MwSt. in der Zeile anzeigen
        table_data = [
            ["Pos.", "Beschreibung", "MwSt.", "Menge", "Einzelpreis", "Gesamtpreis"],
            ["1", transaction.description or "Leistung", f"{vat_rate}%", "1", amount_str, amount_str]
        ]
        col_widths = [30, 220, 40, 50, 80, 80]
    
    table_y = A4[1] - inch - 300
    
    t = Table(table_data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('LINEBELOW', (0,0), (-1,0), 1, colors.black),
        ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,0), 10),
        ('BOTTOMPADDING', (0,1), (-1,-1), 5),
    ]))
    
    w, h = t.wrap(A4[0], A4[1])
    t.drawOn(c, 50, table_y - h)
    
    # --- TOTALS ---
    total_y = table_y - h - 30
    c.setFont("Helvetica-Bold", 10)
    
    amount = abs(transaction.amount)
    
    if is_small_business:
        # Kleinunternehmer
        amount_str = f"{amount:.2f}".replace('.', ',') + " €"
        c.drawRightString(A4[0] - 50, total_y, f"Rechnungsbetrag: {amount_str}")
        
        total_y -= 25
        c.setFont("Helvetica", 9)
        small_business_text = inv_settings.get("small_business_text") or "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet und ausgewiesen."
        c.drawString(50, total_y, small_business_text)
    else:
        # GmbH (Brutto-Rechnung - MwSt. herausrechnen)
        netto = amount / (1 + (vat_rate / 100))
        tax = amount - netto
        
        netto_str = f"{netto:.2f}".replace('.', ',') + " €"
        tax_str = f"{tax:.2f}".replace('.', ',') + " €"
        gross_str = f"{amount:.2f}".replace('.', ',') + " €"
        
        c.setFont("Helvetica", 10)
        c.drawRightString(A4[0] - 150, total_y, "Gesamt Netto:")
        c.drawRightString(A4[0] - 50, total_y, netto_str)
        
        total_y -= 15
        c.drawRightString(A4[0] - 150, total_y, f"zuzüglich {vat_rate}% MwSt.:")
        c.drawRightString(A4[0] - 50, total_y, tax_str)
        
        total_y -= 20
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(A4[0] - 150, total_y, "Rechnungsbetrag (Brutto):")
        c.drawRightString(A4[0] - 50, total_y, gross_str)
        
        total_y -= 25
        c.setFont("Helvetica", 9)
        c.drawString(50, total_y, "Leistungsdatum entspricht Rechnungsdatum.")

    # --- FOOTER ---
    draw_footer(c, inv_settings)
    
    c.showPage()
    c.save()
    
    buffer.seek(0)
    return buffer

def draw_footer(c, settings):
    footer_y = 60
    c.setFont("Helvetica", 8)
    c.setStrokeColor(colors.lightgrey)
    c.line(50, footer_y + 15, A4[0]-50, footer_y + 15)
    
    is_small_business = settings.get("is_small_business", False)
    
    company = settings.get("company_name", "")
    if is_small_business:
        # Bei Kleingewerbe evtl. nur Inhabername in der Fußzeile falls Fantasiename zu lang
        owner_name = settings.get("owner_name") or ""
        fantasie_name = settings.get("fantasie_name") or ""
        if fantasie_name and owner_name:
            company = f"{fantasie_name} – {owner_name}"
        else:
            company = owner_name or fantasie_name or company

    bank = settings.get("bank_name", "")
    iban = settings.get("iban", "")
    bic = settings.get("bic", "")
    tax_nr = settings.get("tax_number", "")
    vat_id = settings.get("vat_id", "")
    reg_court = settings.get("registry_court", "")
    reg_nr = settings.get("registry_number", "")
    footer_text = settings.get("footer_text", "")
    
    # Column 1: Company & Text
    c.drawString(50, footer_y, company[:50])
    
    y_offset = 12

    if footer_text:
        c.setFont("Helvetica", 7)
        c.drawString(50, footer_y - y_offset, footer_text[:60]) # First line
        c.drawString(50, footer_y - y_offset - 10, footer_text[60:120]) # Second line
    
    # Column 2: Tax & Registry
    c.setFont("Helvetica", 8)
    col2_x = 220
    current_col2_y = footer_y
    if is_small_business:
        if tax_nr:
            c.drawString(col2_x, current_col2_y, f"Steuernummer: {tax_nr}")
            current_col2_y -= 12
        if vat_id:
            c.drawString(col2_x, current_col2_y, f"USt-ID: {vat_id}")
            current_col2_y -= 12
    else:
        # GmbH: USt-IdNr zwingend falls vorhanden
        if vat_id:
            c.drawString(col2_x, current_col2_y, f"USt-ID: {vat_id}")
            current_col2_y -= 12
            #if tax_nr:
            #    c.drawString(col2_x, current_col2_y, f"Steuer-Nr: {tax_nr}")
            #    current_col2_y -= 12
        elif tax_nr:
            c.drawString(col2_x, current_col2_y, f"Steuer-Nr: {tax_nr}")
            current_col2_y -= 12
    
    # Registergericht & Nummer (falls vorhanden) unter die Steuernummer
    print(42343434, reg_court, reg_nr)
    if reg_court or reg_nr:
        print("ererre", reg_court, reg_nr)
        reg_line = f"{reg_court} {reg_nr}".strip()
        c.drawString(col2_x, current_col2_y, reg_line[:60])

    # Column 3: Bank
    col3_x = 380
    if bank or iban:
        c.drawString(col3_x, footer_y, "Bankverbindung:")
        c.drawString(col3_x, footer_y - 12, f"{bank}")
        c.drawString(col3_x, footer_y - 24, f"IBAN: {iban}")
        if bic:
            c.drawString(col3_x, footer_y - 36, f"BIC: {bic}")
def generate_invoice_preview(settings: dict, branding_logo_url: str = None) -> io.BytesIO:
    """
    Generates a PDF invoice preview using mock data and provided settings.
    """
    # Create mock transaction
    class MockTransaction:
        id = 0
        amount = 59.90
        description = "Paket: Profi-Hundeschule (Beispiel)"
        date = datetime.now()
        invoice_number = "2026-Vorschau"

    # Create mock user
    class MockUser:
        name = "Max Mustermann"
        first_name = "Max"
        last_name = "Mustermann"

    class MockTenant:
        name = settings.get("company_name") or "Deine Hundeschule"
        config = {
            "invoice_settings": settings,
            "branding": {"logo_url": branding_logo_url} if branding_logo_url else {}
        }

    mock_tx = MockTransaction()
    mock_tenant = MockTenant()
    mock_user = MockUser()

    return generate_invoice_pdf(mock_tx, mock_tenant, mock_user)
