import os
import copy
import locale
import sys
import traceback
import logging # Import des logging Moduls

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Table, TableStyle

import firebase_admin
from firebase_admin import credentials, firestore
import offline_data
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from io import BytesIO
import logger_config
# Konfiguration des Loggers
logger_config.setup_logging()
logging = logging.getLogger(__name__)

json_data = ""
working_mode_online = True
exclude_main_cat_num = False
# todo: Oberkategorie Nummern entfernen
def underline_text(c, x, y, text):
    text_width = c.stringWidth(text)
    c.line(x, y - 2, x + text_width, y - 2)

def get_data(name, db):
    global exclude_main_cat_num
    logging.info(f"Starte Datenabruf für Projekt: {name}") # Logging-Info
    locale.setlocale(locale.LC_ALL, 'de_DE.UTF-8')

    # Fetch all articles and store them in a dictionary
    if working_mode_online:
        logging.info("Arbeite im Online-Modus: Hole Artikel und Kategorien aus Firestore.") # Logging-Info
        articles_ref = db.collection('articles')
        articles = articles_ref.stream()
        collection_ref = db.collection('categories')
        categories = collection_ref.stream()
    else:
        logging.info("Arbeite im Offline-Modus: Hole Artikel und Kategorien aus JSON-Daten.") # Logging-Info
        articles = json_data.get(['articles'])
        categories = json_data.get(["categories"])
    main_cats = {}
    sub_cats = {}
    for category in categories:
        if working_mode_online:
            category = category.to_dict()
        else:
            category = categories[category]
        if category["type"] == "Unterkategorie":
            sub_cats[category["name"]] = category
        if category["type"] == "Hauptkategorie":
            main_cats[category["name"]] = category
    logging.info(f"Hauptkategorien gefunden: {', '.join(main_cats.keys())}") # Logging-Info
    logging.info(f"Unterkategorien gefunden: {', '.join(sub_cats.keys())}") # Logging-Info

    a_num_in_order_per_cat = {}
    articles_data_by_a_num = {}
    for article in articles:
        if working_mode_online:
            article = article.to_dict()
        else:
            article = articles[article]
        main_cat_order = main_cats[article["main_category"]]["order"]
        if main_cat_order not in a_num_in_order_per_cat:
            a_num_in_order_per_cat[main_cat_order] = {}
        sub_cat_order = sub_cats[article["sub_category"]]["order"]
        if sub_cat_order not in a_num_in_order_per_cat[main_cat_order]:
            a_num_in_order_per_cat[main_cat_order][sub_cat_order] = {}
        current_category_ref = a_num_in_order_per_cat[main_cat_order][sub_cat_order]
        if article["order"] not in current_category_ref:
            if article["sub_category"] == "2Kellermontage":
                logging.debug(f"Artikel '{article['name']}' in '2Kellermontage' hinzugefügt.") # Logging-Debug
            current_category_ref[article["order"]] = article
        else:
            if article["sub_category"] == "2Kellermontage":
                logging.warning(f"Duplikater Artikel '{article['name']}' in '2Kellermontage', neue Reihenfolge zugewiesen.") # Logging-Warning
            new_order = max(list(current_category_ref.keys())) + 1
            current_category_ref[new_order] = article
    logging.info(f"Artikel nach Kategorien sortiert. Gesamtanzahl der Artikel: {len(articles_data_by_a_num.keys())}") # Logging-Info

    for i in sorted(list(a_num_in_order_per_cat.keys())):
        sub_cats_in_order = a_num_in_order_per_cat[i]
        for j in sorted(list(sub_cats_in_order.keys())):
            articles_in_order = sub_cats_in_order[j]
            for m in sorted(list(articles_in_order.keys())):
                try:
                    article = articles_in_order[m]
                    articles_data_by_a_num[article["article_number"]] = article
                except Exception: # Generischer Exception-Fang
                    try:
                        article_keys = list(articles_in_order.keys())
                        last_key = m - len(article_keys)
                        article = articles_in_order[article_keys[last_key]]
                        articles_data_by_a_num[article["article_number"]] = article
                    except Exception as e:
                        article_data = None
                        logging.error(f"Fehler beim Verarbeiten des Artikels in get_data: {e}", exc_info=True) # Logging-Error mit Stacktrace
                        traceback.print_exc() # Beibehalten des ursprünglichen Tracebacks
                        logging.debug(f"Fehlerdetails: i={i}, j={j}, m={m}, Artikel in Reihenfolge Keys={list(articles_in_order.keys())}") # Logging-Debug

    logging.info(f"Artikeldaten nach Artikelnummern gesammelt. Anzahl eindeutiger Artikel: {len(articles_data_by_a_num.keys())}") # Logging-Info
    categories = {}
    descriptions = {}
    # Fetch projects with the specified name
    if working_mode_online:
        logging.info(f"Hole Projektdaten für '{name}' aus Firestore.") # Logging-Info
        all_project_data_ref = db.collection('projekte').where("name", "==", name)
        project_ref_list = list(all_project_data_ref.stream())
        if not project_ref_list:
            logging.error(f"Kein Projekt mit dem Namen '{name}' gefunden.") # Logging-Error
            raise ValueError(f"Kein Projekt mit dem Namen '{name}' gefunden.")
        project_ref = project_ref_list[0]
        project_id = project_ref.id
        project_ref = project_ref.to_dict()
    else:
        logging.info(f"Hole Projektdaten für '{name}' aus Offline-JSON.") # Logging-Info
        project_ref, project_id = json_data.where('projekte', "name", name, True)
        if not project_ref:
            logging.error(f"Kein Projekt mit dem Namen '{name}' in Offline-Daten gefunden.") # Logging-Error
            raise ValueError(f"Kein Projekt mit dem Namen '{name}' in Offline-Daten gefunden.")

    addresse = project_ref["adresse"]
    salutation = project_ref["salutation"]
    customer_nr = project_ref["customer_nr"]
    logging.info(f"Projektdaten geladen: Adresse='{addresse}', Anrede='{salutation}', Kundennummer='{customer_nr}'") # Logging-Info

    for article_num, current_article_data in articles_data_by_a_num.items():
        # Search subcollection 'articles' within each project
        if working_mode_online:
            articles_ref = db.collection('projekte').document(project_id).collection("articles").where("article_num",
                                                                                                       "==",
                                                                                                       article_num)
            article_docs = articles_ref.get()
            if article_docs:
                article = article_docs[0]
                sub_doc_ref = db.collection('projekte').document(project_id).collection("articles").document(article.id)
                project_article = sub_doc_ref.get().to_dict()
            else:
                project_article = None
                logging.warning(f"Artikelnummer '{article_num}' nicht in Projekt '{project_id}' gefunden.") # Logging-Warning
        else:
            project_article = json_data.where(['projekte', project_id, "articles"], "article_num", str(article_num))

        logging.debug(f"Verarbeite Artikel: Projekt-ID={project_id}, Projektartikel={project_article}, Artikelnummer={article_num}") # Logging-Debug

        if project_article:
            amount = project_article["amount"]
        else:
            amount = 0
        if int(amount) > 0:
            main_cat = current_article_data["main_category"]
            sub_cat = current_article_data["sub_category"]
            current_data = ["",
                            amount,
                            current_article_data["unit"],
                            current_article_data["name"] + "\n" + current_article_data["description"],
                            locale.format_string('%.2f', float(current_article_data["selling_price"]), grouping=True),
                            locale.format_string('%.2f', float(current_article_data["selling_price"]) * float(amount),
                                                 grouping=True)]
            if main_cat not in categories:
                categories[main_cat] = {}
            if sub_cat not in categories[main_cat]:
                categories[main_cat][sub_cat] = []

            categories[main_cat][sub_cat].append(current_data)
            logging.debug(f"Artikel '{current_article_data['name']}' mit Menge {amount} hinzugefügt.") # Logging-Debug
    data = []
    main_index = 1
    sub_index = 1
    article_index = 1
    exclude_main_cat_num = False
    if len(categories.keys()) == 1:
        exclude_main_cat_num = True
        logging.info("Nur eine Hauptkategorie gefunden, Hauptkategorienummern werden ausgeschlossen.") # Logging-Info

    for main_cat, sub_items in categories.items():
        if not exclude_main_cat_num:
            if working_mode_online:
                categories_ref = db.collection('categories').where("name", "==", main_cat)
                category_docs = categories_ref.get()
                if category_docs:
                    category = category_docs[0].to_dict()
                else:
                    category = {"description": ""}
                    logging.warning(f"Hauptkategorie '{main_cat}' nicht in Firestore gefunden.") # Logging-Warning
            else:
                category = json_data.where(['categories'], "name", main_cat)
            data.append([str(main_index), "", "", main_cat + "\n" + category["description"], "", ""])
            logging.debug(f"Hauptkategorie '{main_cat}' zur Datenliste hinzugefügt.") # Logging-Debug
        sub_index = 1
        for sub_cat, article_items in sub_items.items():
            if working_mode_online:
                categories_ref = db.collection('categories').where("name", "==", sub_cat)
                category_docs = categories_ref.get()
                if category_docs:
                    category = category_docs[0].to_dict()
                else:
                    category = {"description": ""}
                    logging.warning(f"Unterkategorie '{sub_cat}' nicht in Firestore gefunden.") # Logging-Warning
            else:
                category = json_data.where(['categories'], "name", sub_cat)
            if not exclude_main_cat_num:
                category_index_num = ".".join([str(main_index), str(sub_index)])
            else:
                category_index_num = str(sub_index)

            data.append(
                [category_index_num, "", "", sub_cat + "\n" + category["description"], "", ""])
            logging.debug(f"Unterkategorie '{sub_cat}' zur Datenliste hinzugefügt.") # Logging-Debug
            article_index = 1
            for article in article_items:
                if not exclude_main_cat_num:
                    article_index_num = ".".join([str(main_index), str(sub_index), str(article_index)])
                else:
                    article_index_num = ".".join([str(sub_index), str(article_index)])
                article[0] = article_index_num
                data.append(article)
                article_index += 1
            sub_index += 1
        main_index += 1
    logging.info("Daten für PDF-Generierung erfolgreich vorbereitet.") # Logging-Info
    return data, [salutation, name, addresse, customer_nr]


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
        logging.debug(f"PyInstaller-Pfad erkannt: {base_path}") # Logging-Debug
    except Exception:
        base_path = os.path.abspath(".")
        logging.debug(f"Standard-Pfad erkannt: {base_path}") # Logging-Debug

    full_path = os.path.join(base_path, relative_path)
    logging.debug(f"Ressourcenpfad: {full_path}") # Logging-Debug
    return full_path


