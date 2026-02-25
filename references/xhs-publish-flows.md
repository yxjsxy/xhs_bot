# XHS Publish Flows

本文件拆分并细化「发布链路」的操作步骤，供 `SKILL.md` 按需引用。

## 0. 总览

发布类型：
- 视频
- 图文
- 长文

三要素（发布前必须齐全）：
1. 封面
2. 标题
3. 正文

## 1. 图文发布（推荐默认）

### 1.1 上传图文（普通）— 优化低 Token 流程

**URL**: `https://creator.xiaohongshu.com/publish/publish?source=official`

**设计原则**: 用 evaluate 代替重复 snapshot，全流程最多 3 次 snapshot（进入页面、发布前确认、发布结果）。

#### Step 1: 打开发布页 + 切换图文 tab（1 次 snapshot）
```
browser(action="navigate", targetUrl="https://creator.xiaohongshu.com/publish/publish?source=official")
browser(action="snapshot", compact=true)  // 找「上传图文」tab ref
browser(action="act", request={kind:"click", ref:"<上传图文-ref>"})
```

#### Step 2: 上传图片（0 次 snapshot）
```
browser(action="upload", paths=["/absolute/path/to/image.png"])
// 等 2 秒让上传完成，不需要 snapshot 确认
```

#### Step 3: 用 evaluate 填写标题和正文（0 次 snapshot）
```js
// 填标题
browser(action="act", request={kind:"evaluate", fn:`() => {
  const title = document.querySelector('input[placeholder*="标题"], [class*="title"] [contenteditable]');
  if (!title) return {ok:false, err:'title not found'};
  title.focus();
  document.execCommand('selectAll');
  document.execCommand('insertText', false, '小龙虾的自我介绍🦞');
  return {ok:true};
}`})

// 填正文
browser(action="act", request={kind:"evaluate", fn:`() => {
  const editor = document.querySelector('[contenteditable="true"]:not([placeholder*="标题"])') 
    || document.querySelector('.ql-editor, [class*="content"] [contenteditable]');
  if (!editor) return {ok:false, err:'editor not found'};
  editor.focus();
  document.execCommand('selectAll');
  document.execCommand('insertText', false, '正文内容...');
  return {ok:true, len: editor.textContent.length};
}`})
```

**注意**: `insertText` 会触发小红书的 input 事件，确保内容被正确识别。如果 evaluate 填写失败（返回 ok:false），fallback 到 snapshot + click + type 方式。

#### Step 4: 发布前确认（1 次 snapshot）
```
browser(action="snapshot", compact=true, element="<编辑区容器ref>")
// 只抓编辑区，不抓导航/设置/预览，节省 60% tokens
// 确认标题和正文正确 → 告知 Karl 内容就绪
```

#### Step 5: 发布（1 次 snapshot）
```
// Karl 确认后
browser(action="act", request={kind:"evaluate", fn:`() => {
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('发布') && !b.textContent.includes('暂存'));
  if (!btn) return {ok:false};
  btn.click();
  return {ok:true};
}`})

// 等 2 秒，确认结果
browser(action="snapshot", compact=true)  // 看到 "发布成功" 即完成
```

#### Token 对比
| 方式 | snapshot 次数 | 预估 tokens |
|------|--------------|-------------|
| 旧流程 | 6-7 次 | 25-30K |
| 优化后 | 3 次 | 8-12K |

#### Fallback 规则
- evaluate 返回 `ok:false` → 退回 snapshot + click + type 方式
- 连续 2 次 evaluate 失败 → 汇报给 Karl，不盲重试
- 首次发布需绑定手机号（在 XHS APP 中操作）

#### 已知问题
- 「上传图文」tab 可能有两个同名元素，优先点击靠后的
- 小红书编辑器可能拦截 `execCommand`，需测试 evaluate 可靠性
- 如用 evaluate 填正文，换行用 `\n` 可能不生效，需改用 innerHTML 插入 `<p>` 标签

### 1.2 图文-文字配图（大字报）
1. 进入「上传图文」
2. 点击「文字配图」
3. 输入封面大字报文案
4. 点击「生成图片」
5. 在模板页选择样式并点「下一步」
6. 进入编辑页填写：标题、正文、话题/标签
7. 校验三要素后停在发布按钮（待用户确认）

### 1.3 图文半程预发（不发布）
满足以下条件即视为“半程预发完成”：
- 已完成封面生成（或上传）
- 已进入编辑页
- 已填写标题与正文
- 仅停在「发布」按钮可见处，未点击发布

## 2. 视频发布

1. 进入「上传视频」
2. 上传视频文件
3. 补齐封面/标题/正文
4. 校验可见范围与设置
5. 发布前等待用户确认

## 3. 长文发布

1. 进入「写长文」
2. 新建创作或导入链接
3. 填写长文标题与正文结构
4. 若用户目标是图文，避免误走长文链路

## 4. 常见问题与处理

- 误入长文：返回发布笔记，明确切回「上传图文」
- 草稿箱默认视频：切换到「图文笔记」tab后再编辑
- 标题超限：出现 `xx/20` 时立刻压缩
- 只做了封面没填文案：必须补齐标题与正文
- 网页端详情扫码限制：评论优先在通知页处理，必要时改 App 端
