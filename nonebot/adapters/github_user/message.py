"""消息类型：GitHub 侧统一使用 markdown 文本。"""

from collections.abc import Iterable
from typing import Type

from typing_extensions import override

from nonebot.adapters import Message as BaseMessage
from nonebot.adapters import MessageSegment as BaseMessageSegment


class MessageSegment(BaseMessageSegment["Message"]):
    """GitHub 消息段，目前只有 ``markdown`` 一种。"""

    @classmethod
    @override
    def get_message_class(cls) -> Type["Message"]:
        return Message

    @override
    def __str__(self) -> str:
        if self.type == "markdown":
            return self.data["text"]
        return f"<{self.type}:{','.join(f'{k}={v}' for k, v in self.data.items())}>"

    @override
    def is_text(self) -> bool:
        return self.type == "markdown"

    @staticmethod
    def markdown(text: str) -> "MessageSegment":
        return MessageSegment("markdown", {"text": text})


class Message(BaseMessage[MessageSegment]):
    """GitHub 消息，构造时按纯文本处理。"""

    @classmethod
    @override
    def get_segment_class(cls) -> Type[MessageSegment]:
        return MessageSegment

    @staticmethod
    @override
    def _construct(msg: str) -> Iterable[MessageSegment]:
        yield MessageSegment.markdown(msg)


__all__ = ["Message", "MessageSegment"]
