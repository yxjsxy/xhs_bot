#!/usr/bin/env python3
"""
小红书评论管理脚本 — 通过 CDP 连接 OpenClaw 浏览器，复用登录态。

功能:
  1. list       — 查看指定笔记的评论列表
  2. notifications — 查看通知页新评论
  3. reply      — 回复单条评论
  4. auto-reply — 自动回复自己帖子下所有未回复评论（核心功能）

用法:
  # 查看笔记评论
  python3 xhs_comment.py list --note-id <note_id>

  # 查看通知页新评论
  python3 xhs_comment.py notifications

  # 回复单条评论
  python3 xhs_comment.py reply --note-id <note_id> --comment-text "评论内容" --body "回复内容" [--confirm]

  # 自动回复所有未回复评论（预览模式，输出计划但不发送）
  python3 xhs_comment.py auto-reply --note-id <note_id>

  # 自动回复（确认发送）
  python3 xhs_comment.py auto-reply --note-id <note_id> --confirm

  # 自动回复（自定义人设 prompt 文件）
  python3 xhs_comment.py auto-reply --note-id <note_id> --persona persona.md --confirm

  # 限制回复数量 + 间隔秒数
  python3 xhs_comment.py auto-reply --note-id <note_id> --max-replies 10 --delay 12 --confirm

退出码:
  0 = 成功
  1 = 参数错误
  2 = 浏览器连接失败
  3 = 页面操作失败
  4 = AI 生成回复失败
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

CDP_ENDPOINT = os.environ.get("XHS_CDP_ENDPOINT", "http://127.0.0.1:18800")
STEALTH_JS = Path(__file__).parent / "stealth.min.js"
DEFAULT_PERSONA = Path(__file__).parent.parent / "persona.md"
REPLY_LOG_DIR = Path(__file__).parent.parent / "data" / "reply_logs"

# ─── Browser helpers ───

async def connect_browser():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        return pw, browser
    except Exception as e:
        await pw.stop()
        print(json.dumps({"ok": False, "error": f"CDP connect failed: {e}"}))
        sys.exit(2)


async def inject_stealth(page):
    if STEALTH_JS.exists():
        await page.evaluate(STEALTH_JS.read_text())


async def get_page(browser):
    context = browser.contexts[0]
    pages = context.pages
    page = pages[0] if pages else await context.new_page()
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    return page


# ─── Comment extraction (shared logic) ───

EXTRACT_COMMENTS_JS = """(config) => {
    const { myNickname, limit } = config;
    const results = [];

    // 小红书评论区 DOM 结构：.parent-comment > .comment-inner (主评论) + .reply-container (子评论)
    // 尝试多种选择器适配不同版本
    const commentContainers = document.querySelectorAll(
        '.parent-comment, .comment-item-box, [class*="CommentItem"], [class*="commentItem"]'
    );

    if (commentContainers.length === 0) {
        // Fallback: 尝试从评论区整体文本提取
        const section = document.querySelector(
            '.comments-container, .note-comment, [class*="comment-list"], [class*="commentList"]'
        );
        if (section) {
            return [{
                type: "raw_text",
                content: section.innerText.substring(0, 5000),
                message: "Structured extraction failed, returning raw text"
            }];
        }
        return [{type: "error", message: "No comment elements found on page"}];
    }

    for (let i = 0; i < Math.min(commentContainers.length, limit); i++) {
        const container = commentContainers[i];

        // 提取主评论
        const userEl = container.querySelector(
            '.author-wrapper .name, .user-name, .nickname, [class*="userName"], [class*="authorName"]'
        );
        const contentEl = container.querySelector(
            '.note-text, .content, [class*="commentContent"], [class*="noteText"]'
        );
        const timeEl = container.querySelector(
            '.date, .time, [class*="time"], [class*="date"]'
        );
        const likeEl = container.querySelector(
            '.like-wrapper .count, [class*="likeCount"], [class*="like"] .count'
        );

        const user = userEl ? userEl.textContent.trim() : "";
        const content = contentEl ? contentEl.textContent.trim() : container.innerText.trim().substring(0, 300);
        const commentTime = timeEl ? timeEl.textContent.trim() : "";
        const likes = likeEl ? likeEl.textContent.trim() : "0";

        // 检查子评论中是否已有自己（myNickname）的回复
        const replyContainer = container.querySelector(
            '.reply-container, [class*="replyList"], [class*="subComment"]'
        );
        let hasMyReply = false;
        const subComments = [];

        if (replyContainer) {
            const replyItems = replyContainer.querySelectorAll(
                '.reply-item, .comment-item, [class*="replyItem"], [class*="subCommentItem"]'
            );
            for (const ri of replyItems) {
                const riUser = ri.querySelector(
                    '.author-wrapper .name, .user-name, .nickname, [class*="userName"]'
                );
                const riContent = ri.querySelector(
                    '.note-text, .content, [class*="commentContent"]'
                );
                const riUserName = riUser ? riUser.textContent.trim() : "";
                const riContentText = riContent ? riContent.textContent.trim() : ri.innerText.trim().substring(0, 200);

                subComments.push({ user: riUserName, content: riContentText });

                if (myNickname && riUserName === myNickname) {
                    hasMyReply = true;
                }
            }
        }

        // 排除自己发的评论
        const isMyComment = myNickname && user === myNickname;

        results.push({
            index: i + 1,
            user,
            content,
            time: commentTime,
            likes,
            has_my_reply: hasMyReply,
            is_my_comment: isMyComment,
            sub_comments: subComments,
            type: "structured"
        });
    }
    return results;
}""";

EXTRACT_NOTE_INFO_JS = """() => {
    const title = document.querySelector(
        '#detail-title, .title, [class*="noteTitle"]'
    );
    const desc = document.querySelector(
        '#detail-desc, .desc, [class*="noteDesc"], [class*="noteContent"]'
    );
    const author = document.querySelector(
        '.author-wrapper .name, .user-name, [class*="authorName"]'
    );
    return {
        title: title ? title.textContent.trim() : "",
        desc: desc ? desc.textContent.trim().substring(0, 500) : "",
        author: author ? author.textContent.trim() : ""
    };
}"""

EXTRACT_MY_NICKNAME_JS = """() => {
    // 尝试从页面头部用户信息提取
    const el = document.querySelector(
        '.user-nickname, .sidebar .name, [class*="userNickname"]'
    );
    return el ? el.textContent.trim() : "";
}"""


# ─── AI reply generation ───

def _build_reply_prompt(comment_user: str, comment_content: str, note_title: str,
                        note_desc: str, persona_text: str) -> str:
    return f"""{persona_text}

