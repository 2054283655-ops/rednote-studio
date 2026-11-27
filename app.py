from __future__ import annotations

import json
import io
import asyncio
import hmac
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

import requests
import streamlit as st
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright
import qrcode


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "studio.sqlite3"
MEDIA_DIR = APP_DIR / "media"
WECHAT_INBOX = MEDIA_DIR / "wechat_inbox"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
STATUS_LABELS = {
    "needs_review": "待审核",
    "needs_revision": "需修改",
    "approved": "审核通过",
    "scheduled": "已排期",
    "published": "已发布",
}


def safe_error_message(error: Exception) -> str:
    """Keep provider errors useful without echoing API keys into the UI."""
    message = str(error)
    return re.sub(
        r"(sk-[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+([A-Za-z0-9_-]{4})",
        r"\1...REDACTED...\2",
        message,
    )


def api_base_url(configured_url: str | None = None) -> str:
    configured_url = configured_url or st.session_state.get("api_base_url", "https://api.openai.com/v1")
    base_url = configured_url.strip().rstrip("/")
    if not base_url:
        base_url = "https://api.openai.com/v1"
    if base_url.endswith("/responses"):
        base_url = base_url[:-len("/responses")]
    if not re.search(r"/v\d+(?:/|$)", base_url, re.IGNORECASE):
        base_url = f"{base_url}/v1"
    return base_url


def openai_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=api_base_url())


def require_shared_access() -> None:
    try:
        password = str(st.secrets.get("APP_PASSWORD", "")).strip()
        public_app = bool(st.secrets.get("PUBLIC_APP", False))
    except Exception:
        password = os.environ.get("APP_PASSWORD", "").strip()
        public_app = os.environ.get("PUBLIC_APP", "").strip().lower() in {"1", "true", "yes"}
    if not password and not public_app:
        return
    if not password:
        st.error("共享访问尚未配置。请管理员在部署 Secrets 中设置 APP_PASSWORD。")
        st.stop()
    if st.session_state.get("shared_access_granted"):
        return
    st.title("画室小红书运营工作台")
    st.write("这是受密码保护的共享工作台，请输入管理员提供的访问密码。")
    with st.form("shared-access-login"):
        entered_password = st.text_input("访问密码", type="password")
        submitted = st.form_submit_button("进入工作台", type="primary")
    if submitted:
        if hmac.compare_digest(entered_password, password):
            st.session_state.shared_access_granted = True
            st.rerun()
        st.error("密码不正确。")
    st.stop()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize() -> None:
    with db() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                tags TEXT NOT NULL,
                image_brief TEXT NOT NULL,
                requirements TEXT NOT NULL,
                status TEXT NOT NULL,
                review TEXT NOT NULL,
                image_paths TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(posts)").fetchall()}
        for name, declaration in {
            "content_type": "TEXT NOT NULL DEFAULT '画室日常'",
            "source_info": "TEXT NOT NULL DEFAULT ''",
            "event_plan": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {declaration}")


def load_settings() -> dict:
    defaults = {
        "account_name": "画室小红书",
        "niche": "画室日常、美术基础知识、学员作品与绘画兴趣活动",
        "audience": "本地对绘画感兴趣的人、学员及家长",
        "voice": "有审美、专业但自然，尊重每个人的创作表达",
        "rules": "知识内容给出来源或标明是经验观点；不编造艺术家原话和艺术史事实；展示学员作品前确认授权；不公开群聊昵称、头像、联系方式；活动信息核对日期、地点、费用和报名方式。",
        "cta": "欢迎咨询画室课程或报名绘画活动",
        "cadence": "3",
        "weekdays": json.dumps(["周二", "周四", "周六"], ensure_ascii=False),
        "publish_time": "20:00",
    }
    with db() as connection:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    defaults.update({row["key"]: row["value"] for row in rows})
    return defaults


def save_settings(values: dict) -> None:
    with db() as connection:
        connection.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)) for key, value in values.items()],
        )


def all_posts() -> list[sqlite3.Row]:
    with db() as connection:
        return connection.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()


