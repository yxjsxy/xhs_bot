# XHS 运行规则（引用自技能主文）

## 0.1 低 token 与快照约束（最高优先）

**evaluate-first 原则**: 能用 evaluate 完成的操作绝不 snapshot。

- **填写内容**: evaluate + `execCommand('insertText')` 或直接设 innerHTML
- **点击按钮**: evaluate + `document.querySelector('button').click()`
- **读取状态**: evaluate 返回 `{ok, title, bodyLen}` 等结构化数据
- **snapshot 仅限**: ① 首次进入页面找 tab ref ② 发布前最终确认 ③ 发布结果验证
- snapshot 使用 `compact=true` + `element=<容器ref>` 聚焦关键区域
- 全流程 snapshot ≤ 3 次，预估 token ≤ 12K
- 每个动作最多重试 1 次；第二次失败改稳健路径并汇报
- evaluate 失败 → fallback 到 snapshot + click + type，不盲重试

## 0.2 浏览器稳定规则（最高优先）

- 默认仅用内置浏览器：`profile="openclaw"`。
- 每次动作前先确认会话目标 tab（`browser.start --profile openclaw` 后再 `open/snapshot`）。
- 若出现 `no tab is connected`、`profile "chrome"` 等异常，立刻切回 `openclaw` 并重试。
- 连续 2 次点击/导航失败后改稳健路径（如直达点击改为 evaluate+定位），不做盲重试。

## 3.5 搜索并浏览（核心约束）

1. 仅从搜索结果页点击进入帖子，禁止直接 `navigate` 到 `/explore/<id>`。
2. 默认跳过本账号作者内容（避免自刷）。
3. 进入后先校验：不是 404、可见评论/互动信息、可识别标题或作者。
4. 进入方式优先点卡片本体，避免点头像/作者名导致跳错。
5. 若评论控件为 `contenteditable` 或 `p.content-input`，需先触发输入事件再发送。
6. 两条点击失败或 404 后返回搜索页换下一条，不对同链接直跳重试。

## 6.0 回放与降级

- 若搜索结构变化先 snapshot 更新 selector 再继续，不盲跑旧路径。
- 关键页（创作页、探索页、用户页）尽量复用已打开 tab，不重复 `open`。
- 先告诉用户“已达异常节点”，避免无意义继续操作导致误发。
