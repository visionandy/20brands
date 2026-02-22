#!/usr/bin/env python3
"""
Phase 3: 爬取 Amazon 商品评论，每个商品取 3 条
输入: data/products_with_asins.json
输出: data/products_with_reviews.json
"""

import json
import os
import re
import time
import random
import argparse

try:
    import undetected_chromedriver as uc
    HAS_UC = True
except ImportError:
    HAS_UC = False

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# 配置
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "products_with_asins.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "products_with_reviews.json")
PROGRESS_FILE = os.path.join(DATA_DIR, "phase3_progress.json")

REVIEWS_PER_PRODUCT = 3
DELAY_BETWEEN_PRODUCTS = (4, 8)
DELAY_AFTER_10 = (10, 18)
PAGE_LOAD_TIMEOUT = 20
ELEMENT_WAIT = 12


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


def _parse_reviews_from_containers(driver, containers, max_reviews):
    """从评论容器列表解析评论"""
    reviews = []
    for container in containers:
        if len(reviews) >= max_reviews:
            break
        r = {"title": "", "body": "", "rating": "", "date": "", "author": ""}
        for sel in ['[data-hook="review-title"]', 'a.review-title', 'span[data-hook="review-title"]']:
            try:
                el = container.find_element(By.CSS_SELECTOR, sel)
                r["title"] = (el.text or "").strip()
                if r["title"]:
                    break
            except Exception:
                pass
        for sel in ['[data-hook="review-body"] span', 'div[data-hook="review-body"]', 'span.review-text', 'div.a-row.review-data span.review-text', '.review-text-content span']:
            try:
                el = container.find_element(By.CSS_SELECTOR, sel)
                r["body"] = (el.text or "").strip()
                if r["body"]:
                    break
            except Exception:
                pass
        for sel in ['i[data-hook="review-star-rating"]', 'span[data-hook="review-star-rating"]', 'span.a-icon-alt', 'a.a-link-normal[title*="out of"]']:
            try:
                el = container.find_element(By.CSS_SELECTOR, sel)
                txt = el.get_attribute("textContent") or el.get_attribute("title") or el.text or ""
                r["rating"] = txt.strip()[:50]
                if r["rating"]:
                    break
            except Exception:
                pass
        for sel in ['[data-hook="review-date"]', 'span.a-size-base.a-color-secondary.review-date', 'span[data-hook="review-date"]']:
            try:
                el = container.find_element(By.CSS_SELECTOR, sel)
                r["date"] = (el.text or "").strip()
                if r["date"]:
                    break
            except Exception:
                pass
        for sel in ['span.a-profile-name', '[data-hook="review-author"]']:
            try:
                el = container.find_element(By.CSS_SELECTOR, sel)
                r["author"] = (el.text or "").strip()
                if r["author"]:
                    break
            except Exception:
                pass
        if r["body"] or r["title"]:
            reviews.append(r)
    return reviews


def extract_reviews(driver, asin, max_reviews=3, debug=False):
    """
    访问 Amazon 评论页或商品页，提取前 max_reviews 条评论
    返回: list of dict {title, body, rating, date, author}
    """
    reviews = []
    urls_to_try = [
        f"https://www.amazon.com/product-reviews/{asin}",
        f"https://www.amazon.com/dp/{asin}#customerReviews",
    ]
    for url in urls_to_try:
        try:
            driver.get(url)
            time.sleep(random.uniform(3, 6))
            if "#customerReviews" in url:
                try:
                    driver.execute_script("document.getElementById('customerReviews')?.scrollIntoView();")
                    time.sleep(2)
                except Exception:
                    pass

            review_containers = []
            for sel in [
                'div[data-hook="review"]',
                'div.a-section.review',
                'div.a-section.celwidget[data-cel-widget*="review"]',
                'div[data-hook="review-collapsed"]',
                'div.review-views',
            ]:
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els:
                        review_containers = els[:max_reviews * 3]
                        break
                except Exception:
                    continue

            reviews = _parse_reviews_from_containers(driver, review_containers, max_reviews)
            if reviews:
                break

            if debug and not reviews:
                debug_path = os.path.join(DATA_DIR, f"debug_{asin}.html")
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                print(f"    [debug] 已保存页面到 {debug_path}")
        except TimeoutException:
            continue
        except Exception:
            continue

    return reviews[:max_reviews]




def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"done_indices": [], "results": []}


def save_progress(progress):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="限制处理商品数，0=全部")
    parser.add_argument("--resume", action="store_true", help="从上次进度继续")
    parser.add_argument("--reviews", type=int, default=3, help="每商品评论数，默认3")
    parser.add_argument("--debug", action="store_true", help="未获取到评论时保存页面 HTML")
    args = parser.parse_args()

    max_reviews = max(1, min(args.reviews, 10))

    if not os.path.exists(INPUT_FILE):
        print(f"❌ 未找到 {INPUT_FILE}，请先运行 phase2_search_asin.py")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = [p for p in data.get("products", []) if p.get("asin")]
    if not products:
        print("❌ 无带 ASIN 的商品")
        return

    if args.limit > 0:
        products = products[: args.limit]
        print(f"限制处理前 {args.limit} 个商品")

    progress = load_progress() if args.resume else {"done_indices": [], "results": []}
    done_set = set(progress["done_indices"])
    results = {r["idx"]: r for r in progress["results"]}
    if args.resume:
        print(f"从进度恢复，已完成 {len(done_set)} 个")

    print(f"初始化浏览器... 每商品爬取 {max_reviews} 条评论")
    driver = setup_driver()

    success = 0
    fail = 0

    try:
        for i, prod in enumerate(products):
            idx = prod.get("idx", i)
            if idx in done_set and idx in results:
                continue

            asin = prod.get("asin", "")
            brand = prod.get("brand", "")
            product_name = prod.get("product_name", "")

            print(f"  [{i+1}/{len(products)}] {brand}: {product_name[:40]}... ASIN={asin}")

            reviews = extract_reviews(driver, asin, max_reviews=max_reviews, debug=args.debug)

            results[idx] = {
                **prod,
                "reviews": reviews,
                "review_count": len(reviews),
            }
            done_set.add(idx)
            save_progress({"done_indices": list(done_set), "results": list(results.values())})

            if reviews:
                success += 1
                print(f"    ✅ 获取 {len(reviews)} 条评论")
            else:
                fail += 1
                print(f"    ❌ 未获取到评论")

            if (idx + 1) % 10 == 0:
                d = random.uniform(*DELAY_AFTER_10)
                print(f"    ⏸️ 每10商品延迟 {d:.1f}s")
                time.sleep(d)
            else:
                time.sleep(random.uniform(*DELAY_BETWEEN_PRODUCTS))

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
    finally:
        driver.quit()

    out_list = sorted(results.values(), key=lambda x: (x.get("brand", ""), x.get("idx", 0)))
    out_data = {
        "total": len(out_list),
        "with_reviews": sum(1 for r in out_list if r.get("review_count", 0) > 0),
        "without_reviews": sum(1 for r in out_list if r.get("review_count", 0) == 0),
        "products": out_list,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Phase 3 完成")
    print(f"   成功: {success}, 失败: {fail}")
    print(f"   输出: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