def import_work_image(uploaded_file, context: str, permission_confirmed: bool) -> None:
    WECHAT_INBOX.mkdir(parents=True, exist_ok=True)
    asset_id = uuid.uuid4().hex[:12]
    safe_name = Path(uploaded_file.name).name
    image_path = WECHAT_INBOX / f"{asset_id}_{safe_name}"
    image_path.write_bytes(uploaded_file.getvalue())
    metadata = {
        "id": asset_id,
        "filename": safe_name,
        "context": context,
        "permission_confirmed": permission_confirmed,
        "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "path": str(image_path.relative_to(APP_DIR)),
    }
    image_path.with_suffix(image_path.suffix + ".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def work_images() -> list[dict]:
    if not WECHAT_INBOX.exists():
        return []
    items = []
    for image_path in sorted(WECHAT_INBOX.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True):
        if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        metadata_path = image_path.with_suffix(image_path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        items.append({**metadata, "path": str(image_path.relative_to(APP_DIR)), "absolute_path": image_path})
    return items


def get_post(post_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def write_post(post: dict) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    fields = {
        "id": post.get("id", uuid.uuid4().hex[:12]),
        "topic": post.get("topic", ""),
        "title": post.get("title", ""),
        "body": post.get("body", ""),
        "tags": post.get("tags", ""),
        "image_brief": post.get("image_brief", ""),
        "requirements": post.get("requirements", ""),
        "content_type": post.get("content_type", "画室日常"),
        "source_info": post.get("source_info", ""),
        "event_plan": post.get("event_plan", ""),
        "status": post.get("status", "needs_review"),
        "review": json.dumps(post.get("review", {}), ensure_ascii=False),
        "image_paths": json.dumps(post.get("image_paths", []), ensure_ascii=False),
        "scheduled_at": post.get("scheduled_at", ""),
        "created_at": post.get("created_at", now),
        "updated_at": now,
    }
    with db() as connection:
        connection.execute(
            """INSERT INTO posts(id, topic, title, body, tags, image_brief, requirements,
               content_type, source_info, event_plan, status, review, image_paths, scheduled_at, created_at, updated_at)
               VALUES(:id, :topic, :title, :body, :tags, :image_brief, :requirements,
               :content_type, :source_info, :event_plan,
               :status, :review, :image_paths, :scheduled_at, :created_at, :updated_at)
               ON CONFLICT(id) DO UPDATE SET topic=excluded.topic, title=excluded.title,
               body=excluded.body, tags=excluded.tags, image_brief=excluded.image_brief,
               requirements=excluded.requirements, content_type=excluded.content_type,
               source_info=excluded.source_info, event_plan=excluded.event_plan,
               status=excluded.status, review=excluded.review,
               image_paths=excluded.image_paths, scheduled_at=excluded.scheduled_at,
               updated_at=excluded.updated_at""",
            fields,
        )


def delete_post(post_id: str) -> None:
    with db() as connection:
        connection.execute("DELETE FROM posts WHERE id = ?", (post_id,))


def call_json_model(api_key: str, system_prompt: str, user_prompt: str, model: str) -> dict:
    response = openai_client(api_key).chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("模型响应中没有可读取的文本，请检查中转站返回内容。")
    return json.loads(content)


def profile_context(profile: dict) -> str:
    return "\n".join(
        [
            f"账号定位：{profile['niche']}",
            f"目标读者：{profile['audience']}",
            f"表达风格：{profile['voice']}",
            f"内容规则：{profile['rules']}",
            f"互动引导：{profile['cta']}",
        ]
    )


def make_post(api_key: str, model: str, profile: dict, topic: str, brief: str, format_name: str, content_type: str = "画室日常") -> dict:
    system = """你是画室的小红书内容编辑。根据账号要求和指定内容类型写原创中文图文。
美术知识类要把事实、来源和个人经验分开，不要编造艺术史、技法结论或名家原话，缺少来源时提示补充。
作品分享类突出作品本身、媒材和创作过程，不比较学员、不推断作者身份，不写未提供的年龄或获奖经历。
活动类同时给出清楚的活动介绍、适合对象、时间地点、费用和报名方式；缺少字段写“待补充”，不要虚构。
标题具体，正文自然，避免过度营销。只返回 JSON，字段为 title、body、tags、image_brief、event_plan。tags 是不带井号的中文标签数组，非活动内容的 event_plan 留空。"""
    prompt = f"{profile_context(profile)}\n内容类型：{content_type}\n本篇主题：{topic}\n事实来源/作品信息/活动信息：{brief or '尚未提供'}\n笔记结构：{format_name}\n输出可编辑的完整草稿。活动类型需另附一段简要执行方案。"
    result = call_json_model(api_key, system, prompt, model)
    tags = result.get("tags", [])
    if isinstance(tags, str):
        tags = [tag.strip().lstrip("#") for tag in tags.replace("，", ",").split(",") if tag.strip()]
    return {
        "topic": topic,
        "title": str(result.get("title", "")).strip(),
        "body": str(result.get("body", "")).strip(),
        "tags": " ".join(f"#{str(tag).strip().lstrip('#')}" for tag in tags if str(tag).strip()),
        "image_brief": str(result.get("image_brief", "")).strip(),
        "event_plan": str(result.get("event_plan", "")).strip(),
        "content_type": content_type,
        "source_info": brief,
        "requirements": brief,
        "status": "needs_review",
    }


def plan_art_event(api_key: str, model: str, profile: dict, theme: str, constraints: str) -> dict:
    system = """你是画室活动策划。围绕绘画兴趣设计一个能在小型画室落地的体验活动。
只返回 JSON，字段为 activity_name、concept、audience、duration、schedule_suggestion、venue_suggestion、fee_suggestion、capacity_suggestion、materials、agenda、preparation、promotion_angle、poster_visual。
agenda 是数组，每项包含 time 与 activity。不得把未提供的日期、地址、费用、名额写成已确认事实。缺少信息时用明确建议并标注“待画室确认”。方案兼顾新手参与体验、作品完成度和现场执行。"""
    prompt = f"{profile_context(profile)}\n活动主题：{theme}\n画室提供的约束：{constraints or '无。请给出易执行的建议，并把日期、地点、费用、人数标为待画室确认。'}\n给出完整活动方案，宣传角度真实克制。"
    return call_json_model(api_key, system, prompt, model)


def write_art_event_promo(api_key: str, model: str, profile: dict, event_plan: dict, confirmed_details: str) -> dict:
    system = """你是画室的小红书编辑。根据已确认的活动方案写一篇活动招募笔记。
只返回 JSON，字段为 title、body、tags、image_brief。不要补造时间、地点、费用、名额、师资或效果。宣传文案使用清楚的报名引导，信息不足时写“私信咨询”而不捏造联系方式。tags 是不带井号的中文标签数组。"""
    prompt = f"{profile_context(profile)}\n活动方案：{json.dumps(event_plan, ensure_ascii=False)}\n已确认对外信息：\n{confirmed_details}\n输出可发布的活动宣传笔记。"
    result = call_json_model(api_key, system, prompt, model)
    tags = result.get("tags", [])
    if isinstance(tags, str):
        tags = [tag.strip().lstrip("#") for tag in tags.replace("，", ",").split(",") if tag.strip()]
    return {
        "title": str(result.get("title", "")).strip(),
        "body": str(result.get("body", "")).strip(),
        "tags": " ".join(f"#{str(tag).strip().lstrip('#')}" for tag in tags if str(tag).strip()),
        "image_brief": str(result.get("image_brief", "")).strip(),
    }


def review_post(api_key: str, model: str, profile: dict, post: dict) -> dict:
    system = """你是小红书内容的发布前审核员。对照账号规则审核标题、正文、标签和配图说明。
检查隐私与个人信息、违法危险内容、抄袭倾向、广告和效果承诺、医疗金融法律等专业结论、艺术史与技法事实是否有依据、标题与正文是否一致、标签是否相关、作品展示是否提示授权、活动信息是否完整。
不能验证外部事实或图片真实内容。对需核实的事实和专业主张必须列为 revise，不能自行认定真实。
只返回 JSON：decision 为 pass、revise 或 block 之一，另有 issues、suggestions、caveats 三个字符串数组。pass 只表示未发现明显文本风险，不构成平台审核保证。"""
    content = {key: post.get(key, "") for key in ("content_type", "topic", "title", "body", "tags", "image_brief", "source_info", "event_plan")}
    result = call_json_model(
        api_key,
        system,
        f"账号规则：\n{profile_context(profile)}\n\n待审核内容：\n{json.dumps(content, ensure_ascii=False)}",
        model,
    )
    if result.get("decision") not in {"pass", "revise", "block"}:
        result["decision"] = "revise"
        result.setdefault("issues", []).append("审核结构异常，请人工检查。")
    for key in ("issues", "suggestions", "caveats"):
        if not isinstance(result.get(key), list):
            result[key] = []
    return result


def make_event_poster(api_key: str, post: dict) -> Path:
    event_text = post.get("source_info", "") or post.get("event_plan", "")
    prompt = (
        "Create a refined portrait illustration for a small independent art studio's painting workshop. "
        "Use a tactile watercolor and gouache palette, visible paper grain, warm cream paper, muted vermilion, "
        "sage green and cobalt accents, a welcoming tabletop with brushes, palette and unfinished canvas. "
        "Leave the central lower third calm and low-detail for readable text to be added separately. "
        "No words, no letters, no logo, no watermark, no people or faces. "
        f"The event is about: {post.get('topic', '')}. Visual direction: {post.get('image_brief', '')}. "
        "Compose for a 4:5 portrait social media poster."
    )
    client = openai_client(api_key)
    response = client.images.generate(model="dall-e-3", prompt=prompt, n=1, size="1024x1024", quality="standard")
    image_response = requests.get(response.data[0].url, timeout=60)
    image_response.raise_for_status()

    image = Image.open(io.BytesIO(image_response.content)).convert("RGB")
    target_width, target_height = 1080, 1350
    scale = max(target_width / image.width, target_height / image.height)
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    left = (image.width - target_width) // 2
    top = (image.height - target_height) // 2
    poster = image.crop((left, top, left + target_width, top + target_height)).convert("RGBA")
    overlay = Image.new("RGBA", poster.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for y in range(760, target_height):
        alpha = int(35 + 120 * (y - 760) / (target_height - 760))
        overlay_draw.line((0, y, target_width, y), fill=(28, 27, 24, alpha))
    poster = Image.alpha_composite(poster, overlay)
    draw = ImageDraw.Draw(poster)
    font_path = "C:/Windows/Fonts/msyh.ttc"
    try:
        label_font = ImageFont.truetype(font_path, 28)
        title_font = ImageFont.truetype(font_path, 68)
        detail_font = ImageFont.truetype(font_path, 30)
    except OSError:
        label_font = ImageFont.load_default()
        title_font = ImageFont.load_default()
        detail_font = ImageFont.load_default()

    draw.rounded_rectangle((68, 70, 358, 126), radius=28, fill=(255, 250, 241, 235))
    draw.text((92, 82), "画室绘画活动", font=label_font, fill=(125, 54, 39, 255))
    title = post.get("topic", "绘画体验活动")[:24]
    draw.text((70, 810), title, font=title_font, fill=(255, 252, 246, 255), stroke_width=1, stroke_fill=(25, 24, 22, 80))
    detail_lines = [line.strip() for line in event_text.splitlines() if line.strip()][:4]
    y = 1015
    for line in detail_lines:
        draw.text((74, y), line[:42], font=detail_font, fill=(255, 250, 241, 255))
        y += 52

    poster_dir = MEDIA_DIR / "posters"
    poster_dir.mkdir(parents=True, exist_ok=True)
    poster_path = poster_dir / f"{post['id']}_activity-poster.png"
    poster.convert("RGB").save(poster_path, format="PNG", optimize=True)
    return poster_path


async def _xhs_signature(uri: str, data=None, a1: str = "", web_session: str = "") -> dict:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)
            await context.add_cookies([{"name": "a1", "value": a1 or "", "domain": ".xiaohongshu.com", "path": "/"}])
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)
            values = await page.evaluate("([url, body]) => window._webmsxyw(url, body)", [uri, data])
            return {"x-s": values["X-s"], "x-t": str(values["X-t"])}
        finally:
            await browser.close()


def xhs_signature(uri: str, data=None, a1: str = "", web_session: str = "") -> dict:
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_xhs_signature(uri, data, a1, web_session))
    finally:
        loop.close()


def new_xhs_client():
    from xhs import XhsClient

    return XhsClient(sign=xhs_signature)


def post_to_xhs(client, post: dict) -> dict:
    image_paths = [str(APP_DIR / path) for path in json.loads(post.get("image_paths", "[]"))]
    image_paths = [path for path in image_paths if Path(path).is_file()]
    if not image_paths:
        raise ValueError("先为笔记添加至少一张图片，再发布图文笔记。")
    return client.create_image_note(
        post["title"],
        post["body"] + ("\n\n" + post["tags"] if post["tags"] else ""),
        image_paths,
        is_private=False,
    )


def status_badge(status: str) -> str:
    return f'<span class="status status--{status}">{STATUS_LABELS.get(status, status)}</span>'


def fmt_date(value: str) -> str:
    if not value:
        return "未安排时间"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m月%d日 %H:%M")
    except ValueError:
        return value


def note_editor(row: sqlite3.Row, api_key: str, model: str, profile: dict) -> None:
    post = dict(row)
    post["review"] = json.loads(post["review"] or "{}")
    post["image_paths"] = json.loads(post["image_paths"] or "[]")
    with st.expander(f"{post['title'] or post['topic']}　·　{STATUS_LABELS.get(post['status'], post['status'])}", expanded=post["status"] in {"needs_review", "needs_revision"}):
        st.caption(f"内容类型　{post.get('content_type', '画室日常')}")
        for image_path in post["image_paths"]:
            absolute_path = APP_DIR / image_path
            if absolute_path.is_file():
                st.image(str(absolute_path), width=260)
        if post.get("content_type") == "兴趣活动策划与宣传":
            if st.button("AI 生成活动海报", key=f"poster-{post['id']}", disabled=not api_key, use_container_width=True):
                try:
                    with st.spinner("正在生成活动插画并排版海报…"):
                        poster_path = make_event_poster(api_key, post)
                    post["image_paths"].append(str(poster_path.relative_to(APP_DIR)))
                    write_post(post)
                    st.rerun()
                except Exception as error:
                    st.error(f"海报没有生成成功：{safe_error_message(error)}")
            for image_path in post["image_paths"]:
                if "posters" in image_path:
                    poster_path = APP_DIR / image_path
                    if poster_path.is_file():
                        st.download_button("下载活动海报 PNG", poster_path.read_bytes(), file_name=poster_path.name, mime="image/png", key=f"poster-download-{post['id']}")
        with st.form(f"edit-{post['id']}"):
            title = st.text_input("标题", post["title"])
            body = st.text_area("正文", post["body"], height=260)
            tags = st.text_input("标签", post["tags"])
            image_brief = st.text_area("配图构思", post["image_brief"], height=90)
            source_info = st.text_area("知识来源或作品/活动信息", post.get("source_info", post["requirements"]), height=90)
            event_plan = st.text_area("活动执行方案", post.get("event_plan", ""), height=120) if post.get("content_type") == "兴趣活动策划与宣传" else post.get("event_plan", "")
            saved = st.form_submit_button("保存修改", use_container_width=True)
        if saved:
            updated = {**post, "title": title, "body": body, "tags": tags, "image_brief": image_brief, "source_info": source_info, "requirements": source_info, "event_plan": event_plan, "review": {}, "status": "needs_review", "scheduled_at": ""}
            write_post(updated)
            st.rerun()

        if post["review"]:
            decision = post["review"].get("decision", "")
            st.markdown(f"**AI 审核：{ {'pass': '通过', 'revise': '需修改', 'block': '拦截'}.get(decision, decision)}**")
            for issue in post["review"].get("issues", []):
                st.warning(issue)
            for suggestion in post["review"].get("suggestions", []):
                st.write(f"修改建议：{suggestion}")
            for caveat in post["review"].get("caveats", []):
                st.info(f"需核实：{caveat}")

        col_review, col_schedule, col_export = st.columns([1, 1, 1])
        with col_review:
            if st.button("运行 AI 审核", key=f"review-{post['id']}", disabled=not api_key, use_container_width=True):
                try:
                    result = review_post(api_key, model, profile, post)
                    status = {"pass": "approved", "revise": "needs_revision", "block": "needs_revision"}[result["decision"]]
                    write_post({**post, "review": result, "status": status})
                    st.rerun()
                except Exception as error:
                    st.error(f"审核没有完成：{safe_error_message(error)}")
        with col_schedule:
            can_schedule = post["status"] == "approved"
            chosen_day = st.date_input("排期日期", value=date.today() + timedelta(days=1), key=f"date-{post['id']}", label_visibility="collapsed")
            if st.button("加入发布计划", key=f"schedule-{post['id']}", disabled=not can_schedule, use_container_width=True):
                time_parts = profile["publish_time"].split(":")
                chosen_time = time(int(time_parts[0]), int(time_parts[1]))
                scheduled = datetime.combine(chosen_day, chosen_time).astimezone().isoformat(timespec="minutes")
                write_post({**post, "status": "scheduled", "scheduled_at": scheduled})
                st.rerun()
        with col_export:
            event_text = f"\n\n## 活动执行方案\n\n{post.get('event_plan', '')}" if post.get("event_plan") else ""
            source_text = f"\n\n参考来源/素材信息：{post.get('source_info', '')}" if post.get("source_info") else ""
            markdown = f"# {post['title']}\n\n{post['body']}\n\n{post['tags']}\n\n配图构思：{post['image_brief']}{source_text}{event_text}"
            st.download_button("导出笔记", markdown, file_name=f"rednote-{post['id']}.md", mime="text/markdown", key=f"export-{post['id']}", use_container_width=True)

        if post["status"] == "scheduled":
            st.caption(f"计划时间　{fmt_date(post['scheduled_at'])}")
            if st.button("移回草稿", key=f"unschedule-{post['id']}"):
                write_post({**post, "status": "approved", "scheduled_at": ""})
                st.rerun()
        elif post["status"] == "approved":
            st.success("审核已通过。你可以安排时间，或导出后在官方创作中心发布。")
        if post["status"] in {"approved", "scheduled"}:
            st.link_button("打开小红书创作中心", "https://creator.xiaohongshu.com/", use_container_width=True)
            st.caption("创作中心入口可在浏览器登录后手动发布。扫码连接后，也可在本工作台确认并提交。")
        if post["status"] in {"approved", "scheduled"} and st.session_state.get("xhs_connected"):
            confirmed = st.checkbox("我已检查标题、正文和配图，确认公开发布", key=f"confirm-public-{post['id']}")
            if st.button("确认后直接发布到小红书", key=f"publish-xhs-{post['id']}", disabled=not confirmed, type="primary", use_container_width=True):
                try:
                    with st.spinner("正在提交图文笔记…"):
                        result = post_to_xhs(st.session_state.xhs_client, post)
                    write_post({**post, "status": "published", "scheduled_at": ""})
                    st.success("小红书已返回发布成功。")
                    st.json(result)
                    st.rerun()
                except Exception as error:
                    st.error(f"发布没有完成：{safe_error_message(error)}")
        if post["status"] == "scheduled":
            if st.button("标记为已发布", key=f"published-{post['id']}"):
                write_post({**post, "status": "published"})
                st.rerun()
        if st.button("删除这篇草稿", key=f"delete-{post['id']}"):
            delete_post(post["id"])
            st.rerun()


def main() -> None:
    initialize()
    st.set_page_config(page_title="画室小红书运营工作台", page_icon="✳", layout="wide", initial_sidebar_state="expanded")
    require_shared_access()
    st.markdown(
        """<style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&display=swap');
        :root { --paper:#f7f5f1; --surface:#fffdfa; --ink:#24221f; --muted:#706a61; --line:#e5dfd6; --accent:#b8422b; --accent-soft:#f4e8e3; }
        html, body, [class*="css"] { font-family:'DM Sans','Noto Sans SC',sans-serif; color:var(--ink); }
        .stApp { background:var(--paper); }
        [data-testid="stSidebar"] { background:#eee9e1; border-right:1px solid var(--line); }
        [data-testid="stSidebar"] h1 { font-size:1.22rem; letter-spacing:-.03em; }
        .block-container { max-width:1280px; padding-top:2.6rem; padding-bottom:4rem; }
        h1,h2,h3 { color:var(--ink); letter-spacing:-.035em; }
        h1 { font-size:2.25rem; font-weight:600; }
        h2 { font-size:1.35rem; font-weight:600; }
        [data-testid="stMetric"] { background:var(--surface); border:1px solid var(--line); padding:1rem 1.15rem; border-radius:12px; }
        [data-testid="stMetricLabel"] { color:var(--muted); }
        [data-testid="stMetricValue"] { font-weight:600; }
        .eyebrow { color:var(--accent); font-size:.78rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
        .lede { color:var(--muted); font-size:1.03rem; line-height:1.65; max-width:60rem; }
        .panel { background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:1.25rem 1.35rem; }
        .status { display:inline-block; border-radius:999px; padding:.25rem .65rem; background:#eeeae4; color:#514b43; font-size:.78rem; font-weight:600; }
        .status--approved { background:#e6eee6; color:#36543a; }
        .status--scheduled { background:#e7edf3; color:#34516a; }
        .status--needs_revision { background:#f5e7df; color:#803d24; }
        .status--published { background:#e9e8e3; color:#4e4a40; }
        div.stButton > button[kind="primary"] { background:var(--accent); border-color:var(--accent); }
        div.stButton > button, div.stDownloadButton > button { border-radius:8px; min-height:2.8rem; font-weight:600; transition:transform .15s ease, background-color .15s ease; }
        div.stButton > button:hover, div.stDownloadButton > button:hover { transform:translateY(-1px); }
        [data-testid="stExpander"] { background:var(--surface); border:1px solid var(--line); border-radius:12px; }
        [data-baseweb="tab-list"] { gap:.35rem; }
        [data-baseweb="tab"] { padding:.65rem .95rem; }
        hr { border-color:var(--line); }
        @media(max-width:640px) { h1 {font-size:1.8rem;} .block-container {padding-top:1.4rem;} }
        </style>""",
        unsafe_allow_html=True,
    )

    profile = load_settings()
    if "api_key" not in st.session_state:
        st.session_state.api_key = ""
    if "xhs_connected" not in st.session_state:
        st.session_state.xhs_connected = False
    if "api_base_url" not in st.session_state:
        st.session_state.api_base_url = "https://api.openai.com/v1"
    if "api_model" not in st.session_state:
        st.session_state.api_model = "gpt-4o-mini"
    model = st.session_state.api_model

    with st.sidebar:
        st.markdown("# ✳ 内容工作台")
        st.caption("独立的选题、审核与排期流程")
        api_key = st.text_input("API Key", type="password", value=st.session_state.api_key, help="仅保存在当前运行会话内，不写入工作台数据库。")
        st.session_state.api_key = api_key
        api_base_url = st.text_input(
            "API 接口地址",
            value=st.session_state.api_base_url,
            key="api-base-url-input",
            help="填写服务商 API 基础地址，例如 https://zwaca.cc/v1。文本生成使用标准 Chat Completions 接口。",
        ).strip()
        st.session_state.api_base_url = api_base_url
        api_model = st.text_input(
            "模型名称",
            value=st.session_state.api_model,
            key="api-model-input",
            help="填写服务商文档或控制台列出的模型 ID。",
        ).strip()
        st.session_state.api_model = api_model
        model = st.session_state.api_model or "gpt-4o-mini"
        st.caption("接口设置和 API Key 仅保存在当前运行会话。")
        st.caption(f"文本模型　{model}")
        st.divider()
        st.markdown("**小红书账号**")
        st.write(profile["account_name"])
        if st.session_state.xhs_connected:
            st.success("账号已扫码连接（当前会话）")
            if st.button("断开小红书账号", use_container_width=True):
                for key in ("xhs_connected", "xhs_client", "xhs_qr_info", "xhs_qr_image"):
                    st.session_state.pop(key, None)
                st.session_state.xhs_connected = False
                st.rerun()
        else:
            st.caption("账号状态：未连接")
            if st.button("生成扫码登录二维码", use_container_width=True):
                try:
                    with st.spinner("正在向小红书申请登录二维码…"):
                        client = new_xhs_client()
                        qr_info = client.get_qrcode()
                        qr_image = qrcode.make(qr_info["url"])
                        qr_buffer = io.BytesIO()
                        qr_image.save(qr_buffer, format="PNG")
                        st.session_state.xhs_client = client
                        st.session_state.xhs_qr_info = qr_info
                        st.session_state.xhs_qr_image = qr_buffer.getvalue()
                except Exception as error:
                    st.error(f"二维码申请失败：{safe_error_message(error)}")
            if st.session_state.get("xhs_qr_image"):
                st.image(st.session_state.xhs_qr_image, width=210, caption="请用小红书 App 扫码登录")
                if st.button("检查扫码状态", use_container_width=True):
                    try:
                        qr_info = st.session_state.xhs_qr_info
                        result = st.session_state.xhs_client.check_qrcode(qr_info["qr_id"], qr_info["code"])
                        if result.get("code_status") == 2:
                            st.session_state.xhs_connected = True
                            st.session_state.pop("xhs_qr_info", None)
                            st.session_state.pop("xhs_qr_image", None)
                            st.rerun()
                        st.info("还没有确认登录。请在手机小红书里完成扫码确认后再检查。")
                    except Exception as error:
                        st.error(f"检查登录状态失败：{safe_error_message(error)}")
        st.link_button("打开小红书创作中心", "https://creator.xiaohongshu.com/", use_container_width=True)
        if not profile["niche"]:
            st.info("先在「账号规范」填写账号定位，再开始生成。")
        st.markdown("**发布方式**")
        st.caption("扫码连接后，可在审核通过的笔记下确认发布。排期保存在本机。")

    posts = all_posts()
    now = datetime.now().astimezone()
    today_count = sum(1 for row in posts if row["created_at"][:10] == now.date().isoformat())
    pending_count = sum(1 for row in posts if row["status"] in {"needs_review", "needs_revision"})
    scheduled_rows = [row for row in posts if row["status"] == "scheduled"]
    st.markdown('<div class="eyebrow">REDNOTE OPERATIONS</div>', unsafe_allow_html=True)
    st.title("画室小红书运营工作台")
    st.markdown('<p class="lede">把账号要求固定下来，按计划产出笔记，逐篇审核并整理发布日历。</p>', unsafe_allow_html=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("本机草稿", len(posts))
    m2.metric("待审核", pending_count)
    m3.metric("已排期", len(scheduled_rows))
    m4.metric("今天新增", today_count)

    tabs = st.tabs(["工作台", "生成内容", "作品素材", "活动策划", "审核与草稿", "发布日历", "账号规范"])

    with tabs[0]:
        left, right = st.columns([1.3, 1])
        with left:
            st.subheader("本周发布计划")
            selected_days = json.loads(profile["weekdays"])
            st.write(f"每周计划发布 **{profile['cadence']} 篇**，常用时间 **{profile['publish_time']}**")
            st.caption("计划只是排期提醒。发布前仍由你在小红书创作中心确认。")
            st.write("　".join(f"· {day}" if day in selected_days else f"　{day}" for day in WEEKDAYS))
            if scheduled_rows:
                for row in sorted(scheduled_rows, key=lambda item: item["scheduled_at"]):
                    st.markdown(f"{status_badge(row['status'])}　**{row['title']}**　{fmt_date(row['scheduled_at'])}", unsafe_allow_html=True)
            else:
                st.info("还没有排期。先生成一篇笔记，审核通过后即可加入日历。")
        with right:
            st.subheader("账号内容规范")
            st.markdown(f"**{profile['account_name']}**")
            st.write(profile["niche"] or "填写账号定位后，AI 会据此生成内容。")
            if profile["audience"]:
                st.caption(f"主要读者　{profile['audience']}")
            st.caption(f"表达风格　{profile['voice']}")
            st.markdown("**审核规则**")
            st.write(profile["rules"])
        st.divider()
        st.subheader("最近内容")
        if posts:
            for row in posts[:4]:
                st.markdown(f"{status_badge(row['status'])}　**{row['title'] or row['topic']}**　<span style='color:#706a61'>{fmt_date(row['created_at'])}</span>", unsafe_allow_html=True)
        else:
            st.caption("内容会保存在这台电脑的本地数据库中。")

    with tabs[1]:
        st.subheader("按账号要求生成一篇笔记")
        content_type = st.selectbox("内容类型", ["美术知识分享", "画室/学员作品分享"], key="studio-content-type")
        with st.form("generate-post"):
            topic = st.text_input("本篇主题", placeholder={"美术知识分享": "例如：水彩叠色时怎样减少画面变脏", "画室/学员作品分享": "例如：一张从线稿到上色的静物作品"}[content_type])
            if content_type == "美术知识分享":
                source_info = st.text_area("参考来源与要点", placeholder="粘贴你搜索到的文章链接、书名页码或可靠来源，并写下想解释的知识点。来源留空时，AI 会提示核查。", height=115)
                brief = st.text_area("面向读者怎么讲", placeholder="例如：面向零基础，用一个简单练习解释冷暖色。", height=80)
                event_plan = ""
                permission_confirmed = False
                format_options = ["技法步骤", "知识拆解", "常见问题", "材料清单"]
            elif content_type == "画室/学员作品分享":
                source_info = st.text_area("作品背景", placeholder="作品主题、媒材、创作过程、作者希望分享的想法。不要填群聊昵称或联系方式。", height=95)
                permission_confirmed = st.checkbox("我已确认作者（未成年人作品则包括监护人）同意公开展示")
                brief = st.text_area("分享重点", placeholder="例如：重点讲画面里的色彩选择，文案语气轻松，不评价学员水平。", height=75)
                event_plan = ""
                format_options = ["作品故事", "创作过程", "画室作品集", "老师点评（已获同意）"]
            format_name = st.selectbox("笔记结构", format_options)
            uploaded = st.file_uploader("已有配图（可选）", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
            generate = st.form_submit_button("生成并加入待审核", type="primary", use_container_width=True)
        if generate:
            if not st.session_state.api_key.strip():
                st.error("请先在左侧填写 OpenAI API Key。输入后会即时生效。")
            elif not topic.strip():
                st.error("请先填写本篇主题，再生成笔记。")
            else:
                try:
                    with st.spinner("正在生成标题、正文和配图构思…"):
                        full_brief = "\n".join(part for part in [source_info.strip(), brief.strip(), f"作品公开展示授权：{'已确认' if permission_confirmed else '待确认'}" if content_type == "画室/学员作品分享" else ""] if part)
                        draft = make_post(st.session_state.api_key, model, profile, topic.strip(), full_brief, format_name, content_type)
                    draft["source_info"] = source_info.strip()
                    generated_event_plan = draft.get("event_plan", "")
                    draft["event_plan"] = "\n\n".join(part for part in [event_plan, generated_event_plan] if part)
                    draft["image_paths"] = []
                    if uploaded:
                        folder = MEDIA_DIR / uuid.uuid4().hex[:12]
                        folder.mkdir(parents=True, exist_ok=True)
                        for image in uploaded:
                            target = folder / Path(image.name).name
                            target.write_bytes(image.getvalue())
                            draft["image_paths"].append(str(target.relative_to(APP_DIR)))
                    write_post(draft)
                    st.success("笔记已保存到待审核。")
                    st.rerun()
                except Exception as error:
                    st.error(f"生成没有完成：{safe_error_message(error)}")
        st.markdown("#### 生成本周内容计划")
        st.caption("围绕美术知识、画室作品和兴趣活动组合本周选题。作品分享素材与活动日期请在生成单篇草稿时补充。")
        plan_theme = st.text_input("本周重点", key="weekly-theme", placeholder="例如：水彩基础、学员作品过程、周末体验活动")
        if st.button("生成本周草稿", disabled=not bool(st.session_state.api_key and profile["niche"] and plan_theme.strip()), use_container_width=True):
            weekdays = json.loads(profile["weekdays"])
            count = int(profile["cadence"])
            plan_prompt = f"{profile_context(profile)}\n本周重点：{plan_theme}\n请在美术知识分享与画室/学员作品分享两个方向间合理安排，生成 {count} 个互不重复的选题和角度。只返回 JSON，结构 {{\"ideas\":[{{\"topic\":\"\",\"brief\":\"\",\"content_type\":\"美术知识分享或画室/学员作品分享\"}}]}}。"
            try:
                with st.spinner("正在规划并撰写本周草稿…"):
                    ideas = call_json_model(st.session_state.api_key, "你是内容策划。主题具体、有实用信息，不重复、不编造数据。", plan_prompt, model).get("ideas", [])[:count]
                    for index, idea in enumerate(ideas):
                        draft_type = idea.get("content_type", "美术知识分享")
                        if draft_type not in {"美术知识分享", "画室/学员作品分享"}:
                            draft_type = "美术知识分享"
                        draft = make_post(st.session_state.api_key, model, profile, idea.get("topic", ""), idea.get("brief", ""), "经验分享", draft_type)
                        draft["requirements"] = f"本周重点：{plan_theme}\n{idea.get('brief', '')}"
                        write_post(draft)
                st.success(f"已生成 {len(ideas)} 篇本周草稿，已加入待审核。")
                st.rerun()
            except Exception as error:
                st.error(f"本周草稿没有完成：{safe_error_message(error)}")
        weekly_missing = []
        if not st.session_state.api_key.strip():
            weekly_missing.append("左侧 OpenAI API Key")
        if not profile["niche"].strip():
            weekly_missing.append("账号定位")
        if not plan_theme.strip():
            weekly_missing.append("本周重点")
        if weekly_missing:
            st.caption("生成本周草稿前还需填写：" + "、".join(weekly_missing) + "。")

    with tabs[2]:
        st.subheader("作品素材收件箱")
        st.caption("把微信聊天里的图片先在电脑上另存，再上传到这里。素材只保存在本机，上传时请注明作品背景与公开展示授权。")
        with st.form("work-image-import"):
            incoming_images = st.file_uploader("导入作品图片", type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True, key="work-inbox-uploader")
            image_context = st.text_area("作品说明", placeholder="作品媒材、创作主题、作者想法；请勿填写群聊昵称、头像或联系方式。", height=90)
            image_permission = st.checkbox("我已确认作品作者（未成年人作品则包括监护人）同意公开展示")
            import_button = st.form_submit_button("保存到本机作品素材库", type="primary", use_container_width=True, disabled=not bool(incoming_images))
        if import_button:
            for incoming in incoming_images:
                import_work_image(incoming, image_context.strip(), image_permission)
            st.success(f"已保存 {len(incoming_images)} 张图片到本机素材收件箱。")
            st.rerun()
        assets = work_images()
        if assets:
            st.markdown("#### 最近导入")
            asset_columns = st.columns(min(3, len(assets)))
            for index, asset in enumerate(assets[:9]):
                with asset_columns[index % len(asset_columns)]:
                    st.image(str(asset["absolute_path"]))
                    st.caption(f"{asset.get('filename', asset['absolute_path'].name)} · {'展示授权已确认' if asset.get('permission_confirmed') else '授权待确认'}")
                    if asset.get("context"):
                        st.write(asset["context"])
        else:
            st.info("素材收件箱为空。上传保存到电脑的作品图片后，会显示在这里。")

    with tabs[3]:
        st.subheader("AI 策划绘画兴趣活动")
        st.caption("先给主题和可用条件，AI 起草活动概念、流程、材料和宣传角度。你核对实际安排后，再生成招募笔记与 AI 海报。")
        with st.form("activity-plan-request"):
            activity_theme = st.text_input("活动主题", placeholder="例如：周末水彩花卉体验 / 亲子黏土与绘画")
            activity_constraints = st.text_area("你已确定的条件（可选）", placeholder="日期范围、画室区域、面向人群、预算或收费方式、人数上限、可用材料。留空的信息由 AI 给出建议并标记待确认。", height=110)
            plan_clicked = st.form_submit_button("AI 策划活动方案", type="primary", use_container_width=True, disabled=not bool(st.session_state.api_key and activity_theme.strip()))
        if plan_clicked:
            try:
                with st.spinner("正在构思活动主题、流程和物料…"):
                    st.session_state.activity_plan = plan_art_event(
                        st.session_state.api_key, model, profile, activity_theme.strip(), activity_constraints.strip()
                    )
                    st.session_state.activity_theme = activity_theme.strip()
                    st.session_state.activity_constraints = activity_constraints.strip()
                st.rerun()
            except Exception as error:
                st.error(f"活动方案没有生成：{safe_error_message(error)}")

        if st.session_state.get("activity_plan"):
            plan = st.session_state.activity_plan
            st.info("以下是 AI 提案，时间、场地、费用和名额需按画室实际情况核对后再做宣传。")
            with st.form("activity-plan-confirm"):
                activity_name = st.text_input("活动名称", value=str(plan.get("activity_name", st.session_state.get("activity_theme", ""))))
                concept = st.text_area("活动内容", value=str(plan.get("concept", "")), height=90)
                audience = st.text_input("适合对象", value=str(plan.get("audience", "")))
                duration = st.text_input("活动时长", value=str(plan.get("duration", "")))
                schedule = st.text_input("日期与时间", value=str(plan.get("schedule_suggestion", "")))
                venue = st.text_input("活动地点", value=str(plan.get("venue_suggestion", "")))
                fee = st.text_input("费用与材料", value=str(plan.get("fee_suggestion", "")))
                capacity = st.text_input("人数与报名方式", value=str(plan.get("capacity_suggestion", "")))
                materials = st.text_area("材料清单", value=str(plan.get("materials", "")), height=80)
                agenda = plan.get("agenda", [])
                agenda_text = "\n".join(f"{item.get('time', '')}  {item.get('activity', '')}" for item in agenda) if isinstance(agenda, list) else str(agenda)
                agenda_text = st.text_area("活动流程", value=agenda_text, height=120)
                preparation = st.text_area("准备事项", value=str(plan.get("preparation", "")), height=80)
                poster_visual = st.text_area("海报视觉方向", value=str(plan.get("poster_visual", "")), height=75)
                details_confirmed = st.checkbox("我已核对日期、地点、费用、人数与报名信息，可以用于对外宣传")
                create_activity = st.form_submit_button("生成招募笔记和 AI 海报", type="primary", use_container_width=True)
            if create_activity:
                if not details_confirmed:
                    st.error("请先核对活动安排，再生成对外宣传材料。")
                else:
                    confirmed_plan = {
                        "activity_name": activity_name,
                        "concept": concept,
                        "audience": audience,
                        "duration": duration,
                        "schedule": schedule,
                        "venue": venue,
                        "fee": fee,
                        "capacity_and_signup": capacity,
                        "materials": materials,
                        "agenda": agenda_text,
                        "preparation": preparation,
                        "poster_visual": poster_visual,
                    }
                    confirmed_details = "\n".join([
                        f"活动名称：{activity_name}", f"适合对象：{audience}", f"时长：{duration}",
                        f"日期时间：{schedule}", f"地点：{venue}", f"费用/材料：{fee}",
                        f"人数/报名：{capacity}",
                    ])
                    event_plan_text = "\n".join([
                        f"活动概念：{concept}", f"流程：{agenda_text}", f"材料：{materials}",
                        f"准备事项：{preparation}",
                    ])
                    draft = {
                        "id": uuid.uuid4().hex[:12],
                        "topic": activity_name,
                        "content_type": "兴趣活动策划与宣传",
                        "source_info": confirmed_details,
                        "event_plan": event_plan_text,
                        "image_brief": poster_visual,
                        "requirements": st.session_state.get("activity_constraints", ""),
                        "status": "needs_review",
                        "image_paths": [],
                    }
                    try:
                        with st.spinner("正在撰写招募笔记并生成活动插画海报…"):
                            promo = write_art_event_promo(
                                st.session_state.api_key, model, profile, confirmed_plan, confirmed_details
                            )
                            draft.update(promo)
                            write_post(draft)
                            poster_path = make_event_poster(st.session_state.api_key, draft)
                            draft["image_paths"] = [str(poster_path.relative_to(APP_DIR))]
                            write_post(draft)
                        st.success("活动方案、招募笔记和海报已保存到「审核与草稿」。")
                        st.rerun()
                    except Exception as error:
                        st.error(f"活动宣传材料没有全部完成：{safe_error_message(error)}")

    with tabs[4]:
        st.subheader("审核与草稿")
        st.caption("审核通过只表示 AI 没发现明显文本问题。事实来源和图片仍需你自己核对。")
        if not posts:
            st.info("还没有笔记。到「生成内容」创建第一篇。")
        else:
            filter_label = st.selectbox("显示内容", ["全部", "待审核", "需修改", "审核通过", "已排期", "已发布"], label_visibility="collapsed")
            filter_code = {"全部": None, "待审核": "needs_review", "需修改": "needs_revision", "审核通过": "approved", "已排期": "scheduled", "已发布": "published"}[filter_label]
            for row in posts:
                if filter_code is None or row["status"] == filter_code:
                    note_editor(row, st.session_state.api_key, model, profile)

    with tabs[5]:
        st.subheader("发布日历")
        st.caption("排期记录和内容保存在本机。到计划时间后，打开创作中心粘贴发布。")
        calendar_rows = [row for row in posts if row["status"] in {"scheduled", "published"}]
        if not calendar_rows:
            st.info("日历还空着。审核通过的笔记可以从「审核与草稿」加入发布计划。")
        else:
            for row in sorted(calendar_rows, key=lambda item: item["scheduled_at"] or item["updated_at"]):
                with st.container(border=True):
                    col_a, col_b, col_c = st.columns([3, 1, 1])
                    col_a.markdown(f"**{row['title']}**\n\n{row['topic']}")
                    col_b.write(fmt_date(row["scheduled_at"]))
                    col_c.markdown(status_badge(row["status"]), unsafe_allow_html=True)
                    if row["status"] == "scheduled":
                        st.link_button("打开小红书创作中心", "https://creator.xiaohongshu.com/")
                        if st.button("我已发布", key=f"calendar-pub-{row['id']}"):
                            draft = dict(row)
                            draft["review"] = json.loads(row["review"] or "{}")
                            draft["image_paths"] = json.loads(row["image_paths"] or "[]")
                            draft["status"] = "published"
                            write_post(draft)
                            st.rerun()

    with tabs[6]:
        st.subheader("账号规范")
        st.caption("这些规则会用于每次生成和审核。保存后会保留在本机。")
        with st.form("profile-form"):
            account_name = st.text_input("账号名称", profile["account_name"])
            niche = st.text_area("账号定位", profile["niche"], placeholder="你持续分享什么内容，核心价值是什么", height=85)
            audience = st.text_area("目标读者", profile["audience"], placeholder="他们是谁、关心什么、处于什么阶段", height=85)
            voice = st.text_input("表达风格", profile["voice"])
            rules = st.text_area("内容规则", profile["rules"], height=120)
            cta = st.text_input("互动引导", profile["cta"])
            cadence = st.number_input("每周计划篇数", min_value=1, max_value=7, value=int(profile["cadence"]))
            weekdays = st.multiselect("优先发布日", WEEKDAYS, default=json.loads(profile["weekdays"]))
            publish_time = st.time_input("常用发布时间", value=time.fromisoformat(profile["publish_time"]))
            save_profile = st.form_submit_button("保存账号规范", type="primary", use_container_width=True)
        if save_profile:
            save_settings({
                "account_name": account_name.strip() or "我的小红书账号",
                "niche": niche.strip(),
                "audience": audience.strip(),
                "voice": voice.strip(),
                "rules": rules.strip(),
                "cta": cta.strip(),
                "cadence": str(cadence),
                "weekdays": json.dumps(weekdays or ["周二", "周四", "周六"], ensure_ascii=False),
                "publish_time": publish_time.strftime("%H:%M"),
            })
            st.success("账号规范已保存。")
            st.rerun()


if __name__ == "__main__":
    main()
