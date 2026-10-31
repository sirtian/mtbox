from __future__ import annotations


class MTBoxError(Exception):
    """Base class for expected, user-visible failures."""


class ConfigError(MTBoxError):
    pass


class NotFound(MTBoxError):
    pass


class Conflict(MTBoxError):
    pass


class MTeamError(MTBoxError):
    pass


class MTeamAuthError(MTeamError):
    pass


class MTeamRateLimitError(MTeamError):
    pass


class MTeamApiError(MTeamError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"M-Team API error {code or '?'}: {message}")
        self.code = code
        self.message = message


class MTeamNetworkError(MTeamError):
    pass


class BudgetExceeded(MTBoxError):
    def __init__(self, endpoint: str, budget: int, used: int) -> None:
        super().__init__(f"{endpoint} 24 小时调用预算已用尽（{used}/{budget}）")
        self.endpoint = endpoint
        self.budget = budget
        self.used = used


class CooldownActive(MTBoxError):
    def __init__(self, retry_after: int, reason: str = "") -> None:
        message = f"MT 调用冷却中，{retry_after} 秒后恢复"
        if reason:
            message += f"（{reason}）"
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


class RateLimitBusy(MTBoxError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"限速中，{retry_after} 秒后可再次搜索")
        self.retry_after = retry_after


class QBError(MTBoxError):
    pass


class DoubanError(MTBoxError):
    pass


class PathMapError(MTBoxError):
    pass
