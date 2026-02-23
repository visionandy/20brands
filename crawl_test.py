#!/usr/bin/env python3
"""
按站点 id 爬取单个站点的 deal 列表页，验证 selectors 是否有效。
人类化行为对齐 google_search_product.py：分步滚动、随机延时、每卡 scrollIntoView、Stale 跳过。
支持并行：--all 模式下多线程，每个线程跑一个站点，交错启动避免同时请求。
用法: python crawl_test.py <site_id>
示例: python crawl_test.py guess
      python crawl_test.py --all --workers 4 --limit 2
结果写入 output/<brandname>_test.json（如 output/Guess_test.json）。
"""
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock
from urllib.parse import urljoin, urlparse


def _today_date_dir():
    """今日日期目录名，格式 2026_02_22，对齐 deal_crawler。"""
    return datetime.now().strftime("%Y_%m_%d").replace("-", "_")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

# 页面过小或 0 deal 时触发重试（无头可能被拦截）
MIN_PAGE_SIZE = 8000


class DealsEmptyError(Exception):
    """页面过小或 0 deal，需重试（可能 headless 被拦截）。"""

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

# 多币种价格提取：从 "Price reduced from\nMYR 949.00\nto" 等文案中只保留第一处「货币+金额」
# 优先匹配带货币符号的（避免 "4k+ bought" 中的 "4" 被误匹配）
_PRICE_CURRENCY = re.compile(r"(?:MYR|RM|USD|SGD|EUR|GBP|\$|€|£)\s*[\d,]+(?:\.\d+)?")
# 数字+货币后缀：如 "120.00 S$"、"156.00 SGD"、"99.00 €"
_PRICE_NUMBER_SUFFIX = re.compile(r"[\d,]+(?:\.\d+)?\s*(?:S\$|MYR|RM|USD|SGD|EUR|GBP|\$|€|£)")
_PRICE_PLAIN = re.compile(r"[\d,]+(?:\.\d+)?")


def _normalize_price_text(raw):
    """从原始价格文案中提取第一处货币+金额，兼容多站点多币种；无匹配则返回原串 strip。"""
    if not raw:
        return ""
    s = raw.strip()
    if not s:
        return s
    m = _PRICE_CURRENCY.search(s)
    if m:
        return m.group(0).strip()
    m = _PRICE_NUMBER_SUFFIX.search(s)
    if m:
        return m.group(0).strip()
    m = _PRICE_PLAIN.search(s)
    return m.group(0).strip() if m else s


def _parse_price_to_float(raw):
    """从价格字符串中提取数字，返回 float；无法解析时返回 None。区间取第一个数字。"""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    if not s:
        return None
    m = _PRICE_CURRENCY.search(s)
    if m:
        num_str = m.group(0).strip()
    else:
        m = _PRICE_NUMBER_SUFFIX.search(s)
        if m:
            num_str = m.group(0).strip()
        else:
            m = _PRICE_PLAIN.search(s)
            num_str = m.group(0).strip() if m else s
    if not num_str:
        return None
    num_str = re.sub(r"[^\d.]", "", num_str.replace(",", ""))
    if not num_str:
        return None
    try:
        return float(num_str)
    except ValueError:
        return None


# 整行仅为价格：匹配整行只有可选货币符号+数字，不匹配 "Product $99" 等混合行
_PRICE_ONLY_LINE = re.compile(r"^\s*[$€£]?\s*[\d,]+\.?\d*\s*$")


def _clean_title(raw, config=None):
    """
    清洗 title：按整行过滤，删除整行噪音，避免误删商品名中的 $ 等合法字符。
    - 删除整行短标签：如 Sale、New、Clearance、Hot
    - 删除整行仅为价格：匹配 ^\\s*[$€£]?\\s*[\\d,]+\\.?\\d*\\s*$
    - 删除整行包含 current price / original price 的
    - 若全部被删则回退原始串
    """
    if not raw or not raw.strip():
        return raw
    cfg = (config or {}).get("global", {}).get("title_clean") or {}
    labels = cfg.get("remove_whole_line_labels") or ["sale", "new", "clearance", "hot"]
    contains = cfg.get("remove_line_if_contains") or ["current price", "original price"]
    price_pattern = cfg.get("price_only_line_pattern")
    if price_pattern:
        try:
            price_re = re.compile(price_pattern)
        except re.error:
            price_re = _PRICE_ONLY_LINE
    else:
        price_re = _PRICE_ONLY_LINE

    lines = raw.split("\n")
    kept = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.lower() in labels:
            continue
        if price_re.match(s):
            continue
        if any(kw in s.lower() for kw in contains):
            continue
        kept.append(s)
    result = " ".join(kept).strip() if kept else raw.strip()
    return result


def _human_like_delay(min_sec=0.8, max_sec=2.2):
    """随机延时，模拟人类阅读/停顿。"""
    time.sleep(random.uniform(min_sec, max_sec))


