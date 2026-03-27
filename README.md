# astrbot_plugin_wechat_article_shot

自动识别群聊中的**微信公众号文章链接或分享卡片**，提取文章的**标题、作者、正文文字和正文图片**，在本地重新排版生成长图并发送回原群聊。

---

## 功能特点

- 自动监听群消息
- 自动识别：
  - 纯文本中的微信公众号文章链接
  - 分享卡片/XML/JSON 中包含的公众号文章链接
  - raw_message 中嵌套的公众号文章地址
- 本地提取并重排文章内容
  - 抓取标题、作者、发布时间
  - 提取正文文字
  - 下载正文图片并嵌入长图
  - 在本地重新排版生成规整长图
- 支持去重，防止同一篇文章在短时间内重复发送
- 支持屏蔽指定用户触发
- 支持自定义长图宽度、图片数量、配色、字体等参数

---

## 工作原理

1. 监听群聊消息
2. 从文本、消息链、卡片结构、raw_message 中提取公众号文章链接
3. 请求公众号文章页面 HTML
4. 提取标题、作者、发布时间
5. 提取正文文本块与正文图片
6. 在本地用 Pillow 重新排版生成长图
7. 将长图发送到原群

---

## 安装依赖

插件依赖如下：

```bash
pip install requests beautifulsoup4 Pillow
```

---

## 插件目录结构

```text
astrbot_plugin_wechat_article_shot/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
├─ README.md
└─ requirements.txt
```

生成的图片默认保存到：

```text
data/plugins_data/astrbot_plugin_wechat_article_shot/shots/
```

---

## 配置项说明

### enabled
是否启用插件。

### dedupe_ttl_seconds
去重时间，单位秒。  
在该时间内，同一篇文章不会重复处理发送。

### dedupe_scope
去重范围，可选：

- `group_url`
- `group_url_sender`
- `global_url`

默认推荐：`group_url`

### http_timeout_seconds
抓取文章页面和正文图片的超时时间。

### image_quality
生成 JPEG 时的质量参数。  
范围建议 `70-95`。

### local_canvas_width
输出长图宽度。

### local_padding
长图边距。

### local_line_spacing
正文行间距。

### local_section_gap
段落间距。

### local_image_gap
正文图片与上下文之间的间距。

### local_max_images
最多下载并绘制的正文图片数量。

### local_max_blocks
最多处理的文本/图片块数量。  
用来防止超长文章处理过慢。

### font_path
自定义字体文件路径。  
如果系统默认字体显示不理想，可以指定一个中文字体文件。

### local_background_color / local_card_color / local_text_color / local_sub_text_color / local_divider_color
本地长图的配色参数。

### capture_failed_message
文章处理失败时发送到群里的提示语。

### send_error_tip
是否把具体错误信息发回群里。  
建议默认关闭，避免刷屏。

### blocked_sender_ids
屏蔽触发用户列表。  
列表中的 QQ 号发链接时不会触发处理。

---

## 示例配置

```json
{
  "enabled": true,
  "dedupe_ttl_seconds": 300,
  "dedupe_scope": "group_url",
  "http_timeout_seconds": 20,
  "image_quality": 90,
  "local_canvas_width": 900,
  "local_padding": 48,
  "local_line_spacing": 16,
  "local_section_gap": 28,
  "local_image_gap": 20,
  "local_max_images": 6,
  "local_max_blocks": 120,
  "font_path": "",
  "local_background_color": "#F7F8FA",
  "local_card_color": "#FFFFFF",
  "local_text_color": "#1F2329",
  "local_sub_text_color": "#667085",
  "local_divider_color": "#E5E7EB",
  "capture_failed_message": "公众号文章处理失败，请稍后重试。",
  "send_error_tip": false,
  "blocked_sender_ids": []
}
```

---

## 当前版本可提取的内容

当前版本重点支持：

- 标题
- 作者/公众号名称
- 发布时间
- 正文文字段落
- 正文图片

这是一个偏实用的轻量版本，适合群聊中快速将公众号文章转换成长图。

---

## 已知限制

1. **不是 100% 还原原文**
   - 当前方案是本地提取内容后重排。
   - 主要保证标题、作者、正文文字、正文图片的提取与展示。
   - 对复杂嵌入内容、特殊引用块、投票、音视频、小程序卡片等支持有限。

2. **部分公众号文章有访问限制**
   - 某些文章可能存在鉴权、地区限制、风控或已失效。
   - 这种情况下可能提取失败，或者只能部分提取。

3. **正文图片可能下载失败**
   - 个别图片地址可能有防盗链、重定向或时效限制。
   - 当前策略是跳过失败图片，尽量保留文字内容继续生成长图。

4. **中文字体依赖系统环境**
   - 如果运行环境缺少合适字体，长图中文字样式可能一般。
   - 可以通过 `font_path` 指定更合适的中文字体文件。

5. **超长文章处理会更慢**
   - 如果文章图片很多、正文很长，处理时间会增加。
   - 可以通过 `local_max_images` 和 `local_max_blocks` 做限制。

---

## 调优建议

如果你觉得生成效果不理想，可以这样调整：

- 文字太密  
  → 增大 `local_line_spacing`

- 段落太挤  
  → 增大 `local_section_gap`

- 图片太多导致生成太慢  
  → 减小 `local_max_images`

- 文章过长  
  → 减小 `local_max_blocks`

- 字体不好看  
  → 设置 `font_path`

- 图片体积过大  
  → 降低 `image_quality`

---

## 说明

这个插件当前是**自动触发型**插件，不需要额外命令。  
只要群里出现可识别的微信公众号文章链接或卡片，就会自动处理并发送本地构造的长图。  
当前版本仅保留**本地构造长图功能**，不再包含浏览器截图能力。
