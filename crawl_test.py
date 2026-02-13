#!/usr/bin/env python3
"""
按站点 id 爬取单个站点的 deal 列表页，验证 selectors 是否有效。
人类化行为对齐 google_search_product.py：分步滚动、随机延时、每卡 scrollIntoView、Stale 跳过。
用法: python crawl_test.py <site_id>
示例: python crawl_test.py guess
结果写入 output/<brandname>_test.json（如 output/Guess_test.json）。
"""
import json
import os
import random
import re
import sys
import time
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

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
        print(f"  [guess] 从 JSON-LD/URL 补全 {filled} 条 title")
    return deals


def _parse_guess_prices_from_html(html, base_url):
    """
    guess 专用：从 HTML 解析价格（Algolia 在 Selenium 中可能未完全渲染，用静态 HTML 兜底）。
    返回 [(deal_url, current_price, original_price), ...]
    """
    if not HAS_BS4:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.select("li.product__tile:not(.js-aa-product-skeleton-prev):not(.js-aa-product-skeleton-next)")
        result = []
        for node in containers:
            link = node.select_one("a[href*='/last-chance/'][href*='.html']")
            cp_el = node.select_one(".item-price")
            op_el = node.select_one(".price__strike-through.item-salesPrice .value")
            href = (link.get("href", "") or "").strip()
            url = urljoin(base_url, href) if href else ""
            cp = (cp_el.get_text(strip=True) if cp_el else "") or ""
            op = (op_el.get_text(strip=True) if op_el else "") or ""
            if url:
                result.append((url, _normalize_price_text(cp), _normalize_price_text(op)))
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
        print(f"  [athleta] 从 HTML 补全 title/价格")
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
    if url_to_data:
        print(f"  [michaelkors] 从 HTML 补全 title/价格")
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
        print(f"  [cos] 从 JSON-LD 补全 title/image/价格")
    return deals


def _filter_invalid_cos_deals(deals):
    """cos 专用：过滤 deal_url 指向 onetrust、cookie-consent、login、modal 的无效条目。"""
    bad = ("onetrust", "cookie-consent", "login-register-modal", "modal-popup")
    return [d for d in deals if not any(b in (d.get("deal_url") or "").lower() for b in bad)]


def _merge_guess_prices(deals, price_list):
    """将 price_list [(url, cp_display, op_display), ...] 合并到 deals，按 deal_url 匹配。"""
    url_to_prices = {}
    for url, cp, op in price_list:
        url_to_prices[url] = (cp, op)
    filled = 0
    for deal in deals:
        url = deal.get("deal_url", "")
        if not url:
            continue
        prices = url_to_prices.get(url)
        if not prices:
            continue
        cp_display, op_display = prices
        if cp_display and deal.get("current_price") is None:
            deal["current_price"] = _parse_price_to_float(cp_display)
            deal["current_price_display"] = cp_display
            filled += 1
        if op_display and deal.get("original_price") is None:
            deal["original_price"] = _parse_price_to_float(op_display)
            deal["original_price_display"] = op_display
    return filled


def _save_debug_html(driver, site_id):
    """guess 调试：保存当前页面 HTML 供人工检查 Algolia 价格 DOM"""
    if site_id != "guess":
        return
    try:
        debug_dir = os.path.join(os.path.dirname(__file__), "config", "html_samples")
        os.makedirs(debug_dir, exist_ok=True)
        path = os.path.join(debug_dir, f"{site_id}_debug.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"  [调试] 已保存: {path}")
    except Exception as e:
        print(f"  [调试] 保存 HTML 失败: {e}")


def parse_detail_page(driver, site, base_url):
    """
    在当前详情页按 site 的 detail_selectors 抓取字段，返回 dict 合并进 deal。
    image_list 取多元素 src 成列表；其余键取单元素文本，输出为 detail_<key>。
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
        if not selector:
            continue
        if key == "image_list":
            try:
                imgs = driver.find_elements(By.CSS_SELECTOR, selector)
                urls = []
                for el in imgs:
                    src = (el.get_attribute("src") or "").strip()
                    if src:
                        urls.append(urljoin(base_url, src))
                if urls:
                    out["detail_image_list"] = urls
            except Exception:
                pass
        else:
            out[f"detail_{key}"] = _one_text(selector)

    return out


def main():
    if len(sys.argv) < 2:
        print("用法: python crawl_test.py <site_id>")
        print("示例: python crawl_test.py guess")
        sys.exit(1)
    site_id = sys.argv[1].strip()

    config = load_config()
    site = get_site_by_id(config, site_id)
    list_url = site["list_url"]
    base_url = get_base_url(list_url)
    timeout = config.get("global", {}).get("timeout_sec", 45)
    output_dir = config.get("global", {}).get("output_dir", "output")
    brandname = site.get("name", site_id)
    out_path = os.path.join(os.path.dirname(__file__), output_dir, f"{brandname}_test.json")

    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if site.get("force_us_locale"):
        options.add_argument("--lang=en-US")
        options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    proxy = (site.get("proxy") or "").strip()
    if proxy and (proxy.startswith("http://") or proxy.startswith("https://")):
        options.add_argument(f"--proxy-server={proxy}")

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(timeout)
        print(f"打开: {list_url}")
        driver.get(list_url)
        _human_like_delay(1.2, 2.2)
        dismiss_consent_if_any(driver)
        _human_like_delay(0.6, 1.2)
        print("分步滚动以触发懒加载...")
        _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4)
        _human_like_delay(0.6, 1.0)

        deals = parse_deals_from_page(driver, site, base_url, config)
        print(f"解析到 {len(deals)} 条 deal")

        deals = _enrich_guess_titles(driver, deals, site)
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
                    print(f"  [guess] 从 HTML 补全 {filled} 条价格")
        _save_debug_html(driver, site["id"])

        fetch_detail = site.get("fetch_detail")
        if fetch_detail is None:
            fetch_detail = config.get("global", {}).get("fetch_detail", False)
        detail_selectors = site.get("detail_selectors")
        if fetch_detail and detail_selectors and isinstance(detail_selectors, dict) and len(detail_selectors) > 0:
            print(f"进入详情页补全 {len([d for d in deals if d.get('deal_url')])} 条...")
            for i, deal in enumerate(deals):
                url = deal.get("deal_url")
                if not url:
                    continue
                try:
                    driver.get(url)
                    _human_like_delay(1.0, 2.0)
                    dismiss_consent_if_any(driver)
                    _human_like_delay(0.4, 0.8)
                    detail_base = get_base_url(url)
                    extra = parse_detail_page(driver, site, detail_base)
                    deal.update(extra)
                except Exception as e:
                    print(f"  详情页失败 [{i+1}] {url[:50]}...: {e}")
                _human_like_delay(0.5, 1.2)

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"site_id": site["id"], "count": len(deals), "deals": deals}, f, ensure_ascii=False, indent=2)
        print(f"已写入: {out_path}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