def _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4):
    """
    往下分步滑动直到页面高度不再增加（懒加载触发完毕）。
    步长 = 视口高度 × random.uniform(0.7, 1.0)；15% 概率回滚 60–120px；
    连续 no_change_stop 次高度不变则结束；高度不变时再抖动 100–350px。
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
                break
            jitter = random.randint(100, 350)
            driver.execute_script(f"window.scrollBy(0, {jitter});")
            _human_like_delay(0.4, 0.8)
        else:
            no_change_count = 0
        last_height = new_height
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    _human_like_delay(0.6, 1.2)


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config", "sites_draft.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_site_by_id(config, site_id):
    for s in config["sites"]:
        if s.get("id") == site_id:
            return s
    raise ValueError(f"site_id '{site_id}' not found in config")


def get_base_url(list_url):
    parsed = urlparse(list_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def dismiss_consent_if_any(driver):
    """尝试关闭 cookie/consent 弹窗（多选择器，超时 3 秒，与 google 脚本一致）。"""
    selectors = [
        "button[data-action='accept-cookies']",
        "#onetrust-accept-btn-handler",
        "button[id*='accept']",
        "button[aria-label*='Accept']",
        "a[href*='accept']",
        ".cookie-accept",
        "[data-testid='accept-cookies']",
    ]
    for sel in selectors:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
            )
            if btn.is_displayed():
                btn.click()
                _human_like_delay(0.6, 1.2)
                return True
        except Exception:
            pass
    return False


def parse_deals_from_page(driver, site, base_url, config=None):
    """
    在当前页面用 site 的 selectors 解析所有 deal。
    每个卡片先 scrollIntoView(center) + 随机延时，再取值，以触发懒加载；Stale 时跳过。
    original_price：若配置的 selector 返回空，则依次尝试 global.original_price_fallback_selectors。
    """
    sel = site["selectors"]
    container_sel = sel.get("container")
    if not container_sel:
        return []

    containers = driver.find_elements(By.CSS_SELECTOR, container_sel)
    deals = []

    for node in containers:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", node
            )
            _human_like_delay(0.4, 0.8)
        except StaleElementReferenceException:
            continue

        deal = {"source_site": site["id"]}

        def _text(selector):
            try:
                el = node.find_element(By.CSS_SELECTOR, selector)
                return (el.text or "").strip()
            except Exception:
                return ""

        def _attr(selector, attr):
            try:
                el = node.find_element(By.CSS_SELECTOR, selector)
                return (el.get_attribute(attr) or "").strip()
            except Exception:
                return ""

        try:
            if sel.get("title"):
                raw_title = _text(sel["title"])
                if sel.get("title_attr"):
                    attr_val = _attr(sel["title"], sel["title_attr"])
                    if attr_val:
                        m = re.search(r"View Product:\s*(.+?)(?:\s*-\s|$)", attr_val)
                        raw_title = m.group(1).strip() if m else attr_val
                deal["title"] = _clean_title(raw_title or "", config)
            if sel.get("current_price"):
                raw_display = _normalize_price_text(_text(sel["current_price"]))
                deal["current_price_display"] = raw_display or ""
                deal["current_price"] = _parse_price_to_float(raw_display) if raw_display else None
            if sel.get("original_price"):
                raw = _text(sel["original_price"])
                if not raw:
                    fallbacks = (config or {}).get("global", {}).get("original_price_fallback_selectors") or [
                        "span.h-text-line-through", "del", "s"
                    ]
                    for fb_sel in fallbacks:
                        raw = _text(fb_sel)
                        if raw:
                            break
                raw_display = _normalize_price_text(raw)
                deal["original_price_display"] = raw_display or ""
                deal["original_price"] = _parse_price_to_float(raw_display) if raw_display else None
            if sel.get("deal_url"):
                href = _attr(sel["deal_url"], "href")
                deal["deal_url"] = urljoin(base_url, href) if href else ""
            if sel.get("image_url"):
                img_el = None
                try:
                    imgs = node.find_elements(By.CSS_SELECTOR, sel["image_url"])
                    img_el = imgs[0] if imgs else None
                except Exception:
                    pass
                if img_el:
                    src = (img_el.get_attribute("src") or "").strip()
                    data_src = (img_el.get_attribute("data-src") or "").strip()
                    # 若 src 为占位图（empty_loading、banner_loading、1x1）则优先用 data-src
                    if src and ("empty_loading" in src or "banner_loading" in src or "1x1" in src):
                        url = data_src
                    else:
                        url = src or data_src
                    # srcset 兜底：当 src 为 data: 或空时，从 srcset 取第一张图（如 Lululemon）
                    if (not url or url.startswith("data:")) and img_el.get_attribute("srcset"):
                        srcset = (img_el.get_attribute("srcset") or "").strip()
                        if srcset:
                            first = srcset.split(",")[0].strip().split()[0]
                            if first and first.startswith("http"):
                                url = first
                    # 过滤掉懒加载占位图
                    if url and not url.startswith("data:") and "1x1" not in url and "empty_loading" not in url and "banner_loading" not in url:
                        deal["image_url"] = url
                    else:
                        deal["image_url"] = ""
                else:
                    deal["image_url"] = ""

            if sel.get("deal_percent"):
                raw = _text(sel["deal_percent"])
                # 移除换行、Discount 等多余文案，保留纯折扣如 -30%
                deal["deal_percent"] = raw.replace("\n", "").replace("Discount", "").strip()
            if sel.get("deals_detail"):
                deal["deals_detail"] = _text(sel["deals_detail"])

            deals.append(deal)
        except StaleElementReferenceException:
            continue

    return deals


def _title_from_url_slug(url):
    """从 deal_url 提取商品 slug 并格式化为 Title Case，如 sequin-denim-mini-dress-blue -> Sequin Denim Mini Dress Blue"""
    if not url:
        return ""
    try:
        # 匹配 .../product-slug/PRODUCTCODE.html 中的 product-slug
        m = re.search(r"/([^/]+)/[A-Z0-9]+-[A-Z0-9]+\.html", url, re.I)
        if m:
            slug = m.group(1)
            return slug.replace("-", " ").title()
    except Exception:
        pass
    return ""


def _parse_guess_deals_from_jsonld(html, base_url, site_id):
    """
    guess 专用：当 DOM 解析为 0 时，从 productItemsList JSON-LD 构建 deals。
    返回 [{"source_site": site_id, "title": str, "deal_url": str, ...}, ...]
    """
    try:
        m = re.search(r'<script\s+id="productItemsList"\s+type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(1).strip())
        deals = []
        for item in data.get("itemListElement", []):
            name = item.get("name", "").strip()
            url = (item.get("url", "") or "").strip()
            if not url:
                continue
            full_url = urljoin(base_url, url) if not url.startswith("http") else url
            deals.append({
                "source_site": site_id,
                "title": _clean_title(name or _title_from_url_slug(full_url), None),
                "deal_url": full_url,
                "current_price": None,
                "original_price": None,
                "image_url": "",
            })
        return deals
    except Exception:
        return []


def _enrich_guess_titles(driver, deals, site):
    """
    guess 专用：从 productItemsList JSON-LD 补全 title，兜底从 deal_url 提取。
    """
    if site.get("id") != "guess" or not deals:
        return deals

    url_to_name = {}
    try:
        html = driver.page_source
        m = re.search(r'<script\s+id="productItemsList"\s+type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        if m:
            data = json.loads(m.group(1).strip())
            for item in data.get("itemListElement", []):
                name = item.get("name", "").strip()
                url = item.get("url", "").strip()
                if name and url:
                    url_to_name[url] = name
    except Exception:
        pass

    filled = 0
    for deal in deals:
        if deal.get("title"):
            continue
        url = deal.get("deal_url", "")
        if not url:
            continue
        title = url_to_name.get(url)
        if not title:
            title = _title_from_url_slug(url)
        if title:
            deal["title"] = title
            filled += 1

    if filled:
        _safe_print(f"  [guess] 从 JSON-LD/URL 补全 {filled} 条 title")
    return deals


def _parse_guess_prices_from_html(html, base_url):
    """
    guess 专用：从 HTML 解析价格和图片（Algolia 在 Selenium 中可能未完全渲染，用静态 HTML 兜底）。
    返回 [(deal_url, current_price, original_price, image_url), ...]
    """
    if not HAS_BS4:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("li.product__tile, li.ais-InfiniteHits-item.product__tile")
        result = []
        for node in containers:
            link = node.select_one(
                "a.js-aa-pdp-href, a.product-grid__image[href*='.html'], "
                "a[href*='/last-chance/'][href*='.html'], a[href*='/sale/'][href*='.html'], "
                "a[href*='guess.com'][href*='.html']"
            )
            cp_el = node.select_one(".item-price, .value.item-price")
            op_el = node.select_one(".price__strike-through.item-salesPrice .value")
            slide = node.select_one("li.guess-carousel__slide--show")
            img_el = node.select_one("img.product-grid__image[src*='img.guess.com']")
            img_src = ""
            if slide:
                img_src = (slide.get("data-img-src", "") or "").strip()
            if not img_src and img_el:
                img_src = (img_el.get("src", "") or "").strip()
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            cp = (cp_el.get_text(strip=True) if cp_el else "") or ""
            op = (op_el.get_text(strip=True) if op_el else "") or ""
            img_url = urljoin(base_url, img_src) if img_src and not img_src.startswith("http") else img_src
            if url:
                result.append((url, _normalize_price_text(cp), _normalize_price_text(op), img_url or ""))
        return result
    except Exception:
        return []


def _parse_athleta_from_html(html, base_url):
    """
    athleta 专用：从 HTML 解析 title、价格（Selenium 可能未完全渲染，用 BeautifulSoup 兜底）。
    返回 {url: {"title": str, "current_price": str, "original_price": str}}
    """
    if not HAS_BS4:
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("[data-testid='plp_product-card']")
        result = {}
        for node in containers:
            link = node.select_one("a[data-testid='plp_product-info']")
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            if not url:
                continue
            title_el = node.select_one("[data-testid='plp_product-name']")
            cp_el = node.select_one("span.fds__core-web-price-percent-off .plp-product-price-small, .plp-product-price-small")
            op_el = node.select_one("span.fds__core-web-original-price")
            title = (title_el.get_text(strip=True) if title_el else "") or ""
            cp_display = _normalize_price_text((cp_el.get_text(strip=True) if cp_el else "") or "")
            op_display = _normalize_price_text((op_el.get_text(strip=True) if op_el else "") or "")
            result[url] = {
                "title": _clean_title(title, None),
                "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                "current_price_display": cp_display or "",
                "original_price": _parse_price_to_float(op_display) if op_display else None,
                "original_price_display": op_display or "",
            }
        return result
    except Exception:
        return {}


def _enrich_calvinklein_promo(driver, deals, site):
    """calvinklein 专用：从页面 header / promotions-container 抓取 deal_percent、Use code 补全。"""
    if site.get("id") != "calvinklein" or not deals:
        return deals
    use_code = ""
    deal_pct = ""
    try:
        # 1) 优先从 .promotions-container 元素取 deal_percent（部分商品无此元素，用页面级第一个）
        try:
            els = driver.find_elements(By.CSS_SELECTOR, ".promotions-container")
            for el in els:
                txt = (el.text or "").strip()
                if txt and re.search(r"\d+%\s+off", txt, re.I):
                    deal_pct = txt
                    break
        except Exception:
            pass
        html = driver.page_source
        # 2) 若未取到，从 page_source 正则匹配
        if not deal_pct:
            m2 = re.search(r"(\d+%\s+off[^<]*?)(?:\s*[|*]|\s*Use\s+code|$)", html, re.I)
            if m2:
                deal_pct = m2.group(1).strip().rstrip("*").strip()
            if not deal_pct:
                m3 = re.search(r"(\d+%\s+off[^<]+)", html, re.I)
                if m3:
                    deal_pct = m3.group(1).strip().rstrip("*").strip()
        # 3) deals_detail: Use code: GET40
        m = re.search(r"Use\s+code:\s*([A-Z0-9]+)", html, re.I)
        use_code = f"Use code: {m.group(1).strip()}" if m else ""
        for d in deals:
            if use_code and not (d.get("deals_detail") or "").strip():
                d["deals_detail"] = use_code
            if deal_pct and not (d.get("deal_percent") or "").strip():
                d["deal_percent"] = deal_pct
    except Exception:
        pass
    return deals


def _enrich_athleta_from_html(driver, deals, site, base_url):
    """athleta 专用：从 HTML 补全 title、价格。"""
    if site.get("id") != "athleta" or not deals:
        return deals
    url_to_data = _parse_athleta_from_html(driver.page_source, base_url)
    filled = 0
    for deal in deals:
        url = deal.get("deal_url", "")
        if not url:
            continue
        data = url_to_data.get(url)
        if not data:
            continue
        if data.get("title") and not deal.get("title"):
            deal["title"] = data["title"]
            filled += 1
        if data.get("current_price_display") and deal.get("current_price") is None:
            deal["current_price"] = data.get("current_price")
            deal["current_price_display"] = data.get("current_price_display", "")
            filled += 1
        if data.get("original_price_display") and deal.get("original_price") is None:
            deal["original_price"] = data.get("original_price")
            deal["original_price_display"] = data.get("original_price_display", "")
            filled += 1
    if url_to_data:
        _safe_print(f"  [athleta] 从 HTML 补全 title/价格")
    return deals


def _parse_michaelkors_from_html(html, base_url):
    """
    michaelkors 专用：从 HTML 解析 title、价格（Selenium 可能未完全渲染，用 BeautifulSoup 兜底）。
    兼容 US 站 (michaelkors.com) 与 Malaysia 站 (michaelkors.global)。
    返回 {url: {"title": str, "current_price": str, "original_price": str}}
    """
    if not HAS_BS4:
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("article.product-tile-container")
        result = {}
        for node in containers:
            link = node.select_one("a.product-tile-image-link") or node.select_one("a[href*='.html']:not([href*='Wishlist'])")
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            if not url:
                continue
            # title: 优先 h2.pdp-link a（US 站），fallback .pdp-link（Malaysia 站）
            title_el = node.select_one("h2.pdp-link a") or node.select_one(".pdp-link a") or node.select_one(".pdp-link")
            title = (title_el.get_text(strip=True) if title_el else "") or ""
            # 移除 " Now RM 1,250" / " Now $104" 等价格后缀
            title = re.sub(r"\s+Now\s+(?:RM|MYR|USD|\$|€|£)?\s*[\d,]+\.?\d*\s*$", "", title).strip()
            cp_el = node.select_one(".price .sales .value")
            op_el = node.select_one(".price .list .value") or node.select_one(".price .list")
            cp_display = _normalize_price_text((cp_el.get_text(strip=True) if cp_el else "") or "")
            op_display = _normalize_price_text((op_el.get_text(strip=True) if op_el else "") or "")
            dp_el = node.select_one(".default-price__discount")
            promo_el = node.select_one(".promotion-callout")
            deal_percent = (dp_el.get_text(strip=True) if dp_el else "") or ""
            deals_detail = (promo_el.get_text(strip=True) if promo_el else "") or ""
            result[url] = {
                "title": _clean_title(title, None),
                "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                "current_price_display": cp_display or "",
                "original_price": _parse_price_to_float(op_display) if op_display else None,
                "original_price_display": op_display or "",
                "deal_percent": deal_percent,
                "deals_detail": deals_detail,
            }
        return result
    except Exception:
        return {}


def _parse_calvinklein_from_html(html, base_url):
    """calvinklein 专用：DOM 无结果时从 HTML 兜底解析。"""
    if not HAS_BS4:
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("div.product")
        result = {}
        for node in containers:
            link = node.select_one("a.ds-product-name, .product-name-link a.name-link, a.pdpurl")
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            if not url:
                continue
            title_el = node.select_one("a.ds-product-name span, .product-name-link a.name-link")
            cp_el = node.select_one(".price .sales .value, .price.ds-product-price .value")
            op_el = node.select_one(".price .strike-through.list .value")
            title = (title_el.get_text(strip=True) if title_el else "") or ""
            cp_display = _normalize_price_text((cp_el.get_text(strip=True) if cp_el else "") or "")
            op_display = _normalize_price_text((op_el.get_text(strip=True) if op_el else "") or "")
            result[url] = {
                "title": _clean_title(title, None),
                "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                "current_price_display": cp_display or "",
                "original_price": _parse_price_to_float(op_display) if op_display else None,
                "original_price_display": op_display or "",
            }
        return result
    except Exception:
        return {}


def _parse_coach_from_html(html, base_url):
    """
    coach 专用：从 HTML 解析（DOM 可能未渲染时兜底）。
    返回 {url: {"title": str, "current_price": str, "original_price": str}}
    """
    if not HAS_BS4:
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select(".product-tile, [data-qa='product-tile']")
        result = {}
        for node in containers:
            link = node.select_one(".product-name a, a[href*='/products/']")
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            if not url:
                continue
            title_el = node.select_one("p[data-qa='cm_pdt_link_pt_title'], .product-name p")
            cp_el = node.select_one("span.salesPrice, [data-qa='m_plp_txt_pt_price_upper_rl']")
            op_el = node.select_one("[data-qa='cm_txt_pdt_price_strthr'], [data-qa='txt_comparable_value_price']")
            title = (title_el.get_text(strip=True) if title_el else "") or ""
            cp_display = _normalize_price_text((cp_el.get_text(strip=True) if cp_el else "") or "")
            op_display = _normalize_price_text((op_el.get_text(strip=True) if op_el else "") or "")
            result[url] = {
                "title": _clean_title(title, None),
                "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                "current_price_display": cp_display or "",
                "original_price": _parse_price_to_float(op_display) if op_display else None,
                "original_price_display": op_display or "",
            }
        return result
    except Exception:
        return {}


def _enrich_michaelkors_from_html(driver, deals, site, base_url):
    """michaelkors 专用：从 HTML 补全 title、价格。"""
    if site.get("id") != "michaelkors" or not deals:
        return deals
    url_to_data = _parse_michaelkors_from_html(driver.page_source, base_url)
    # URL 可能带不同 query，用 path 匹配
    def _norm(u):
        return urlparse(u).path if u else ""
    path_to_data = {_norm(u): d for u, d in url_to_data.items()}
    filled = 0
    for deal in deals:
        url = deal.get("deal_url", "")
        if not url:
            continue
        path = _norm(url)
        data = path_to_data.get(path)
        if not data:
            continue
        if data.get("title") and not deal.get("title"):
            deal["title"] = data["title"]
            filled += 1
        if data.get("current_price_display") and deal.get("current_price") is None:
            deal["current_price"] = data.get("current_price")
            deal["current_price_display"] = data.get("current_price_display", "")
            filled += 1
        if data.get("original_price_display") and deal.get("original_price") is None:
            deal["original_price"] = data.get("original_price")
            deal["original_price_display"] = data.get("original_price_display", "")
            filled += 1
        if data.get("deal_percent") and not deal.get("deal_percent"):
            deal["deal_percent"] = data.get("deal_percent", "")
            filled += 1
        if data.get("deals_detail") and not deal.get("deals_detail"):
            deal["deals_detail"] = data.get("deals_detail", "")
            filled += 1
    if url_to_data:
        _safe_print(f"  [michaelkors] 从 HTML 补全 title/价格/折扣")
    return deals


def _parse_cos_deals_from_jsonld(html, site_id):
    """
    cos 专用：当 DOM 解析为 0 时，从 schema.org ItemList JSON-LD 直接生成 deals 列表。
    返回 [deal, ...]
    """
    deals = []
    try:
        for m in re.finditer(
            r'<script\s+type="application/ld\+json">\s*(.*?)\s*</script>',
            html,
            re.DOTALL,
        ):
            raw = m.group(1).strip()
            if '"@type":"ItemList"' not in raw and '"@type": "ItemList"' not in raw:
                continue
            data = json.loads(raw)
            for item in data.get("itemListElement", []):
                prod = item.get("item") or item
                if prod.get("@type") != "Product":
                    continue
                url = (prod.get("url") or "").strip()
                if not url:
                    continue
                name = (prod.get("name") or "").strip()
                image = (prod.get("image") or "").strip()
                offers = prod.get("offers") or {}
                low = offers.get("lowPrice")
                high = offers.get("highPrice")
                currency = offers.get("priceCurrency") or "£"
                cp_display = f"{currency}{low}" if low is not None else ""
                op_display = f"{currency}{high}" if high is not None else ""
                deals.append({
                    "source_site": site_id,
                    "title": name,
                    "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                    "current_price_display": cp_display or "",
                    "original_price": _parse_price_to_float(op_display) if op_display else None,
                    "original_price_display": op_display or "",
                    "deal_url": url,
                    "image_url": image or "",
                })
            break
    except Exception:
        pass
    return deals


def _parse_cos_jsonld(html):
    """
    cos 专用：从 schema.org ItemList JSON-LD 解析商品数据（含真实图片 URL）。
    返回 {url: {"title": str, "image_url": str, "current_price": str, "original_price": str}}
    """
    result = {}
    try:
        for m in re.finditer(
            r'<script\s+type="application/ld\+json">\s*(.*?)\s*</script>',
            html,
            re.DOTALL,
        ):
            raw = m.group(1).strip()
            if '"@type":"ItemList"' not in raw and '"@type": "ItemList"' not in raw:
                continue
            data = json.loads(raw)
            for item in data.get("itemListElement", []):
                prod = item.get("item") or item
                if prod.get("@type") != "Product":
                    continue
                url = (prod.get("url") or "").strip()
                if not url:
                    continue
                name = (prod.get("name") or "").strip()
                image = (prod.get("image") or "").strip()
                offers = prod.get("offers") or {}
                low = offers.get("lowPrice")
                high = offers.get("highPrice")
                currency = offers.get("priceCurrency") or "£"
                cp_display = f"{currency}{low}" if low is not None else ""
                op_display = f"{currency}{high}" if high is not None else ""
                result[url] = {
                    "title": name,
                    "image_url": image,
                    "current_price": _parse_price_to_float(cp_display) if cp_display else None,
                    "current_price_display": cp_display or "",
                    "original_price": _parse_price_to_float(op_display) if op_display else None,
                    "original_price_display": op_display or "",
                }
            break
    except Exception:
        pass
    return result


def _enrich_cos_from_jsonld(driver, deals, site):
    """cos 专用：从 JSON-LD 补全 title、image_url、价格（img 只有 placeholder）。"""
    if site.get("id") != "cos" or not deals:
        return deals
    url_to_data = _parse_cos_jsonld(driver.page_source)
    filled = 0
    for deal in deals:
        url = deal.get("deal_url", "")
        if not url:
            continue
        data = url_to_data.get(url)
        if not data:
            continue
        if data.get("title") and not deal.get("title"):
            deal["title"] = data["title"]
            filled += 1
        if data.get("image_url") and (not deal.get("image_url") or "placeholder" in (deal.get("image_url") or "")):
            deal["image_url"] = data["image_url"]
        if data.get("current_price_display") and deal.get("current_price") is None:
            deal["current_price"] = data.get("current_price")
            deal["current_price_display"] = data.get("current_price_display", "")
        if data.get("original_price_display") and deal.get("original_price") is None:
            deal["original_price"] = data.get("original_price")
            deal["original_price_display"] = data.get("original_price_display", "")
    if url_to_data:
        _safe_print(f"  [cos] 从 JSON-LD 补全 title/image/价格")
    return deals


def _filter_invalid_cos_deals(deals):
    """cos 专用：过滤 deal_url 指向 onetrust、cookie-consent、login、modal 的无效条目。"""
    bad = ("onetrust", "cookie-consent", "login-register-modal", "modal-popup")
    return [d for d in deals if not any(b in (d.get("deal_url") or "").lower() for b in bad)]


def _inject_cos_stealth(driver):
    """COS 反检测：CDP 注入，伪造 navigator.webdriver 等。"""
    stealth_js = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
    """
    for method in ("execute_cdp_cmd", "execute_cdp_command"):
        fn = getattr(driver, method, None)
        if fn:
            try:
                fn("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_js})
                return
            except Exception:
                pass


def _cos_simulate_human_behavior(driver):
    """COS 行为模拟：随机滚动、停留。"""
    _human_like_delay(1.0, 2.0)
    for _ in range(random.randint(1, 3)):
        scroll_h = random.randint(100, 400)
        driver.execute_script(f"window.scrollBy(0, {scroll_h});")
        _human_like_delay(0.5, 1.2)
    driver.execute_script("window.scrollTo(0, 0);")
    _human_like_delay(0.5, 1.0)


def _handle_cos_region_selection(driver):
    """COS 地区选择兜底：检测到选择页则点击北美/US 等选项。"""
    try:
        page_src = (driver.page_source or "").lower()
        if "select your location" not in page_src and "redirecting" not in page_src:
            return True
        if "redirecting you to selected" in page_src:
            _safe_print("  [cos] 页面正在重定向，等待 5 秒...")
            time.sleep(5)
            try:
                page_src = (driver.page_source or "").lower()
                if "select your location" not in page_src and "redirecting" not in page_src:
                    return True
            except Exception:
                return True
    except Exception:
        return True
    _safe_print("  [cos] 检测到地区选择页，尝试点击北美/US...")
    _human_like_delay(0.5, 1.0)
    xpaths = [
        "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'North America')]",
        "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'United States')]",
        "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'Americas')]",
        "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'Canada')]",
        "//*[contains(translate(text(),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'), 'USA')]",
        "//a[contains(@href,'en-us')]",
        "//*[contains(@data-market,'us')]",
    ]
    for xpath in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xpath)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.3, 0.6)
                        el.click()
                        _safe_print(f"  [cos] 已点击地区选项")
                        _human_like_delay(2.5, 4.0)
                        return True
                    except Exception:
                        continue
        except Exception:
            continue
    _safe_print("  [cos] 未找到可点击的地区选项，尝试通过 cookie 设置...")
    try:
        driver.execute_script(
            "document.cookie = 'ecom_locale=us_en-US; path=/; domain=.cos.com; max-age=86400';"
        )
        driver.refresh()
        _human_like_delay(3.0, 5.0)
    except Exception:
        pass
    return False


