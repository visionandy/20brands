import re
import json
import os
import sys
import time
import random
from urllib.parse import urljoin, urlparse
from selenium import webdriver
try:
    import requests
    from bs4 import BeautifulSoup
    _HAS_REQUESTS_BS4 = True
except ImportError:
    _HAS_REQUESTS_BS4 = False
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException



def _human_like_delay(min_sec=0.8, max_sec=2.2):
    """随机延时，模拟人类阅读/停顿。"""
    time.sleep(random.uniform(min_sec, max_sec))


def _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4):
    """
    往下滑动鼠标直到加载完所有返回结果（页面高度不再增加），再返回。
    用于在获取 page_source 前拿到全部懒加载内容。
    """
    min_pause, max_pause = base_pause
    _human_like_delay(0.5, 1.0)
    last_height = driver.execute_script("return document.body.scrollHeight")
    no_change_count = 0
    round_num = 0
    while round_num < max_rounds:
        round_num += 1
        if round_num > 1 and random.random() < 0.15:
            back = random.randint(60, 120)
            driver.execute_script(f"window.scrollBy(0, -{back});")
            _human_like_delay(0.3, 0.7)
        if random.random() < 0.8:
            ratio = random.uniform(0.7, 1.0)
            step = int(driver.execute_script(f"return window.innerHeight * {ratio};"))
            driver.execute_script(f"window.scrollBy(0, {step});")
        else:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        _human_like_delay(min_pause, max_pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            no_change_count += 1
            if no_change_count >= no_change_stop:
                # 连续多次高度不变，认为已加载完所有结果
                break
            jitter = random.randint(100, 350)
            driver.execute_script(f"window.scrollBy(0, {jitter});")
            _human_like_delay(0.4, 0.8)
        else:
            no_change_count = 0
        last_height = new_height
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    _human_like_delay(0.6, 1.2)


# ----- GPT-recommended: Selenium element extraction (avoids base64 / lazy-loaded img) -----
PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
BY_BRAND_RE = re.compile(r"\bby\s+([^,–\-]+)", re.IGNORECASE)


def _dismiss_consent_if_any(driver):
    """Dismiss Google consent / cookie banner if present."""
    selectors = [
        "button#L2AGLb",
        "button[aria-label='Accept all']",
        "div[role='none'] button",
    ]
    for sel in selectors:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            btn.click()
            time.sleep(1)
            return True
        except Exception:
            pass
    return False


def _try_click_more(driver):
    """Click 'More results' / 'Show more' if visible."""
    selectors = [
        "input[value*='More']",
        "button[jsname='b3VHJd']",
        "span.VfPpkd-vQzf8d",
        "div[role='button']",
    ]
    for sel in selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed() and btn.is_enabled():
                btn.click()
                time.sleep(random.uniform(0.8, 1.5))
                return True
        except Exception:
            pass
    return False


def _scroll_page_to_load_all_items(driver, item_selector="div.vEWxFf.RCxtQc.my5z3d", max_rounds=40, stable_rounds=4):
    """
    Scroll and click 'More' until item count stabilizes.
    Returns final number of items found.
    """
    last_count = 0
    stable = 0
    for _ in range(max_rounds):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(1.0, 1.8))
        _try_click_more(driver)
        try:
            items = driver.find_elements(By.CSS_SELECTOR, item_selector)
            cur = len(items)
        except Exception:
            cur = 0
        if cur <= last_count:
            stable += 1
        else:
            stable = 0
            last_count = cur
        if stable >= stable_rounds:
            break
    return last_count


def _pick_best_srcset(srcset: str | None) -> str | None:
    if not srcset:
        return None
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    if not parts:
        return None
    return parts[-1].split()[0].strip().strip("'\"")


