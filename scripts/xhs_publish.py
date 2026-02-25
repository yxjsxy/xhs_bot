#!/usr/bin/env python3
"""
小红书图文发布脚本 — 通过 CDP 连接 OpenClaw 浏览器，复用登录态。

用法:
  # 发布（需要 --confirm 才真正点发布）
  python3 xhs_publish.py --title "标题" --body "正文内容" --images img1.png img2.png --confirm

  # 预览模式（填写内容但不发布，返回状态）
  python3 xhs_publish.py --title "标题" --body "正文内容" --images img1.png

  # 从 JSON 文件读取内容
  python3 xhs_publish.py --from-json content.json --confirm

JSON 格式:
  {"title": "标题", "body": "正文...", "images": ["path1.png"], "tags": ["tag1", "tag2"]}

退出码:
  0 = 成功发布 / 预览就绪
  1 = 参数错误
  2 = 浏览器连接失败
  3 = 页面操作失败
"""

import argparse
import asyncio
import json
import sys
import os
from pathlib import Path

# CDP endpoint of OpenClaw's browser
CDP_ENDPOINT = os.environ.get("XHS_CDP_ENDPOINT", "http://127.0.0.1:18800")

async def connect_browser():
    """Connect to the running OpenClaw browser via CDP."""
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        return pw, browser
    except Exception as e:
        await pw.stop()
        print(json.dumps({"ok": False, "error": f"CDP connect failed: {e}", "hint": "Is OpenClaw browser running?"}))
        sys.exit(2)


async def publish(title: str, body: str, images: list[str], confirm: bool = False, dry_run: bool = False):
    pw, browser = await connect_browser()

    try:
        # Use existing context (has cookies/login)
        context = browser.contexts[0]
        # Reuse existing page if available, else create new
        pages = context.pages
        if pages:
            page = pages[0]
        else:
            page = await context.new_page()

        # Handle "leave page?" confirmation dialogs
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))

        # 1. Navigate to publish page
        print(json.dumps({"step": "navigating"}), file=sys.stderr, flush=True)
        await page.goto(
            "https://creator.xiaohongshu.com/publish/publish?source=official",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await page.wait_for_timeout(3000)  # let page render

        # 2. Switch to 图文 tab (default is video) — use JS click to bypass viewport issues
        switched = await page.evaluate("""() => {
            const tabs = [...document.querySelectorAll('span, div, a')].filter(
                el => el.textContent.trim() === '上传图文'
            );
            for (const tab of tabs) {
                tab.click();
            }
            return tabs.length > 0;
        }""")
        if not switched:
            print(json.dumps({"ok": False, "error": "Cannot find 上传图文 tab"}))
            sys.exit(3)
        await page.wait_for_timeout(1500)

        # 3. Upload images
        file_input = page.locator('input[type="file"]').first
        abs_images = [str(Path(p).resolve()) for p in images]
        await file_input.set_input_files(abs_images)
        # Wait for upload processing
        await page.wait_for_timeout(3000)

        # 4. Fill title — use evaluate to bypass viewport
        await page.evaluate(
            """(title) => {
                const input = document.querySelector('[placeholder*="标题"]')
                    || document.querySelector('input[class*="title"]');
                if (input) {
                    input.focus();
                    input.value = '';
                    // For contenteditable or input
                    if (input.tagName === 'INPUT') {
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(input, title);
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    } else {
                        input.textContent = title;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
            }""",
            title,
        )

        # 5. Fill body — contenteditable editor, all via evaluate
        paragraphs = body.split("\n")
        html_body = "".join(f"<p>{line if line.strip() else '<br>'}</p>" for line in paragraphs)

        await page.evaluate(
            """(html) => {
                const editors = document.querySelectorAll('[contenteditable="true"]');
                // The body editor is usually the last contenteditable (first is title if contenteditable)
                const editor = editors[editors.length - 1];
                if (editor) {
                    editor.focus();
                    editor.innerHTML = html;
                    editor.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }""",
            html_body,
        )
        await page.wait_for_timeout(500)

        # 6. Read back for verification
        actual_title = await page.evaluate("""() => {
            const input = document.querySelector('[placeholder*="标题"]');
            return input ? (input.value || input.textContent || '') : '';
        }""")
        actual_body_len = await page.evaluate(
            """() => {
                const ed = document.querySelector('[contenteditable="true"]:last-of-type')
                    || [...document.querySelectorAll('[contenteditable="true"]')].pop();
                return ed ? ed.textContent.length : 0;
            }"""
        )

        if not confirm:
            result = {
                "ok": True,
                "status": "preview_ready",
                "title": actual_title.strip(),
                "body_length": actual_body_len,
                "images_count": len(images),
                "message": "Content filled. Pass --confirm to publish.",
            }
            print(json.dumps(result, ensure_ascii=False))
            if dry_run:
                print(json.dumps({"step": "dry_run_waiting_10s"}), file=sys.stderr, flush=True)
                await page.wait_for_timeout(10000)
            return

        # 7. Click publish button via JS
        await page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button')];
            const pub = btns.find(b => b.textContent.includes('发布') && !b.textContent.includes('暂存'));
            if (pub) pub.click();
        }""")

        # 8. Wait for success
        try:
            await page.locator("text=发布成功").wait_for(timeout=10000)
            result = {
                "ok": True,
                "status": "published",
                "title": actual_title.strip(),
                "body_length": actual_body_len,
                "images_count": len(images),
            }
        except Exception:
            # Check for error messages
            page_text = await page.locator("body").text_content()
            if "绑定手机" in (page_text or ""):
                result = {"ok": False, "error": "需要绑定手机号", "hint": "在小红书 APP 中绑定手机号后重试"}
            else:
                result = {"ok": False, "error": "发布后未检测到成功提示", "page_snippet": (page_text or "")[:200]}

        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(3)
    finally:
        await pw.stop()


def main():
    parser = argparse.ArgumentParser(description="小红书图文发布")
    parser.add_argument("--title", type=str, help="笔记标题 (≤20字)")
    parser.add_argument("--body", type=str, help="笔记正文")
    parser.add_argument("--images", nargs="+", help="图片路径列表")
    parser.add_argument("--tags", nargs="*", default=[], help="话题标签（自动追加到正文末尾）")
    parser.add_argument("--from-json", type=str, help="从 JSON 文件读取 title/body/images/tags")
    parser.add_argument("--confirm", action="store_true", help="确认发布（不传则只填写不发布）")
    parser.add_argument("--dry-run", action="store_true", help="填写内容后等待 10 秒供截图验证，然后退出")
    args = parser.parse_args()

    # Load from JSON if specified
    if args.from_json:
        with open(args.from_json) as f:
            data = json.load(f)
        title = data.get("title", args.title)
        body = data.get("body", args.body)
        images = data.get("images", args.images or [])
        tags = data.get("tags", args.tags or [])
    else:
        title = args.title
        body = args.body
        images = args.images or []
        tags = args.tags or []

    if not title or not body or not images:
        print(json.dumps({"ok": False, "error": "Missing required: --title, --body, --images"}))
        sys.exit(1)

    # Append tags to body
    if tags:
        tag_line = " ".join(f"#{t}" for t in tags)
        body = body.rstrip() + "\n\n" + tag_line

    asyncio.run(publish(title, body, images, confirm=args.confirm, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