def _merge_guess_prices(deals, price_list):
    """将 price_list [(url, cp_display, op_display, image_url?), ...] 合并到 deals，按 deal_url 匹配。"""
    url_to_data = {}
    for row in price_list:
        url = row[0]
        cp = row[1] if len(row) > 1 else ""
        op = row[2] if len(row) > 2 else ""
        img = row[3] if len(row) > 3 else ""
        url_to_data[url] = (cp, op, img)
    filled = 0
    for deal in deals:
        url = deal.get("deal_url", "")
        if not url:
            continue
        data = url_to_data.get(url)
        if not data:
            continue
        cp_display, op_display, img_url = data[0], data[1], data[2] if len(data) > 2 else ""
        if cp_display and deal.get("current_price") is None:
            deal["current_price"] = _parse_price_to_float(cp_display)
            deal["current_price_display"] = cp_display
            filled += 1
        if op_display and deal.get("original_price") is None:
            deal["original_price"] = _parse_price_to_float(op_display)
            deal["original_price_display"] = op_display
        if img_url and not deal.get("image_url"):
            deal["image_url"] = img_url
    return filled


def _save_debug_html(driver, site_id):
    """保存当前页面 HTML 到 config/html_samples/，供 debug_crawl.py 分析"""
    if site_id not in ("guess", "cos", "michaelkors", "lululemon_athletica", "tommy_hilfiger", "adidas", "alo_yoga", "athleta", "calvinklein", "coach", "columbia", "h_m", "j_crew", "levis", "nike", "target", "old_navy", "theory", "tory_burch", "tory_sport"):
        return
    try:
        debug_dir = os.path.join(os.path.dirname(__file__), "config", "html_samples")
        os.makedirs(debug_dir, exist_ok=True)
        suffix = "_debug.html" if site_id == "guess" else ".html"
        path = os.path.join(debug_dir, f"{site_id}{suffix}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        _safe_print(f"  [调试] 已保存: {path}")
    except Exception as e:
        _safe_print(f"  [调试] 保存 HTML 失败: {e}")


def parse_detail_page(driver, site, base_url, detail_url=None):
    """
    在当前详情页按 site 的 detail_selectors 抓取字段，返回 dict 合并进 deal。
    image_list 取多元素 src 成列表；其余键取单元素文本，输出为 detail_<key>。
    detail_url 可选，用于 Calvin Klein 等需按商品 ID 过滤推荐图。
    """
    out = {}
    sel = site.get("detail_selectors") or {}
    if not sel:
        return out

    def _one_text(selector):
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            return (el.text or "").strip()
        except Exception:
            return ""

    for key, selector in sel.items():
        if key == "detail_image_denoise":
            continue
        if not selector:
            continue
        if key == "image_list":
            try:
                imgs = driver.find_elements(By.CSS_SELECTOR, selector)
                urls = []
                seen = set()  # 去重（同图多尺寸）
                for el in imgs:
                    src = (el.get_attribute("src") or "").strip()
                    data_src = (el.get_attribute("data-src") or "").strip()
                    if src and ("empty_loading" in src or "banner_loading" in src or "1x1" in src):
                        src = ""
                    url = src or data_src
                    if (not url or url.startswith("data:")) and el.get_attribute("srcset"):
                        srcset = (el.get_attribute("srcset") or "").strip()
                        if srcset:
                            first = srcset.split(",")[0].strip().split()[0]
                            if first and first.startswith("http"):
                                url = first
                    if url and not url.startswith("data:") and "1x1" not in url and "empty_loading" not in url and "banner_loading" not in url:
                        full = urljoin(base_url, url)
                        # Athleta/Gap: 图片实际在 CDN，替换域名为 www1.assets-gap.com
                        if "athleta.gap.com" in full and "/webcontent/" in full:
                            full = full.replace("https://athleta.gap.com", "https://www1.assets-gap.com").replace("http://athleta.gap.com", "https://www1.assets-gap.com")
                        # 去重：Adidas/Shopify/Scene7 等同图多尺寸，按 base 去重
                        if "assets.adidas" in full:
                            base = re.sub(r"/w_\d+,[^/]+/", "/w_1,/", full)
                        elif "cdn.shopify.com" in full:
                            base = re.sub(r"_\d+x(?=\.)", "_1x", full).split("?")[0]
                        elif "scene7.com" in full:
                            base = full.split("?")[0]
                        elif "s7-img-facade" in full or "jcrew.com" in full:
                            base = full.split("?")[0]
                        elif "images.lululemon.com" in full:
                            base = full.split("?")[0]
                        elif "static.nike.com" in full:
                            base = full.split("?")[0]
                        else:
                            base = full
                        if base not in seen:
                            seen.add(base)
                            urls.append(full)
                if urls:
                    denoise = sel.get("detail_image_denoise")
                    if denoise and isinstance(denoise, dict):
                        exclude = denoise.get("exclude_patterns") or []
                        color_pats = denoise.get("color_thumbnail_patterns") or []
                        split = denoise.get("split_color_thumbnails", False)
                        main = [u for u in urls if not any(p in u for p in exclude)]
                        out["detail_image_list"] = main
                        if split and color_pats:
                            color_thumbnails = [u for u in urls if any(p in u for p in color_pats)]
                            if color_thumbnails:
                                out["color_thumbnails"] = color_thumbnails
                    else:
                        # Calvin Klein / Columbia: 过滤推荐区图片，只保留当前商品图
                        if site.get("id") == "columbia" and detail_url:
                            pm = re.search(r"/p/[^/]+-(\d+)\.html", detail_url)
                            pid = pm.group(1) if pm else ""
                            if pid:
                                # /i/columbia/PRODUCTID_ 格式需匹配当前商品；columbiasprtswr 等通用图保留
                                filtered = []
                                for u in urls:
                                    m = re.search(r"/columbia/(\d+)_", u)
                                    if m:
                                        if m.group(1) == pid:
                                            filtered.append(u)
                                    else:
                                        filtered.append(u)
                                urls = filtered
                        if site.get("id") == "calvinklein" and detail_url:
                            m = re.search(r"/([A-Z0-9]+)-([A-Z0-9]+)\.html", detail_url, re.I)
                            pid = f"{m.group(1)}_{m.group(2)}" if m else ""
                            if pid:
                                urls = [u for u in urls if pid in u]
                            # 若 DOM 只抓到主图（懒加载），从 JSON-LD 补全
                            if pid and len(urls) < 2:
                                try:
                                    scripts = driver.find_elements(By.CSS_SELECTOR, 'script[type="application/ld+json"]')
                                    for s in scripts:
                                        txt = (s.get_attribute("innerHTML") or "").strip()
                                        if txt and '"@type":"Product"' in txt and "image" in txt:
                                            data = json.loads(txt)
                                            imgs = data.get("image") or []
                                            if isinstance(imgs, str):
                                                imgs = [imgs]
                                            for u in imgs:
                                                if isinstance(u, str) and pid in u and u not in urls:
                                                    urls.append(u)
                                            break
                                except Exception:
                                    pass
                        out["detail_image_list"] = urls
                # Athleta 兜底：img 可能懒加载，从父 div 的 data-imageurl 取
                elif not urls and site.get("id") == "athleta" and "athleta.gap.com" in base_url:
                    try:
                        bricks = driver.find_elements(By.CSS_SELECTOR, "[data-testid='pdp-photo-brick-image']")
                        for b in bricks:
                            u = (b.get_attribute("data-imageurl") or "").strip()
                            if u and u.startswith("/webcontent/"):
                                full = urljoin("https://www1.assets-gap.com", u)
                                if full not in seen:
                                    seen.add(full)
                                    urls.append(full)
                        if urls:
                            out["detail_image_list"] = urls
                    except Exception:
                        pass
                # Columbia 兜底：从 page_source 正则提取 media.columbia.com 图片（仅保留当前商品 ID）
                elif not urls and site.get("id") == "columbia" and "columbia.com" in base_url:
                    try:
                        html = driver.page_source
                        pid = ""
                        if detail_url:
                            pm = re.search(r"/p/[^/]+-(\d+)\.html", detail_url)
                            pid = pm.group(1) if pm else ""
                        for m in re.finditer(r'https?://media\.columbia\.com/[^\s"\'<>]+', html):
                            full = m.group(0).replace("&amp;", "&").rstrip("&\"'")
                            if "1x1" not in full and "empty" not in full:
                                if pid and pid not in full:
                                    continue
                                base = full.split("?")[0]
                                if base not in seen:
                                    seen.add(base)
                                    urls.append(base)
                        if urls:
                            out["detail_image_list"] = urls[:20]
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            out[f"detail_{key}"] = _one_text(selector)

    return out


def _parse_args():
    """解析命令行：支持 <site_id> [--limit N] [--headless] 或 --all [--exclude id1,id2,...] [--limit N] [--workers N] [--headless]"""
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not args:
        return None, [], None, None, False
    exclude = []
    limit = None
    workers = None
    headless = False
    remaining = []
    i = 0
    while i < len(args):
        if args[i] == "--exclude" and i + 1 < len(args):
            exclude.extend(s.strip() for s in args[i + 1].split(",") if s.strip())
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i] == "--workers" and i + 1 < len(args):
            try:
                workers = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        elif args[i] == "--headless":
            headless = True
            i += 1
        else:
            remaining.append(args[i])
            i += 1
    if not remaining:
        return None, exclude, limit, workers, headless
    if remaining[0] == "--all":
        return "--all", exclude, limit, workers, headless
    return remaining[0], exclude, limit, workers, headless