def _clean_url_attr(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().strip("'\"")
    return u if u else None


def _is_tbn(url: str | None) -> bool:
    """True if URL is Google encrypted-tbn thumbnail (优先用非 tbn 的真实图)."""
    if not url or not isinstance(url, str):
        return False
    return "encrypted-tbn" in url.lower()


def extract_best_image_from_panel(driver) -> str | None:
    """
    从右侧 panel / 弹层里拿「真实大图」URL（非 base64、非 tbn）。
    优先 srcset 最后一项（通常最大），否则 src；用 Selenium get_attribute 读运行时 DOM。
    """
    selectors = [
        "div#rhs img",
        "div[data-hveid] img",
        "div[jsname] img",
        "div[role='dialog'] img",
        "[role='dialog'] img",
        "div.qR5FEd img",
        "div.rnk6Jf img",
        "div.GwdVUe img",
        "div.cR2Mef img",
    ]
    seen_urls = set()
    for sel in selectors:
        try:
            imgs = driver.find_elements(By.CSS_SELECTOR, sel)
            for img in imgs:
                if not img.is_displayed():
                    continue
                for attr in ("srcset", "src"):
                    val = img.get_attribute(attr)
                    if not val:
                        continue
                    if attr == "srcset":
                        parts = [x.strip() for x in val.split(",") if x.strip()]
                        if not parts:
                            continue
                        last = parts[-1].strip().split()
                        url = (last[0] if last else "").strip().strip("'\"")
                    else:
                        url = val.strip().strip("'\"")
                    url = _clean_url_attr(url)
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    if url.lower().startswith("data:image"):
                        continue
                    if "encrypted-tbn" in url.lower():
                        continue
                    return url
                cur = driver.execute_script(
                    "return arguments[0].currentSrc || arguments[0].src || '';", img
                )
                cur = _clean_url_attr(cur)
                if cur and cur not in seen_urls:
                    seen_urls.add(cur)
                    if not cur.lower().startswith("data:image") and "encrypted-tbn" not in cur.lower():
                        return cur
        except Exception:
            continue
    return None


def _extract_prices_from_text(text_or_html: str):
    prices = PRICE_RE.findall(text_or_html or "")
    prices = [p.replace("$ ", "$") for p in prices]
    seen, out = set(), []
    for p in prices:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _price_to_num(p: str) -> float:
    return float(p.replace("$", "").replace(",", ""))


def choose_image_url_from_item(driver, item_el, base_url: str | None = None) -> str | None:
    """
    Robust image url extraction for Google Lens cards.
    Priority:
      1) img.currentSrc (runtime, best for lazy-loaded images)
      2) data-iurl / data-src / data-original / srcset / src
      3) background-image url(...) on divs inside card
      4) skip base64 placeholders
    """
    def _abs(u: str | None) -> str | None:
        u = _clean_url_attr(u)
        if not u:
            return None
        if base_url:
            u = urljoin(base_url, u)
        return u

    candidates = []
    try:
        imgs = item_el.find_elements(By.CSS_SELECTOR, "img")
    except Exception:
        imgs = []

    for img in imgs[:3]:
        try:
            cur = driver.execute_script(
                "return arguments[0].currentSrc || arguments[0].src || '';", img
            )
            cur = _abs(cur)
            if cur:
                candidates.append(cur)
            for k in ["data-iurl", "data-src", "data-original", "data-lazy", "data-zoom-image"]:
                v = _abs(img.get_attribute(k))
                if v:
                    candidates.append(v)
            for k in ["srcset", "data-srcset"]:
                best = _pick_best_srcset(img.get_attribute(k))
                best = _abs(best)
                if best:
                    candidates.append(best)
            v = _abs(img.get_attribute("src"))
            if v:
                candidates.append(v)
        except Exception:
            continue

    try:
        bg = driver.execute_script("""
            const root = arguments[0];
            const nodes = root.querySelectorAll('*');
            for (const n of nodes) {
              const st = window.getComputedStyle(n);
              const bg = st && st.backgroundImage;
              if (bg && bg.startsWith('url(')) return bg;
            }
            return '';
        """, item_el)
        if bg and "url(" in bg:
            m = re.search(r'url\(["\']?(.*?)["\']?\)', bg)
            if m:
                candidates.append(_abs(m.group(1)))
    except Exception:
        pass

    for u in candidates:
        if u and not u.startswith("data:image"):
            return u
    return None


def _brand_from_domain(url: str | None) -> str | None:
    """Infer brand from product URL domain (e.g. macys.com -> Macy's)."""
    if not url:
        return None
    try:
        host = urlparse(url).netloc or ""
        host = host.lower().replace("www.", "")
        if not host:
            return None
        domain = host.split(".")[0]
        known = {
            "macys": "Macy's", "nordstrom": "Nordstrom", "amazon": "Amazon.com",
            "walmart": "Walmart", "belk": "Belk", "anthropologie": "Anthropologie",
            "bloomingdales": "Bloomingdale's", "madewell": "Madewell", "guess": "Guess",
            "nordstromrack": "Nordstrom Rack", "joesjeans": "Joe's Jeans",
        }
        return known.get(domain) or domain.title()
    except Exception:
        return None


PANEL_PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")


def _panel_price_to_num(p: str) -> float:
    try:
        return float(p.replace("$", "").replace(",", "").replace(" ", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _extract_from_right_panel(driver):
    """
    After clicking a Lens card, Google often shows a right-side panel.
    Extract: price (all $xx → sale=min, original=max), image (srcset best then currentSrc/src), rating, reviews.
    """
    panel = None
    for sel in [
        "div[role='dialog']",
        "[role='dialog']",
        "div.qR5FEd",
        "div.rnk6Jf",
        "div[data-ved][aria-modal]",
        "div.GwdVUe",
        "div.cR2Mef",
    ]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed():
                    panel = el
                    break
            if panel:
                break
        except Exception:
            pass
    if not panel:
        try:
            for d in driver.find_elements(By.CSS_SELECTOR, "div[role='dialog'], [aria-modal='true']"):
                if d.is_displayed() and ("$" in (d.text or "") or "reviews" in (d.text or "").lower()):
                    panel = d
                    break
        except Exception:
            pass
    if not panel:
        return {}

    txt = panel.text or ""
    prices_raw = PANEL_PRICE_RE.findall(txt)
    prices_raw = list(dict.fromkeys([p.replace("$ ", "$").strip() for p in prices_raw]))
    prices = []
    for p in prices_raw:
        v = _panel_price_to_num(p)
        if 1 <= v <= 50000:
            prices.append(v)
    prices = sorted(set(prices))
    panel_prices = []
    if len(prices) == 1:
        panel_prices = [f"${prices[0]:.2f}" if prices[0] != int(prices[0]) else f"${int(prices[0])}"]
    elif len(prices) >= 2:
        panel_prices = [f"${prices[0]:.2f}" if prices[0] != int(prices[0]) else f"${int(prices[0])}", f"${prices[-1]:.2f}" if prices[-1] != int(prices[-1]) else f"${int(prices[-1])}"]

    # 优先：panel/弹层内非 tbn 大图（extract_best_image_from_panel）；其次：panel 内 img（可含 tbn）
    img_url = extract_best_image_from_panel(driver)
    if not img_url:
        try:
            imgs = panel.find_elements(By.CSS_SELECTOR, "img")
            for img in imgs[:4]:
                try:
                    srcset = img.get_attribute("srcset")
                    if srcset:
                        parts = [x.strip() for x in srcset.split(",") if x.strip()]
                        if parts:
                            best = parts[-1].split()[0].strip().strip("'\"")
                            best = _clean_url_attr(best)
                            if best and not best.startswith("data:image"):
                                img_url = best
                                break
                    if not img_url:
                        cur = driver.execute_script(
                            "return arguments[0].currentSrc || arguments[0].src || '';", img
                        )
                        cur = _clean_url_attr(cur)
                        if cur and not cur.startswith("data:image"):
                            img_url = cur
                            break
                except Exception:
                    continue
        except Exception:
            pass

    rating = None
    m = re.search(r"(\d\.\d)\s*(?:out of 5|\/5)?", txt)
    if m:
        try:
            rating = float(m.group(1))
        except (ValueError, TypeError):
            pass

    reviews = None
    m = re.search(r"([\d,]+)\s+reviews", txt, re.I)
    if m:
        try:
            reviews = int(m.group(1).replace(",", ""))
        except (ValueError, TypeError):
            pass

    return {
        "panel_prices": panel_prices,
        "panel_image_url": img_url,
        "panel_rating": rating,
        "panel_reviews": reviews,
    }


def parse_google_lens_item(driver, item_el, base_url: str | None = None) -> dict:
    """
    Extract product fields from one Google Lens / Image search item element.
    Uses element attributes and .text so we get runtime values (not only page_source).
    Tries multiple selectors for robustness.
    """
    aria = None
    for sel in ["div.T3Fozb[aria-label]", "[aria-label]", "div[role='listitem']"]:
        try:
            lab = item_el.find_element(By.CSS_SELECTOR, sel)
            val = lab.get_attribute("aria-label")
            if val and len(val) > 2:
                aria = val
                break
        except Exception:
            pass
    product_name = None
    brand = None
    if aria:
        product_name = aria.split(" – ")[0].split(",")[0].strip() or None
        m = BY_BRAND_RE.search(aria)
        if m:
            brand = m.group(1).strip()
        elif " – " in aria:
            after = aria.split(" – ", 1)[1]
            brand = after.split(",")[0].strip() or None
    product_url = None
    for sel in ["a.LBcIee", "a[href^='http']:not([href*='google'])"]:
        try:
            links = item_el.find_elements(By.CSS_SELECTOR, sel)
            for a in links:
                href = _clean_url_attr(a.get_attribute("href"))
                if href and "google." not in href and "gstatic" not in href and len(href) > 10:
                    product_url = href
                    break
            if product_url:
                break
        except Exception:
            pass
    if not brand and product_url:
        brand = _brand_from_domain(product_url)
    image_url = choose_image_url_from_item(driver, item_el, base_url=base_url)
    txt = (item_el.text or "") + "\n" + (aria or "")
    prices = _extract_prices_from_text(txt)
    if not product_name and txt:
        first_line = txt.strip().split("\n")[0].strip()
        if first_line and len(first_line) > 2 and not first_line.startswith("$"):
            product_name = first_line[:200] if len(first_line) > 200 else first_line
    sale_prices_list = []
    original_prices_list = []
    if len(prices) == 1:
        sale_prices_list = prices
    elif len(prices) >= 2:
        sp = sorted(prices, key=_price_to_num)
        sale_prices_list = [sp[0]]
        if sp[-1] != sp[0]:
            original_prices_list = [sp[-1]]
    rating = None
    for sel in ["span.yi40Hd", ".yi40Hd", "[class*='rating']"]:
        try:
            r = item_el.find_element(By.CSS_SELECTOR, sel)
            t = (r.text or "").strip()
            if t:
                v = float(t)
                if 0 <= v <= 10:
                    rating = v
                    break
        except Exception:
            pass
    number_of_reviews = None
    for sel in ["span.RDApEe", ".RDApEe", "[class*='review']"]:
        try:
            rv = item_el.find_element(By.CSS_SELECTOR, sel)
            m = re.search(r"[\d,]+", rv.text or "")
            if m:
                number_of_reviews = int(m.group(0).replace(",", ""))
                break
        except Exception:
            pass
    return {
        "product_name": product_name,
        "product_url": product_url,
        "image_url": image_url,
        "original_prices": original_prices_list,
        "sale_prices": sale_prices_list,
        "brand": brand,
        "rating": rating,
        "number_of_reviews": number_of_reviews,
    }


def _lens_item_to_canonical(item: dict) -> dict:
    """Convert parse_google_lens_item output to canonical single-value format."""
    sale = None
    if item.get("sale_prices") and len(item["sale_prices"]) > 0:
        try:
            sale = int(_price_to_num(item["sale_prices"][0]))
        except (ValueError, TypeError):
            pass
    orig = None
    if item.get("original_prices") and len(item["original_prices"]) > 0:
        try:
            orig = int(_price_to_num(item["original_prices"][0]))
        except (ValueError, TypeError):
            pass
    return {
        "product_name": item.get("product_name"),
        "product_url": item.get("product_url"),
        "image_url": item.get("image_url"),
        "original_prices": orig,
        "sale_prices": sale,
        "brand": item.get("brand"),
        "rating": item.get("rating"),
        "number_of_reviews": item.get("number_of_reviews"),
    }


# ----- Google-only Top-7 pipeline: 强约束 + 极简 fallback -----

# 美国市场：只接受 USD，排除 ¥/￥/JPY/CNY
USD_CURRENCY_CODES_TOP7 = ("USD", "US")
USD_PRICE_MAX_HEURISTIC_TOP7 = 5000


def _is_usd_price_top7(p: dict) -> bool:
    """仅当价格为 USD（或未标明且数值在合理美元范围）时返回 True。"""
    curr = p.get("price_currency")
    if curr is not None and isinstance(curr, str):
        if curr.strip().upper() in USD_CURRENCY_CODES_TOP7:
            return True
        return False
    price = p.get("sale_prices")
    if price is None:
        return True
    try:
        v = float(price)
        if v > USD_PRICE_MAX_HEURISTIC_TOP7:
            return False
    except (TypeError, ValueError):
        pass
    return True


# 品牌/域名可信度（与 enrich_top7 一致，用于 Top-7 排序）
DOMAIN_PRIORITY_TOP7 = [
    "amazon.com", "walmart.com", "target.com", "nordstrom.com", "macys.com",
    "bloomingdales.com", "nordstromrack.com", "kohls.com", "costco.com",
    "shopbop.com", "revolve.com", "zappos.com", "ebay.com", "gap.com",
    "oldnavy.gap.com", "ae.com", "levi.com", "madewell.com", "guess.com",
    "belk.com", "anthropologie.com",
]
URL_PENALTY_PATHS_TOP7 = ("/collection", "/collections/", "/search", "/fit-guide", "/fit-guide/", "/guide", "/c/")


def _domain_from_url(url: str) -> str | None:
    if not url:
        return None
    host = (urlparse(url).netloc or "").lower().replace("www.", "")
    return host or None


def _domain_rank_top7(domain: str) -> int:
    """越小越优先。"""
    if not domain:
        return 10_000
    for i, d in enumerate(DOMAIN_PRIORITY_TOP7):
        if domain == d or domain.endswith("." + d):
            return i
    return 9999


def product_trust_score(p: dict) -> tuple:
    """
    品牌/可信度打分，用于 Top-7 排序。返回 sort key（越小越优）：
    (domain_rank, -completeness_bonus, -has_rating, is_detail_url, url_stable).
    """
    url = p.get("product_url") or ""
    domain = _domain_from_url(url)
    rank = _domain_rank_top7(domain)
    completeness = 0
    if p.get("image_url"):
        completeness += 10
    if p.get("sale_prices") is not None:
        completeness += 10
    if p.get("rating") is not None:
        completeness += 5
    if p.get("number_of_reviews") is not None:
        completeness += 3
    if p.get("brand"):
        completeness += 2
    detail_ok = 1
    if url:
        ul = url.lower()
        for bad in URL_PENALTY_PATHS_TOP7:
            if bad in ul:
                detail_ok = 0
                break
    return (rank, -completeness, 0 if p.get("rating") is not None else 1, -detail_ok, url)


def is_complete_product(p: dict) -> bool:
    """
    Top-7 硬约束：product_url、image_url（非 base64/非 tbn）、sale_prices（至少 1 个）必须有。
    image_url 为 tbn 或 None 时视为不完整，触发 fallback 抓 og:image。
    """
    if not p.get("product_url"):
        return False
    img = p.get("image_url")
    if not img or not isinstance(img, str) or img.strip() == "":
        return False
    if img.strip().lower().startswith("data:image"):
        return False
    if _is_tbn(img):
        return False
    sp = p.get("sale_prices")
    if isinstance(sp, list):
        return len(sp) > 0
    return sp is not None


def fetch_product_page_minimal(url: str, timeout: int = 10) -> dict:
    """
    极简 fallback：只抓 JSON-LD Product + og:image + price，绝不重爬整页。
    返回可合并的 dict：sale_prices（int 或 list）、image_url、rating、number_of_reviews 等。
    """
    out = {}
    if not _HAS_REQUESTS_BS4:
        return out
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception:
        return out
    if r.status_code >= 400 or not r.text:
        return out
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return out
    def _parse_product_node(node):
        if not isinstance(node, dict):
            return
        offers = node.get("offers")
        if isinstance(offers, dict):
            curr = offers.get("priceCurrency")
            if curr is not None and str(curr).strip().upper() not in USD_CURRENCY_CODES_TOP7:
                return
            price = offers.get("price")
            if price is not None and out.get("sale_prices") is None:
                try:
                    v = float(str(price).replace(",", "").strip())
                    out["sale_prices"] = int(v) if v == int(v) else v
                    if curr is not None:
                        out["price_currency"] = str(curr).strip().upper()
                except (ValueError, TypeError):
                    pass
        elif isinstance(offers, list) and offers:
            o = offers[0] if isinstance(offers[0], dict) else None
            if o and out.get("sale_prices") is None:
                curr = o.get("priceCurrency")
                if curr is not None and str(curr).strip().upper() not in USD_CURRENCY_CODES_TOP7:
                    return
                price = o.get("price")
                if price is not None:
                    try:
                        v = float(str(price).replace(",", "").strip())
                        out["sale_prices"] = int(v) if v == int(v) else v
                        if curr is not None:
                            out["price_currency"] = str(curr).strip().upper()
                    except (ValueError, TypeError):
                        pass
        ar = node.get("aggregateRating")
        if isinstance(ar, dict):
            if out.get("rating") is None and ar.get("ratingValue") is not None:
                try:
                    out["rating"] = float(ar["ratingValue"])
                except (ValueError, TypeError):
                    pass
            if out.get("number_of_reviews") is None and ar.get("reviewCount") is not None:
                try:
                    out["number_of_reviews"] = int(str(ar["reviewCount"]).replace(",", ""))
                except (ValueError, TypeError):
                    pass

    for s in soup.select('script[type="application/ld+json"]'):
        try:
            raw = s.string or getattr(s, "text", None) or ""
            data = json.loads(raw) if raw else {}
        except Exception:
            continue
        if isinstance(data, dict):
            if data.get("@type") == "Product":
                _parse_product_node(data)
            if isinstance(data.get("@graph"), list):
                for node in data["@graph"]:
                    if isinstance(node, dict) and node.get("@type") == "Product":
                        _parse_product_node(node)
        if out.get("sale_prices") is not None:
            break
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        out["image_url"] = (og["content"] or "").strip()
    return out


def google_enrich_top7(driver, lens_items, max_candidates=40, top_k=7):
    """
    Google-only → 强约束 Top-7 → 自动兜底。
    lens_items: Selenium elements (e.g. div.vEWxFf.RCxtQc.my5z3d).
    Step A: 扩大候选，点卡片抽右侧 panel 合并；
    Step B: 强约束筛选（product_url + image_url + sale_prices）；
    Step C: 不足 top_k 时只对缺字段的 1–3 条做极简 fallback 抓取。
    Returns: guaranteed-complete Top-7 products (or fewer + warning).
    """
    enriched = []
    base_url = driver.current_url
    for it in lens_items[:max_candidates]:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", it)
            time.sleep(random.uniform(0.4, 0.8))
            prod = parse_google_lens_item(driver, it, base_url=base_url)
            if not prod.get("product_url"):
                continue
            try:
                it.click()
                time.sleep(random.uniform(0.6, 1.2))
                panel_info = _extract_from_right_panel(driver)
                if panel_info.get("panel_image_url"):
                    cur_img = prod.get("image_url")
                    panel_img = panel_info["panel_image_url"]
                    if not cur_img or _is_tbn(cur_img) or not _is_tbn(panel_img):
                        prod["image_url"] = panel_img
                if panel_info.get("panel_prices"):
                    nums = sorted([_price_to_num(p) for p in panel_info["panel_prices"]])
                    if nums:
                        prod["sale_prices"] = (
                            [f"${int(nums[0])}" if nums[0] == int(nums[0]) else f"${nums[0]:.2f}"]
                        )
                        if len(nums) >= 2 and nums[-1] > nums[0]:
                            prod["original_prices"] = (
                                [f"${int(nums[-1])}" if nums[-1] == int(nums[-1]) else f"${nums[-1]:.2f}"]
                            )
                if prod.get("rating") is None and panel_info.get("panel_rating") is not None:
                    prod["rating"] = panel_info["panel_rating"]
                if prod.get("number_of_reviews") is None and panel_info.get("panel_reviews") is not None:
                    prod["number_of_reviews"] = panel_info["panel_reviews"]
            except Exception:
                pass
            enriched.append(_lens_item_to_canonical(prod))
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(random.uniform(0.2, 0.4))
            except Exception:
                pass
        except (StaleElementReferenceException, Exception):
            continue
    uniq = {}
    for p in enriched:
        u = p.get("product_url")
        if u and u not in uniq:
            uniq[u] = p
    enriched = list(uniq.values())
    complete = [p for p in enriched if is_complete_product(p) and _is_usd_price_top7(p)]
    complete.sort(key=product_trust_score)
    if len(complete) >= top_k:
        return complete[:top_k]
    result_urls = {p.get("product_url") for p in complete}
    need = [
        p for p in enriched
        if p.get("product_url") and p.get("product_url") not in result_urls
    ]
    need = sorted(need, key=lambda p: (0 if (not p.get("image_url") or _is_tbn(p.get("image_url"))) else 1))[: max(top_k - len(complete) + 2, 1)]
    for p in need:
        try:
            fetched = fetch_product_page_minimal(p["product_url"])
            for k in ["original_prices", "rating", "number_of_reviews"]:
                if (p.get(k) is None or (isinstance(p.get(k), (list, str)) and len(str(p.get(k)).strip()) == 0)) and fetched.get(k) is not None:
                    p[k] = fetched[k]
            if fetched.get("price_currency") is not None:
                p["price_currency"] = fetched["price_currency"]
            if fetched.get("sale_prices") is not None and _is_usd_price_top7({"price_currency": fetched.get("price_currency"), "sale_prices": fetched.get("sale_prices")}):
                p["sale_prices"] = fetched["sale_prices"]
            if fetched.get("image_url") and (not p.get("image_url") or _is_tbn(p.get("image_url"))):
                p["image_url"] = fetched["image_url"]
            if is_complete_product(p) and p.get("product_url") not in result_urls:
                complete.append(p)
                result_urls.add(p.get("product_url"))
        except Exception:
            continue
        if len(complete) >= top_k:
            break
    complete = [p for p in complete if _is_usd_price_top7(p)]
    complete.sort(key=product_trust_score)
    if len(complete) < top_k:
        import warnings
        warnings.warn(
            f"google_enrich_top7: only {len(complete)} complete products (target {top_k}).",
            UserWarning,
            stacklevel=2,
        )
    return complete[:top_k]


def run_top7_agent(
    image_path: str,
    out_json_path: str | None = None,
    max_candidates: int = 40,
    top_k: int = 7,
) -> list[dict] | None:
    """
    Agent 入口：输入图片路径 → 跑 Google Lens + Top-k 强约束 pipeline → 返回/保存 JSON。
    使用品牌/可信度打分排序后输出最多 top_k 条完整品。
    Args:
        image_path: 本地图片路径
        out_json_path: 可选，结果保存路径（默认与脚本同目录 top_result.json）
        max_candidates: 最多参与排序的 Lens 卡片数（默认 40）
        top_k: 返回最多几条商品（默认 7）
    Returns:
        Top-k 产品列表（canonical 格式），失败返回 None。
    """
    if not os.path.isfile(image_path):
        print(f"Error: File not found at {image_path}")
        return None
    _dir = os.path.dirname(os.path.abspath(__file__))
    save_path = out_json_path if out_json_path else os.path.join(_dir, "top_result.json")
    result = img_search_from_local_file_selenium(
        image_path,
        out_json_path=save_path,
        max_items=max_candidates,
        return_top7=True,
        top_k=top_k,
    )
    if result is not None:
        print(f"Top-{len(result)} saved -> {save_path}")
    return result


def product_information_extraction_v3(data):
    """'
    from HTML content, extract product name, price, image, brand and URL information.
    Args:
        data: HTML content string
    Returns:
        list: list of product information
    """
    # step 1: extract price and product URL
    price_url_matches = re.findall(
        r'class="EwVMFc">\s*(?:US)?\$?(\d+)[^\d<]*</span></span></div></div></div><div class="kb0PBd cvP2Ce".*?href="(https?://[^\s]+?)"', 
        data
    )
    
    # import IPython;IPython.embed()
    
    # step 2: save price and URL to dictionary
    product_data = {}
    for price, url in price_url_matches:
        try:
            product_data[url] = {
                "Product_price": int(price.strip()),
                "Product_url": url.strip(),
                "Product_name": None,
                "Image_url": None,
                "Brand": None
            }
        except ValueError:
            print(f"error: cannot parse price: {price}")
            continue

    # step 3: extract product name, image and brand information from URL
    for url in list(product_data.keys()):  # use list() to avoid dictionary size changing during iteration
        # convert '=' to Unicode escape sequence
        escaped_url = url.replace('=', '\\u003d')
        
        # use escaped URL for matching
        name_match = re.search(
            rf'"2003":\[null,"[A-Za-z0-9-_]+","{escaped_url}","([^"]+)"', 
            data
        )
        # print(name_match)
        if name_match:
            product_data[url]["Product_name"] = name_match.group(1)
        else:
            del product_data[url]
            continue

        # extract product brand
        brand_match = re.search(
            rf'{{"2003":\[null,"[A-Za-z0-9]+","{escaped_url}",".*?",null,null,null,null,null,null,null,null,"(.*?)"', 
            data
        )
        if brand_match:
            product_data[url]["Brand"] = brand_match.group(1)

        # extract image URL
        image_match = re.search(
            rf'\["(https?://(?!encrypted-tbn)[^\s]+?\.(?:jpg|jpeg|png|gif))",\d+,\d+\].*?"{escaped_url}"',
            data
        )
        if image_match:
            product_data[url]["Image_url"] = image_match.group(1)
    
    product_list = [product_info for product_info in product_data.values() if product_info["Product_name"]]

    try:
        with open('product_info.json', 'w', encoding='utf-8') as f:
            json.dump(product_list, f, ensure_ascii=False, indent=4)
        print(f"save product information to product_info.json")
    except Exception as e:
        print(f"error: {e}")

    # convert to list format and return
    return product_list


def _normalize_html(html: str) -> str:
    """Decode escaped HTML so we can match both raw and \\x3c/\\x22 form."""
    if not html:
        return ""
    s = html
    for a, b in [("\\x3c", "<"), ("\\x3e", ">"), ("\\x22", '"'), ("\\x27", "'")]:
        s = s.replace(a, b)
    return s


def _find_url_position(data: str, url: str) -> int:
    """Return character position of url in data, or -1. Tries raw URL and JSON-escaped form."""
    pos = data.find(url)
    if pos != -1:
        return pos
    escaped = url.replace("/", "\\u002f").replace("=", "\\u003d").replace("&", "\\u0026")
    pos = data.find(escaped)
    if pos != -1:
        return pos
    return -1


def _extract_chunk_around(data: str, substring: str, size: int = 2000) -> str:
    """Return a chunk of data centered around the first occurrence of substring."""
    pos = _find_url_position(data, substring)
    if pos == -1:
        return ""
    start = max(0, pos - size)
    end = min(len(data), pos + len(substring) + size)
    return data[start:end]


def _build_url_positions(data: str, urls: list) -> list:
    """
    Return list of (position, url) for each url's first occurrence in data, sorted by position.
    Ensures we only associate content with the correct product by document order.
    """
    positions = []
    for url in urls:
        pos = _find_url_position(data, url)
        if pos != -1:
            positions.append((pos, url))
    positions.sort(key=lambda x: x[0])
    return positions


def _segment_for_url(data: str, url: str, url_positions: list) -> tuple:
    """
    Return (start, end) character range in data that belongs to this product URL.
    Segment is from previous product URL (or 0) to next product URL (or len(data)),
    so image/price in this range are for the same product.
    Returns (0, 0) if url not in url_positions.
    """
    idx = next((i for i, (_, u) in enumerate(url_positions) if u == url), None)
    if idx is None:
        return (0, 0)
    start = url_positions[idx - 1][0] if idx > 0 else 0
    end = url_positions[idx + 1][0] if idx + 1 < len(url_positions) else len(data)
    return (start, end)


def _best_image_in_segment(data: str, seg_start: int, seg_end: int, url_pos: int, img_matches: list) -> str | None:
    """
    Prefer image that appears *before* this product URL (same card) over image after (next card).
    Returns best image URL in segment, or None.
    """
    best_before = None
    best_before_dist = float("inf")
    best_after = None
    best_after_dist = float("inf")
    for m in img_matches:
        if m.start() < seg_start or m.start() >= seg_end:
            continue
        img_url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
        if "encrypted-tbn" in img_url or len(img_url) < 20 or "gstatic.com" in img_url:
            continue
        dist = abs(m.start() - url_pos)
        if m.start() <= url_pos and dist < best_before_dist:
            best_before_dist = dist
            best_before = img_url
        elif m.start() > url_pos and dist < best_after_dist:
            best_after_dist = dist
            best_after = img_url
    return best_before if best_before else best_after


def _clean_encrypted_tbn_url(url: str) -> str | None:
    """Return URL if valid product-thumbnail encrypted-tbn; None for favicons or malformed."""
    if not url or "gstatic.com" not in url or len(url) < 30:
        return None
    if "favicon" in url.lower():
        return None
    for stop in ("&quot;", "&amp;", '",', "',", '" ', "' "):
        if stop in url:
            url = url.split(stop)[0].strip()
    return url if len(url) >= 30 else None


def _first_encrypted_tbn_in_segment(segment: str) -> str | None:
    """Return first encrypted-tbn (Google thumbnail) URL in segment; usable when no direct image URL found."""
    m = re.search(
        r'(https?://encrypted-tbn[^"\'<>\s]+?)(?:["\'\s]|&quot;|&amp;|$)',
        segment,
        re.IGNORECASE,
    )
    if not m:
        return None
    url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
    return _clean_encrypted_tbn_url(url)


def _closest_encrypted_tbn_to_pos(data: str, center_pos: int, window: int = 4000) -> str | None:
    """Return encrypted-tbn URL in data near center_pos (within window chars) closest to center_pos."""
    start = max(0, center_pos - window)
    end = min(len(data), center_pos + window)
    chunk = data[start:end]
    best_url = None
    best_dist = float("inf")
    for m in re.finditer(r'(https?://encrypted-tbn[^"\'<>\s]+?)(?:["\'\s]|&quot;|&amp;|$)', chunk, re.IGNORECASE):
        url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
        url = _clean_encrypted_tbn_url(url)
        if not url:
            continue
        pos = start + m.start()
        dist = abs(pos - center_pos)
        if dist < best_dist:
            best_dist = dist
            best_url = url
    return best_url


def _all_encrypted_tbn_with_positions(data: str) -> list[tuple[int, str]]:
    """Return list of (position, url) for all encrypted-tbn URLs in data, sorted by position."""
    out = []
    for m in re.finditer(r'(https?://encrypted-tbn[^"\'<>\s]+?)(?:["\'\s]|&quot;|&amp;|$)', data, re.IGNORECASE):
        url = m.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
        url = _clean_encrypted_tbn_url(url)
        if url:
            out.append((m.start(), url))
    return out


def _make_product(url: str, **kwargs) -> dict:
    """One product dict with all standard keys; kwargs override."""
    p = {
        "product_name": None,
        "product_url": url,
        "image_url": None,
        "original_prices": None,
        "sale_prices": None,
        "brand": None,
        "rating": None,
        "number_of_reviews": None,
    }
    p.update({k: v for k, v in kwargs.items() if k in p or k in ("product_name", "product_url", "image_url", "original_prices", "sale_prices", "brand", "rating", "number_of_reviews")})
    return p


def _normalize_image_url_for_compare(url: str) -> str:
    """Normalize image URL for comparison (strip, lowercase, optional query strip)."""
    if not url:
        return ""
    u = url.strip().lower()
    # Remove common query params so we can match with or without ?v=...
    if "?" in u:
        u = u.split("?")[0]
    return u


def _is_excluded_image(img_url: str, exclude_norm: set) -> bool:
    """True if img_url should not be used as a product image (e.g. query image, Lens context)."""
    if not img_url:
        return False
    if exclude_norm and _normalize_image_url_for_compare(img_url) in exclude_norm:
        return True
    # Google Lens / search context URLs are not product thumbnails
    if "lens.usercontent.google.com" in img_url or "google.com/image?" in img_url:
        return True
    return False


def parse_google_image_search_products(html: str, query_image_urls: list | None = None):
    """
    Parse Google Image Search results HTML and extract product info.
    Uses multiple strategies to be robust to different Google HTML variations.
    Returns list of dicts with: product_name, product_url, image_url, original_prices,
    sale_prices, brand, rating, number_of_reviews. Missing fields are None.

    query_image_urls: optional list of URLs to never assign as product image (e.g. the
    search query image). These are normalized (strip, no query) before comparison.

    Format is compatible with scripts/search_extracted_clothing.py:
    - post_process_products() expects Product_url/product_url, and at least one of
      Current_price/Product_price/Original_price (or lowercase) > 0; Image_url optional.
    - validate_product_category_match() and normalization use Product_name/product_name/name,
      Product_url/product_url, Image_url/image_url, Brand/brand/vendor.
    - Aliases (Product_name, Product_url, Image_url, Brand, vendor, current_price, etc.)
      are set so the same dict works when passed to search_clothing_products flow.
    """
    if not html or not html.strip():
        return []
    exclude_img_norm = set()
    if query_image_urls:
        for u in query_image_urls:
            if u:
                exclude_img_norm.add(_normalize_image_url_for_compare(u))
    data = html
    data_norm = _normalize_html(html)
    product_by_url = {}

    # ----- Strategy 1: Product links with aria-label (e.g. KEVENd) -----
    # <a class="KEVENd" aria-label="Product Name – site.com" href="https://...">
    for blob in [data, data_norm]:
        for m in re.finditer(
            r'<a[^>]*?(?:aria-label=["\']([^"\']+)["\'][^>]*?href=["\'](https?://[^"\']+)["\']|href=["\'](https?://[^"\']+)["\'][^>]*?aria-label=["\']([^"\']+)["\'])',
            blob,
            re.IGNORECASE | re.DOTALL,
        ):
            if m.lastindex == 2:
                label, url = m.group(1), m.group(2)
            else:
                url, label = m.group(3), m.group(4)
            url = url.split("#")[0].strip()
            if not url or "google.com" in url or "gstatic.com" in url or "googleusercontent" in url or "policies.google" in url:
                continue
            if url not in product_by_url:
                name = label.strip()
                brand_from_label = None
                if " – " in name:
                    parts = name.split(" – ", 1)
                    name = parts[0].strip()
                    brand_from_label = parts[1].strip() if len(parts) > 1 else None
                product_by_url[url] = _make_product(url, product_name=name or None, brand=brand_from_label)

    # ----- Strategy 2: EwVMFc price + product URL (Shopping-style snippet) -----
    price_url_matches = re.findall(
        r'class="EwVMFc">\s*(?:US)?\$?(\d+)[^\d<]*</span>[^<]*</span>[^<]*</div>[^<]*</div>[^<]*</div><div[^>]*class="[^"]*kb0PBd[^"]*"[^>]*>.*?href="(https?://[^\s"]+)"',
        data,
        re.DOTALL,
    )
    if not price_url_matches:
        price_url_matches = re.findall(
            r'class="EwVMFc">\s*(?:US)?\$?(\d+)[^\d<]*.*?href="(https?://[^\s"]+)"',
            data,
            re.DOTALL,
        )
    for price, url in price_url_matches:
        url = url.split("#")[0].strip()
        if not url:
            continue
        try:
            sale = int(price.strip()) if price.strip().isdigit() else None
        except (ValueError, TypeError):
            sale = None
        if url not in product_by_url:
            product_by_url[url] = _make_product(url, sale_prices=sale)
        elif product_by_url[url].get("sale_prices") is None:
            product_by_url[url]["sale_prices"] = sale

    # ----- Strategy 3: "2003" JSON block (name, brand, image per URL) -----
    for url in list(product_by_url.keys()):
        escaped_url = re.escape(url.replace("=", "\\u003d"))
        for pattern in [
            rf'"2003":\s*\[null,"[A-Za-z0-9_-]+","{re.escape(url)}","([^"]+)"',
            rf'"2003":\s*\[null,"[A-Za-z0-9_-]+","{escaped_url}","([^"]+)"',
        ]:
            name_match = re.search(pattern, data)
            if name_match:
                product_by_url[url]["product_name"] = product_by_url[url].get("product_name") or name_match.group(1)
                break
        brand_match = re.search(
            rf'{{"2003":\s*\[null,"[A-Za-z0-9]+","{re.escape(url)}",".*?",null,null,null,null,null,null,null,null,"(.*?)"',
            data,
        )
        if not brand_match:
            brand_match = re.search(
                rf'{{"2003":\s*\[null,"[A-Za-z0-9]+","{escaped_url}",".*?",null,null,null,null,null,null,null,null,"(.*?)"',
                data,
            )
        if brand_match and not product_by_url[url].get("brand"):
            product_by_url[url]["brand"] = brand_match.group(1)
        # Image: try JSON-style [image_url, width, height] near this product URL (prefer image before URL)
        img_patterns = [
            rf'\["(https?://(?!encrypted-tbn)[^"]+?\.(?:jpg|jpeg|png|gif|webp)[^"]*)",\d+,\d+\].*?{re.escape(url)}',
            rf'\["(https?://(?!encrypted-tbn)[^"]+?\.(?:jpg|jpeg|png|gif|webp)[^"]*)",\d+,\d+\].*?{escaped_url}',
            rf'{re.escape(url)}.*?"(https?://(?!encrypted-tbn)[^"]+?\.(?:jpg|jpeg|png|gif|webp)[^"]*)"',
            # Google often embeds only encrypted-tbn thumbnail URLs
            rf'\["(https?://encrypted-tbn[^"]+)",\d+,\d+\].*?{re.escape(url)}',
            rf'\["(https?://encrypted-tbn[^"]+)",\d+,\d+\].*?{escaped_url}',
        ]
        for img_p in img_patterns:
            image_match = re.search(img_p, data)
            if image_match:
                img_url = image_match.group(1).replace("\\u003d", "=").replace("\\u0026", "&")
                if not _is_excluded_image(img_url, exclude_img_norm):
                    product_by_url[url]["image_url"] = img_url
                    break

    # ----- Strategy 3b: Per-product image only from this product's segment (correct matching) -----
    all_img_matches = list(re.finditer(
        r'(https?://(?!encrypted-tbn|www\.google)[^"\'\s]+?\.(?:jpg|jpeg|png|gif|webp)[^"\']*)',
        data,
        re.IGNORECASE,
    ))
    url_positions = _build_url_positions(data, list(product_by_url.keys()))
    for url, p in list(product_by_url.items()):
        if p.get("image_url"):
            continue
        seg_start, seg_end = _segment_for_url(data, url, url_positions)
        if seg_start >= seg_end:
            continue
        segment = data[seg_start:seg_end]
        img_in_segment = re.search(
            r'(https?://(?!encrypted-tbn)[^"\'\s]+?\.(?:jpg|jpeg|png|gif|webp)[^"\']*)',
            segment,
            re.IGNORECASE,
        )
        if img_in_segment:
            img_url = img_in_segment.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
            if len(img_url) > 25 and "gstatic.com" not in img_url and not _is_excluded_image(img_url, exclude_img_norm):
                product_by_url[url]["image_url"] = img_url
                continue
        # img src / data-src in segment (often encrypted-tbn or CDN)
        for attr in ("data-src", "src"):
            img_src = re.search(rf'<img[^>]+{re.escape(attr)}=["\'](https?://[^"\']+)["\']', segment, re.IGNORECASE)
            if img_src:
                img_url = img_src.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
                if len(img_url) > 20 and not _is_excluded_image(img_url, exclude_img_norm):
                    product_by_url[url]["image_url"] = img_url
                    break
        if product_by_url[url].get("image_url"):
            continue
        pos = _find_url_position(data, url)
        if pos == -1:
            continue
        best_img = _best_image_in_segment(data, seg_start, seg_end, pos, all_img_matches)
        if best_img and not _is_excluded_image(best_img, exclude_img_norm):
            product_by_url[url]["image_url"] = best_img

    # ----- Strategy 2b: Original/sale price from same product's segment only -----
    url_positions_2b = _build_url_positions(data, list(product_by_url.keys()))
    for url in list(product_by_url.keys()):
        if product_by_url[url].get("original_prices") is not None:
            continue
        seg_start, seg_end = _segment_for_url(data, url, url_positions_2b)
        if seg_start >= seg_end:
            chunk = _extract_chunk_around(data, url, 2000)
        else:
            chunk = data[seg_start:seg_end]
        if not chunk:
            continue
        # Explicit "was" / "list price" / "compare at" -> original only
        orig_match = re.search(r'(?:was|list\s*price|original|compare\s*at)\s*[\$]?\s*(\d{1,6})(?:\.\d{2})?', chunk, re.I)
        if orig_match:
            try:
                v = int(orig_match.group(1))
                if 5 <= v <= 50000:
                    product_by_url[url]["original_prices"] = v
            except (ValueError, TypeError):
                pass
        # Two plausible prices (both 10-5000): treat larger as original, smaller as sale
        if product_by_url[url].get("original_prices") is None:
            two_prices = re.findall(r'[\$]\s*(\d{1,6})(?:\.\d{2})?|(?:^|[\s>])(\d{2,5})\s*[\$]', chunk)
            nums = []
            for a, b in two_prices:
                n = (a or b or "").strip()
                if n.isdigit():
                    v = int(n)
                    if 10 <= v <= 5000:
                        nums.append(v)
            if len(nums) >= 2:
                larger, smaller = max(nums[0], nums[1]), min(nums[0], nums[1])
                if larger > smaller and larger - smaller >= 5:
                    product_by_url[url]["original_prices"] = larger
                    if product_by_url[url].get("sale_prices") is None:
                        product_by_url[url]["sale_prices"] = smaller
        # If still no sale_prices, try single $ price in chunk
        if product_by_url[url].get("sale_prices") is None:
            price_in_chunk = re.search(r'[\$]\s*(\d{2,6})(?:\.\d{2})?', chunk)
            if price_in_chunk:
                g = price_in_chunk.group(1)
                if g and g.isdigit():
                    v = int(g)
                    if 5 <= v <= 50000:
                        product_by_url[url]["sale_prices"] = v

    # ----- Strategy 4: Any <a href="https://..."> that looks like product (no google) -----
    for m in re.finditer(r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>', data):
        url = m.group(1).split("#")[0].strip()
        if not url or "google." in url or "gstatic." in url or "youtube.com" in url or "wikipedia.org" in url:
            continue
        if "/product" in url.lower() or "/products/" in url.lower() or "/p/" in url or "/item/" in url.lower():
            if url not in product_by_url:
                product_by_url[url] = _make_product(url)

    # ----- After Strategy 4: segment-based image for products still with null (no wrong-image fallback) -----
    url_positions = _build_url_positions(data, list(product_by_url.keys()))
    for url, p in list(product_by_url.items()):
        if p.get("image_url"):
            continue
        seg_start, seg_end = _segment_for_url(data, url, url_positions)
        if seg_start >= seg_end:
            continue
        segment = data[seg_start:seg_end]
        img_in_segment = re.search(
            r'(https?://(?!encrypted-tbn)[^"\'\s]+?\.(?:jpg|jpeg|png|gif|webp)[^"\']*)',
            segment,
            re.IGNORECASE,
        )
        if img_in_segment:
            img_url = img_in_segment.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
            if len(img_url) > 25 and "gstatic.com" not in img_url and not _is_excluded_image(img_url, exclude_img_norm):
                product_by_url[url]["image_url"] = img_url
                continue
        for attr in ("data-src", "src"):
            img_src = re.search(rf'<img[^>]+{re.escape(attr)}=["\'](https?://[^"\']+)["\']', segment, re.IGNORECASE)
            if img_src:
                img_url = img_src.group(1).replace("\\u003d", "=").replace("\\u0026", "&").strip()
                if len(img_url) > 20 and not _is_excluded_image(img_url, exclude_img_norm):
                    product_by_url[url]["image_url"] = img_url
                    break
        if product_by_url[url].get("image_url"):
            continue
        pos = _find_url_position(data, url)
        if pos == -1:
            continue
        best_img = _best_image_in_segment(data, seg_start, seg_end, pos, all_img_matches)
        if best_img and not _is_excluded_image(best_img, exclude_img_norm):
            product_by_url[url]["image_url"] = best_img

    # ----- Fallback: encrypted-tbn (Google thumbnail) when image_url still null -----
    url_positions_tbn = _build_url_positions(data, list(product_by_url.keys()))
    for url, p in list(product_by_url.items()):
        if p.get("image_url"):
            continue
        seg_start, seg_end = _segment_for_url(data, url, url_positions_tbn)
        if seg_start >= seg_end:
            chunk = _extract_chunk_around(data, url, 3500)
        else:
            chunk = data[seg_start:seg_end]
            if len(chunk) < 400:
                chunk = _extract_chunk_around(data, url, 3500)
        tbn_url = _first_encrypted_tbn_in_segment(chunk)
        if not tbn_url:
            pos = _find_url_position(data, url)
            if pos != -1:
                tbn_url = _closest_encrypted_tbn_to_pos(data, pos, 4000)
        if tbn_url and not _is_excluded_image(tbn_url, exclude_img_norm):
            product_by_url[url]["image_url"] = tbn_url

    # ----- Fallback 2: assign encrypted-tbn by document order (1:1 when possible) -----
    need_img = [(pos, url) for url in product_by_url if not product_by_url[url].get("image_url") for pos in [_find_url_position(data, url)] if pos != -1]
    need_img.sort(key=lambda x: x[0])
    all_tbns = _all_encrypted_tbn_with_positions(data)
    used_tbn_idx = set()
    for p_pos, p_url in need_img:
        if product_by_url[p_url].get("image_url"):
            continue
        best_idx = None
        best_dist = float("inf")
        for i, (t_pos, t_url) in enumerate(all_tbns):
            if i in used_tbn_idx or _is_excluded_image(t_url, exclude_img_norm):
                continue
            dist = abs(t_pos - p_pos)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx is not None:
            product_by_url[p_url]["image_url"] = all_tbns[best_idx][1]
            used_tbn_idx.add(best_idx)

    out = []
    seen = set()
    for url, p in product_by_url.items():
        if url in seen:
            continue
        seen.add(url)
        if not p.get("product_name") and not p.get("product_url"):
            continue
        p["product_url"] = url
        out.append(p)
    # Nullify image_url when the same *direct* image is assigned to multiple products (avoid wrong product image).
    # Allow duplicate encrypted-tbn URLs (Google thumbnails) so more products keep an image.
    image_count_by_norm = {}
    for p in out:
        u = p.get("image_url")
        if u:
            norm = _normalize_image_url_for_compare(u)
            image_count_by_norm[norm] = image_count_by_norm.get(norm, 0) + 1
    duplicate_norm = {norm for norm, c in image_count_by_norm.items() if c > 1}
    for p in out:
        u = p.get("image_url")
        if not u:
            continue
        norm = _normalize_image_url_for_compare(u)
        if norm not in duplicate_norm:
            continue
        # Keep encrypted-tbn even when duplicated (shared thumbnail is ok)
        if "encrypted-tbn" in u:
            continue
        p["image_url"] = None
    # Single canonical keys only (no duplicate Product_name/name, etc.)
    canonical = []
    for p in out:
        canonical.append({
            "product_name": p.get("product_name"),
            "product_url": p.get("product_url"),
            "image_url": p.get("image_url"),
            "original_prices": p.get("original_prices"),
            "sale_prices": p.get("sale_prices"),
            "brand": p.get("brand"),
            "rating": p.get("rating"),
            "number_of_reviews": p.get("number_of_reviews"),
        })
    return canonical


def img_search_from_file_selenium(image_path_url: str):
    options = Options()
    # options.add_argument('--headless')  # if don't want to see the browser
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--disable-infobars')
    options.add_experimental_option('excludeSwitches', ['enable-automation'])
    options.add_experimental_option('useAutomationExtension', False)
    
    
    driver = webdriver.Chrome(options=options)
    try:
        # visit google image search page
        driver.get('https://images.google.com/')
        time.sleep(random.uniform(1.2, 2.2))
        camera_button = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.nDcEnd"))
        )
        camera_button.click()
        time.sleep(random.uniform(0.6, 1.2))
        url_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input.cB9M7"))
        )
        url_input.clear()
        url_input.send_keys(image_path_url)
        time.sleep(0.5)
        url_input.send_keys(Keys.RETURN)
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.o5UAab"))
            )
        except Exception:
            WebDriverWait(driver, 12).until(lambda d: "search" in d.current_url.lower() or "upload" in d.current_url.lower())
            time.sleep(5)
        time.sleep(random.uniform(2, 3.5))
        _scroll_page_to_load_all(driver)
        # Extract directly from driver.page_source (no file save — saved HTML can alter image URLs)
        html = driver.page_source
        _script_dir = os.path.dirname(os.path.abspath(__file__))

        # Parser: exclude the query image URL so it is not assigned as product image
        products = parse_google_image_search_products(html, query_image_urls=[image_path_url])
        products_path = os.path.join(_script_dir, "google_search_products.json")
        try:
            with open(products_path, 'w', encoding='utf-8') as f:
                json.dump(products, f, ensure_ascii=False, indent=2)
            print("parsed products:", len(products), "->", products_path)
        except Exception as e:
            print("save products json failed:", e)
        return html

    except Exception as e:
        print(f"error: {str(e)}")
        return None
    finally:
        driver.quit()

# ========== 主改函数：本地图片搜图并保存 HTML ==========
def img_search_from_local_file_selenium(
    image_path: str,
    out_json_path: str | None = None,
    max_items: int | None = None,
    return_top7: bool = False,
    top_k: int = 7,
):
    """
    用本地图片在 Google 图片/Lens 搜索，滚动 + 点「更多结果」加载完，
    优先从 Selenium 元素直接取属性（data-iurl/data-src/src 等，避免 base64/懒加载），
    失败则回退到 page_source 解析。
    Args:
        image_path: 本地图片路径
        out_json_path: 可选，覆盖默认 JSON 保存路径
        max_items: 可选，最多处理多少张卡片（用于快速测试，None=全部）
        return_top7: 若 True，走 Google-only Top-k 强约束 pipeline（点卡片+panel+极简 fallback），直接返回最多 top_k 条完整品
        top_k: return_top7 时返回最多几条（默认 7）
    Returns:
        产品列表（canonical 格式）；return_top7 时为最多 top_k 条；失败返回 None。
    """
    if not os.path.isfile(image_path):
        print(f"Error: File not found at {image_path}")
        return None

    options = Options()
    # options.add_argument('--headless')  # if don't want to see the browser
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--disable-infobars')
    options.add_experimental_option('excludeSwitches', ['enable-automation'])
    options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=options)
    try:
        # Step 1: 打开 Google 图片首页
        driver.get("https://images.google.com/")
        time.sleep(random.uniform(1.2, 2.2))
        _dismiss_consent_if_any(driver)
        # Step 2: 点相机图标
        camera_button = WebDriverWait(driver, 12).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "div.nDcEnd"))
        )
        camera_button.click()
        time.sleep(random.uniform(0.6, 1.2))
        # Step 3: 找到文件输入框并上传本地图片
        try:
            file_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
            
            # Use absolute path to ensure it works correctly
            abs_path = os.path.abspath(image_path)
            print(f"Uploading file: {abs_path}")
            file_input.send_keys(abs_path)
            time.sleep(random.uniform(1.8, 3))
        except Exception as e:
            print(f"Failed to find file input: {e}")
            # Try to find file input using JavaScript
            try:
                driver.execute_script("""
                    var input = document.createElement('input');
                    input.type = 'file';
                    input.onchange = function() {
                        var file = this.files[0];
                        console.log('File selected: ' + file.name);
                    };
                    document.body.appendChild(input);
                    return input;
                """)
                file_input = driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                abs_path = os.path.abspath(image_path)
                file_input.send_keys(abs_path)
                print("Uploaded file using JavaScript-created input")
            except Exception as e2:
                print(f"JavaScript approach failed: {e2}")

        # Step 4: 等结果页出来
        try:
            WebDriverWait(driver, 35).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.o5UAab"))
            )
        except Exception as e:
            print(f"Warning: Could not detect results element: {e}")
            try:
                WebDriverWait(driver, 15).until(
                    lambda d: "search" in d.current_url.lower() or "upload" in d.current_url.lower()
                )
            except Exception:
                pass
            time.sleep(6)
        time.sleep(random.uniform(2, 3.5))
        # Step 5: 滚动 + 点“更多结果”直到 item 数稳定（GPT 推荐：按元素数量判断）
        item_selector = "div.vEWxFf.RCxtQc.my5z3d"
        total_items = _scroll_page_to_load_all_items(driver, item_selector=item_selector)
        if total_items == 0:
            _scroll_page_to_load_all(driver)
        # Step 6: 优先 Selenium 元素取属性；点卡片后从右侧 panel 抓 price/image/rating/reviews
        base_url = driver.current_url
        products = []
        try:
            items = driver.find_elements(By.CSS_SELECTOR, item_selector)
            if max_items is not None and max_items > 0:
                items = items[:max_items]
                print(f"Processing first {len(items)} items (max_items={max_items})")
            if return_top7:
                max_cand = min(max_items or 40, len(items)) if items else 0
                products = google_enrich_top7(driver, items, max_candidates=max_cand, top_k=top_k)
                print(f"Top-{top_k} pipeline: {len(products)} complete products")
            else:
                for it in items:
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", it
                        )
                        time.sleep(random.uniform(0.6, 1.0))
                        obj = parse_google_lens_item(driver, it, base_url=base_url)
                        try:
                            it.click()
                            time.sleep(random.uniform(0.5, 0.9))
                        except Exception:
                            pass
                        panel = _extract_from_right_panel(driver)

                        if panel.get("panel_image_url"):
                            cur_img = obj.get("image_url")
                            panel_img = panel["panel_image_url"]
                            if not cur_img or _is_tbn(cur_img) or not _is_tbn(panel_img):
                                obj["image_url"] = panel_img
                        if (not obj.get("sale_prices")) and panel.get("panel_prices"):
                            nums = sorted([_price_to_num(p) for p in panel["panel_prices"]])
                            if nums:
                                obj["sale_prices"] = (
                                    [f"${nums[0]:.0f}"]
                                    if nums[0].is_integer()
                                    else [f"${nums[0]:.2f}"]
                                )
                                if len(nums) >= 2 and nums[-1] > nums[0]:
                                    obj["original_prices"] = (
                                        [f"${nums[-1]:.0f}"]
                                        if nums[-1].is_integer()
                                        else [f"${nums[-1]:.2f}"]
                                    )
                        if obj.get("rating") is None and panel.get("panel_rating") is not None:
                            obj["rating"] = panel["panel_rating"]
                        if obj.get("number_of_reviews") is None and panel.get("panel_reviews") is not None:
                            obj["number_of_reviews"] = panel["panel_reviews"]

                        if obj.get("product_url"):
                            products.append(_lens_item_to_canonical(obj))
                        try:
                            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                            time.sleep(random.uniform(0.2, 0.4))
                        except Exception:
                            pass
                    except StaleElementReferenceException:
                        continue
            uniq = {}
            for r in products:
                k = r.get("product_url")
                if k and k not in uniq:
                    uniq[k] = r
            products = list(uniq.values())
            missing_img_count = sum(1 for r in products if not r.get("image_url"))
            if missing_img_count > 0:
                html = driver.page_source
                html_products = parse_google_image_search_products(html)
                url_to_img = {p.get("product_url"): p.get("image_url") for p in html_products if p.get("product_url") and p.get("image_url")}
                for p in products:
                    if not p.get("image_url") and p.get("product_url") and p["product_url"] in url_to_img:
                        p["image_url"] = url_to_img[p["product_url"]]
        except Exception as e:
            print("Selenium item extraction failed:", e)
            products = []
        if not products:
            html = driver.page_source
            products = parse_google_image_search_products(html)
        missing_img = sum(1 for r in products if not r.get("image_url"))
        print(f"Results: {len(products)} | missing image_url: {missing_img}")
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        products_path = out_json_path or os.path.join(_script_dir, "google_search_products.json")
        try:
            with open(products_path, "w", encoding="utf-8") as f:
                json.dump(products, f, ensure_ascii=False, indent=2)
            print("parsed products:", len(products), "->", products_path)
        except Exception as e:
            print("save products json failed:", e)
        return products
        
    except Exception as e:
        print(f"Error searching with local file: {str(e)}")
        return None
    finally:
        driver.quit()


if __name__ == "__main__":
    import sys
    _here = os.path.abspath(__file__)
    _root = os.path.dirname(os.path.dirname(os.path.dirname(_here)))
    _default_img = os.path.join(_root, "examples", "google_lens.png")
    if os.path.isfile(_default_img):
        local_image_path = _default_img
    else:
        local_image_path = r"D:\project\test.jpg"
    max_items_arg = None
    return_top7_arg = False
    args = [a for a in sys.argv[1:] if a != "--top7"]
    if "--top7" in sys.argv:
        return_top7_arg = True
    if args:
        local_image_path = args[0]
    if len(args) > 1:
        try:
            max_items_arg = int(args[1])
        except ValueError:
            pass
    print("Image:", local_image_path, flush=True)
    if max_items_arg is not None:
        print("max_items:", max_items_arg, flush=True)
    if return_top7_arg:
        print("return_top7: True (Google-only Top-7 pipeline)", flush=True)
    result = img_search_from_local_file_selenium(
        local_image_path,
        max_items=max_items_arg,
        return_top7=return_top7_arg,
    )
    if result is not None:
        print("Done: products extracted (Selenium + HTML image merge).", flush=True)
    else:
        print("Failed: no results.", flush=True)
