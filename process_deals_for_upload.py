#!/usr/bin/env python3
"""
将 20brands 爬取的 deal JSON 处理成 mysmartshop.net Product 格式。
- 输出格式兼容 https://mysmartshop.net/admin/store/product/ 的 Product 模型
- 使用 GPT 进行类别分类（fashion taxonomy，与 ecom deal_aggregator 一致）
- 可输出 JSON 供 import_20brands_products 管理命令导入

用法:
  python process_deals_for_upload.py                           # 处理 output/YYYY_MM_DD/
  python process_deals_for_upload.py --date-dir 2026_02_22 --gpt-rewrite --merge all_products.json
  python process_deals_for_upload.py output/2026_02_22/Guess_test.json
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _today_date_dir() -> str:
    """今日日期目录名，格式 2026_02_22，对齐 deal_crawler。"""
    return datetime.now().strftime("%Y_%m_%d").replace("-", "_")

# Load .env for OPENAI_API_KEY (check 20brands, ecom, deal_crawler)
try:
    from dotenv import load_dotenv
    base = Path(__file__).resolve().parent
    load_dotenv(base / ".env")
    load_dotenv(base.parent / "ecom" / "ecom" / ".env")
    load_dotenv(base.parent / "deal_crawler" / ".env")
except ImportError:
    pass

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

# Fashion taxonomy (与 ecom deal_aggregator 一致)
CATEGORY_TAXONOMY = [
    "Dresses",
    "Tops & Blouses",
    "Bottoms",
    "Outerwear",
    "Shoes",
    "Accessories",
    "Bags & Handbags",
    "Jewelry",
    "Activewear",
    "Lingerie & Intimates",
    "Swimwear",
    "Beauty & Cosmetics",
    "Other",
]

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
PROCESSED_DIR = Path(__file__).resolve().parent / "output" / "processed"


def _resolve_date_dir(date_dir: Optional[str] = None) -> Path:
    """解析日期目录路径 output/YYYY_MM_DD。"""
    name = date_dir or _today_date_dir()
    return OUTPUT_DIR / name


def _ensure_product_id(raw: Dict[str, Any]) -> str:
    """生成唯一 productID（mysmartshop Product 必填，max 50）。"""
    link = (raw.get("Product_link") or raw.get("deal_url") or "").strip()
    if link:
        return ("url_" + hashlib.sha256(link.encode()).hexdigest()[:16])[:50]
    name = (raw.get("Product_name") or raw.get("title") or "").strip()
    brand = (raw.get("Brand") or raw.get("source_site") or "").strip()
    raw_key = f"{brand}|{name}"
    return ("h_" + hashlib.sha256(raw_key.encode()).hexdigest()[:16])[:50]


def _parse_price(val: Any) -> Optional[float]:
    """从字符串或数字解析价格。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (ValueError, TypeError):
            return None
    s = re.sub(r"[^\d.]", "", str(val).strip())
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _is_valid_http_url(url: str) -> bool:
    u = (url or "").strip()
    return bool(u and u.startswith(("http://", "https://")))


def _collect_image_urls(raw: Dict[str, Any]) -> List[str]:
    """收集所有产品图片 URL：image_url + detail_image_list，去重且仅保留有效 http(s)。"""
    main = (raw.get("image_url") or "").strip()
    detail_list = raw.get("detail_image_list") or raw.get("image_list_deals") or []
    if not isinstance(detail_list, list):
        detail_list = []
    seen = set()
    urls: List[str] = []
    for u in ([main] if _is_valid_http_url(main) else []) + detail_list:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if _is_valid_http_url(u) and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


# site_id / 旧 Brand 值 -> 正确品牌显示名（兼容旧数据及 id 格式）
SITE_ID_TO_BRAND = {
    "site_01": "Michael Kors",
    "site_07": "Tommy Hilfiger",
    "site_13": "Target",
    "michaelkors": "Michael Kors",
    "calvinklein": "Calvin Klein",
    "coach": "Coach",
    "lululemon_athletica": "Lululemon Athletica",
    "alo_yoga": "Alo Yoga",
    "j_crew": "J.Crew",
    "tommy_hilfiger": "Tommy Hilfiger",
    "nike": "Nike",
    "adidas": "Adidas",
    "levis": "Levi's",
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
}