_PRINT_LOCK = Lock()
_orig_print = print


def _safe_print(*args, **kwargs):
    """线程安全打印，避免并发时输出交错。"""
    with _PRINT_LOCK:
        _orig_print(*args, **kwargs)


def _create_driver(site, config, headless=False, timeout_override=None):
    """创建 Chrome driver，支持 anti_detect、headless 模式。站点级 headless:false 强制非无头（反爬站点）。"""
    anti = site.get("anti_detect") or {}
    use_uc = anti.get("use_undetected", False) and HAS_UC
    timeout = timeout_override if timeout_override is not None else config.get("global", {}).get("timeout_sec", 40)
    # 站点显式 headless:false 时强制非无头（部分站点 headless 被拦截）
    if site.get("headless") is False:
        headless = False
    else:
        headless = headless or config.get("global", {}).get("headless", False)
    if use_uc:
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--lang=en-US")
        options.add_argument("--disable-geolocation")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--log-level=3")
            options.add_argument("--disable-logging")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-default-apps")
        if site.get("force_us_locale"):
            options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
        proxy = (site.get("proxy") or "").strip()
        if proxy and (proxy.startswith("http://") or proxy.startswith("https://")):
            options.add_argument(f"--proxy-server={proxy}")
        driver = uc.Chrome(options=options)
    else:
        options = Options()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-infobars")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--log-level=3")
            options.add_argument("--disable-logging")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-default-apps")
        if site.get("force_us_locale"):
            options.add_argument("--lang=en-US")
            options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
        proxy = (site.get("proxy") or "").strip()
        if proxy and (proxy.startswith("http://") or proxy.startswith("https://")):
            options.add_argument(f"--proxy-server={proxy}")
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(timeout)
    return driver


