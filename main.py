# main.py
# -*- coding: utf-8 -*-

import re
import aiohttp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType

# -----------------------------------------
# 🔹 匹配 B 站视频链接
# -----------------------------------------
BILI_LINK_PATTERN = r"(https?://)?(?:www\.)?(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|\?|$)|b23\.tv/[A-Za-z0-9_-]+|bili2233\.cn/[A-Za-z0-9_-]+)"
CARD_ESCAPED_LINK_PATTERN = (
    r"https:\\\\/\\\\/(?:www\\.)?(?:"
    r"bilibili\.com\\\\/video\\\\/(BV[0-9A-Za-z]{10}|av\\d+)(?:\\\\/|\\?|$)"
    r"|b23\.tv\\\\/[A-Za-z0-9_-]+"
    r"|bili2233\.cn\\\\/[A-Za-z0-9_-]+)"
)
BV_OR_AV_ID_PATTERN = r"(BV[0-9A-Za-z]{10}|av\d+)"

# -----------------------------------------
# 🔹 Todo List 模板
# -----------------------------------------
TMPL = '''
<div style="font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            font-size: 28px; padding: 24px; line-height: 1.4;">
  <h1 style="margin: 0 0 16px; font-size: 40px; color: #111;">Todo List</h1>
  <ul style="margin: 0; padding-left: 28px;">
  {% for item in items %}
    <li style="margin: 6px 0;">{{ item }}</li>
  {% endfor %}
  </ul>
</div>
'''

# -----------------------------------------
# 🔹 新闻卡片模板（可选图片）
# -----------------------------------------
NEWS_TMPL = '''
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0; padding:0; background:#0b0f14;">
  <div style="width:100%; box-sizing:border-box; background:#0d1117; color:#e6edf3;
              border-radius:16px; padding:28px; font-family:-apple-system,BlinkMacSystemFont,
              Segoe UI,Roboto,Helvetica,Arial,sans-serif; line-height:1.75;">
    {% if cover %}
    <div style="width:100%; height:360px; border-radius:12px; overflow:hidden;">
      <img src="{{ cover }}" alt="" style="width:100%; height:100%; display:block; object-fit:cover;">
    </div>
    {% endif %}

    <h2 style="margin:18px 0 8px; font-size:28px; font-weight:800; color:#e6edf3;">
      三星 Galaxy XR 支持轻松侧载应用且拥有开放引导程序
    </h2>

    <p style="margin:0 0 12px; font-size:18px; color:#c9d1d9;">
      三星 Galaxy XR 默认支持侧载 APK 文件，无需连接 PC 或启用开发者模式，同时还拥有开放的引导程序。
      这使得谷歌的 Android XR 平台成为三大独立 XR 平台中最开放的系统。相比之下，苹果的 visionOS 完全不允许侧载应用，
      而 Meta 的 Horizon OS 需要注册开发者账户并连接外部设备才能侧载。
    </p>

    <p style="margin:0 0 12px; font-size:18px; color:#c9d1d9;">
      UploadVR 确认，用户可以直接在 Galaxy XR 的内置 Chrome 浏览器中下载 Android APK 文件，
      只需在设置中给予浏览器安装“未知应用”的权限即可安装。此外，用户甚至可以解锁设备的引导程序，理论上可以安装自定义操作系统。
    </p>

    <div style="margin-top:12px;">
      <span style="display:inline-block; padding:8px 12px; font-size:16px; border-radius:10px;
                   background:#111827; color:#9ca3af;">
        UploadVR
      </span>
    </div>
  </div>
</body>
</html>
'''


