# -*- coding: utf-8 -*-
"""
飞书 发送提醒服务

职责：
1. 通过 webhook 发送飞书消息
"""
import logging
import json
from typing import Dict, Any
import requests
import time

from src.config import Config
from src.feishu_cards import build_feishu_interactive_card
from src.formatters import format_feishu_markdown, chunk_content_by_max_bytes


logger = logging.getLogger(__name__)


def _create_feishu_reply_client(app_id: str, app_secret: str):
    """创建飞书开放平台回复客户端；SDK 不可用时返回 None。"""
    try:
        from bot.platforms.feishu_stream import (
            FEISHU_SDK_AVAILABLE,
            FeishuReplyClient,
        )
    except Exception as e:
        logger.error(f"导入飞书开放平台客户端失败: {e}")
        return None

    if not FEISHU_SDK_AVAILABLE:
        logger.warning("飞书 SDK 不可用，无法通过应用主动推送")
        return None

    return FeishuReplyClient(app_id, app_secret)


class FeishuSender:
    
    def __init__(self, config: Config):
        """
        初始化飞书配置

        Args:
            config: 配置对象
        """
        self._feishu_url = getattr(config, 'feishu_webhook_url', None)
        self._feishu_app_id = getattr(config, 'feishu_app_id', None)
        self._feishu_app_secret = getattr(config, 'feishu_app_secret', None)
        self._feishu_chat_id = getattr(config, 'feishu_chat_id', None)
        self._feishu_max_bytes = getattr(config, 'feishu_max_bytes', 20000)
        self._webhook_verify_ssl = getattr(config, 'webhook_verify_ssl', True)
    
    def is_feishu_configured(self) -> bool:
        """检查是否配置了飞书 Webhook 或开放平台应用主动推送。"""
        return bool(
            self._feishu_url
            or (
                self._feishu_app_id
                and self._feishu_app_secret
                and self._feishu_chat_id
            )
        )

          
    def send_to_feishu(self, content: str) -> bool:
        """
        推送消息到飞书机器人
        
        飞书自定义机器人 Webhook 消息格式：
        {
            "msg_type": "text",
            "content": {
                "text": "文本内容"
            }
        }
        
        说明：飞书文本消息不会渲染 Markdown，需使用交互卡片（lark_md）格式
        
        注意：飞书文本消息限制约 20KB，超长内容会自动分批发送
        可通过环境变量 FEISHU_MAX_BYTES 调整限制值
        
        Args:
            content: 消息内容（Markdown 会转为纯文本）
            
        Returns:
            是否发送成功
        """
        if not self.is_feishu_configured():
            logger.warning("飞书通知未配置，跳过推送")
            return False

        if not self._feishu_url:
            return self._send_feishu_app_message(content)
        
        # 飞书 lark_md 支持有限，先做格式转换
        formatted_content = format_feishu_markdown(content)

        max_bytes = self._feishu_max_bytes  # 从配置读取，默认 20000 字节
        
        # 按最终交互卡片 payload 判断大小，避免卡片包装后超过飞书限制。
        payload_bytes = self._interactive_payload_bytes(
            formatted_content,
            collapse_long_content=True,
        )
        if payload_bytes > max_bytes:
            logger.info(f"飞书消息内容超长({payload_bytes}字节/{len(content)}字符)，将分批发送")
            return self._send_feishu_chunked(formatted_content, max_bytes)
        
        try:
            return self._send_feishu_message(formatted_content)
        except Exception as e:
            logger.error(f"发送飞书消息失败: {e}")
            return False
   
    def _send_feishu_chunked(self, content: str, max_bytes: int) -> bool:
        """
        分批发送长消息到飞书
        
        按股票分析块（以 --- 或 ### 分隔）智能分割，确保每批不超过限制
        
        Args:
            content: 完整消息内容
            max_bytes: 单条消息最大字节数
            
        Returns:
            是否全部发送成功
        """
        chunk_max_bytes = self._find_chunk_content_budget(content, max_bytes)
        chunks = chunk_content_by_max_bytes(content, chunk_max_bytes, add_page_marker=True)
        
        # 分批发送
        total_chunks = len(chunks)
        success_count = 0
        
        logger.info(f"飞书分批发送：共 {total_chunks} 批")
        
        for i, chunk in enumerate(chunks):
            try:
                if self._send_feishu_message(chunk, collapse_long_content=False):
                    success_count += 1
                    logger.info(f"飞书第 {i+1}/{total_chunks} 批发送成功")
                else:
                    logger.error(f"飞书第 {i+1}/{total_chunks} 批发送失败")
            except Exception as e:
                logger.error(f"飞书第 {i+1}/{total_chunks} 批发送异常: {e}")
            
            # 批次间隔，避免触发频率限制
            if i < total_chunks - 1:
                time.sleep(1)
        
        return success_count == total_chunks
    
    def _send_feishu_message(self, content: str, *, collapse_long_content: bool = True) -> bool:
        """发送单条飞书消息（优先使用 Markdown 卡片）"""
        def _post_payload(payload: Dict[str, Any]) -> bool:
            logger.debug(f"飞书请求 URL: {self._feishu_url}")
            logger.debug(f"飞书请求 payload 长度: {len(content)} 字符")

            response = requests.post(
                self._feishu_url,
                json=payload,
                timeout=30,
                verify=self._webhook_verify_ssl
            )

            logger.debug(f"飞书响应状态码: {response.status_code}")
            logger.debug(f"飞书响应内容: {response.text}")

            if response.status_code == 200:
                result = response.json()
                code = result.get('code') if 'code' in result else result.get('StatusCode')
                if code == 0:
                    logger.info("飞书消息发送成功")
                    return True
                else:
                    error_msg = result.get('msg') or result.get('StatusMessage', '未知错误')
                    error_code = result.get('code') or result.get('StatusCode', 'N/A')
                    logger.error(f"飞书返回错误 [code={error_code}]: {error_msg}")
                    logger.error(f"完整响应: {result}")
                    return False
            else:
                logger.error(f"飞书请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        # 1) 优先使用交互卡片（支持 Markdown 渲染，长内容默认折叠）
        card_payload = self._build_interactive_payload(
            content,
            collapse_long_content=collapse_long_content,
        )

        if _post_payload(card_payload):
            return True

        # 2) 回退为普通文本消息
        text_payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }

        return _post_payload(text_payload)

    def _build_interactive_payload(
        self,
        content: str,
        *,
        collapse_long_content: bool = True,
    ) -> Dict[str, Any]:
        return {
            "msg_type": "interactive",
            "card": build_feishu_interactive_card(
                content,
                collapse_long_content=collapse_long_content,
            ),
        }

    def _interactive_payload_bytes(
        self,
        content: str,
        *,
        collapse_long_content: bool,
    ) -> int:
        payload = self._build_interactive_payload(
            content,
            collapse_long_content=collapse_long_content,
        )
        # requests.post(json=payload) uses the stdlib default JSON encoder,
        # which escapes non-ASCII characters. Estimate with the same behavior.
        return len(json.dumps(payload).encode("utf-8"))

    def _find_chunk_content_budget(self, content: str, max_bytes: int) -> int:
        """Find a chunk size whose final card payload fits the configured limit."""
        budget = max_bytes
        while budget > 200:
            chunks = chunk_content_by_max_bytes(content, budget, add_page_marker=True)
            if all(
                self._interactive_payload_bytes(chunk, collapse_long_content=False) <= max_bytes
                for chunk in chunks
            ):
                return budget
            budget = int(budget * 0.8)
        return max(40, budget)

    def _send_feishu_app_message(self, content: str) -> bool:
        """通过飞书开放平台应用向配置的群聊主动发送消息。"""
        app_id = self._feishu_app_id
        app_secret = self._feishu_app_secret
        chat_id = self._feishu_chat_id

        if not app_id or not app_secret or not chat_id:
            logger.warning("飞书 APP_ID、APP_SECRET 或 CHAT_ID 未配置")
            return False

        try:
            reply_client = _create_feishu_reply_client(app_id, app_secret)
            if reply_client is None:
                return False
            return reply_client.send_to_chat(chat_id, content)
        except Exception as e:
            logger.error(f"飞书应用主动推送异常: {e}")
            return False