def _deal_to_product_format(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 20brands 单条 deal 转为 mysmartshop Product 格式。
    输入字段：Product_name, Product_link, current_price, original_price, image_url, detail_image_list, Brand, ...
    """
    name = raw.get("Product_name") or raw.get("title") or ""
    link = raw.get("Product_link") or raw.get("deal_url") or ""
    image_url = raw.get("image_url") or ""
    brand = raw.get("Brand") or raw.get("source_site") or ""
    if brand in SITE_ID_TO_BRAND:
        brand = SITE_ID_TO_BRAND[brand]
    elif brand and re.match(r"^site_\d+$", brand):
        brand = SITE_ID_TO_BRAND.get(brand, brand.replace("_", " ").title())
    current_price = _parse_price(raw.get("current_price"))
    original_price = _parse_price(raw.get("original_price"))
    image_urls = _collect_image_urls(raw)

    sale_price = current_price if current_price is not None else 0.0
    regular_price = original_price if original_price is not None else (current_price or sale_price or 0.0)

    return {
        "product_name": name,
        "productID": _ensure_product_id(raw),
        "product_link": link,
        "sale_price": sale_price,
        "regular_price": regular_price,
        "image_url": image_url,
        "image_urls": image_urls,
        "brand": brand,
        "category": raw.get("category") or "Other",
        "product_description": raw.get("product_description") or "",
        "seo_description": raw.get("seo_description") or "",
        "rating": raw.get("rating", 0),
        "rating_n": raw.get("rating_n", 0),
    }


def _is_valid_for_upload(item: Dict[str, Any]) -> bool:
    """校验：product_link 和 image_url 必须为有效 http(s) URL。"""
    link = item.get("product_link") or ""
    img = item.get("image_url") or ""
    return _is_valid_http_url(link) and _is_valid_http_url(img)


def gpt_classify_product(
    item: Dict[str, Any],
    model: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
) -> bool:
    """
    使用 GPT 对产品进行类别分类。成功时原地修改 item 的 category、brand、product_description。
    """
    if not _OPENAI_AVAILABLE:
        return False
    key = (api_key or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        return False

    name = (item.get("product_name") or "").strip()
    brand = (item.get("brand") or "").strip()
    desc = (item.get("product_description") or "").strip()[:500]

    if not name:
        return False

    categories_str = ", ".join(CATEGORY_TAXONOMY)
    prompt = f"""You are a fashion product expert. For each product output:
1. category - one from the list
2. brand - real brand (not retailer)
3. product_description - 1-2 sentences, key features/style
4. seo_description - meta description for Google & customers (max 160 chars). MUST be compelling: include product name, brand, key benefit, urgency/value. Write like a viral hit - grab attention, drive clicks. NEVER leave empty.

Product Name: {name}
Brand (current): {brand}
Existing description: {desc or 'None'}

Categories: {categories_str}

Respond ONLY with valid JSON:
{{
  "category": "Exact category from list",
  "brand": "Real brand name",
  "product_description": "1-2 sentence product description. NEVER empty.",
  "seo_description": "Compelling meta description ≤160 chars. Include name, brand, benefit. Must attract Google search & customers. Viral/hit style."
}}

Output ONLY valid JSON, no markdown."""

    try:
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You output only valid JSON with the exact keys requested. No markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            cat = (data.get("category") or "").strip()
            if cat in CATEGORY_TAXONOMY:
                item["category"] = cat
            else:
                cat_lower = cat.lower()
                for c in CATEGORY_TAXONOMY:
                    if c.lower() == cat_lower:
                        item["category"] = c
                        break
                else:
                    item["category"] = "Other"
            if data.get("brand"):
                item["brand"] = (data.get("brand") or "").strip()
            desc = (data.get("product_description") or "").strip()
            if not desc:
                b = item.get("brand") or "this brand"
                c = item.get("category") or "fashion"
                desc = f"Stylish {c} from {b}. {name}."
            item["product_description"] = desc[:500]
            seo = (data.get("seo_description") or "").strip()
            if seo:
                item["seo_description"] = seo[:160]
            elif name and item.get("brand"):
                item["seo_description"] = f"{name} by {item['brand']} - Shop now. Limited time offer."[:160]
            else:
                item["seo_description"] = f"{name} - Best deal. Shop now."[:160]
            return True
    except Exception as e:
        print(f"[GPT] {e}", flush=True)
    return False


def load_deals_from_json(path: Path) -> List[Dict[str, Any]]:
    """从 JSON 加载 deal 列表。支持 {'deals': [...]} 或 {'products': [...]}。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "deals" in data:
            return data["deals"]
        if "products" in data:
            return data["products"]
    return []


def process_file(
    input_path: Path,
    output_path: Optional[Path] = None,
    *,
    gpt_rewrite: bool = False,
    gpt_model: str = "gpt-4o-mini",
) -> List[Dict[str, Any]]:
    """
    处理单个 JSON 文件，写出为 mysmartshop Product 格式。
    若 gpt_rewrite=True，使用 GPT 分类。
    """
    products = load_deals_from_json(input_path)
    converted = [_deal_to_product_format(p) for p in products]
    valid = [x for x in converted if _is_valid_for_upload(x)]
    skipped = len(converted) - len(valid)
    if skipped > 0:
        print(f"  [skip] {skipped} items missing valid product_link or image_url")
    converted = valid

    if gpt_rewrite and converted:
        if not _OPENAI_AVAILABLE:
            print("  [GPT] openai not installed, skip classification")
        elif not os.getenv("OPENAI_API_KEY"):
            print("  [GPT] OPENAI_API_KEY not set, skip classification")
        else:
            for i, p in enumerate(converted):
                ok = gpt_classify_product(p, model=gpt_model)
                if (i + 1) % 5 == 0 or i == 0:
                    print(f"  [GPT] classified {i + 1}/{len(converted)}" + (" (ok)" if ok else " (fallback)"))
                time.sleep(0.3)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        out_data = {"products": converted}
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False, indent=2)
        print(f"  Written: {output_path} ({len(converted)} products)")
    return converted