def create_pdf(filename, name, date, end_date, a_nr, db, heading, new_json_data, new_working_mode_online):
    global json_data, working_mode_online, side_num
    logging.info(f"Starte PDF-Erstellung für Datei: {filename}, Projekt: {name}") # Logging-Info
    side_num = -1
    json_data = new_json_data
    working_mode_online = new_working_mode_online

    data, customer = get_data(name, db)
    c = canvas.Canvas(filename, pagesize=A4)
    style = getSampleStyleSheet()
    normal_style = style['Normal']
    normal_style.fontSize = 10  # Schriftgröße ändern
    normal_style.leading = 14
    c.setFont(normal_style.fontName, 10)
    small_style = style['Normal']
    small_style.fontSize = 6  # Schriftgröße ändern
    small_style.leading = 14
    small_size = 7
    normal_size = 10
    zeilenabstand = 14
    start_customer = 150
    # Bild 1 einfügen
    image1_path = resource_path('logo2.png')  # Passe den Pfad zur ersten Bilddatei an
    c.drawImage(image1_path, 50, A4[1] - inch - 70, width=399//1.4, height=156//1.4)
    c.drawString(50, A4[1] - inch - 120, "ViP Haustechnik GmbH, Von-Drais-Str. 27/1, 77855 Achern")
    underline_text(c, 50, A4[1] - inch - 120, "ViP Haustechnik GmbH, Von-Drais-Str. 27/1, 77855 Achern")
    logging.debug(f"Logo 'logo2.png' und Absenderadresse platziert.") # Logging-Debug

    c.drawString(50, A4[1] - inch - start_customer, customer[0])
    c.drawString(50, A4[1] - inch - (start_customer + zeilenabstand), customer[1])
    c.drawString(50, A4[1] - inch - (start_customer + 2*zeilenabstand), customer[2])
    logging.debug(f"Kundenadresse platziert: {customer[1]}, {customer[2]}") # Logging-Debug

    # Bild 2 einfügen
    image2_path = resource_path('logo_white_bg.png')  # Passe den Pfad zur zweiten Bilddatei an
    c.drawImage(image2_path, A4[0] - inch - 135, A4[1] - inch - 60, width=426//2.4, height=219//2.4)
    start_line = 110
    c.drawString(A4[0] - inch - 135, A4[1] - inch - 85, "Von-Drais-Str. 27/1, 77855 Achern")
    c.drawString(A4[0] - inch - 135, A4[1] - inch - start_line, "Telefon")
    c.drawString(A4[0] - inch - 135, A4[1] - inch - (start_line + zeilenabstand), "Fax")
    c.drawString(A4[0] - inch - 135, A4[1] - inch - (start_line + 2*zeilenabstand), "E-Mail")

    email = "kontakt@vip-haustechnik.de"
    c.drawString(A4[0] - inch - 90, A4[1] - inch - start_line, "07841 / 640 66 30")
    c.drawString(A4[0] - inch - 90, A4[1] - inch - (start_line + zeilenabstand), "07841 / 640 66 32")
    c.drawString(A4[0] - inch - 90, A4[1] - inch - (start_line + 2*zeilenabstand), email)

    c.drawString(A4[0] - inch - 135, A4[1] - inch - (start_line + 4 * zeilenabstand), "Datum:")
    c.drawString(A4[0] - inch - 135, A4[1] - inch - (start_line + 5 * zeilenabstand), "Bindefrist:")
    text_width = c.stringWidth(email)
    c.drawString(A4[0] - inch - 90 + text_width - c.stringWidth(date), A4[1] - inch - (start_line + 4 * zeilenabstand), date)
    c.drawString(A4[0] - inch - 90 + text_width - c.stringWidth(end_date), A4[1] - inch - (start_line + 5 * zeilenabstand), end_date)
    c.drawString(A4[0] - inch - 135, A4[1] - inch - (start_line + 7 * zeilenabstand), "Angebots-Nr.:")
    c.setFont(normal_style.fontName, normal_size)
    logging.debug(f"Kontaktdaten und Angebotsdetails platziert (Datum: {date}, Bindefrist: {end_date}, Angebots-Nr.: {a_nr}).") # Logging-Debug


    #c.drawString(A4[0] - inch - 90 + text_width - c.stringWidth(customer[3]), A4[1] - inch - (start_line + 7 * zeilenabstand), customer[3])
    c.drawString(A4[0] - inch - 90 + text_width - c.stringWidth(a_nr), A4[1] - inch - (start_line + 7 * zeilenabstand), a_nr)

    c.setFont("Helvetica", 20)

    c.drawString(50, A4[1] - inch - (start_line + 10 * zeilenabstand) - 30, heading)
    logging.debug(f"Überschrift platziert: '{heading}'") # Logging-Debug

    c.setFont(normal_style.fontName, normal_size)

    footer(c, style)
    start = 0
    run = True
    column_widths = [45,45,25,300,50,50]
    row_heights, rows_with_lines, total_sum = adjust_data(c, data)
    new_page = False
    logging.info("Beginne mit dem Zeichnen der Artikeltabelle.") # Logging-Info
    while run:
        row_heights_index = 0
        row_num = 0
        current_row_height_sum = 0
        if start == 0:
            current_row_height = 310
            offset = 400
            logging.debug("Erste Seite der Tabelle wird gezeichnet.") # Logging-Debug
        else:
            current_row_height = 670
            offset = 35
            c.setFont("Helvetica", 11)

            text_width = c.stringWidth(f"{date} {a_nr}")
            c.drawString(A4[0] - text_width - 30,
                         A4[1] - 2 * 15, f"{date} {a_nr}")
            logging.debug("Neue Seite der Tabelle wird gezeichnet.") # Logging-Debug

        for row_height in row_heights[start:]:
            current_row_height_sum += row_height
            row_num += 1
            if current_row_height_sum >= current_row_height:
                break

        if start > len(data):
            logging.info("Alle Daten für die Tabelle wurden verarbeitet.") # Logging-Info
            break

        current_data = data[start:start + row_num]
        current_row_heights = row_heights[start:start + row_num]
        current_rows_with_lines = rows_with_lines[start:start + row_num]
        try:
            current_data.insert(0, ["Position", "Menge", "ME", "Leistung", "Einzel €", "Gesamt €"])
            current_row_heights.insert(0, 15)
            current_row_heights[1] = 15
            table = Table(current_data, colWidths=column_widths, rowHeights=current_row_heights)
            styles = TableStyle([
                ('LINEABOVE', (0, 0), (-1, 0), 1, colors.black),  # Line above the first row
                ('LINEBELOW', (0, 0), (-1, 0), 1, colors.black),
                ('ALIGN', (-1, 1), (-1, -1), 'RIGHT')
            ])
            for row in current_rows_with_lines:
                if row != "":
                    styles.add('LINEABOVE', (3, row-start), (-1, row-start), 1, colors.black)
            table.setStyle(styles)
            table.wrapOn(c, A4[0], A4[1])
            table_width, table_height = table.wrap(A4[0], A4[1])
            y_position_from_bottom = A4[1] - table_height - offset

            table.drawOn(c, 50, y_position_from_bottom)
            footer(c, style)
            if start + row_num > len(data) - 1:
                logging.info("Letzte Seite der Tabelle gezeichnet.") # Logging-Info
                break
            c.showPage()
            new_page = True
            start += row_num
        except Exception as e:
            logging.error(f"Fehler beim Zeichnen der Tabelle: {e}", exc_info=True) # Logging-Error
            break
    if not new_page:
        c.showPage()
        current_row_heights = []
    column_widths = [180,30,60]
    bill_data = [["Summe Netto", "€", locale.format_string('%.2f', total_sum, grouping=True)],
                 ["zuzgl. 19 % gesetzl. MwSt. ", "€", locale.format_string('%.2f', total_sum * 0.19, grouping=True)], ["Endbetrag", "€", locale.format_string('%.2f', total_sum * 1.19, grouping=True)]]
    table = Table(bill_data, colWidths=column_widths, rowHeights=[15, 15, 15])
    styles = TableStyle([
        ('LINEABOVE', (0, 0), (-1, 0), 1, colors.black),  # Line above the first row
        ('LINEBELOW', (0, 1), (-1, 1), 1, colors.black),  # Line below the first row
        ('LINEBELOW', (0, 2), (-1, 2), 1, colors.black),  # Line below the first row
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 2), (0, 2), 'Helvetica-Bold'),
        ('FONTNAME', (-1, 2), (-1, 2), 'Helvetica-Bold')
    ])
    table.setStyle(styles)
    table.wrapOn(c, A4[0], A4[1])
    table_width, table_height = table.wrap(A4[0], A4[1])
    y_position_from_bottom = A4[1] - table_height - sum(current_row_heights) - 3*15 + 10
    x_position_from_bottom = A4[0] - table_width - 30

    table.drawOn(c, x_position_from_bottom, y_position_from_bottom)
    footer(c, style)
    logging.info("Rechnungssummen-Tabelle gezeichnet.") # Logging-Info

    if working_mode_online:
        general_data = db.collection('general_data').document("general_data").get().to_dict()
        logging.debug("Allgemeine Daten aus Firestore geladen.") # Logging-Debug
    else:
        general_data = json_data.get(['general_data', 'general_data'])
        logging.debug("Allgemeine Daten aus Offline-JSON geladen.") # Logging-Debug
    c.setFont("Helvetica", 10)
    pay_info = general_data["pay_info"]
    line_offset = 0
    tab_positions = [25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300]
    c.setFont("Helvetica", 10)
    tab_positions = [50, 100, 150, 200, 250, 300]
    if y_position_from_bottom - 60 - pay_info.count("\n") * 12 < 130:
        c.showPage()
        footer(c, style)
        y_position_from_bottom = A4[1]
        logging.debug("Neue Seite für Zahlungsinfo begonnen.") # Logging-Debug

    for line_num, line in enumerate(pay_info.split("\n")):
        last_start = 0
        last_space = 0
        current_x = 50  # Startposition wewewefür jede Zeile
        current_x_offset = 0
        parts = line.split('\t')  # Teile die Zeile an Tabs auf
        c.setFont("Helvetica", 10)

        for part_index, part in enumerate(parts):
            if part_index > 0:
                # Setze die aktuelle x-Position auf die nächste Tab-Position
                current_x = current_x_offset + tab_positions[min(part_index, len(tab_positions) - 1)-1]
            else:
                for i in tab_positions:
                    if i > c.stringWidth(part)-25:
                        current_x_offset = i
                        break
            for letter_num, letter in enumerate(part):
                if letter == " ":
                    last_space = letter_num
                c.setFont("Helvetica", 10)

                if c.stringWidth(part[last_start:letter_num]) > (600 - current_x):
                    if y_position_from_bottom - 60 - line_offset * 12 > 130:
                        c.drawString(current_x, y_position_from_bottom - 60 - (line_offset) * 12,
                                     part[last_start:last_space].strip())
                    else:
                        c.showPage()
                        y_position_from_bottom = A4[1]
                        c.setFont("Helvetica", 10)
                        c.drawString(current_x, y_position_from_bottom - 60 - (line_offset) * 12,
                                     part[last_start:last_space].strip())
                        footer(c, style)

                    line_offset += 1
                    last_start = last_space
                    current_x = 50  # Zurück zur linken Seite für den nächsten Teil der Zeile
            # Zeichne den verbleibenden Teil der Zeile
            c.setFont("Helvetica", 10)

            if y_position_from_bottom - 60 - line_offset * 12 > 130:
                c.drawString(current_x, y_position_from_bottom - 60 - line_offset * 12,
                             part[last_start:len(part)].strip())

            else:
                c.showPage()
                footer(c, style)
                y_position_from_bottom = A4[1]
                c.drawString(current_x, A4[1] - 60 -(line_offset) * 12,
                             part[last_start:len(part)].strip())
            #line_offset += 1
            last_start = 0  # Zurücksetzen für den nächsten Teil
            last_space = 0

        line_offset += 1

        # Erhöhe die Zeilenanzahl nach jedem Zeilenumbruch
    line_offset += 1
    c.showPage()
    y_start = A4[1]
    c.setFont("Helvetica", 8)
    agbs = general_data["agbs"]
    tab_positions = [25, 50, 75, 100, 150, 200, 250, 300]
    line_offset = 0
    logging.info("Beginne mit dem Zeichnen der AGBs.") # Logging-Info
    for line_num, line in enumerate(agbs.split("\n")):
        last_start = 0
        last_space = 0
        current_x = 50  # Startposition für jede Zeile
        current_x_offset = 0
        parts = line.split('\t')  # Teile die Zeile an Tabs auf
        for part_index, part in enumerate(parts):
            if part_index > 0:
                # Setze die aktuelle x-Position auf die nächste Tab-Position
                current_x = current_x_offset + tab_positions[min(part_index, len(tab_positions) - 1)]
            else:
                for i in tab_positions:
                    if i > c.stringWidth(part) - 25:
                        current_x_offset = i
                        break
            for letter_num, letter in enumerate(part):
                if letter == " ":
                    last_space = letter_num

                if c.stringWidth(part[last_start:letter_num]) > (500 - current_x):
                    c.drawString(current_x, y_start - 60 -(line_offset) * 12,
                                 part[last_start:last_space].strip())
                    line_offset += 1
                    last_start = last_space
                    #current_x = 50  # Zurück zur linken Seite für den nächsten Teil der Zeile

            # Zeichne den verbleibenden Teil der Zeile
            c.drawString(current_x, y_start - 60 -(line_offset) * 12,
                         part[last_start:len(part)].strip())
            last_start = 0  # Zurücksetzen für den nächsten Teil
            last_space = 0

        # Erhöhe die Zeilenanzahl nach jedem Zeilenumbruch
        line_offset += 1
    c.save()
    right_border = A4[0] - 30
    add_page_numbers(filename, filename, right_border)
    logging.info(f"PDF-Erstellung abgeschlossen und Seitenzahlen hinzugefügt: {filename}") # Logging-Info