---
你正在回复自己小红书帖子下的一条评论。

【帖子标题】{note_title}
【帖子内容摘要】{note_desc[:200]}
【评论者】{comment_user}
【评论内容】{comment_content}

请根据上方人设，生成 1 条回复（≤ 100 字）。
- 风格：短句、口语化、符合人设
- 如果评论是夸奖/感谢 → 接梗 + 轻松回应
- 如果评论是提问 → 简短回答 + 收尾
- 如果评论是杠精/无意义 → 轻飘飘带过
- 不要加引号，直接输出回复文本
"""


def _clean_reply(text: str) -> str:
    """清理 AI 回复的多余引号/空白"""
    text = text.strip()
    if (text.startswith('"') and text.endswith('"')) or \
       (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def _call_openclaw_gateway(prompt: str, model: str) -> str | None:
    """
    通过 OpenClaw Gateway 的 chat completions endpoint 调用模型。
    自动使用 OpenClaw 已配置的 auth，无需单独配 API key。
    """
    try:
        import requests
        gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
        gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

        headers = {"Content-Type": "application/json"}
        if gateway_token:
            headers["Authorization"] = f"Bearer {gateway_token}"

        resp = requests.post(
            f"{gateway_url}/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200
            },
            timeout=30
        )
        if resp.status_code == 200:
            return _clean_reply(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return None


def _try_claude_sonnet(prompt: str) -> str | None:
    """首选：Claude Sonnet 4 via CLI"""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "claude-sonnet-4-20250514"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return _clean_reply(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _try_minimax(prompt: str) -> str | None:
    """Fallback 1: MiniMax M2.1 Lightning via OpenClaw gateway or direct API"""
    # 先走 gateway
    result = _call_openclaw_gateway(prompt, "minimax/MiniMax-M2.1-lightning")
    if result:
        return result
    # 直接 API fallback
    try:
        api_key = os.environ.get("MINIMAX_API_KEY", "")
        group_id = os.environ.get("MINIMAX_GROUP_ID", "2017621601956144027")
        if not api_key:
            return None
        import requests
        resp = requests.post(
            f"https://api.minimaxi.chat/v1/text/chatcompletion_v2?GroupId={group_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "MiniMax-Text-01",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200
            },
            timeout=20
        )
        if resp.status_code == 200:
            return _clean_reply(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return None


def _try_qwen(prompt: str) -> str | None:
    """Fallback 2: Qwen 3.5 Plus via OpenClaw gateway or direct API"""
    # 先走 gateway
    result = _call_openclaw_gateway(prompt, "dashscope/qwen3.5-plus")
    if result:
        return result
    # 直接 API fallback
    try:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            return None
        import requests
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "qwen3.5-plus",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200
            },
            timeout=20
        )
        if resp.status_code == 200:
            return _clean_reply(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return None


def generate_reply_with_ai(comment_user: str, comment_content: str, note_title: str,
                           note_desc: str, persona_text: str) -> str:
    """
    AI 生成小红书风格回复。
    链路: Claude Sonnet 4 → Kimi (k2.5) → Qwen 3.5 Plus → 模板兜底
    """
    prompt = _build_reply_prompt(comment_user, comment_content, note_title, note_desc, persona_text)

    log = lambda msg: print(json.dumps({"log": msg}), file=sys.stderr, flush=True)

    # 1) Claude Sonnet 4
    log("Trying Claude Sonnet 4...")
    reply = _try_claude_sonnet(prompt)
    if reply:
        log("✅ Claude Sonnet 4 success")
        return reply

    # 2) MiniMax M2.1 Lightning
    log("Claude failed, trying MiniMax...")
    reply = _try_minimax(prompt)
    if reply:
        log("✅ MiniMax success")
        return reply

    # 3) Qwen 3.5 Plus
    log("Kimi failed, trying Qwen 3.5 Plus...")
    reply = _try_qwen(prompt)
    if reply:
        log("✅ Qwen success")
        return reply

    # 4) 模板兜底
    log("⚠️ All models failed, using template fallback")
    templates = [
        "谢谢关注～我继续打工了 🦞",
        "行行行，收到！",
        "哈哈 感谢支持～",
        "我不说太多，懂的都懂 😼",
    ]
    return random.choice(templates)


# ─── Commands ───

async def cmd_list_comments(note_id: str, limit: int = 20):
    pw, browser = await connect_browser()
    try:
        page = await get_page(browser)
        await inject_stealth(page)

        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)

        # Scroll down to load comments
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        my_nickname = await page.evaluate(EXTRACT_MY_NICKNAME_JS)
        comments = await page.evaluate(EXTRACT_COMMENTS_JS, {"myNickname": my_nickname, "limit": limit})
        note_info = await page.evaluate(EXTRACT_NOTE_INFO_JS)

        result = {
            "ok": True,
            "note_id": note_id,
            "note_info": note_info,
            "my_nickname": my_nickname,
            "comments_count": len(comments),
            "comments": comments
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(3)
    finally:
        await pw.stop()


async def cmd_notifications():
    pw, browser = await connect_browser()
    try:
        page = await get_page(browser)
        await inject_stealth(page)

        await page.goto("https://www.xiaohongshu.com/notification",
                        wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)

        notifications = await page.evaluate("""() => {
            const results = [];
            const items = document.querySelectorAll(
                '.notification-item, [class*="notify"], [class*="notification"], .message-item'
            );
            if (items.length === 0) {
                const body = document.querySelector('.main, .content, [class*="notification"]');
                if (body) {
                    return [{type: "raw_text", content: body.innerText.substring(0, 3000)}];
                }
                return [{type: "error", message: "No notification elements found"}];
            }
            for (let i = 0; i < Math.min(items.length, 30); i++) {
                const el = items[i];
                const userEl = el.querySelector('.user-name, .nickname, [class*="name"]');
                const contentEl = el.querySelector('.content, .text, [class*="content"]');
                const timeEl = el.querySelector('.time, .date, [class*="time"]');
                results.push({
                    index: i + 1,
                    user: userEl ? userEl.textContent.trim() : "unknown",
                    content: contentEl ? contentEl.textContent.trim() : el.innerText.trim().substring(0, 200),
                    time: timeEl ? timeEl.textContent.trim() : "",
                    type: "structured"
                });
            }
            return results;
        }""")

        print(json.dumps({"ok": True, "count": len(notifications), "notifications": notifications},
                         ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(3)
    finally:
        await pw.stop()


async def cmd_reply_single(note_id: str, comment_text: str, body: str, confirm: bool):
    pw, browser = await connect_browser()
    try:
        page = await get_page(browser)
        await inject_stealth(page)

        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)

        if not confirm:
            print(json.dumps({
                "ok": True, "status": "preview", "note_id": note_id,
                "target_comment": comment_text[:50], "reply_body": body,
                "message": "Pass --confirm to send."
            }, ensure_ascii=False))
            return

        success = await _do_reply_on_page(page, comment_text, body)
        print(json.dumps(success, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(3)
    finally:
        await pw.stop()


async def _do_reply_on_page(page, comment_text: str, body: str) -> dict:
    """在已打开的笔记页面上，找到评论并回复。返回结果 dict。"""

    # 点击评论的回复按钮
    found = await page.evaluate("""(targetText) => {
        const containers = document.querySelectorAll(
            '.parent-comment, .comment-item-box, .comment-item, [class*="CommentItem"]'
        );
        for (const el of containers) {
            if (el.innerText.includes(targetText)) {
                // 找回复按钮
                const btn = el.querySelector(
                    '[class*="reply"], .reply-btn, [class*="replyBtn"]'
                );
                if (btn) { btn.click(); return {found: true, method: "button"}; }
                // 有些 UI 是点击评论文字区域触发回复
                const textEl = el.querySelector('.note-text, .content');
                if (textEl) { textEl.click(); return {found: true, method: "text_click"}; }
            }
        }
        return {found: false};
    }""", comment_text[:30])

    if not found.get("found"):
        return {"ok": False, "error": f"Comment not found: '{comment_text[:50]}'"}

    await page.wait_for_timeout(1500)

    # 输入回复内容
    typed = await page.evaluate("""(text) => {
        const inputs = document.querySelectorAll(
            'textarea, [contenteditable="true"], input[type="text"], [placeholder*="回复"]'
        );
        for (const el of inputs) {
            const rect = el.getBoundingClientRect();
            if (rect.height > 0 && rect.width > 0) {
                el.focus();
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(el, text);
                    else el.value = text;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                } else {
                    el.textContent = text;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }
                return {typed: true};
            }
        }
        return {typed: false};
    }""", body)

    if not typed.get("typed"):
        return {"ok": False, "error": "Reply input not found after clicking reply"}

    await page.wait_for_timeout(500)

    # 点击发送
    sent = await page.evaluate("""() => {
        const btns = [...document.querySelectorAll('button, [class*="submit"], [class*="send"]')];
        const btn = btns.find(b => {
            const t = b.textContent.trim();
            return t === '发送' || t === '回复' || t === '发布';
        });
        if (btn && !btn.disabled) { btn.click(); return {sent: true}; }
        return {sent: false};
    }""")

    await page.wait_for_timeout(2000)

    if sent.get("sent"):
        return {"ok": True, "status": "replied", "reply_body": body}
    else:
        return {"ok": False, "error": "Send button not found or disabled"}


async def cmd_auto_reply(note_id: str, confirm: bool, persona_path: str,
                         max_replies: int, delay_seconds: float):
    """
    自动回复笔记下所有未回复的评论。

    流程：
    1. 打开笔记页，滚动加载评论
    2. 提取所有评论，识别哪些已被自己回复过
    3. 对未回复的评论，逐条用 AI 生成回复
    4. 预览模式：输出回复计划（JSON）
    5. 确认模式：逐条执行回复（带随机间隔防风控）
    """

    # 加载人设
    persona_file = Path(persona_path) if persona_path else DEFAULT_PERSONA
    persona_text = ""
    if persona_file.exists():
        persona_text = persona_file.read_text(encoding="utf-8")
    else:
        persona_text = "你是一个友善活泼的小红书博主，回复风格简短口语化。"

    pw, browser = await connect_browser()
    try:
        page = await get_page(browser)
        await inject_stealth(page)

        url = f"https://www.xiaohongshu.com/explore/{note_id}"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)

        # 滚动加载更多评论
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

        # 获取自己的昵称
        my_nickname = await page.evaluate(EXTRACT_MY_NICKNAME_JS)
        # Fallback: 从笔记作者获取
        if not my_nickname:
            note_info = await page.evaluate(EXTRACT_NOTE_INFO_JS)
            my_nickname = note_info.get("author", "")
        else:
            note_info = await page.evaluate(EXTRACT_NOTE_INFO_JS)

        if not my_nickname:
            print(json.dumps({
                "ok": False,
                "error": "Cannot determine your nickname. Please ensure you're logged in.",
                "hint": "Try: bash scripts/xhs_run.sh xhs_comment list --note-id " + note_id
            }))
            sys.exit(3)

        # 提取评论
        comments = await page.evaluate(EXTRACT_COMMENTS_JS, {
            "myNickname": my_nickname, "limit": 50
        })

        if not comments or (len(comments) == 1 and comments[0].get("type") == "error"):
            print(json.dumps({
                "ok": True,
                "note_id": note_id,
                "my_nickname": my_nickname,
                "message": "No comments found on this note.",
                "unreplied_count": 0,
                "plan": []
            }, ensure_ascii=False, indent=2))
            return

        # 筛选未回复的评论（排除自己发的评论，排除已回复的）
        unreplied = []
        for c in comments:
            if c.get("type") != "structured":
                continue
            if c.get("is_my_comment"):
                continue
            if c.get("has_my_reply"):
                continue
            unreplied.append(c)

        if not unreplied:
            print(json.dumps({
                "ok": True,
                "note_id": note_id,
                "my_nickname": my_nickname,
                "message": "All comments have been replied to!",
                "total_comments": len(comments),
                "unreplied_count": 0,
                "plan": []
            }, ensure_ascii=False, indent=2))
            return

        # 限制回复数量
        to_reply = unreplied[:max_replies]

        # 为每条生成 AI 回复
        plan = []
        log_info = lambda msg: print(json.dumps({"log": msg}), file=sys.stderr, flush=True)

        for idx, c in enumerate(to_reply):
            log_info(f"Generating reply {idx+1}/{len(to_reply)} for: {c['user']}")
            ai_reply = generate_reply_with_ai(
                comment_user=c["user"],
                comment_content=c["content"],
                note_title=note_info.get("title", ""),
                note_desc=note_info.get("desc", ""),
                persona_text=persona_text
            )
            plan.append({
                "index": idx + 1,
                "comment_user": c["user"],
                "comment_content": c["content"][:100],
                "generated_reply": ai_reply,
                "status": "pending"
            })

        if not confirm:
            # 预览模式：输出计划
            result = {
                "ok": True,
                "status": "preview",
                "note_id": note_id,
                "note_title": note_info.get("title", ""),
                "my_nickname": my_nickname,
                "total_comments": len(comments),
                "unreplied_count": len(unreplied),
                "plan_count": len(plan),
                "plan": plan,
                "message": "Pass --confirm to execute all replies."
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        # 确认模式：逐条执行回复
        log_info(f"Starting auto-reply: {len(plan)} replies to send")
        results = []

        for item in plan:
            comment_text = item["comment_content"]
            reply_body = item["generated_reply"]

            log_info(f"Replying to {item['comment_user']}: {reply_body[:30]}...")

            reply_result = await _do_reply_on_page(page, comment_text, reply_body)

            item["status"] = "sent" if reply_result.get("ok") else "failed"
            item["error"] = reply_result.get("error")
            results.append(item)

            if reply_result.get("ok"):
                # 随机延迟防风控
                actual_delay = delay_seconds + random.uniform(0, delay_seconds * 0.5)
                log_info(f"Success. Waiting {actual_delay:.1f}s before next...")
                await page.wait_for_timeout(int(actual_delay * 1000))
            else:
                log_info(f"Failed: {reply_result.get('error')}. Continuing...")
                # 失败后也等一下
                await page.wait_for_timeout(3000)

        # 保存回复日志
        REPLY_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = REPLY_LOG_DIR / f"{note_id}_{int(time.time())}.json"
        log_data = {
            "note_id": note_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "my_nickname": my_nickname,
            "replies": results
        }
        log_file.write_text(json.dumps(log_data, ensure_ascii=False, indent=2))

        sent_count = sum(1 for r in results if r["status"] == "sent")
        failed_count = sum(1 for r in results if r["status"] == "failed")

        final_result = {
            "ok": True,
            "status": "completed",
            "note_id": note_id,
            "total_comments": len(comments),
            "unreplied_before": len(unreplied),
            "attempted": len(results),
            "sent": sent_count,
            "failed": failed_count,
            "log_file": str(log_file),
            "results": results
        }
        print(json.dumps(final_result, ensure_ascii=False, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(3)
    finally:
        await pw.stop()


# ─── Main ───

def main():
    parser = argparse.ArgumentParser(description="小红书评论管理")
    subparsers = parser.add_subparsers(dest="command")

    # list
    p = subparsers.add_parser("list", help="查看笔记评论")
    p.add_argument("--note-id", required=True)
    p.add_argument("-n", "--limit", type=int, default=20)

    # notifications
    subparsers.add_parser("notifications", help="查看通知页新评论")

    # reply (single)
    p = subparsers.add_parser("reply", help="回复单条评论")
    p.add_argument("--note-id", required=True)
    p.add_argument("--comment-text", required=True, help="目标评论内容（用于匹配）")
    p.add_argument("--body", required=True, help="回复内容")
    p.add_argument("--confirm", action="store_true")

    # auto-reply (核心功能)
    p = subparsers.add_parser("auto-reply", help="自动回复所有未回复评论")
    p.add_argument("--note-id", required=True, help="笔记 ID")
    p.add_argument("--confirm", action="store_true", help="确认执行（不传则只预览计划）")
    p.add_argument("--persona", default="", help="人设文件路径（默认用 persona.md）")
    p.add_argument("--max-replies", type=int, default=20, help="最多回复条数（默认20）")
    p.add_argument("--delay", type=float, default=10, help="每条回复间隔秒数（默认10）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        asyncio.run(cmd_list_comments(args.note_id, args.limit))
    elif args.command == "notifications":
        asyncio.run(cmd_notifications())
    elif args.command == "reply":
        asyncio.run(cmd_reply_single(args.note_id, args.comment_text, args.body, args.confirm))
    elif args.command == "auto-reply":
        asyncio.run(cmd_auto_reply(
            note_id=args.note_id,
            confirm=args.confirm,
            persona_path=args.persona,
            max_replies=args.max_replies,
            delay_seconds=args.delay
        ))


if __name__ == "__main__":
    main()