def _kill_driver(driver):
    """彻底关闭 driver 并杀死相关进程，确保无残留。"""
    if not driver:
        return
    try:
        driver.quit()
    except Exception:
        pass
    try:
        driver.close()
    except Exception:
        pass
    try:
        if hasattr(driver, "service") and driver.service:
            proc = getattr(driver.service, "process", None)
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
    except Exception:
        pass


def _kill_orphan_browsers():
    """跑完后清理可能残留的 chromedriver 进程，确保无残留。"""
    if sys.platform == "win32":
        for name in ("chromedriver.exe",):
            try:
                subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, timeout=5)
            except Exception:
                pass
    else:
        try:
            subprocess.run(["pkill", "-9", "chromedriver"], capture_output=True, timeout=5)
        except Exception:
            pass


def _crawl_site_worker(site, config, limit=None, headless=False, timeout_override=None, date_dir=None, idx=None, total=None):
    """
    单站点爬取 worker：交错启动、创建 driver、爬取、退出。
    每个线程独立浏览器实例，互不干扰；人类化行为保留在 _crawl_one_site 内。
    超时（默认 40 秒）则跳过。0 deal 且页面<8KB 时自动重试非无头模式。
    """
    site_id = site["id"]
    timeout = timeout_override if timeout_override is not None else config.get("global", {}).get("timeout_sec", 40)
    stagger = random.uniform(0, 3)
    time.sleep(stagger)
    driver = None
    for attempt in range(2):
        driver = None
        try:
            use_headless = headless if attempt == 0 else False
            if attempt == 1:
                _safe_print(f"  [{site_id}] 0 deal 且页面过小，重试非无头模式")
            driver = _create_driver(site, config, headless=use_headless, timeout_override=timeout_override)
            _crawl_one_site(driver, site, config, limit=limit, date_dir=date_dir)
            _kill_driver(driver)
            return (site_id, True, None)
        except DealsEmptyError:
            _kill_driver(driver)
            if attempt == 0:
                continue
            return (site_id, False, "重试后仍 0 deal")
        except TimeoutException:
            _kill_driver(driver)
            return (site_id, False, f"超时 {timeout} 秒，跳过")
        except Exception as e:
            _kill_driver(driver)
            return (site_id, False, str(e))
    return (site_id, False, "重试后仍失败")