side_num = -1
def footer(c, style):
    global side_num
    side_num += 1
    style = style['Normal']
    style.fontName = 'Helvetica-Bold'  # Setze die Schriftart auf fett

    # Zeichne den Text in fetter Schrift
    c.setFont(style.fontName, 8)
    # Positioniere die Fußzeile
    footer_start = 100
    area1 = 50
    area2 = 230
    area3 = 400
    area4 = 450
    right_border = A4[0] - 30
    line_dis = -10
    c.line(area1, 110, right_border, 110)
    # Schreibe den Text in die Fußzeile
    c.drawString(area1, footer_start, "VIP Haustechnik GmbH")
    c.drawString(area2, footer_start, "Geschäftsführung")
    c.drawString(area3, footer_start, "Bank:")
    c.drawString(area4, footer_start, "Sparkasse Bühl")
    c.setFont("Helvetica", 8)
    c.drawString(area1, footer_start + line_dis, "Registergericht Mannheim HRB701604")
    c.drawString(area2, footer_start + line_dis, "Horst Ruschmann")
    c.drawString(area3, footer_start + line_dis, "Konto-Nr:")
    c.drawString(area4, footer_start + line_dis, "508853")

    c.drawString(area3, footer_start + 2 * line_dis, "BLZ:")
    c.drawString(area4, footer_start + 2 * line_dis, "66251434")

    c.drawString(area1, footer_start + 3 * line_dis, "USt.-ID-Nr. DE815795390")
    c.drawString(area2, footer_start + 3 * line_dis, "Steuer-Nr. 14064/62509")
    c.drawString(area3, footer_start + 3 * line_dis, "IBAN:")
    c.drawString(area4, footer_start + 3 * line_dis, "DE46 6625 1434 0000 5088 53")

    c.drawString(area3, footer_start + 4 * line_dis, "BIC:")
    c.drawString(area4, footer_start + 4 * line_dis, "SOLADES1BHL")
    logging.debug(f"Fußzeile auf Seite {side_num} gezeichnet.") # Logging-Debug
    #if side_num > 0:
    #    c.drawString(right_border-c.stringWidth(str(side_num)), 30, str(side_num))


