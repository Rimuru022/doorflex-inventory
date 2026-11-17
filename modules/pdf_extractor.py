import io
import json
import re

import pdfplumber

from text_extractor import InvoiceDeterministicParser

NON_PART_ITEMS = {"labor", "tax", "lube & tune", "warranty coverage/doorflex discount",
                  "reset operator force settings and limits"}


class PDFProcessor:
    def __init__(self):
        self.parser = InvoiceDeterministicParser()

    def extract(self, pdf_bytes: bytes) -> dict:
        text = self._extract_text(pdf_bytes)
        data = self.parser.parse(text)
        format_type, parts_list, description = self._detect_format(data)
        data["format"] = format_type
        data["parts_list"] = parts_list
        data["package_description"] = description
        return data

    def _extract_text(self, pdf_bytes: bytes) -> str:
        text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        return text

    def _detect_format(self, data: dict) -> tuple:
        items = data.get("items", [])
        part_items = [i for i in items if i["description"].strip().lower() not in NON_PART_ITEMS]

        if len(part_items) == 1 and part_items[0]["quantity"] == 1:
            description = part_items[0]["description"]
            return "package_pricing", [], description

        parts_list = []
        for item in part_items:
            desc = item["description"].strip()
            qty = item["quantity"]
            parts_list.append({"raw_name": desc, "quantity": qty})

        return "itemized", parts_list, ""


processor = PDFProcessor()
