from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
DEFAULT_PROMPT = ('提取重要通知、待办与截止时间、变更与取消，分类展示，空分类省略。'
                  '合并重复事项，保留来源与日期；不明确的信息标为待确认，忽略闲聊。')


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return result.astimezone(UTC)


def split_umo(value: str, expected: str | None = None) -> tuple[str, str, str]:
    pieces = value.split(':', 2)
    if len(pieces) != 3 or not all(pieces):
        raise ValueError('需要完整 UMO：平台实例:消息类型:号码')
    platform, kind, number = pieces
    if kind not in ('GroupMessage', 'FriendMessage') or not number.isascii() or not number.isdigit():
        raise ValueError('仅支持 QQ 群或私聊的数字会话号码')
    if expected and kind != expected:
        raise ValueError(f'会话类型必须为 {expected}')
    return platform, kind, number


def positive_int(value, name, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name}必须为正整数')
    if maximum is not None and value > maximum:
        raise ValueError(f'{name}不能超过 {maximum}')
    return value


@dataclass
class TaskConfig:
    name: str = ''
    groups: list[str] = field(default_factory=list)
    provider_id: str = ''
    persona_id: str = ''
    prompt: str = DEFAULT_PROMPT
    recipient: str = ''
    frequency: str = 'daily'
    weekdays: list[int] = field(default_factory=lambda: [0])
    time: str = '08:00'
    timezone: str = 'Asia/Shanghai'
    lookback_hours: int = 24
    lead_minutes: int = 30
    candidate_days: int = 7
    selection_hours: int = 24
    personal_background: str = ''

    @classmethod
    def from_dict(cls, raw: dict):
        if not isinstance(raw, dict):
            raise ValueError('任务配置必须为对象')
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError('未知配置项：' + ', '.join(sorted(unknown)))
        raw = dict(raw)
        if 'lookback_hours' not in raw and raw.get('frequency') == 'weekly':
            raw['lookback_hours'] = 168
        config = cls(**raw)
        config.validate(complete=False)
        return config

    def validate(self, complete=True):
        for key in ('name', 'provider_id', 'persona_id', 'prompt', 'recipient', 'time', 'timezone', 'personal_background'):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f'{key}必须为文字')
        if self.frequency not in ('daily', 'weekly'):
            raise ValueError('频率必须为 daily 或 weekly')
        try:
            hour, minute = map(int, self.time.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError()
            ZoneInfo(self.timezone)
        except (ValueError, KeyError) as exc:
            raise ValueError('无效的执行时间或时区') from exc
        if not isinstance(self.weekdays, list) or not self.weekdays or any(
            type(x) is not int or x not in range(7) for x in self.weekdays
        ):
            raise ValueError('星期范围为 0（周一）至 6（周日）')
        for key in ('lookback_hours', 'candidate_days', 'selection_hours'):
            positive_int(getattr(self, key), key)
        if type(self.lead_minutes) is not int or self.lead_minutes < 0:
            raise ValueError('提前量必须为非负整数')
        if not isinstance(self.groups, list) or any(not isinstance(g, str) for g in self.groups):
            raise ValueError('源群必须为 UMO 列表')
        for group in self.groups:
            split_umo(group, 'GroupMessage')
        if len(set(self.groups)) != len(self.groups):
            raise ValueError('源群不能重复')
        if self.recipient:
            split_umo(self.recipient, 'FriendMessage')
        if complete and (not self.name.strip() or not self.groups or not self.provider_id
                         or not self.prompt.strip() or not self.recipient):
            raise ValueError('请填写任务名、源群、主模型、提示词与私聊收件人')

    def to_dict(self):
        return asdict(self)

    def window(self, planned: datetime):
        end = planned.astimezone(UTC)
        return end - timedelta(hours=self.lookback_hours), end

    def next_after(self, instant: datetime) -> datetime:
        zone = ZoneInfo(self.timezone)
        local = instant.astimezone(zone)
        hour, minute = map(int, self.time.split(':'))
        for days in range(9):
            day = (local + timedelta(days=days)).date()
            if self.frequency == 'weekly' and day.weekday() not in self.weekdays:
                continue
            wall = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            candidate = wall.astimezone(UTC)
            # Skip nonexistent local times; use first fold once for repeated times.
            if candidate.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
                continue
            if candidate > instant.astimezone(UTC):
                return candidate
        raise ValueError('无法计算下一计划时点')


def split_text(text: str, limit=1500) -> list[str]:
    positive_int(limit, '单条长度')
    if limit < 32:
        raise ValueError('单条长度至少 32')
    if not text.strip():
        return []
    # Fixed-point sizing makes the ordinal itself part of the length budget.
    reserve = 0
    while True:
        remaining, parts = text, []
        budget = limit - reserve
        while remaining:
            end = min(len(remaining), budget)
            if end < len(remaining):
                for separator in ('\n\n', '\n', '。', '；', '. '):
                    pos = remaining.rfind(separator, 0, end)
                    if pos >= budget // 3:
                        end = pos + len(separator)
                        break
            parts.append(remaining[:end])
            remaining = remaining[end:]
        required = len(f'[{len(parts)}/{len(parts)}] ') if len(parts) > 1 else 0
        if required <= reserve:
            return [f'[{i}/{len(parts)}] {part}' for i, part in enumerate(parts, 1)] if len(parts) > 1 else parts
        reserve = required