def process_output_dir(
    input_dir: Path = OUTPUT_DIR,
    processed_dir: Path = PROCESSED_DIR,
    glob_pattern: str = "*_test.json",
    gpt_rewrite: bool = False,
    merge_output: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    处理 input_dir 下所有匹配的 JSON，合并或分别写入 processed_dir。
    merge_output: 合并输出文件名，如 "all_products.json"
    """
    input_dir = Path(input_dir)
    processed_dir = Path(processed_dir)
    processed_dir.mkdir(parents=True, exist_ok=True)

    all_products: List[Dict[str, Any]] = []
    files = list(input_dir.glob(glob_pattern))

    for fp in files:
        if not fp.is_file():
            continue
        print(f"\nProcessing: {fp.name}")
        out_path = processed_dir / fp.name.replace("_test.json", "_products.json") if not merge_output else None
        items = process_file(fp, out_path, gpt_rewrite=gpt_rewrite)
        all_products.extend(items)

    if merge_output and all_products:
        merge_path = processed_dir / merge_output
        with open(merge_path, "w", encoding="utf-8") as f:
            json.dump({"products": all_products}, f, ensure_ascii=False, indent=2)
        print(f"\nMerged: {merge_path} ({len(all_products)} products)")
    return all_products


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Process 20brands deals for mysmartshop upload")
    parser.add_argument("input", nargs="?", default=None, help="Input file or directory (default: output/YYYY_MM_DD)")
    parser.add_argument("--date-dir", type=str, default=None, metavar="DIR", help="Date dir e.g. 2026_02_22 (default: today)")
    parser.add_argument("--output", "-o", type=str, help="Output JSON path")
    parser.add_argument("--gpt-rewrite", action="store_true", help="Use GPT for category classification")
    parser.add_argument("--merge", type=str, default=None, metavar="NAME", help="Merge all into one file (e.g. all_products.json)")
    args = parser.parse_args()

    date_dir_path = _resolve_date_dir(args.date_dir)
    default_input = date_dir_path

    input_path = Path(args.input) if args.input else default_input
    if not input_path.exists():
        print(f"Error: {input_path} not found")
        sys.exit(1)

    if input_path.is_file():
        out_dir = Path(args.output).parent if args.output else (input_path.parent / "processed")
        output_path = Path(args.output) if args.output else out_dir / (input_path.stem + "_products.json")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        process_file(input_path, output_path, gpt_rewrite=args.gpt_rewrite)
    else:
        out_processed = input_path / "processed"
        process_output_dir(
            input_dir=input_path,
            processed_dir=out_processed,
            gpt_rewrite=args.gpt_rewrite,
            merge_output=args.merge or (args.output.split("/")[-1] if args.output else None),
        )


if __name__ == "__main__":
    main()
