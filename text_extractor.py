import os
import json
import re
import pdfplumber

class InvoiceDeterministicParser:
    def __init__(self):
        # 1. Bloque "From": Todo desde "From " hasta "Invoice for" o "Job ID #"
        self.from_pattern = re.compile(r"From\s+(.*?)\s+(?:Invoice for|Job ID #)", re.DOTALL)
        
        # 2. Campos del Encabezado (Anclados a sus palabras clave exactas)
        self.job_id_pattern = re.compile(r"Job ID #\s*(\d+)")
        self.invoice_number_pattern = re.compile(r"Invoice #\s*(\d+)")
        self.issue_date_pattern = re.compile(r"Issue date\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})")
        self.work_start_date_pattern = re.compile(r"Work start date\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})")
        
        # 3. Dirección del trabajo: Desde "Job Address " hasta "Item Quantity"
        self.job_address_pattern = re.compile(r"Job Address\s+(.*?)\s+Item\s+Quantity", re.DOTALL)
        
        # 4. Datos del Cliente (Bill To): Anclados verticalmente a las líneas adyacentes
        self.bill_to_name_pattern = re.compile(r"Invoice for\s+(.*?)\s*\n")
        self.bill_to_email_pattern = re.compile(r"Issue date\s+.*?\n\s*(.*?@.*?)\s*\n")
        self.bill_to_phone_pattern = re.compile(r"Work start date\s+.*?\n\s*(\+\d+)\s*\n")
        
        # 5. Ítems: Solo captura líneas que terminan estrictamente en "Cantidad Precio Total"
        self.item_pattern = re.compile(r"^(.*?)\s+(\d+)\s+(\$-?[\d.,]+)\s+(\$-?[\d.,]+)$", re.MULTILINE)
        
        # 6. Totales: Líneas que empiezan exactamente con Subtotal, Tax, Total
        self.subtotal_pattern = re.compile(r"^Subtotal\s+(\$[\d.,]+)$", re.MULTILINE)
        self.tax_pattern = re.compile(r"^Tax\s+(\$[\d.,]+)$", re.MULTILINE)
        self.total_pattern = re.compile(r"^Total\s+(\$[\d.,]+)$", re.MULTILINE)
        
        # 7. Firmas: Nombre - Fecha MST NombreTécnico \n Customer \n EmpresaTécnico
        self.sig_pattern = re.compile(r"Signatures\s+(.*?)\s+-\s+(.*?)\s+Customer\s+(.*?)\s+Terms", re.DOTALL)
        
        # Patrones auxiliares
        self.email_pattern = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
        self.phone_pattern = re.compile(r"\+\d{10,12}")

    def parse(self, text: str) -> dict:
        data = {
            "from_details": "", "from_email": "", "from_phone": "",
            "bill_to_name": "", "bill_to_email": "", "bill_to_phone": "",
            "job_id": "", "invoice_number": "", "issue_date": "", "work_start_date": "", "job_address": "",
            "items": [], "subtotal": "", "tax": "", "total": "",
            "customer_signature_name": "", "signature_date": "", "technician_name": "", "technician_company": ""
        }

        # 1. From Details
        from_match = self.from_pattern.search(text)
        if from_match:
            data["from_details"] = self._clean(from_match.group(1))
            data["from_email"] = self._extract(self.email_pattern, data["from_details"])
            data["from_phone"] = self._extract(self.phone_pattern, data["from_details"])

        # 2. Header
        if (m := self.job_id_pattern.search(text)): data["job_id"] = m.group(1)
        if (m := self.invoice_number_pattern.search(text)): data["invoice_number"] = m.group(1)
        if (m := self.issue_date_pattern.search(text)): data["issue_date"] = m.group(1)
        if (m := self.work_start_date_pattern.search(text)): data["work_start_date"] = m.group(1)
        if (m := self.job_address_pattern.search(text)): data["job_address"] = self._clean(m.group(1))

        # 3. Bill To
        if (m := self.bill_to_name_pattern.search(text)): data["bill_to_name"] = self._clean(m.group(1))
        if (m := self.bill_to_email_pattern.search(text)): data["bill_to_email"] = m.group(1).strip()
        if (m := self.bill_to_phone_pattern.search(text)): data["bill_to_phone"] = m.group(1).strip()

        # 4. Items
        for match in self.item_pattern.finditer(text):
            desc = self._clean(match.group(1))
            
            # NORMALIZACIÓN: Eliminar guiones iniciales (viñetas) y espacios adyacentes
            # Esto convierte "- Spring retention" en "Spring retention" y "-Tax" en "Tax"
            desc = re.sub(r'^[-–—\s]+', '', desc).strip()
            
            data["items"].append({
                "description": desc,
                "quantity": int(match.group(2)),
                "price": match.group(3),
                "total": match.group(4)
            })

        # 5. Totals
        if (m := self.subtotal_pattern.search(text)): data["subtotal"] = m.group(1)
        if (m := self.tax_pattern.search(text)): data["tax"] = m.group(1)
        if (m := self.total_pattern.search(text)): data["total"] = m.group(1)

        # 6. Signatures
        sig_match = self.sig_pattern.search(text)
        if sig_match:
            data["customer_signature_name"] = self._clean(sig_match.group(1))
            
            # Separamos "07/22/2026 9:19am MST Nick H" en fecha y nombre del técnico
            date_tech = sig_match.group(2).split(" MST ")
            if len(date_tech) == 2:
                data["signature_date"] = date_tech[0].strip() + " MST"
                data["technician_name"] = date_tech[1].strip()
            else:
                data["signature_date"] = self._clean(sig_match.group(2))
                
            data["technician_company"] = self._clean(sig_match.group(3))

        return data

    def _clean(self, text: str) -> str:
        """Reemplaza todos los saltos de línea y espacios múltiples por un solo espacio"""
        return re.sub(r'\s+', ' ', text).strip()

    def _extract(self, pattern, text):
        match = pattern.search(text)
        return match.group(0) if match else ""

def extract_text_from_pdf(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() + "\n"
    return text

if __name__ == "__main__":
    files_to_process = [
        "Invoice_1350 itemized.pdf",
        "Invoice_70068 package format.pdf"
    ]
    
    parser = InvoiceDeterministicParser()
    
    for file_name in files_to_process:
        if os.path.exists(file_name):
            print(f"\n{'='*20} PROCESANDO: {file_name} {'='*20}")
            
            raw_text = extract_text_from_pdf(file_name)
            result = parser.parse(raw_text)
            
            print(json.dumps(result, indent=4, ensure_ascii=False))
        else:
            print(f"[ERROR] No se encontró el archivo: {file_name}")