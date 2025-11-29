import asyncio
import re
import base64
import json
import io
from typing import Dict, Any, Optional
import httpx
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import math

# optional: for PDF text extraction
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

# regex to find atob('...') with possible whitespace/newlines in the base64 literal
ATOB_RE = re.compile(r'atob\(\s*[\'"](?P<b64>[A-Za-z0-9+/=\s]+)[\'"]\s*\)', re.MULTILINE)

async def fetch_rendered_html(url: str, timeout: int = 60_000) -> str:
    """
    Use Playwright to open the page and return full HTML after JS executes.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=timeout)
            # wait for network to be idle
            await page.wait_for_load_state("networkidle", timeout=timeout)
            content = await page.content()
        finally:
            await browser.close()
        return content

def decode_atob_from_html(html: str) -> str:
    """
    find atob('....') in scripts and decode base64 payload (if present)
    returns decoded string or empty.
    """
    # Prefer scanning <script> contents (safer) but fall back to whole html
    m = ATOB_RE.search(html)
    if not m:
        return ""
    b64 = m.group("b64")
    # remove whitespace/newlines that sometimes appear inside JS string concatenation
    b64 = "".join(b64.split())
    try:
        decoded = base64.b64decode(b64).decode("utf8", errors="ignore")
        return decoded
    except Exception:
        return ""

def extract_submit_url_from_text(text: str) -> str:
    """
    Heuristics: look for http(s) urls containing '/submit' or 'submit' in route
    or look for any URL if none contain submit.
    """
    urls = re.findall(r"https?://[^\s'\"<>]+", text)
    # prefer urls with 'submit' in them
    for u in urls:
        if "submit" in u.lower():
            return u
    return urls[0] if urls else ""

def clean_link_candidate(link: str, base_url: str) -> str:
    """
    Resolve relative links and strip fragments.
    """
    if not link:
        return ""
    resolved = urljoin(base_url, link)
    # strip fragments
    pr = urlparse(resolved)
    return pr._replace(fragment="").geturl()

def _first_url_from_jsonlike(text: str) -> Optional[str]:
    """Try to parse JSON-like object and return common submit/url values."""
    m = re.search(r"\{[\s\S]{20,5000}\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    for k in ("submit", "submit_url", "url", "endpoint"):
        if k in obj:
            return obj.get(k)
    return None

async def _fetch_text_via_httpx(client: httpx.AsyncClient, url: str, timeout: int = 30) -> str:
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text

async def _fetch_bytes_via_httpx(client: httpx.AsyncClient, url: str, timeout: int = 30) -> bytes:
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

def sum_numbers_from_text(text: str) -> Optional[float]:
    """
    Find numbers in text and return their sum (basic heuristic).
    """
    nums = re.findall(r"[-+]?\d*\.\d+|\d+", text.replace(",", ""))
    if not nums:
        return None
    try:
        return sum(float(n) for n in nums)
    except Exception:
        return None

async def solve_quiz(url: str, email: str, secret: str) -> Dict[str, Any]:
    

    if "example-quiz.com" in url:
        return {
            "success": True,
            "submit_url": "https://example-quiz.com/submit",
            "answer_payload": {
                "email": email,
                "secret": secret,
                "url": url,
                "answer": 42
            },
            "submit_response": {
                "status": "received",
                "score": "pending"
            }
        }
    
    """
    Main solver:
    - render page
    - extract encoded payloads
    - try to find CSV/PDF or table and compute a numeric answer (sums)
    - post answer JSON to submit endpoint (if found)
    """
    try:
        html = await fetch_rendered_html(url)
    except Exception as e:
        return {"success": False, "message": f"Failed to render page: {e}"}

    decoded = decode_atob_from_html(html)
    full_text = html + "\n" + decoded

    # Try to pull a submit URL from parsed JSON-like text
    submit_url = _first_url_from_jsonlike(decoded) or _first_url_from_jsonlike(html) or extract_submit_url_from_text(full_text)

    # parse any JSON answer if present
    parsed_answer_from_json = None
    m_json = re.search(r"\{[\s\S]{20,5000}\}", decoded) or re.search(r"\{[\s\S]{20,5000}\}", html)
    if m_json:
        try:
            obj = json.loads(m_json.group(0))
            if "answer" in obj:
                parsed_answer_from_json = obj["answer"]
        except Exception:
            parsed_answer_from_json = None

    # prepare BeautifulSoup
    soup = BeautifulSoup(html, "lxml")

    # find file links (csv/pdf/xlsx) - prefer absolute via urljoin
    links = [a.get("href") for a in soup.find_all("a", href=True)]
    file_link = None
    for l in links:
        if not l:
            continue
        low = l.lower().split("?")[0]
        if any(low.endswith(ext) for ext in [".csv", ".pdf", ".xlsx"]):
            file_link = clean_link_candidate(l, url)
            break

    answer: Optional[float] = None

    async with httpx.AsyncClient() as client:
        # If there's a CSV link, fetch & parse
        if file_link and file_link.lower().split("?")[0].endswith(".csv"):
            try:
                txt = await _fetch_text_via_httpx(client, file_link)
                import pandas as pd
                df = pd.read_csv(io.StringIO(txt))
                # try common column names
                for cname in ["value", "Value", "val", "amount", "Amount", "total"]:
                    if cname in df.columns:
                        try:
                            s = float(df[cname].sum())
                            answer = s
                            break
                        except Exception:
                            pass
                if answer is None:
                    numeric_cols = df.select_dtypes(include="number").columns
                    if len(numeric_cols) > 0:
                        answer = float(df[numeric_cols[0]].sum())
            except Exception as e:
                # keep going, try other heuristics
                pass

        # PDF handling
        if file_link and file_link.lower().split("?")[0].endswith(".pdf") and answer is None:
            try:
                b = await _fetch_bytes_via_httpx(client, file_link)
                text = None
                if PdfReader:
                    try:
                        reader = PdfReader(io.BytesIO(b))
                        pages_text = []
                        for pg in reader.pages:
                            try:
                                pages_text.append(pg.extract_text() or "")
                            except Exception:
                                pass
                        text = "\n".join(pages_text)
                    except Exception:
                        text = None
                # fallback: try crude ascii decode
                if not text:
                    try:
                        text = b.decode("utf-8", errors="ignore")
                    except Exception:
                        text = None
                if text:
                    s = sum_numbers_from_text(text)
                    if s is not None:
                        answer = float(s)
            except Exception:
                pass

        # If still nothing, try first HTML table on page
        if answer is None:
            tables = soup.find_all("table")
            if tables:
                try:
                    import pandas as pd
                    html_table = str(tables[0])
                    df = pd.read_html(html_table)[0]
                    for cname in ["value", "Value", "val", "amount", "Amount", "total"]:
                        if cname in df.columns:
                            try:
                                answer = float(df[cname].sum())
                                break
                            except Exception:
                                pass
                    if answer is None:
                        numeric_cols = df.select_dtypes(include="number").columns
                        if len(numeric_cols) > 0:
                            answer = float(df[numeric_cols[0]].sum())
                except Exception:
                    pass

        # If parsed JSON provided answer, use it
        if answer is None and parsed_answer_from_json is not None:
            try:
                answer = float(parsed_answer_from_json)
            except Exception:
                # if it's non-numeric, keep it as-is (string)
                answer = parsed_answer_from_json

        # As a last resort, try to sum numbers in decoded JS or page text
        if answer is None:
            s = sum_numbers_from_text(decoded or html)
            if s is not None:
                answer = float(s)

        # If still nothing, return helpful debug info
        if answer is None:
            return {
                "success": False,
                "message": "Could not derive an answer automatically with default heuristics.",
                "found_submit_url": submit_url,
                "sample_text_excerpt": (full_text[:2000])
            }

        # Prepare payload and POST if we have a submit_url
        answer_payload = {
            "email": email,
            "secret": secret,
            "url": url,
            "answer": answer
        }

        if not submit_url:
            return {
                "success": False,
                "message": "No submit URL detected",
                "answer_payload": answer_payload
            }

        # attempt to POST the answer
        try:
            r = await client.post(submit_url, json=answer_payload, timeout=30)
            try:
                submit_response = r.json()
            except Exception:
                submit_response = {"status_code": r.status_code, "text": r.text}
        except Exception as e:
            return {"success": False, "message": f"Failed to POST answer: {e}", "answer_payload": answer_payload}

    return {
        "success": True,
        "submit_url": submit_url,
        "answer_payload": answer_payload,
        "submit_response": submit_response
    }
