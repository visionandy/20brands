#!/usr/bin/env python3
"""
按 site_id 爬取单个站点的 deal 列表页，验证 selectors 是否有效。
人类化行为对齐 google_search_product.py：分步滚动、随机延时、每卡 scrollIntoView、Stale 跳过。
用法: python crawl_test.py <site_id>
示例: python crawl_test.py site_02
结果写入 output/<brandname>_test.json（如 output/calvinklein_test.json）。
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

# 多币种价格提取：从 "Price reduced from\nMYR 949.00\nto" 等文案中只保留第一处「货币+金额」
_PRICE_PATTERN = re.compile(
    r"(?:MYR|RM|USD|SGD|EUR|GBP|\$|€|£)\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?"
)


def _normalize_price_text(raw):
    """从原始价格文案中提取第一处货币+金额，兼容多站点多币种；无匹配则返回原串 strip。"""
    if not raw:
        return ""
    s = raw.strip()
    if not s:
        return s
    m = _PRICE_PATTERN.search(s)
    return m.group(0).strip() if m else s


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


def parse_deals_from_page(driver, site, base_url):
    """
    在当前页面用 site 的 selectors 解析所有 deal。
    每个卡片先 scrollIntoView(center) + 随机延时，再取值，以触发懒加载；Stale 时跳过。
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
                deal["title"] = _text(sel["title"])
            if sel.get("current_price"):
                deal["current_price"] = _normalize_price_text(_text(sel["current_price"]))
            if sel.get("original_price"):
                deal["original_price"] = _normalize_price_text(_text(sel["original_price"]))
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
                    url = (img_el.get_attribute("src") or img_el.get_attribute("data-src") or "").strip()
                    # 过滤掉懒加载占位图（如 data:image 或 1x1 透明图）
                    if url and not url.startswith("data:") and "1x1" not in url:
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
        print("示例: python crawl_test.py site_02")
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

        deals = parse_deals_from_page(driver, site, base_url)
        print(f"解析到 {len(deals)} 条 deal")

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