def adjust_data(c, data):
    logging.info("Beginne mit der Anpassung der Daten für die Tabelle (adjust_data).") # Logging-Info
    row_heights = []
    rows_with_lines = []
    main_summe = 0
    sub_summe = 0
    offset = 0
    old_numbers = ""
    total_sum = 0
    main_cat_index = 0
    logging.debug(f"Exclude main category number: {exclude_main_cat_num}") # Logging-Debug
    if not exclude_main_cat_num:
        last_main_heading = data[main_cat_index][3].split("\n")[0]
    else:
        main_cat_index -= 1
    last_sub_heading = data[main_cat_index+1][3].split("\n")[0]
    logging.debug(f"Initial last_sub_heading: {last_sub_heading}") # Logging-Debug
    data_copy = copy.deepcopy(data)
    first = True
    headings = 1
    text_length = 210
    for index, d in enumerate(data_copy):
        new_line = []
        for line in d[3].split("\n"):
            start = 0
            last_space = 0
            for i in range(len(line)):
                if line[i] == " ":
                    last_space = i
                if c.stringWidth(line[start:i]) > text_length:
                    new_line.append(line[start:last_space].strip())
                    start = last_space
                if i == len(line) - 1:
                    new_line.append(line[start:].strip())
        try:
            data.remove(d)
            #del data[index + offset]
            offset -= 1
            logging.debug(f"Originaldatensatz entfernt bei Index {index}.") # Logging-Debug
        except Exception as e:
            row_heights.append(12)
            logging.warning(f"Fehler beim Entfernen des Originaldatensatzes bei Index {index}: {e}") # Logging-Warning
        for j, new_data in enumerate(new_line):
            offset += 1

            numbers = str(d[0]).split(".")
            new_old_numbers = data_copy[index-1][0].split(".")
            old_numbers = new_old_numbers if old_numbers != new_old_numbers else [""]
            try:
                if old_numbers[0] != "" and old_numbers[0] < numbers[0]:
                    rows_with_lines.append(index + offset + headings)
                    headings -= 1
                    data.insert(index + offset+1, ["", "", "", f"Summe {last_sub_heading}", "", locale.format_string('%.2f', sub_summe, grouping=True)])
                    offset += 2
                    row_heights.append(16)
                    logging.debug(f"Zwischensumme für Unterkategorie '{last_sub_heading}' hinzugefügt: {sub_summe}") # Logging-Debug
                    if not exclude_main_cat_num:
                        rows_with_lines.append(index + offset + headings)
                        data.insert(index + offset, ["", "", "", f"Summe {last_main_heading}", "", locale.format_string('%.2f', main_summe, grouping=True)])
                        offset += 1
                        row_heights.append(16)
                        logging.debug(f"Zwischensumme für Hauptkategorie '{last_main_heading}' hinzugefügt: {main_summe}") # Logging-Debug
                    main_summe += sub_summe
                    sub_summe = 0
                    total_sum += main_summe
                    main_summe = 0
                    cat_offset = 1
                    if not exclude_main_cat_num:
                        last_main_heading = data_copy[index][3].split("\n")[0]
                    else:
                        cat_offset = 0
                    last_sub_heading = data_copy[index+cat_offset][3].split("\n")[0]

                elif old_numbers[0] != "" and old_numbers[1] < numbers[1] and not exclude_main_cat_num:
                    rows_with_lines.append(index + offset + 1)
                    data.insert(index + offset,
                                ["", "", "", f"Summe {last_sub_heading}", "", locale.format_string('%.2f', sub_summe, grouping=True)])
                    offset += 1
                    row_heights.append(16)
                    main_summe += sub_summe
                    sub_summe = 0
                    last_sub_heading = data_copy[index][3].split("\n")[0]
                    logging.debug(f"Zwischensumme für Unterkategorie '{last_sub_heading}' hinzugefügt: {sub_summe}") # Logging-Debug
                else:
                    rows_with_lines.append("")
            except Exception as e:
                rows_with_lines.append("")
                logging.debug(f"Keine Summenzeile hinzugefügt (Fehler oder Bedingung nicht erfüllt): {e}") # Logging-Debug

            if j == 0:
                if sub_summe == 0:
                    row_heights.append(16)
                else:
                    row_heights.append(15)

                if d[5] != "":
                    sub_summe += float(d[5].replace(".", "").replace(",", "."))
                data.insert(index + offset, [d[0], d[1], d[2], new_data, d[4], d[5]])
                logging.debug(f"Artikelzeile hinzugefügt: {d[0]} - {new_data}") # Logging-Debug

            else:
                row_heights.append(12)
                data.insert(index + offset, ["", "", "", new_data, "", ""])
                logging.debug(f"Fortsetzungszeile für Artikel hinzugefügt: {new_data}") # Logging-Debug

    data.insert(index + offset + 1, ["", "", "", f"Summe {last_sub_heading}", "", locale.format_string('%.2f', sub_summe, grouping=True)])
    offset += 2
    row_heights.append(15)
    main_summe += sub_summe
    total_sum += main_summe
    logging.debug(f"Endgültige Zwischensumme für Unterkategorie '{last_sub_heading}' hinzugefügt: {sub_summe}") # Logging-Debug
    if not exclude_main_cat_num:
        rows_with_lines.append(index + offset + headings)
        data.insert(index + offset, ["", "", "", f"Summe {last_main_heading}", "", locale.format_string('%.2f', main_summe, grouping=True)])
        offset += 1
        row_heights.append(15)
        logging.debug(f"Endgültige Zwischensumme für Hauptkategorie '{last_main_heading}' hinzugefügt: {main_summe}") # Logging-Debug

    logging.info(f"Datenanpassung abgeschlossen. Gesamtsumme: {total_sum}") # Logging-Info
    return row_heights, rows_with_lines, total_sum

