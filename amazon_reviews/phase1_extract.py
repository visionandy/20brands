#!/usr/bin/env python3
"""
Phase 1: 从 20brands output 提取商品列表，构造 Amazon 搜索词
输出: data/products_for_amazon_search.json
"""

import json
import os
import re

# 配置
OUTPUT_DIR = "output"
DATA_DIR = "data"
PRODUCTS_PER_BRAND = 25
MAX_SEARCH_TERM_LEN = 100  # 搜索词最大长度

# Brand (site_id) -> Amazon 搜索用品牌名
BRAND_AMAZON_MAP = {
    "nike": "Nike",
    "adidas": "Adidas",
    "michaelkors": "Michael Kors",
    "calvinklein": "Calvin Klein",
    "coach": "Coach",
    "lululemon_athletica": "Lululemon",
    "alo_yoga": "Alo Yoga",
    "j_crew": "J.Crew",
    "tommy_hilfiger": "Tommy Hilfiger",
    "old_navy": "Old Navy",
    "h_m": "H&M",
    "target": "Target",
    "columbia": "Columbia",
    "athleta": "Athleta",
    "guess": "Guess",
    "tory_burch": "Tory Burch",
    "tory_sport": "Tory Sport",
    "theory": "Theory",
    "cos": "COS",
    "levis": "Levi's",
    "site_01": "Michael Kors",
    "site_07": "Tommy Hilfiger",
    "site_13": "Target",
}


def clean_search_term(text):
    """清洗搜索词：去掉特殊符号、过长截断"""
    if not text or not isinstance(text, str):
        return ""
    # 去掉换行、多余空格
    s = re.sub(r"\s+", " ", text.strip())
    # 去掉可能影响搜索的字符（保留基本标点）
    s = re.sub(r'[<>"\\|]', "", s)
    s = s.strip()
    if len(s) > MAX_SEARCH_TERM_LEN:
        s = s[:MAX_SEARCH_TERM_LEN].rsplit(" ", 1)[0]  # 按词截断
    return s


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_dir = os.path.join(base_dir, OUTPUT_DIR)
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)

    all_products = []
    brand_stats = {}

    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith("_test.json"):
            continue
        filepath = os.path.join(output_dir, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"跳过 {fname}: {e}")
            continue

        site_id = data.get("site_id", "")
        deals = data.get("deals", [])
        brand_amazon = BRAND_AMAZON_MAP.get(site_id, site_id.replace("_", " ").title())

        # 取前 N 个
        taken = 0
        for deal in deals:
            if taken >= PRODUCTS_PER_BRAND:
                break
            product_name = deal.get("Product_name") or ""
            product_link = deal.get("Product_link") or ""
            brand_raw = deal.get("Brand") or site_id

            if not product_name.strip():
                continue

            search_term = f"{brand_amazon} {clean_search_term(product_name)}"
            if not search_term.strip():
                continue

            all_products.append({
                "brand": brand_amazon,
                "brand_raw": brand_raw,
                "product_name": product_name,
                "product_link": product_link,
                "search_term": search_term,
            })
            taken += 1

        brand_stats[brand_amazon] = taken
        print(f"  {brand_amazon}: {taken} 商品")

    out_path = os.path.join(data_dir, "products_for_amazon_search.json")
    out_data = {
        "total_products": len(all_products),
        "total_brands": len(brand_stats),
        "products_per_brand_limit": PRODUCTS_PER_BRAND,
        "brand_stats": brand_stats,
        "products": all_products,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Phase 1 完成")
    print(f"   总商品: {len(all_products)}")
    print(f"   总品牌: {len(brand_stats)}")
    print(f"   输出: {out_path}")


if __name__ == "__main__":
    main()
