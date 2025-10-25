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

# 正则表达式模式
BILI_VIDEO_PATTERN = r"(https?:\/\/)?www\.bilibili\.com\/video\/(BV\w+|av\d+)\/?"

@register("bilibili_parse", "功德无量", "一个哔哩哔哩视频解析插件", "1.0.0")
class Bilibili(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def get(self, url):
        """发送 GET 请求并返回响应"""
        try:
            response = requests.get(url)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None

    @staticmethod
    def get_file_size(size_in_bytes):
        """将字节转换为可读的文件大小格式"""
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        index = 0
        size = float(size_in_bytes or 0)
        while size >= 1024 and index < len(units) - 1:
            size /= 1024
            index += 1
        return f"{size:.2f} {units[index]}"

    async def get_video_info(self, bvid: str, accept: int):
        """获取 Bilibili 视频信息，返回结构化结果"""
        # 注意：accept 这里由你的后端服务决定，这里仍传 80（1080p）
        try:
            json_data = await self.get(f'http://114.134.188.188:3003/api?bvid={bvid}&accept=80')
            if json_data is None or json_data.get('code') != 0:
                return {'code': -1, 'msg': "解析失败，参数可能不正确"}

            first = json_data['data'][0]
            result = {
                'code': 0,
                'msg': '视频解析成功',
                'title': json_data.get('title'),
                'video_url': first.get('video_url'),
                'pic': json_data.get('imgurl'),
                'video_size': first.get('video_size'),
                'quality': first.get('accept_format'),
                'comment': first.get('comment')
            }
            return result

        except requests.RequestException as e:
            return {'code': -1, 'msg': f"请求错误: {str(e)}"}
        except Exception as e:
            return {'code': -1, 'msg': f"解析失败: {str(e)}"}

    @filter.regex(BILI_VIDEO_PATTERN)
    @event_message_type(EventMessageType.ALL)
    async def bilibili_parse(self, event):
        """处理 Bilibili 视频解析请求：直接发送视频而不是图片"""
        try:
            url = event.message_obj.message_str
            match = re.search(BILI_VIDEO_PATTERN, url)
            if not match:
                return

            bvid = match.group(2)  # BV号或av号（你的后端接口需支持 BV）
            accept_quality = 80     # 默认清晰度（你的后端固定传 80）

            video_info = await self.get_video_info(bvid, accept_quality)

            if not isinstance(video_info, dict) or video_info.get('code') != 0:
                msg = video_info.get('msg') if isinstance(video_info, dict) else "解析失败"
                yield event.plain_result(msg)
                return

            video_url = video_info['video_url']
            title = video_info.get('title') or "Bilibili 视频"
            size_human = self.get_file_size(video_info.get('video_size'))
            quality = video_info.get('quality') or "未知"

            # 先回一条简单文本（可选）
            yield event.plain_result(f"🎬 标题: {title}\n👓 清晰度: {quality}\n📦 大小: {size_human}")

            # 核心：直接发送解析到的视频
            # 若框架支持，最简方式：
            yield event.video_result(video_url)

            # 如果你的运行环境不支持 video_result，可以改为用组件发送（备选）：
            # yield event.message_result([Video(video_url)])

        except Exception as e:
            yield event.plain_result(f"出错了：{e}")
