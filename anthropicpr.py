# This program is licensed under the GNU General Public License v3.0.
# For more details, see: https://www.gnu.org/licenses/gpl-3.0.en.html
#
# Author: Jonathan Graziola (isidore.gpt@gmail.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

import streamlit as st
import datetime
import io
import zipfile
import os
import tempfile
from fpdf import FPDF
import pandas as pd
from openai import OpenAI
import re
import time
import base64
from PIL import Image
import pytesseract
from pdf2image import convert_from_path, convert_from_bytes
import fitz  # PyMuPDF
import docx2txt
from pptx import Presentation
import openpyxl
import glob
import numpy as np
from io import BytesIO

# --- Configuration ---
MODEL_OPTIONS = {
    "OpenAI": {
        "models": ["gpt-4.1-2025-04-14", "gpt-4o-2024-08-06", "gpt-4o-mini-2024-07-18"],
        "supports_temperature": True,
        "temp_range": (0.0, 1.0)
    },
    "Anthropic": {
        "models": ["claude-3-5-sonnet-20241022", "claude-3-7-sonnet-20250219"],
        "supports_temperature": True,
        "temp_range": (0.0, 1.0)
    }
}

DOCUMENT_TYPES = [
    "CIM", "Teaser", "Pitch Deck", "Investor Letter", "NDA", "LOI",
    "SPA", "Board Presentation", "Financial Statements", "Forecasts"
]

FINANCIAL_TERMS = [
    "EBITDA", "Enterprise Value", "Run-Rate Revenue", "ARR", "MRR",
    "Cap Table", "Trailing Twelve Months (TTM)", "Debt-to-EBITDA",
    "MOIC", "IRR", "Net Income", "Gross Margin",
    "Operating Margin", "LTM", "YoY", "QoQ"
]

# --- Document Processor ---
class DocumentProcessor:
    @staticmethod
    def extract_from_txt(file_content):
        try:
            if isinstance(file_content, bytes):
                text = file_content.decode('utf-8', errors='replace')
            else:
                text = file_content
            return {'text': text, 'images': [], 'tables': [], 'slides': [], 'charts': []}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def extract_from_docx(file_content):
        try:
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                tmp.write(file_content)
                path = tmp.name
            text = docx2txt.process(path)
            images = []
            img_dir = tempfile.mkdtemp()
            docx2txt.process(path, img_dir)
            for img_path in glob.glob(os.path.join(img_dir, "*.*")):
                with open(img_path, 'rb') as f:
                    images.append({'data': f.read(), 'format': os.path.splitext(img_path)[1].lstrip('.'), 'filename': os.path.basename(img_path)})
            os.unlink(path)
            return {'text': text, 'images': images, 'tables': [], 'slides': [], 'charts': []}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def extract_from_pdf(file_content):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_content)
                path = tmp.name
            doc = fitz.open(path)
            text, images, tables = "", [], []
            for i, page in enumerate(doc):
                text += page.get_text()
                for img_index, img in enumerate(page.get_images(full=True)):
                    xref = img[0]
                    base = doc.extract_image(xref)
                    img_bytes = base['image']
                    ext = base['ext']
                    images.append({'data': img_bytes, 'format': ext, 'filename': f"page_{i+1}_img_{img_index+1}.{ext}"})
                blocks = page.get_text("blocks")
                for b in blocks:
                    if len(b[4].split('\n'))>3 and '  ' in b[4]: tables.append({'page':i+1,'content':b[4],'bbox':b[:4]})
            doc.close(); os.unlink(path)
            return {'text': text, 'images': images, 'tables': tables, 'slides': [], 'charts': []}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def extract_from_pptx(file_content):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as tmp:
                tmp.write(file_content); path=tmp.name
            prs=Presentation(path); text,images,slides="",[],[]
            for idx,slide in enumerate(prs.slides):
                stxt, imgs = "", []
                for shape in slide.shapes:
                    if hasattr(shape,'text'): stxt += shape.text + '\n'
                    if shape.shape_type==13:
                        blob=shape.image.blob; ext=shape.image.ext
                        images.append({'data':blob,'format':ext,'filename':f"slide_{idx+1}_img.{ext}"}); imgs.append(shape.image.filename)
                slides.append({'number':idx+1,'text':stxt,'images':imgs}); text+=stxt
            os.unlink(path)
            return {'text':text,'images':images,'tables':[],'slides':slides,'charts':[]}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def extract_from_xlsx(file_content):
        try:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(file_content); path=tmp.name
            wb=openpyxl.load_workbook(path,data_only=True)
            text, tables, charts = "", [], []
            for sheet in wb.worksheets:
                sheet_text = f"Sheet: {sheet.title}\n"
                data=[]
                for row in sheet.iter_rows(values_only=True):
                    row_vals=[str(c) if c is not None else '' for c in row]
                    sheet_text+=' | '.join(row_vals)+'\n'; data.append(row_vals)
                text+=sheet_text+'\n'
                tables.append({'sheet':sheet.title,'data':data})
                for ch in sheet._charts: charts.append({'sheet':sheet.title,'type':'Excel chart'})
            wb.close(); os.unlink(path)
            return {'text':text,'images':[],'tables':tables,'slides':[],'charts':charts}
        except Exception as e:
            return {'error': str(e)}

    @staticmethod
    def analyze_image(img_data,fmt):
        try:
            img=Image.open(BytesIO(img_data)); text=pytesseract.image_to_string(img)
            w,h=img.size; return {'text':text,'width':w,'height':h,'format':fmt,'is_small':w<300 or h<300}
        except Exception as e: return {'error': str(e)}

    @staticmethod
    def process_document(file_content, file_name):
        ext=os.path.splitext(file_name)[1].lower()
        if ext in ['.txt','.md']: return DocumentProcessor.extract_from_txt(file_content)
        if ext=='.docx': return DocumentProcessor.extract_from_docx(file_content)
        if ext=='.pdf': return DocumentProcessor.extract_from_pdf(file_content)
        if ext=='.pptx': return DocumentProcessor.extract_from_pptx(file_content)
        if ext in ['.xlsx','.xlsm','.xltx']: return DocumentProcessor.extract_from_xlsx(file_content)
        try: return {'text':file_content.decode('utf-8',errors='replace'),'images':[],'tables':[],'slides':[],'charts':[]}
        except: return {'error':f"Unsupported: {ext}"}

