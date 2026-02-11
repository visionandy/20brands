# 20 站 Deal 爬虫 — 完整分步骤方案

（不写代码，仅方案与顺序。当前全局配置：`timeout_sec: 45`，`request_delay_sec: [3, 5]`，`page_dwell_sec: [8, 18]`，其余见 `sites_draft.json`。）

---

## 阶段一：准备配置与环境

**步骤 1.1** 定好 20 个 deal 站点  
- 在 `config/sites_draft.json` 的 `sites` 里，为每个站点填好：`id`、`name`、`list_url`（该站 deal 列表页 URL）。  
- 若某站暂无 URL，可先留空或填占位，实现时对空 URL 做跳过处理。

**步骤 1.2** 为每个站写解析规则（选择器）  
- 用浏览器打开该站 deal 列表页，用开发者工具查看 DOM。  
- 在对应站点的 `selectors` 里填写：  
  - `container`：单条 deal 卡片的父元素（CSS 选择器）。  
  - `title`、`current_price`、`original_price`（可选）、`deal_url`、`image_url`（可选）：各自在卡片内的选择器。  
- 若某站结构特殊（如价格由多个元素拼成），在 `notes` 里注明，实现时可为该站单独写解析逻辑或加 `parser: "custom"` 等标记。

**步骤 1.3** 确认全局参数  
- `request_delay_sec: [3, 5]` — 每爬完一个站后随机等 3～5 秒再访问下一站。  
- `timeout_sec: 45` — 单页加载/请求超时 45 秒。  
- `page_dwell_sec: [8, 18]` — 每个列表页加载完成后随机停留 8～18 秒再解析。  
- `use_selenium`、`output_dir` 按需保留或修改。

---

## 阶段二：实现「读配置 + 单站访问」

**步骤 2.1** 加载配置  
- 从 `config/sites_draft.json` 读取 `global` 和 `sites`。  
- 解析 `request_delay_sec`、`page_dwell_sec`、`timeout_sec`、`output_dir` 等，供后续步骤使用。

**步骤 2.2** 初始化浏览器（若 `use_selenium` 为 true）  
- 创建 Selenium WebDriver（如 Chrome），设置页面加载超时为 `timeout_sec`（45 秒）。  
- 可选：设置 User-Agent 等请求头，使其更接近真实浏览器。

**步骤 2.3** 单站访问流程（先对一个站点跑通）  
- 取当前站点的 `list_url`；若为空则跳过。  
- 用 driver 打开该 URL，等待页面加载（或等待某关键元素出现，超时 45 秒）。  
- 若有 cookie/consent 弹窗，先执行关闭逻辑（如点击「接受」），再继续。  
- 按 `page_dwell_sec` 随机等待 8～18 秒（每页停留）。  
- 停留结束后再取 DOM 或 `page_source`，进入解析步骤。

---

## 阶段三：实现「单站解析」

**步骤 3.1** 按选择器找卡片  
- 用该站 `selectors.container` 在当前页面中查找所有 deal 卡片元素（或从 page_source 里用 BeautifulSoup 等解析出所有卡片）。

**步骤 3.2** 对每条卡片提取字段  
- 在每个卡片内，用 `title`、`current_price`、`original_price`、`deal_url`、`image_url` 等选择器取文本或属性。  
- 拼成一条 deal 对象，字段名与 `deal_fields` 一致；并写入 `source_site` 为当前站点的 `id`。  
- 若某站用了 `parser: "custom"` 或 notes 中说明需特殊处理，则走该站专用解析逻辑。

**步骤 3.3** 去重与清洗（可选）  
- 同一页内按 `deal_url` 或 title+price 去重。  
- 对价格、URL 做简单清洗（去空白、补全相对链接等）。

**步骤 3.4** 单站结果  
- 当前站解析得到列表 `list[deal]`；若解析失败或超时，得到空列表并记一条错误信息（如站点 id + 错误原因），便于排查。

---

## 阶段四：实现「20 站循环 + 防封」

**步骤 4.1** 按顺序遍历 20 个站点  
- 对 `sites` 中每个站点执行：访问 `list_url` → 关弹窗 → 每页停留（page_dwell_sec）→ 解析 → 得到该站 deal 列表。

**步骤 4.2** 站与站之间的延时  
- 每完成一个站（或每打开下一站前），按 `request_delay_sec` 随机等待 3～5 秒，再访问下一个站点。

**步骤 4.3** 失败与重试  
- 若某站访问超时（45 秒）或解析异常，可重试 1～2 次；仍失败则记录该站 id 与错误信息，不中断整体，继续下一站。

**步骤 4.4** 汇总结果  
- 在内存中维护：按站汇总 `{ "site_01": [...], "site_02": [...], ... }`，以及扁平列表 `[ deal1, deal2, ... ]`（每条带 `source_site`）。

---

## 阶段五：输出与收尾

**步骤 5.1** 写按站汇总结果  
- 将 `{ "site_01": [...], ... }` 写入 `output/deals_by_site.json`（或配置中的 `output_dir` 下同名文件）。

**步骤 5.2** 写扁平列表结果  
- 将全部 deal 的扁平列表写入 `output/deals_all.json`，每条均含 `source_site` 及 `deal_fields` 中约定字段。

**步骤 5.3** 关闭浏览器与资源  
- 若使用 Selenium，关闭 driver；释放连接等资源。

**步骤 5.4** 可选：日志与报错汇总  
- 将本轮运行中「跳过/失败的站点」及原因记到日志或单独文件，便于后续补爬或修选择器。

---

## 顺序小结

1. **准备**：填好 20 站 URL + 每站 selectors，确认 global（含 timeout_sec: 45、request_delay_sec: [3, 5]、page_dwell_sec: [8, 18]）。  
2. **实现**：读配置 → 初始化浏览器 → 对每站：访问 → 关弹窗 → 每页停留 8～18 秒 → 解析 → 站间延时 3～5 秒 → 失败重试 1～2 次。  
3. **输出**：写 `deals_by_site.json` 与 `deals_all.json`，关浏览器，可选写错误/跳过日志。

按以上步骤实现即可得到「每页停留时间长、站间延时 3～5 秒、超时 45 秒」的一版完整方案；其余配置不变。
