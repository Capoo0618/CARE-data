# utils.py
import re
import html

def clean_html(raw_html):
    """清洗 HTML 標籤與特殊字元，回傳純文字"""
    if not raw_html: return ""
    clean_text = re.sub(r'<[^>]+>', '', raw_html)
    clean_text = clean_text.replace('&nbsp;', ' ').replace('&rdquo;', '"').replace('&ldquo;', '"')
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    return html.unescape(clean_text)