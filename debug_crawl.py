#!/usr/bin/env python3
"""
自动调试爬虫：分析 output 与 html_samples，诊断失败原因并生成报告。
可单独运行分析，或带 --crawl 抓取失败站点获取新 debug HTML。

用法:
  python debug_crawl.py                    # 仅分析现有 output/html_samples
  python debug_crawl.py --crawl            # 分析 + 抓取失败站点（--limit 1）保存 debug
  python debug_crawl.py --crawl --limit 2  # 每个失败站抓 2 条
"""
import json
import os
import re
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
HTML_SAMPLES = ROOT / "config" / "html_samples"
CONFIG_PATH = ROOT / "config" / "sites_draft.json"

# 失败页面特征 -> 诊断类型（按优先级，先匹配先返回）
FAILURE_PATTERNS = [
    (r"<title>Access Denied</title>|Reference #18\.\w+", "ACCESS_DENIED", "Akamai/边缘 CDN 拦截，需 US 代理"),
    (r"HTTP 403|403 - Forbidden|security issue.*identified|unable to give you access", "HTTP_403", "WAF/安全拦截，需代理或换 IP"),
    (r"Just a moment|challenge-platform|cf-chl-widget|Your connection needs to be verified", "CLOUDFLARE", "Cloudflare 人机验证，需 undetected-chromedriver"),
    (r"<title>Calvin Klein.*Error</title>|Oops! Something went wrong|maintenance-page", "ERROR_PAGE", "站点错误/维护页"),
    (r"error-code|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_REFUSED", "CHROME_ERR", "Chrome 连接错误"),
    (r"data-country=\"SG\"|guess\.eu/en-sg|en-sg.*guess", "WRONG_REGION", "被重定向到非 US 区域，需代理"),
]


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _detect_page_failure(html: str) -> tuple:
    """返回 (failure_type, recommendation) 或 (None, None)"""
    if not html:
        return ("EMPTY_TINY", "页面为空")
    for pattern, ftype, rec in FAILURE_PATTERNS:
        if re.search(pattern, html, re.I):
            return (ftype, rec)
    if len(html) < 3000:
        return ("EMPTY_TINY", "页面过小，可能未加载或超时")
    return (None, None)


def _site_id_from_filename(name: str) -> str:
    """从 Michael Kors_test.json -> michaelkors, guess_debug.html -> guess"""
    base = name.replace("_test.json", "").replace("_debug.html", "").replace(".html", "")
    # 品牌名 -> id 映射
    name_to_id = {
        "Michael Kors": "michaelkors", "Calvin Klein": "calvinklein", "Coach": "coach",
        "Lululemon Athletica": "lululemon_athletica", "Alo Yoga": "alo_yoga",
        "J.Crew": "j_crew", "Tommy Hilfiger": "tommy_hilfiger", "Levi's": "levis",
        "H&M": "h_m", "Old Navy": "old_navy", "Columbia": "columbia", "Athleta": "athleta",
        "Guess": "guess", "Tory Burch": "tory_burch", "Tory Sport": "tory_sport",
        "Theory": "theory", "COS": "cos", "Nike": "nike", "Adidas": "adidas",
        "Target": "target",
    }
    for disp, sid in name_to_id.items():
        if disp.lower().replace(" ", "_") == base.lower().replace(" ", "_"):
            return sid
    return base.lower().replace(" ", "_").replace("'", "")