def add_page_numbers(input_pdf_path: str, output_pdf_path: str, right_border: float):
    """Fügt Seitennummern in die untere rechte Ecke der Seiten einer PDF-Datei ein.

    Args:
        input_pdf_path (str): Pfad zur Eingabe-PDF-Datei.
        output_pdf_path (str): Pfad zur Ausgabe-PDF-Datei.
        right_border (float): Rechte Grenze der Daten in der PDF
    """
    logging.info(f"Füge Seitennummern zu '{input_pdf_path}' hinzu.") # Logging-Info
    # Lies die Anzahl der Seiten der PDF
    reader = PdfReader(input_pdf_path)
    total_pages = len(reader.pages)
    logging.debug(f"Gesamtanzahl der Seiten: {total_pages}") # Logging-Debug

    # Füge die Seitennummern zur Original-PDF hinzu
    writer = PdfWriter()
    for page_number in range(1, total_pages + 1):
        original_page = reader.pages[page_number - 1]

        # Erstelle eine neue PDF-Seite mit der Seitennummer im Speicher (BytesIO anstatt temporärer Datei)
        packet = BytesIO()
        c = canvas.Canvas(packet)
        c.setFont("Helvetica", 10)  # Schriftart und Schriftgröße
        side_num = f"Seite: {page_number} / {total_pages}"
        c.drawString(right_border - c.stringWidth(side_num), 48, side_num)  # Position (x=500, y=10) für untere rechte Ecke
        c.save()

        # Zurück zum Anfang des BytesIO-Puffers springen
        packet.seek(0)

        # Lese die PDF-Seite aus dem Speicher
        temp_reader = PdfReader(packet)
        temp_page = temp_reader.pages[0]

        # Füge die Seitennummer über die Originalseite
        original_page.merge_page(temp_page)

        # Füge die aktualisierte Seite zum Writer hinzu
        writer.add_page(original_page)
        logging.debug(f"Seitennummer {page_number} zu Seite hinzugefügt.") # Logging-Debug

    # Schreibe die neue PDF-Datei mit den Seitenzahlen
    with open(output_pdf_path, 'wb') as output_file:
        writer.write(output_file)

    logging.info(f"Neue PDF mit Seitennummern gespeichert unter: {output_pdf_path}") # Logging-Info

if __name__ == "__main__":
    import os

    logging.info("Skript 'create_pdf_file.py' direkt gestartet.") # Logging-Info

    # Schließe Adobe Acrobat (Windows-spezifisch)
    try:
        os.system('taskkill /F /IM acrobat.exe')
        logging.info("Adobe Acrobat wurde versucht zu schließen.") # Logging-Info
    except Exception as e:
        logging.warning(f"Konnte Adobe Acrobat nicht schließen (möglicherweise nicht geöffnet oder nicht Windows): {e}") # Logging-Warning

    # Dateinamen für die erstellte PDF-Datei
    pdf_filename = "output8.pdf"
    json_data = offline_data.JsonDataFile("firebase_artikel.json")
    cred = credentials.Certificate("firebase_artikel.json")
    firebase_admin.initialize_app(cred)
    logging.info("Firebase-App initialisiert.") # Logging-Info

    db = firestore.client()
    # PDF erstellen
    create_pdf(pdf_filename, "231232", "14.12.23", "27.12.23", "AN23-205", db, "Angebot Wärmepumpenanlage Panasonic Splitsyste", json_data, True)

    logging.info(f"PDF wurde erfolgreich erstellt: {pdf_filename}") # Logging-Info