def _crawl_one_site(driver, site, config, limit=None, date_dir=None):
    """爬取单个站点，写入 output/<date_dir>/<brandname>_test.json。limit: 仅爬取前 N 条（用于测试）。"""
    site_id = site["id"]
    list_url = site["list_url"]
    base_url = get_base_url(list_url)
    timeout = config.get("global", {}).get("timeout_sec", 40)
    output_dir = config.get("global", {}).get("output_dir", "output")
    date_dir = date_dir or _today_date_dir()
    brandname = site.get("name", site_id)
    out_path = os.path.join(os.path.dirname(__file__), output_dir, date_dir, f"{brandname}_test.json")

    _safe_print(f"打开: {list_url}")
    if site.get("id") == "cos":
        anti = site.get("anti_detect") or {}
        pre_url = (anti.get("pre_navigate_url") or "").strip()
        if pre_url:
            _safe_print("  [cos] 预热访问建立会话...")
            driver.get(pre_url)
            _cos_simulate_human_behavior(driver)
        driver.get(list_url)
        _human_like_delay(1.5, 2.5)
        dismiss_consent_if_any(driver)
        _human_like_delay(0.6, 1.2)
        if anti.get("region_selection_fallback", {}).get("enabled", True):
            _handle_cos_region_selection(driver)
        try:
            for sel in ["[data-testid='product-card-wrapper']", "li.ais-Hits-item", "a[href*='/product/']", "[data-product-id]"]:
                try:
                    WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    break
                except Exception:
                    continue
        except Exception:
            pass
        _human_like_delay(2.0, 3.0)
    else:
        driver.get(list_url)
        _human_like_delay(1.2, 2.2)
        dismiss_consent_if_any(driver)
        _human_like_delay(0.6, 1.2)
        if site.get("id") == "michaelkors":
            try:
                WebDriverWait(driver, 18).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "article.product-tile-container, a.product-tile-image-link"))
                )
                _human_like_delay(1.5, 2.5)
            except Exception:
                pass
        if site.get("id") == "coach":
            try:
                WebDriverWait(driver, 18).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".product-tile, [data-qa='cm_pdt_link_pt_title']"))
                )
                _human_like_delay(1.5, 2.5)
            except Exception:
                pass
        if site.get("id") == "calvinklein":
            try:
                WebDriverWait(driver, 18).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.product, .product-name-link"))
                )
                _human_like_delay(1.5, 2.5)
            except Exception:
                pass
        if site.get("id") == "guess":
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li.product__tile, .aa-ItemContentTitle, script#productItemsList"))
                )
                _human_like_delay(1.0, 2.0)
            except Exception:
                pass
        if site.get("id") == "levis":
            try:
                WebDriverWait(driver, 12).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-cell, a.cell-image-link"))
                )
                _human_like_delay(1.0, 2.0)
            except Exception:
                pass
        if site.get("id") == "lululemon_athletica":
            try:
                WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='product-tile'], div.product-tile"))
                )
                _human_like_delay(2.0, 3.5)
            except Exception:
                pass
    _safe_print("分步滚动以触发懒加载...")
    _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4)
    _human_like_delay(0.6, 1.0)

    deals = parse_deals_from_page(driver, site, base_url, config)
    if site.get("id") == "cos" and not deals:
        deals = _parse_cos_deals_from_jsonld(driver.page_source, site["id"])
        if deals:
            _safe_print(f"  [cos] DOM 无结果，从 JSON-LD 解析到 {len(deals)} 条")
    if site.get("id") == "guess" and not deals:
        deals = _parse_guess_deals_from_jsonld(driver.page_source, base_url, site["id"])
        if deals:
            _safe_print(f"  [guess] DOM 无结果，从 JSON-LD 解析到 {len(deals)} 条")
    if site.get("id") == "michaelkors" and not deals and HAS_BS4:
        url_to_data = _parse_michaelkors_from_html(driver.page_source, base_url)
        if url_to_data:
            deals = [
                {
                    "source_site": site["id"],
                    "title": d.get("title", ""),
                    "current_price": d.get("current_price"),
                    "current_price_display": d.get("current_price_display", ""),
                    "original_price": d.get("original_price"),
                    "original_price_display": d.get("original_price_display", ""),
                    "deal_url": url,
                    "image_url": "",
                    "deal_percent": d.get("deal_percent", ""),
                    "deals_detail": d.get("deals_detail", ""),
                }
                for url, d in url_to_data.items()
            ]
            _safe_print(f"  [michaelkors] DOM 无结果，从 HTML 解析到 {len(deals)} 条")
    if site.get("id") == "coach" and not deals and HAS_BS4:
        url_to_data = _parse_coach_from_html(driver.page_source, base_url)
        if url_to_data:
            deals = [
                {
                    "source_site": site["id"],
                    "title": d.get("title", ""),
                    "current_price": d.get("current_price"),
                    "current_price_display": d.get("current_price_display", ""),
                    "original_price": d.get("original_price"),
                    "original_price_display": d.get("original_price_display", ""),
                    "deal_url": url,
                    "image_url": "",
                }
                for url, d in url_to_data.items()
            ]
            _safe_print(f"  [coach] DOM 无结果，从 HTML 解析到 {len(deals)} 条")
    if site.get("id") == "calvinklein" and not deals and HAS_BS4:
        url_to_data = _parse_calvinklein_from_html(driver.page_source, base_url)
        if url_to_data:
            deals = [
                {
                    "source_site": site["id"],
                    "title": d.get("title", ""),
                    "current_price": d.get("current_price"),
                    "current_price_display": d.get("current_price_display", ""),
                    "original_price": d.get("original_price"),
                    "original_price_display": d.get("original_price_display", ""),
                    "deal_url": url,
                    "image_url": "",
                }
                for url, d in url_to_data.items()
            ]
            _safe_print(f"  [calvinklein] DOM 无结果，从 HTML 解析到 {len(deals)} 条")
    _safe_print(f"解析到 {len(deals)} 条 deal")

    page_sz = len(driver.page_source or "")
    if not deals and page_sz < MIN_PAGE_SIZE:
        raise DealsEmptyError(f"页面仅 {page_sz} 字符、0 deal，疑似被拦截，将重试非无头模式")

    deals = _enrich_guess_titles(driver, deals, site)
    deals = _enrich_calvinklein_promo(driver, deals, site)
    deals = _enrich_athleta_from_html(driver, deals, site, base_url)
    deals = _enrich_michaelkors_from_html(driver, deals, site, base_url)
    if site.get("id") == "cos":
        deals = _filter_invalid_cos_deals(deals)
        deals = _enrich_cos_from_jsonld(driver, deals, site)
    if site.get("id") == "guess" and deals:
        empty_prices = sum(1 for d in deals if not d.get("current_price"))
        if empty_prices > 0 and HAS_BS4:
            price_list = _parse_guess_prices_from_html(driver.page_source, base_url)
            filled = _merge_guess_prices(deals, price_list)
            if filled:
                _safe_print(f"  [guess] 从 HTML 补全 {filled} 条价格")
    if limit is not None and limit > 0:
        deals = deals[:limit]
        _safe_print(f"限制为前 {limit} 条")
    _save_debug_html(driver, site["id"])

    fetch_detail = site.get("fetch_detail")
    if fetch_detail is None:
        fetch_detail = config.get("global", {}).get("fetch_detail", False)
    detail_selectors = site.get("detail_selectors")
    if fetch_detail and detail_selectors and isinstance(detail_selectors, dict) and len(detail_selectors) > 0:
        _safe_print(f"进入详情页补全 {len([d for d in deals if d.get('deal_url')])} 条...")
        pdp_saved = False
        for i, deal in enumerate(deals):
            url = deal.get("deal_url")
            if not url:
                continue
            try:
                driver.get(url)
                _human_like_delay(1.0, 2.0)
                dismiss_consent_if_any(driver)
                _human_like_delay(0.4, 0.8)
                if site_id == "athleta":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='pdp-photo-brick-image'] img"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "coach":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "#splide02 img[src*='scene7.com'], [data-qa='m_pdp_btn_pdt_img'][src*='scene7.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "columbia":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='media.columbia.com'], img[src*='scene7.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "guess":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='img.guess.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "h_m":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='image.hm.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "j_crew":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='s7-img-facade']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "michaelkors":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='michaelkors.scene7.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "levis":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='lscoglobal.scene7.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "lululemon_athletica":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='images.lululemon.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id == "nike":
                    try:
                        el = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='static.nike.com']"))
                        )
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        _human_like_delay(0.5, 1.0)
                    except Exception:
                        pass
                if site_id in ("adidas", "alo_yoga", "athleta", "calvinklein", "coach", "columbia", "guess", "h_m", "j_crew", "levis", "lululemon_athletica", "michaelkors", "nike") and not pdp_saved:
                    try:
                        debug_dir = os.path.join(os.path.dirname(__file__), "config", "html_samples")
                        os.makedirs(debug_dir, exist_ok=True)
                        path = os.path.join(debug_dir, f"{site_id}_pdp.html")
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(driver.page_source)
                        _safe_print(f"  [调试] 已保存 PDP: {path}")
                        pdp_saved = True
                    except Exception:
                        pass
                detail_base = get_base_url(url)
                extra = parse_detail_page(driver, site, detail_base, detail_url=url)
                deal.update(extra)
                # 列表页 title/price 为空时，用详情页抓到的 detail_* 回填
                for main_key, detail_key in [("title", "detail_title"), ("current_price", "detail_current_price"), ("original_price", "detail_original_price")]:
                    if not deal.get(main_key) and deal.get(detail_key):
                        deal[main_key] = deal[detail_key]
            except Exception as e:
                _safe_print(f"  详情页失败 [{i+1}] {url[:50]}...: {e}")
            _human_like_delay(0.5, 1.2)

    brand_display = site.get("name", site_id)

    def _format_deal_for_output(d):
        """输出格式：source_site->Brand(用 site.name), title->Product_name, deal_url->Product_link，移除 *_display"""
        out = {}
        for k, v in d.items():
            if k == "source_site":
                out["Brand"] = brand_display
            elif k == "title":
                out["Product_name"] = v or d.get("detail_title") or ""
            elif k == "deal_url":
                out["Product_link"] = v
            elif k in ("current_price_display", "original_price_display", "detail_title", "detail_current_price", "detail_original_price"):
                continue
            elif k in ("current_price", "original_price"):
                val = v if v is not None else d.get("detail_" + k)
                if val is not None:
                    # 确保价格输出为 float，无法解析时输出 None
                    if isinstance(val, (int, float)):
                        out[k] = float(val)
                    elif isinstance(val, str):
                        parsed = _parse_price_to_float(val)
                        out[k] = float(parsed) if parsed is not None else None
                    else:
                        out[k] = float(val) if isinstance(val, (int, float)) else None
            else:
                out[k] = v
        return out

    deals_out = [_format_deal_for_output(d) for d in deals]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"site_id": site["id"], "count": len(deals_out), "deals": deals_out}, f, ensure_ascii=False, indent=2)
    _safe_print(f"已写入: {out_path}")


