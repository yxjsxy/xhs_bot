# 小红书自动化 (XHS Automation)

小Rei 的小红书运营自动化工具集 — 基于浏览器 RPA，复用 OpenClaw 浏览器登录态。

## 功能

- **发布图文** (`scripts/xhs_publish.py`) — CDP 自动化发帖
- **评论管理** (`scripts/xhs_comment.py`) — 查看通知/评论列表/回复/自动回复
- **内容渲染** (`scripts/render_xhs_v2.py`) — 本地卡片图片生成
- **人设文案** (`persona.md`) — 小Rei 人设 & 风格指南

## 使用

所有脚本通过 CDP 连接 OpenClaw 浏览器（端口 18800），需先启动浏览器并登录小红书。

```bash
# 查看通知
python3 scripts/xhs_comment.py notifications

# 查看笔记评论
python3 scripts/xhs_comment.py list --note-id <note_id>

# 自动回复（预览模式）
python3 scripts/xhs_comment.py auto-reply --note-id <note_id>

# 发布笔记
python3 scripts/xhs_publish.py --title "标题" --content "正文" --images img1.png img2.png
```

## 架构

```
scripts/         # 核心自动化脚本
assets/          # HTML 模板 & 样式
references/      # 操作流程文档
persona.md       # 人设定义
```

## 集成

OpenClaw skill (`xiaohongshu-unified`) 调用本 repo 的脚本，skill 负责调度逻辑，repo 负责具体实现。
