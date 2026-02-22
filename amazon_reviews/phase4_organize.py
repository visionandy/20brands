#!/usr/bin/env python3
"""
Phase 4: 数据组织与输出
输入: data/products_with_reviews.json
输出: 
  - data/by_brand/  (按品牌分目录)
  - data/reviews_summary.json (汇总)
"""

import json
import os
import argparse

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "products_with_reviews.json")
OUTPUT_DIR = os.path.join(DATA_DIR, "by_brand")
SUMMARY_FILE = os.path.join(DATA_DIR, "reviews_summary.json")


def sanitize_filename(name):
    """品牌名转安全文件名"""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=INPUT_FILE, help="Phase 3 输出文件")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"❌ 未找到 {args.input}，请先运行 phase3_reviews.py")
        return

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    products = data.get("products", [])
    if not products:
        print("❌ 无商品数据")
        return

    # 按品牌分组
    by_brand = {}
    for p in products:
        brand = p.get("brand", "Unknown")
        if brand not in by_brand:
            by_brand[brand] = []
        by_brand[brand].append(p)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    summary = {
        "total_products": len(products),
        "total_brands": len(by_brand),
        "total_reviews": sum(p.get("review_count", 0) for p in products),
        "brands": {},
        "files": [],
    }

    for brand, prods in sorted(by_brand.items()):
        safe_name = sanitize_filename(brand)
        out_path = os.path.join(OUTPUT_DIR, f"{safe_name}.json")

        brand_data = {
            "brand": brand,
            "product_count": len(prods),
            "review_count": sum(p.get("review_count", 0) for p in prods),
            "products": prods,
        }

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(brand_data, f, indent=2, ensure_ascii=False)

        summary["brands"][brand] = {
            "product_count": len(prods),
            "review_count": brand_data["review_count"],
            "file": f"by_brand/{safe_name}.json",
        }
        summary["files"].append(out_path)
        print(f"  ✅ {brand}: {len(prods)} 商品, {brand_data['review_count']} 条评论 -> {out_path}")

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Phase 4 完成")
    print(f"   品牌数: {summary['total_brands']}")
    print(f"   商品数: {summary['total_products']}")
    print(f"   评论总数: {summary['total_reviews']}")
    print(f"   汇总: {SUMMARY_FILE}")
    print(f"   按品牌: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