# --- Proofreading Manager ---
class ProofreadingManager:
    def __init__(self,api_key): self.api_key=api_key; self.openai_client=OpenAI(api_key=api_key)

    def proofread_document_with_openai(self, model, document_data, document_type, style_guide=None, temperature=0.1):
        try:
            text=document_data.get('text',''); ni=len(document_data.get('images',[])); nt=len(document_data.get('tables',[])); ns=len(document_data.get('slides',[])); nc=len(document_data.get('charts',[]))
            system= (
                "You are an expert proofreader for PE deals..."
                f"\nDocType:{document_type}, images:{ni},tables:{nt},slides:{ns},charts:{nc}\n"
            )
            req = text
            resp=self.openai_client.chat.completions.create(model=model,messages=[{'role':'system','content':system},{'role':'user','content':req}],temperature=temperature)
            return {'content':resp.choices[0].message.content}
        except Exception as e: return {'content':str(e)}

    def proofread_document_with_anthropic(self, model, document_data, document_type, style_guide=None, temperature=0.1):
        try:
            import requests
            text=document_data.get('text','');
            system="You are an expert proofreader for PE deals..."
            data={'model':model,'system':system,'messages':[{'role':'user','content':text}],'temperature':temperature,'max_tokens':4000}
            hdrs={"Content-Type":"application/json","x-api-key":self.api_key}
            r=requests.post("https://api.anthropic.com/v1/messages",json=data,headers=hdrs); r.raise_for_status()
            return {'content':r.json()['content'][0]['text']}
        except Exception as e: return {'content':str(e)}

    def proofread_document(self,provider,model,document_data,document_type,style_guide=None,temperature=0.1):
        if provider=="OpenAI": return self.proofread_document_with_openai(model,document_data,document_type,style_guide,temperature)
        if provider=="Anthropic": return self.proofread_document_with_anthropic(model,document_data,document_type,style_guide,temperature)
        return {'content':f"Unsupported provider {provider}"}

    def extract_financial_terms(self,document_text):
        counts,vars={},{}
        for term in FINANCIAL_TERMS:
            base=term.split()[0]; pat=rf"\b{re.escape(base)}[-\s]*\w*\b"; m=re.findall(pat,document_text,re.I)
            if m: counts[term]=len(m); u=list(set(m));
            if len(u)>1: vars[term]=u
        return {'terms_count':counts,'variants':vars}

    def check_monetary_formatting(self,document_text):
        pats={'spelled_out':r'\$\s*\d+(?:\.\d+)?\s*(?:million|billion|thousand)','M_format':r'\$\s*\d+(?:\.\d+)?M\b'}
        res={}
        for n,p in pats.items(): m=re.findall(p,document_text);
        if m: res[n]=m
        return {'formats':res,'inconsistent':len(res)>1}

    def analyze_dates(self,document_text):
        pats={'ymd':r'\b\d{4}-\d{1,2}-\d{1,2}\b','mdy':r'\b\d{1,2}/\d{1,2}/\d{4}\b','month_dm':r'\b(?:January|...)
