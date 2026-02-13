#!/usr/bin/env python3
"""
按站点 id 抓取列表页 HTML 到 config/html_samples/<site_id>.html。
复用 crawl_test 的 dismiss_consent、分步滚动策略，确保懒加载内容完整。
用法: python fetch_html_samples.py [site_id]
示例: python fetch_html_samples.py guess
不传 site_id 则抓取所有有 list_url 的站点。
"""
import json
import os
import random
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def _human_like_delay(min_sec=0.8, max_sec=2.2):
    time.sleep(random.uniform(min_sec, max_sec))


def _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4):
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


def dismiss_consent_if_any(driver):
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


def fetch_one(driver, site, output_dir, timeout=45):
    list_url = site["list_url"]
    site_id = site["id"]
    fetch_opts = site.get("fetch_html") or {}
    wait_sel = fetch_opts.get("wait_for_selector")
    scroll_before = fetch_opts.get("scroll_before_save", True)
    dismiss = fetch_opts.get("dismiss_consent", True)

    print(f"[{site_id}] 打开: {list_url}")
    driver.get(list_url)
    _human_like_delay(1.2, 2.2)

    if dismiss:
        dismiss_consent_if_any(driver)
        _human_like_delay(0.6, 1.2)

    if wait_sel:
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_sel))
            )
            print(f"[{site_id}] 已等待元素: {wait_sel}")
        except Exception as e:
            print(f"[{site_id}] 等待 {wait_sel} 超时，继续: {e}")

    if scroll_before:
        print(f"[{site_id}] 分步滚动...")
        _scroll_page_to_load_all(driver, base_pause=(0.9, 1.8), max_rounds=50, no_change_stop=4)
        _human_like_delay(0.6, 1.0)

    html = driver.page_source or ""
    out_path = os.path.join(output_dir, f"{site_id}.html")
    os.makedirs(output_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    size_kb = len(html) / 1024
    print(f"[{site_id}] 已保存: {out_path} ({size_kb:.1f} KB)")


def main():
    site_id = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config()
    output_dir = os.path.join(os.path.dirname(__file__), "config", "html_samples")

    if site_id:
        site = get_site_by_id(config, site_id)
        sites = [site]
    else:
        sites = [s for s in config["sites"] if s.get("list_url")]

    timeout = config.get("global", {}).get("timeout_sec", 45)
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    need_us = any(s.get("force_us_locale") for s in sites)
    if need_us:
        options.add_argument("--lang=en-US")
        options.add_experimental_option("prefs", {"intl.accept_languages": "en-US,en"})
    proxy = (sites[0].get("proxy") or "").strip() if sites else ""
    if proxy and (proxy.startswith("http://") or proxy.startswith("https://")):
        options.add_argument(f"--proxy-server={proxy}")

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(timeout)
        for site in sites:
            try:
                fetch_one(driver, site, output_dir, timeout)
            except Exception as e:
                print(f"[{site['id']}] 失败: {e}")
            if len(sites) > 1:
                _human_like_delay(2, 4)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
