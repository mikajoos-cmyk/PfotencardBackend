import os
import json
import logging
from typing import List, Dict, Any, Optional
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML # <-- NEU: WeasyPrint Import
import io

logger = logging.getLogger("pfotencard")

class CertificateLayoutMetadata:
    def __init__(self, id: str, name: str, image_slots: List[Dict[str, Any]], placeholders: List[str]):
        self.id = id
        self.name = name
        self.image_slots = image_slots
        self.placeholders = placeholders

class CertificateManager:
    def __init__(self, templates_dir: str = None):
        if templates_dir is None:
            templates_dir = os.path.join(os.path.dirname(__file__), "templates")
        self.templates_dir = templates_dir
        
        self.jinja_env = Environment(loader=FileSystemLoader(self.templates_dir))
        self.layouts: Dict[str, CertificateLayoutMetadata] = {}
        self.reload_layouts()

    def reload_layouts(self):
        self.layouts = {}
        if not os.path.exists(self.templates_dir):
            logger.warning(f"Templates directory {self.templates_dir} does not exist.")
            return

        for filename in os.listdir(self.templates_dir):
            if filename.endswith(".html"):
                layout_id = filename[:-5]
                json_path = os.path.join(self.templates_dir, f"{layout_id}.json")
                
                if os.path.exists(json_path):
                    try:
                        with open(json_path, 'r', encoding='utf-8') as f:
                            meta = json.load(f)
                            self.layouts[layout_id] = CertificateLayoutMetadata(
                                id=layout_id,
                                name=meta.get("name", layout_id),
                                image_slots=meta.get("image_slots", []),
                                placeholders=meta.get("placeholders", [])
                            )
                            logger.info(f"Loaded HTML certificate layout: {layout_id}")
                    except Exception as e:
                        logger.error(f"Error loading metadata for {layout_id}: {e}")
                else:
                    # Default metadata if JSON is missing
                    self.layouts[layout_id] = CertificateLayoutMetadata(
                        id=layout_id,
                        name=layout_id.capitalize(),
                        image_slots=[],
                        placeholders=["hundename", "kundenname", "datum", "hundeschule_name"]
                    )
                    logger.info(f"Loaded HTML certificate layout (no JSON): {layout_id}")

    def list_layouts(self) -> List[CertificateLayoutMetadata]:
        if not self.layouts:
            self.reload_layouts()
        return list(self.layouts.values())

    def get_layout_metadata(self, layout_id: str) -> Optional[CertificateLayoutMetadata]:
        if not self.layouts:
            self.reload_layouts()
        return self.layouts.get(layout_id)

    def render_html(self, layout_id: str, data: Dict[str, Any]) -> str:
        try:
            template = self.jinja_env.get_template(f"{layout_id}.html")
            return template.render(**data)
        except Exception as e:
            logger.error(f"Error rendering HTML template {layout_id}: {e}")
            return f"Error: {e}"

    def render_pdf(self, layout_id: str, data: Dict[str, Any]) -> io.BytesIO:
        # 1. HTML rendern
        html_content = self.render_html(layout_id, data)
        result = io.BytesIO()
        
        # 2. Mit WeasyPrint als PDF schreiben
        try:
            # base_url ist wichtig, falls du relative Pfade für Bilder verwendest
            HTML(string=html_content, base_url=self.templates_dir).write_pdf(result)
        except Exception as e:
            logger.error(f"Error creating PDF with WeasyPrint: {e}")
        
        result.seek(0)
        return result

# Global instance
manager = CertificateManager()
