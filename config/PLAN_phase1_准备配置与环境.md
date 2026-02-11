# 阶段一：准备配置与环境 — 展开说明

本文件对应总方案中的**阶段一**，把每一步拆开说明要改哪个文件、填什么、怎么检查。

---

## 步骤 1.1：定好 20 个 deal 站点

**目标**：在配置里明确「爬哪 20 个站」以及每个站的列表页地址。

**操作文件**：`config/sites_draft.json`

**要填内容**（对 `sites` 数组里每一个元素）：

| 字段       | 说明 |
|------------|------|
| `id`       | 站点唯一标识，建议固定格式如 `site_01` … `site_20`，不要重复。输出里用 `source_site` 会带这个值。 |
| `name`     | 站点显示名，方便人看（如 "SlickDeals"、"DealNews"）。可随意起，不必和 URL 一致。 |
| `list_url` | 该站的 **deal 列表页** 的完整 URL。爬虫只会先访问这一页；若以后要做分页，再在这里或同站下扩展。 |

**示例（单个站点）**：

```json
{
  "id": "site_01",
  "name": "SlickDeals 首页",
  "list_url": "https://slickdeals.net/deals/",
  "notes": "",
  "selectors": { ... }
}
```

**注意**：

- 若某站暂时没有定好列表页，`list_url` 可先留空 `""`；实现时应对空 URL 做「跳过该站」处理。
- 确保 URL 是**列表页**（一页能看到多条 deal），而不是单条 deal 详情页或首页导航。

**自检**：打开 `sites_draft.json`，数一下 `sites` 数组是否有 20 个对象，且每个都有 `id`、`name`，需要爬的站都填了 `list_url`。

---

## 步骤 1.2：为每个站写解析规则（选择器）

**目标**：告诉爬虫「在每个站的列表页上，哪里是一条 deal、哪里是标题/价格/链接/图」，以便后续按选择器抓取。

**操作文件**：仍是 `config/sites_draft.json`，每个站点下的 `selectors` 对象。

**如何找选择器**：

1. 用浏览器打开该站的 deal 列表页。
2. 按 F12 打开开发者工具，用「选择元素」点中页面上**一条 deal 卡片**（整块区域）。
3. 在 DOM 里找到包住这条 deal 的**最小父元素**（通常是 `div`、`article`、`li` 等），看它是否有 class/id，写出能唯一定位「所有这类卡片」的 CSS 选择器 → 填到 `container`。
4. 在该卡片内部，再分别找到：标题、当前价、原价（如有）、点击进去的链接、主图（如有），记下各自的选择器（可相对 container 写，如 `.title`，或绝对如 `a.deal-link`，实现时会在卡片内查找）。

**selectors 里计划填的键**：

| 键名              | 含义 |
|-------------------|------|
| `container`       | **必填**。页面上「一条 deal」的父元素 CSS 选择器。爬虫先用它找出所有卡片，再在每条卡片内取下面字段。 |
| `title`           | deal 标题的 CSS 选择器（取元素文本）。 |
| `current_price`  | 当前价/折扣价的 CSS 选择器（取文本，实现时再做数字清洗）。 |
| `original_price`  | 原价（可选）。没有就留空或不写。 |
| `deal_url`        | 指向 deal 详情或商品页的链接，一般是 `<a href="...">`，选择器要能取到 `href`。 |
| `image_url`       | 主图（可选）。一般是 `<img src="...">`，选择器要能取到 `src`。没有就留空或不写。 |
| `deal_percent`    | 可选。折扣百分比文案（如「20% off」）的 CSS 选择器，取元素文本。 |
| `deals_detail`    | 可选。deal 补充说明（如「Buy 3 get -10%; 5 get -20%」）的 CSS 选择器；若页面上有多个匹配，实现时取第一个非空文本或拼接所有非空。 |

**示例（某站 selectors）**：

```json
"selectors": {
  "container": "article.deal-card",
  "title": "h2.deal-title a",
  "current_price": ".price-current",
  "original_price": ".price-original",
  "deal_url": "h2.deal-title a",
  "image_url": ".deal-image img"
}
```

**关于 `notes`**：  
- `notes` 是**给人看的备注**，写什么都可以，代码默认不会解析它（除非你后来在实现里主动根据 notes 做分支）。  
- 可以写和结构相关的，例如：「价格由 .currency 与 .amount 拼成，需自定义解析」。  
- 也可以写和结构无关的，例如：「非官网站点」「数据仅供参考」「需登录」「选择器容易失效」等，方便自己以后区分或排查。

**若某站结构特殊**（例如价格由「$」和数字分在两个 span 里）：  
- 在该站点的 `notes` 里写清楚结构说明；实现时可为该站单独写解析逻辑，或在站点配置里加 `"parser": "custom"` 等标记，由代码分支处理。

**自检**：对每个要爬的站，确认 `selectors.container` 已填且能在该站列表页上选中多条卡片；`title`、`current_price`、`deal_url` 至少已填，其余可选。

---

## 步骤 1.3：确认全局参数

**目标**：确认「站与站之间延时、单页超时、每页停留时间、是否用 Selenium、结果往哪写」等全局设置符合预期。

**操作文件**：`config/sites_draft.json` 顶层的 `global` 对象。

**当前约定值及含义**：

| 键名                 | 当前值        | 含义 |
|----------------------|---------------|------|
| `request_delay_sec`  | `[3, 5]`      | 每爬完**一个站**后，随机等待 3～5 秒再访问下一个站。 |
| `page_dwell_sec`      | `[8, 18]`     | **每个列表页**加载完成后，随机停留 8～18 秒再解析、再离开。 |
| `timeout_sec`         | `45`          | 单页加载或单次请求的超时时间（秒）。 |
| `use_selenium`        | `true`        | 是否用 Selenium 打开浏览器访问；若改为 `false`，则需用 requests + 解析静态 HTML（仅适合无强 JS 的站）。 |
| `output_dir`          | `"output"`    | 爬取结果 JSON 写到的目录（相对项目根或当前工作目录）。 |

**自检**：打开 `sites_draft.json`，看一眼 `global` 里上述几项是否与你要的一致；若有需要可改成 `[5, 10]`、`60` 等，其余不必动。

---

## 阶段一完成检查清单

- [ ] `sites` 中共有 20 个站点对象，且每个都有 `id`、`name`。
- [ ] 要爬的站点都填了有效的 `list_url`（空 URL 的站会被实现时跳过）。
- [ ] 每个要爬的站点的 `selectors.container` 已填，且能在该站列表页上选中多条 deal 卡片。
- [ ] 每个要爬的站点的 `selectors` 中至少填了 `title`、`current_price`、`deal_url`；`original_price`、`image_url` 按需填写。
- [ ] 特殊站点在 `notes` 中说明了特殊结构，或已加 `parser: "custom"` 等标记。
- [ ] `global` 中 `request_delay_sec`、`page_dwell_sec`、`timeout_sec`、`use_selenium`、`output_dir` 已按需确认或修改。

全部打勾后，阶段一即完成，可进入阶段二（读配置 + 单站访问）。
