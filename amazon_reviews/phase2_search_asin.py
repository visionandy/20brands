#!/usr/bin/env python3
"""
Phase 2: 在 Amazon 搜索，取第一个结果 ASIN
输入: data/products_for_amazon_search.json
输出: data/products_with_asins.json
"""

import json
import os
import re
import time
import random
import argparse
from urllib.parse import quote_plus

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# 配置
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "products_for_amazon_search.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "products_with_asins.json")
PROGRESS_FILE = os.path.join(DATA_DIR, "phase2_progress.json")

DELAY_BETWEEN_SEARCHES = (3, 6)
DELAY_AFTER_10 = (8, 15)
PAGE_LOAD_TIMEOUT = 15
ELEMENT_WAIT = 10

# ASIN 正则: 10位字母数字，常见格式 /dp/B0XXX 或 /gp/product/B0XXX
ASIN_PATTERN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")


def extract_asin_from_url(url):
    """从 URL 提取 ASIN"""
    if not url:
        return None
    m = ASIN_PATTERN.search(url)
    return m.group(1) if m else None


def setup_driver():
    """初始化浏览器"""
    if HAS_UC:
        options = uc.ChromeOptions()
        options.add_argument("--lang=en-US")
        options.add_argument("--window-size=1920,1080")
        driver = uc.Chrome(options=options)
    else:
        from selenium.webdriver.chrome.options import Options
        options = Options()
        options.add_argument("--lang=en-US")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


def search_amazon_get_asin(driver, search_term):
    """
    在 Amazon 搜索，返回第一个结果的 ASIN 和标题
    返回: (asin, amazon_title) 或 (None, None)
    """
    try:
        url = f"https://www.amazon.com/s?k={quote_plus(search_term)}"
        driver.get(url)
        time.sleep(random.uniform(2, 4))

        # 尝试多种选择器获取第一个商品链接
        selectors = [
            'div[data-component-type="s-search-result"] a.a-link-normal[href*="/dp/"]',
            'div[data-asin] a[href*="/dp/"]',
            'div.s-result-item[data-asin] a[href*="/dp/"]',
            'a.a-link-normal.s-no-outline[href*="/dp/"]',
        ]

        asin = None
        amazon_title = ""

        for sel in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els[:5]:  # 检查前5个
                    href = el.get_attribute("href") or ""
                    a = extract_asin_from_url(href)
                    if a and len(a) == 10:
                        asin = a
                        try:
                            # 尝试获取标题
                            title_el = el.find_element(By.CSS_SELECTOR, "h2 span, .a-text-normal")
                            amazon_title = (title_el.text or "").strip()[:200]
                        except Exception:
                            pass
                        break
                if asin:
                    break
            except Exception:
                continue

        # 备选：从 data-asin 属性提取
        if not asin:
            try:
                items = driver.find_elements(By.CSS_SELECTOR, 'div[data-asin]')
                for item in items:
                    aid = item.get_attribute("data-asin") or ""
                    if len(aid) == 10 and aid != "":
                        asin = aid
                        break
            except Exception:
                pass

        return (asin, amazon_title)

    except TimeoutException:
        return (None, None)
    except Exception as e:
        print(f"    错误: {e}")
        return (None, None)


def load_progress():
    """加载进度"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"done_indices": [], "results": []}


def save_progress(progress):
    """保存进度"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="限制处理商品数，0=全部")
    parser.add_argument("--resume", action="store_true", help="从上次进度继续")
    args = parser.parse_args()

    # 加载输入
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 未找到 {INPUT_FILE}，请先运行 phase1_extract.py")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = data.get("products", [])
    if not products:
        print("❌ 无商品数据")
        return

    if args.limit > 0:
        products = products[: args.limit]
        print(f"限制处理前 {args.limit} 个商品")

    progress = load_progress() if args.resume else {"done_indices": [], "results": []}
    done_set = set(progress["done_indices"])
    results = {r["idx"]: r for r in progress["results"]}
    if args.resume:
        print(f"从进度恢复，已完成 {len(done_set)} 个")

    print("初始化浏览器...")
    driver = setup_driver()

    success = 0
    fail = 0

    try:
        for idx, prod in enumerate(products):
            if idx in done_set and idx in results:
                continue

            brand = prod.get("brand", "")
            product_name = prod.get("product_name", "")
            search_term = prod.get("search_term", "")
            product_link = prod.get("product_link", "")

            print(f"  [{idx+1}/{len(products)}] {brand}: {product_name[:50]}...")

            asin, amazon_title = search_amazon_get_asin(driver, search_term)

            results[idx] = {
                "idx": idx,
                "brand": brand,
                "product_name": product_name,
                "product_link": product_link,
                "search_term": search_term,
                "asin": asin,
                "amazon_title": amazon_title,
            }
            done_set.add(idx)
            save_progress({"done_indices": list(done_set), "results": list(results.values())})

            if asin:
                success += 1
                print(f"    ✅ ASIN: {asin}")
            else:
                fail += 1
                print(f"    ❌ 未找到")

            # 延迟
            if (idx + 1) % 10 == 0:
                d = random.uniform(*DELAY_AFTER_10)
                print(f"    ⏸️ 每10商品延迟 {d:.1f}s")
                time.sleep(d)
            else:
                time.sleep(random.uniform(*DELAY_BETWEEN_SEARCHES))

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
    finally:
        driver.quit()

    # 写入最终结果
    out_list = [results[i] for i in range(len(products)) if i in results]
    out_data = {
        "total": len(out_list),
        "with_asin": sum(1 for r in out_list if r.get("asin")),
        "without_asin": sum(1 for r in out_list if not r.get("asin")),
        "products": sorted(out_list, key=lambda x: (x["brand"], x["idx"])),
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Phase 2 完成")
    print(f"   成功: {success}, 失败: {fail}")
    print(f"   输出: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
