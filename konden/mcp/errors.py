class McpInputError(ValueError):
    """MCP 控制面能够确定的外部输入或前置条件错误。"""


class McpConfigurationError(McpInputError):
    pass


class McpServerNotFoundError(McpInputError):
    pass


class McpServerDisabledError(McpInputError):
    pass


class McpRecoveryPreconditionError(McpInputError):
    pass