# -----------------------------------------
# 🔹 主类
# -----------------------------------------
@register("bilibili_parse", "功德无量", "B站视频解析并直接发送视频（含b23短链兜底，支持卡片）", "1.3.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # ---------- HTTP 工具 ----------
    async def _http_get_json(self, url: str):
        """异步 GET JSON"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
        except Exception as e:
            logger.error(f"[bilibili_parse] HTTP GET 失败: {e}")
            return None

    async def _expand_url(self, url: str) -> str:
        """短链展开"""
        try:
            if not url.startswith("http"):
                url = "https://" + url
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=20) as resp:
                    return str(resp.url)
        except Exception as e:
            logger.error(f"[bilibili_parse] 短链展开失败: {e}")
            return url

    # ---------- 小工具 ----------
    @staticmethod
    def _fmt_size(raw) -> str:
        try:
            size = int(raw)
        except Exception:
            return "未知"
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size >= 1024 and i < len(units) - 1:
            size /= 1024
            i += 1
        return f"{size:.2f} {units[i]}"

    @staticmethod
    def _unescape_card_url(s: str) -> str:
        return s.replace("\\\\", "\\").replace("\\/", "/")

    def _extract_bili_url_from_event(self, event: AstrMessageEvent) -> str | None:
        candidates_text = []
        for attr in ("message_str",):
            v = getattr(event, attr, None)
            if v:
                candidates_text.append(v)
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is not None:
            v = getattr(msg_obj, "message_str", None)
            if v:
                candidates_text.append(v)
            candidates_text.append(str(msg_obj))
        for txt in candidates_text:
            m = re.search(BILI_LINK_PATTERN, txt)
            if m:
                url = m.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                return url
        for txt in candidates_text:
            m = re.search(CARD_ESCAPED_LINK_PATTERN, txt)
            if m:
                url = self._unescape_card_url(m.group(0))
                if url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http"):
                    url = "https://" + url
                return url
        for txt in candidates_text:
            m = re.search(BV_OR_AV_ID_PATTERN, txt)
            if m:
                return f"https://www.bilibili.com/video/{m.group(0)}"
        return None

    async def get_video_info(self, bvid: str, accept_qn: int = 80):
        api = f"http://114.134.188.188:3003/api?bvid={bvid}&accept={accept_qn}"
        data = await self._http_get_json(api)
        if not data:
            return {"code": -1, "msg": "API 请求失败"}
        if data.get("code") != 0 or not data.get("data"):
            return {"code": -1, "msg": data.get("msg", "解析失败")}
        item = data["data"][0]
        return {
            "code": 0,
            "title": data.get("title", "未知标题"),
            "video_url": item.get("video_url", ""),
            "pic": data.get("imgurl", ""),
            "video_size": item.get("video_size", 0),
            "quality": item.get("accept_format", "未知清晰度"),
            "comment": item.get("comment", ""),
        }

    # ---------- B站解析 ----------
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event: AstrMessageEvent):
        try:
            matched_url = self._extract_bili_url_from_event(event)
            if not matched_url:
                return
            text = matched_url
            if any(d in matched_url for d in ("b23.tv", "bili2233.cn")):
                text = await self._expand_url(matched_url)
            m_bvid = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", text)
            if not m_bvid:
                m_id = re.search(BV_OR_AV_ID_PATTERN, text)
                if m_id:
                    bvid = m_id.group(0)
                else:
                    return
            else:
                bvid = m_bvid.group(1)
            info = await self.get_video_info(bvid, 80)
            if not info or info.get("code") != 0:
                msg = info.get("msg", "解析失败") if info else "解析失败"
                yield event.plain_result(f"解析B站视频失败：{msg}")
                return
            title = info["title"]
            video_url = info["video_url"]
            cover = info["pic"]
            caption = f"🎬 标题: {title}\n"
            try:
                from astrbot.api.message_components import Video
                video_comp = Video.fromURL(url=video_url)
                if hasattr(event, "chain_result"):
                    yield event.chain_result([video_comp])
                else:
                    cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                    yield event.plain_result(cq)
            except Exception as send_err:
                logger.warning(f"[bilibili_parse] 组件方式发送失败: {send_err}")
                cq = f"[CQ:video,file={video_url},cover={cover},title={title}]"
                yield event.plain_result(cq)
            yield event.plain_result(caption)
        except Exception as e:
            logger.error(f"[bilibili_parse] 未知错误: {e}")

    # ---------- todo 命令 ----------
    @filter.command("todo")
    async def todo_card(self, event: AstrMessageEvent):
        raw = getattr(event, "message_str", "") or getattr(getattr(event, "message_obj", None), "message_str", "")
        m = re.search(r"^\s*todo\b(.*)$", raw, re.I | re.S)
        rest = m.group(1).strip() if m else ""
        if rest:
            parts = re.split(r"[,\u3001\uFF0C|\s]+", rest)
            items = [p for p in parts if p]
        else:
            items = ["吃饭", "睡觉", "玩原神"]
        url = await self.html_render(TMPL, {"items": items})
        yield event.image_result(url)

    # ---------- newsxr 命令 ----------
    @filter.command("newsxr")
    async def news_card(self, event: AstrMessageEvent):
        """
        生成 Galaxy XR 新闻卡片，图片可选。
        用法：
        - newsxr
        - newsxr https://example.com/pic.jpg
        """
        raw = getattr(event, "message_str", "") or ""
        m = re.search(r"^\s*newsxr\b(.*)$", raw, re.I)
        img = m.group(1).strip() if m else ""
        cover = img if img.startswith("http") else None
        url = await self.html_render(NEWS_TMPL, {"cover": cover})
        yield event.image_result(url)
