import asyncio
import re
import base64
import json
import io
from typing import Dict, Any, Optional
import httpx
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None
ATOB_RE = re.compile(r'atob\(\s*[\'"](?P<b64>[A-Za-z0-9+/=\s]+)[\'"]\s*\)', re.MULTILINE)
JSON_RE = re.compile(r"\{[\s\S]{2,10000}\}")
URL_EXTRACT_RE = re.compile(r"https?://[^\s'\"<>]+")
NUM_RE = re.compile(r"[-+]?\d*\.\d+|\d+")
async def fetch_rendered_html(url: str, timeout: int = 60_000) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=timeout)
            await page.wait_for_load_state("networkidle", timeout=timeout)
            content = await page.content()
        finally:
            await browser.close()
        return content
def ensure_scheme(u: str) -> str:
    u = u.strip()
    p = urlparse(u)
    if p.scheme:
        return u
    return "https://" + u
def decode_atob_from_html(html: str) -> str:
    m = ATOB_RE.search(html)
    if not m:
        scripts = "".join(re.findall(r"<script[\s\S]*?>[\s\S]*?</script>", html, flags=re.IGNORECASE))
        m = ATOB_RE.search(scripts)
        if not m:
            return ""
    b64 = "".join(m.group("b64").split())
    try:
        return base64.b64decode(b64).decode("utf8", errors="ignore")
    except Exception:
        return ""
def extract_json_from_text(text: str) -> Optional[dict]:
    m = JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        try:
            cleaned = m.group(0).replace("\n", " ")
            return json.loads(cleaned)
        except Exception:
            return None
def find_submit_url(text: str, base: Optional[str] = None) -> str:
    urls = URL_EXTRACT_RE.findall(text)
    for u in urls:
        if "/submit" in u.lower() or "submit" in u.lower():
            return u
    if urls:
        return urls[0]
    if base:
        return base
    return ""
async def _fetch_text(client: httpx.AsyncClient, url: str, timeout: int = 30) -> str:
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text
async def _fetch_bytes(client: httpx.AsyncClient, url: str, timeout: int = 30) -> bytes:
    r = await client.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content
def sum_numbers_from_text(text: str) -> Optional[float]:
    nums = NUM_RE.findall((text or "").replace(",", ""))
    if not nums:
        return None
    try:
        return sum(float(n) for n in nums)
    except Exception:
        return None
async def try_fetch_with_scheme(url: str, timeout: int = 60_000):
    normalized = ensure_scheme(url)
    parsed = urlparse(normalized)
    attempts = [normalized]
    if parsed.scheme == "https":
        attempts.append(urlunparse(parsed._replace(scheme="http")))
    elif parsed.scheme == "http":
        attempts.append(urlunparse(parsed._replace(scheme="https")))
    last_err = None
    for a in attempts:
        try:
            html = await fetch_rendered_html(a, timeout=timeout)
            return html, a, None
        except Exception as e:
            last_err = str(e)
            continue
    return None, None, last_err
async def solve_quiz(url: str, email: str, secret: str) -> Dict[str, Any]:
    html, used_url, fetch_err = await try_fetch_with_scheme(url)
    if html is None:
        return {"success": False, "message": f"Failed to render page: {fetch_err} at {url}", "debug_attempted_url": used_url or url}
    decoded = decode_atob_from_html(html)
    full_text = html + "\n" + decoded
    parsed_json = extract_json_from_text(decoded) or extract_json_from_text(html)
    submit_url = ""
    if parsed_json:
        submit_url = parsed_json.get("submit") or parsed_json.get("submit_url") or parsed_json.get("url") or ""
    if not submit_url:
        submit_url = find_submit_url(full_text, base=used_url)
    soup = BeautifulSoup(html, "lxml")
    pre_texts = [p.get_text() for p in soup.find_all("pre")]
    for ptxt in pre_texts:
        j = extract_json_from_text(ptxt)
        if j:
            parsed_json = j
            if not submit_url:
                submit_url = j.get("submit") or j.get("submit_url") or j.get("url") or submit_url
            break
    if not submit_url:
        spans = soup.select(".origin")
        if spans:
            origins = [s.get_text(strip=True) for s in spans if s.get_text(strip=True)]
            if origins:
                candidate = origins[0]
                if not candidate.startswith("http"):
                    candidate = ensure_scheme(candidate)
                submit_url = candidate.rstrip("/") + "/submit"
    parsed_answer = None
    if parsed_json and "answer" in parsed_json:
        parsed_answer = parsed_json["answer"]
    if parsed_answer is None:
        for ptxt in pre_texts:
            if "answer" in ptxt.lower():
                j = extract_json_from_text(ptxt)
                if j and "answer" in j:
                    parsed_answer = j["answer"]
                    break
    links = [a.get("href") for a in soup.find_all("a", href=True)]
    file_link = None
    for l in links:
        if not l:
            continue
        low = l.lower().split("?")[0]
        if any(low.endswith(ext) for ext in [".csv", ".pdf", ".xlsx"]):
            file_link = urljoin(used_url or url, l)
            break
    answer: Optional[Any] = None
    async with httpx.AsyncClient() as client:
        if file_link and file_link.lower().split("?")[0].endswith(".csv"):
            try:
                txt = await _fetch_text(client, file_link)
                import pandas as pd
                df = pd.read_csv(io.StringIO(txt))
                for cname in ["value", "Value", "val", "amount", "Amount", "total", "sum"]:
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
        if file_link and file_link.lower().split("?")[0].endswith(".pdf") and answer is None:
            try:
                b = await _fetch_bytes(client, file_link)
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
        if answer is None and parsed_answer is not None:
            try:
                answer = float(parsed_answer)
            except Exception:
                answer = parsed_answer
        if answer is None:
            s = sum_numbers_from_text(decoded or html)
            if s is not None:
                answer = float(s)
        if answer is None:
            textual_hint = None
            m = re.search(r'"answer"\s*:\s*"(.*?)"', full_text, flags=re.IGNORECASE|re.DOTALL)
            if m:
                textual_hint = m.group(1).strip()
            if textual_hint:
                answer = textual_hint
        if answer is None:
            return {"success": False, "message": "Could not derive an answer automatically with default heuristics.", "found_submit_url": submit_url, "sample_text_excerpt": full_text[:2000]}
        answer_payload = {"email": email, "secret": secret, "url": url, "answer": answer}
        if not submit_url:
            return {"success": False, "message": "No submit URL detected", "answer_payload": answer_payload}
        try:
            r = await client.post(submit_url, json=answer_payload, timeout=30)
            try:
                submit_response = r.json()
            except Exception:
                submit_response = {"status_code": r.status_code, "text": r.text}
        except Exception as e:
            return {"success": False, "message": f"Failed to POST answer: {e}", "answer_payload": answer_payload}
    return {"success": True, "submit_url": submit_url, "answer_payload": answer_payload, "submit_response": submit_response}