def analyze_output():
    """分析 output 下所有 *_test.json"""
    results = {}
    for path in OUTPUT_DIR.rglob("*_test.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            results[path.stem] = {"count": -1, "error": str(e), "status": "PARSE_ERR"}
            continue
        count = data.get("count", 0)
        site_id = data.get("site_id", path.stem)
        size_kb = path.stat().st_size / 1024
        status = "OK" if count > 0 else "EMPTY"
        results[path.stem] = {
            "site_id": site_id,
            "count": count,
            "size_kb": round(size_kb, 1),
            "status": status,
        }
    return results


def analyze_html_samples():
    """分析 html_samples 下 HTML，诊断失败类型"""
    results = {}
    for path in HTML_SAMPLES.glob("*.html"):
        if "_pdp" in path.name:
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            results[path.stem] = {"error": str(e)}
            continue
        ftype, rec = _detect_page_failure(html)
        site_id = path.stem.replace("_debug", "")
        results[path.stem] = {
            "site_id": site_id,
            "size": len(html),
            "failure_type": ftype,
            "recommendation": rec,
        }
    return results


def build_report(out_analysis, html_analysis, config):
    """生成调试报告"""
    site_ids = {s["id"] for s in config["sites"]}
    lines = [
        "# 20brands 爬虫调试报告",
        "",
        "## 1. Output 分析",
        "",
        "| 站点 | Deal 数 | 文件大小 | 状态 |",
        "|------|--------|----------|------|",
    ]
    out_by_site = {r.get("site_id", k): r for k, r in out_analysis.items()}
    for site in config["sites"]:
        sid = site["id"]
        r = out_by_site.get(sid) or out_by_site.get(site.get("name", "").replace(" ", "_"))
        if not r:
            lines.append(f"| {site.get('name', sid)} | - | - | 未抓取 |")
        else:
            status = "✅" if r["status"] == "OK" else "❌"
            lines.append(f"| {site.get('name', sid)} | {r['count']} | {r.get('size_kb', 0)}KB | {status} |")

    lines.extend([
        "",
        "## 2. HTML 样本诊断（失败原因）",
        "",
        "| 站点 | 页面大小 | 失败类型 | 建议 |",
        "|------|----------|----------|------|",
    ])
    html_by_site = {}
    for name, r in html_analysis.items():
        sid = (r.get("site_id") or name).replace("_debug", "").replace("_pdp", "")
        if sid not in html_by_site or (r.get("failure_type") and not html_by_site[sid].get("failure_type")):
            html_by_site[sid] = r
    for site in config["sites"]:
        sid = site["id"]
        r = html_by_site.get(sid)
        if not r:
            lines.append(f"| {site.get('name', sid)} | - | - | 无样本 |")
        elif r.get("failure_type"):
            lines.append(f"| {site.get('name', sid)} | {r.get('size', 0)} 字符 | {r['failure_type']} | {r.get('recommendation', '')} |")
        else:
            lines.append(f"| {site.get('name', sid)} | {r.get('size', 0)} 字符 | ✅ 正常 | - |")

    lines.extend([
        "",
        "## 3. 失败站点汇总与建议",
        "",
    ])
    failed = []
    for site in config["sites"]:
        sid = site["id"]
        out_r = out_by_site.get(sid)
        html_r = html_by_site.get(sid)
        count = (out_r or {}).get("count", -1)
        ftype = (html_r or {}).get("failure_type")
        if count == 0 or ftype:
            rec = (html_r or {}).get("recommendation") if ftype else "0 deal，需检查选择器或重跑抓取"
            failed.append({
                "name": site.get("name", sid),
                "id": sid,
                "count": count,
                "failure_type": ftype or "EMPTY",
                "rec": rec,
            })
    if failed:
        for f in failed:
            lines.append(f"- **{f['name']}** ({f['id']}): {f.get('failure_type', '0 deal')} — {f['rec']}")
    else:
        lines.append("无失败站点。")
    lines.extend([
        "",
        "## 4. 配置建议",
        "",
        "对于 ACCESS_DENIED / HTTP_403 站点，需在 config/sites_draft.json 中配置 US 代理：",
        "```json",
        '"proxy": "http://your-us-proxy:port"',
        "```",
        "",
        "对于 CLOUDFLARE 站点，可尝试启用 undetected-chromedriver：",
        "```json",
        '"anti_detect": { "use_undetected": true }',
        "```",
        "",
        "对于 WRONG_REGION，确保 force_us_locale: true，或使用 US 代理。",
        "",
    ])
    return "\n".join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description="20brands 爬虫自动调试")
    p.add_argument("--crawl", action="store_true", help="抓取失败站点获取新 debug HTML")
    p.add_argument("--limit", type=int, default=1, help="--crawl 时每站抓取条数")
    p.add_argument("--output", "-o", type=str, default=None, help="报告输出路径")
    args = p.parse_args()

    config = _load_config()
    out_analysis = analyze_output()
    html_analysis = analyze_html_samples()
    report = build_report(out_analysis, html_analysis, config)

    out_path = Path(args.output) if args.output else ROOT / "output" / "debug_report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"报告已写入: {out_path}")
    print()
    print(report)

    if args.crawl:
        failed_ids = []
        for site in config["sites"]:
            sid = site["id"]
            r = out_analysis.get(sid)
            if r is None or r.get("count", 0) == 0:
                failed_ids.append(sid)
        if failed_ids:
            print(f"\n抓取失败站点: {failed_ids}")
            cmd = [sys.executable, str(ROOT / "crawl_test.py"), "--all", "--limit", str(args.limit)]
            for fid in failed_ids:
                print(f"  运行: python crawl_test.py {fid} --limit {args.limit}")
            # 实际执行：逐个抓取失败站
            for fid in failed_ids:
                import subprocess
                r = subprocess.run(
                    [sys.executable, str(ROOT / "crawl_test.py"), fid, "--limit", str(args.limit)],
                    cwd=str(ROOT),
                    capture_output=True,
                    timeout=120,
                    encoding="utf-8",
                    errors="replace",
                )
                if r.returncode != 0:
                    print(f"  [{fid}] 失败: {r.stderr[:200] if r.stderr else r.stdout[:200]}")
                else:
                    print(f"  [{fid}] 完成")
            print("\n请重新运行 python debug_crawl.py 查看更新后的报告。")
        else:
            print("\n无失败站点，无需抓取。")


if __name__ == "__main__":
    main()
