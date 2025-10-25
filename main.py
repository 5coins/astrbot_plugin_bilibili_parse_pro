import os
import re
import requests
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import *
from astrbot.api.message_components import Video

# 正则表达式模式：匹配 BV 或 av 链接
BILI_VIDEO_PATTERN = r"(https?:\/\/)?www\.bilibili\.com\/video\/(BV\w+|av\d+)\/?"

@register("bilibili_parse", "功德无量", "一个哔哩哔哩视频解析插件", "1.0.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def get(self, url):
        """发送 GET 请求并返回 JSON 响应"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; BiliParser/1.0; +https://example.com)"
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"[bilibili_parse] HTTP 请求失败: {e}")
            return None

    @staticmethod
    def format_size(value):
        """
        将多种形式的 size 统一为可读字符串：
        - 数字(字节) => 转换为合适单位
        - 纯数字字符串 => 当作字节转换
        - 已带单位字符串(如 '2.33 MB'、'233KB') => 规范化显示
        - 其它/None => 返回 '未知大小' 或原样
        """
        if value is None:
            return "未知大小"

        # 数值 => 视为字节数
        if isinstance(value, (int, float)):
            size = float(value)
            units = ['B', 'KB', 'MB', 'GB', 'TB']
            idx = 0
            while size >= 1024 and idx < len(units) - 1:
                size /= 1024
                idx += 1
            return f"{size:.2f} {units[idx]}"

        s = str(value).strip()

        # 纯数字/小数字符串 => 视为字节数
        if re.fullmatch(r"\d+(\.\d+)?", s):
            try:
                size = float(s)
                units = ['B', 'KB', 'MB', 'GB', 'TB']
                idx = 0
                while size >= 1024 and idx < len(units) - 1:
                    size /= 1024
                    idx += 1
                return f"{size:.2f} {units[idx]}"
            except Exception:
                return s

        # 已带单位：KB/MB/GB/TB（大小写均可）
        m = re.match(r"^\s*([\d.]+)\s*([KMGT]?B)\s*$", s, re.I)
        if m:
            try:
                num = float(m.group(1))
                unit = m.group(2).upper()
                return f"{num:.2f} {unit}"
            except Exception:
                return s

        # 其它情况
        return s

    async def get_video_info(self, bvid: str, accept: int = 80):
        """
        获取 Bilibili 视频信息，返回结构化结果：
        - code: 0 表示成功
        - title, video_url, pic, video_size, quality, comment
        """
        try:
            api_url = f'http://114.134.188.188:3003/api?bvid={bvid}&accept={accept}'
            json_data = await self.get(api_url)

            if not json_data or json_data.get('code') != 0:
                msg = (json_data or {}).get('msg') or "解析失败，参数可能不正确"
                return {'code': -1, 'msg': msg}

            data_list = json_data.get('data') or []
            if not data_list:
                return {'code': -1, 'msg': "解析失败：未返回可用的播放数据"}

            first = data_list[0] or {}
            result = {
                'code': 0,
                'msg': '视频解析成功',
                'title': json_data.get('title'),
                'video_url': first.get('video_url'),
                'pic': json_data.get('imgurl'),
                'video_size': first.get('video_size'),
                'quality': first.get('accept_format'),
                'comment': first.get('comment'),
            }
            return result

        except requests.RequestException as e:
            return {'code': -1, 'msg': f"请求错误: {str(e)}"}
        except Exception as e:
            return {'code': -1, 'msg': f"解析失败: {str(e)}"}

    @filter.regex(BILI_VIDEO_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event):
        """处理 Bilibili 视频解析请求：直接发送视频而不是生成图片"""
        try:
            content = event.message_obj.message_str  # 原消息文本
            match = re.search(BILI_VIDEO_PATTERN, content)
            if not match:
                return

            bvid_or_avid = match.group(2)  # BVxxxx 或 av123456
            accept_quality = 80  # 默认 1080p；若服务端只认 80 就保持 80

            video_info = await self.get_video_info(bvid_or_avid, accept_quality)
            if not isinstance(video_info, dict) or video_info.get('code') != 0:
                msg = video_info.get('msg') if isinstance(video_info, dict) else "解析失败"
                yield event.plain_result(msg)
                return

            video_url = video_info.get('video_url')
            if not video_url:
                yield event.plain_result("解析失败：未获取到视频直链")
                return

            title = video_info.get('title') or "Bilibili 视频"
            size_human = self.format_size(video_info.get('video_size'))
            quality = video_info.get('quality') or "未知"
            # 可选：先回一条文本说明
            yield event.plain_result(f"🎬 标题: {title}\n👓 清晰度: {quality}\n📦 大小: {size_human}")

            # 首选：直接以视频形式发送
            try:
                # 某些适配器支持 video_result
                yield event.video_result(video_url)
            except Exception:
                # 兼容方案：用组件发送
                yield event.message_result([Video(video_url)])

        except Exception as e:
            yield event.plain_result(f"出错了：{e}")
