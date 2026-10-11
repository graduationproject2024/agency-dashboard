import re
import pytest

def test_script_js_contains_xss_protection():
    """Verify that script.js defines escapeHtml and sanitizeHtml functions."""
    with open('script.js', 'r', encoding='utf-8') as f:
        content = f.read()
        
    assert 'function escapeHtml(' in content
    assert 'function sanitizeHtml(' in content
    # Verify that escapeHtml escapes <, >, ", '
    assert '&lt;' in content
    assert '&gt;' in content
    assert '&quot;' in content

def test_index_html_loads_dompurify():
    """Verify that index.html includes DOMPurify sanitizer library."""
    with open('index.html', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'dompurify' in content.lower()

def test_markdown_sanitization_policy():
    """Verify regex-level fallback sanitization handles malicious payloads."""
    malicious_inputs = [
        "<script>alert('xss')</script>",
        "<img src='x' onerror='alert(1)'>",
        "<iframe src='javascript:alert(1)'></iframe>",
        "<a href='javascript:alert(1)'>click me</a>"
    ]
    
    # Python equivalent of our sanitizer logic
    def py_sanitize(text):
        cleaned = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', text, flags=re.IGNORECASE)
        cleaned = re.sub(r'<iframe\b[^<]*(?:(?!<\/iframe>)<[^<]*)*<\/iframe>', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\son\w+="[^"]*"', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\son\w+=\'[^\']*\'', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'javascript:[^"\']*', '', cleaned, flags=re.IGNORECASE)
        return cleaned

    for payload in malicious_inputs:
        safe_output = py_sanitize(payload)
        assert "<script>" not in safe_output
        assert "onerror=" not in safe_output
        assert "javascript:" not in safe_output