def main():
    site_id, exclude, limit, workers, headless = _parse_args()
    if not site_id:
        _safe_print("用法: python crawl_test.py <site_id> [--limit N] [--headless]")
        _safe_print("      python crawl_test.py --all [--exclude id1,id2,...] [--limit N] [--workers N] [--headless]")
        _safe_print("示例: python crawl_test.py guess --headless")
        _safe_print("      python crawl_test.py adidas --limit 3")
        _safe_print("      python crawl_test.py --all --exclude coach --workers 4 --headless")
        sys.exit(1)

    config = load_config()
    date_dir = _today_date_dir()
    output_dir = config.get("global", {}).get("output_dir", "output")
    output_base = os.path.join(os.path.dirname(__file__), output_dir)
    output_path = os.path.join(output_base, date_dir)
    os.makedirs(output_path, exist_ok=True)
    os.makedirs(os.path.join(output_path, "processed"), exist_ok=True)
    _safe_print(f"输出目录: {output_dir}/{date_dir}/")

    try:
        if site_id == "--all":
            sites = [s for s in config["sites"] if s.get("list_url")]
            sites = [s for s in sites if s.get("id") not in exclude]
            if exclude:
                _safe_print(f"排除站点: {exclude}")
            if not sites:
                _safe_print("无可用站点")
                sys.exit(1)
            max_workers = workers or config.get("global", {}).get("max_workers", 4)
            max_workers = min(max_workers, len(sites))
            timeout_sec = config.get("global", {}).get("timeout_sec", 40)
            _safe_print(f"批量爬取 {len(sites)} 个站点，并行 {max_workers} 线程（每个线程跑一个站点，交错启动）")
            _safe_print(f"页面加载超时 {timeout_sec} 秒则跳过")
            if headless:
                _safe_print("headless 模式")
            if limit is not None:
                _safe_print(f"限制爬取前 {limit} 条")
            failed = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_crawl_site_worker, site, config, limit, headless, None): site
                    for i, site in enumerate(sites)
                }
                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        sid, ok, err = future.result()
                        if not ok:
                            failed.append((sid, err))
                            _safe_print(f"  [{sid}] 失败: {err}")
                    except Exception as e:
                        sid = site.get("id", "?")
                        failed.append((sid, str(e)))
                        _safe_print(f"  [{sid}] 异常: {e}")
            if failed:
                _safe_print(f"\n完成，{len(failed)} 个站点失败: {[f[0] for f in failed]}")
            else:
                _safe_print(f"\n全部 {len(sites)} 个站点爬取完成")
        else:
            site = get_site_by_id(config, site_id)
            if headless:
                _safe_print("headless 模式")
            if limit is not None:
                _safe_print(f"限制爬取前 {limit} 条")
            driver = _create_driver(site, config, headless=headless)
            try:
                _crawl_one_site(driver, site, config, limit=limit, date_dir=date_dir)
            except Exception as e:
                _safe_print(f"  [{site['id']}] 失败: {e}")
            finally:
                _kill_driver(driver)
    finally:
        _kill_orphan_browsers()


if __name__ == "__main__":
    main()
