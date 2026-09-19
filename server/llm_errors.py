"""大模型调用的异常类，放在独立模块里。

**为什么不放在 claude_client.py 里：** `python3 server/claude_client.py check` 运行时，这个文件是
`__main__`；而 openrouter_client 里 `from claude_client import ClaudeError` 会再导入一份同名模块——
于是抛出的 ClaudeError 和 check() 里要捕获的 ClaudeError 是**两个不同的类**，except 抓不到，用户
看到的是一屏 traceback 而不是"key 无效"。放进独立模块，无论从哪里导入都是同一个类。
"""


class ClaudeError(RuntimeError):
    """调用失败。带 `status` 字段区分可重试与不可重试，便于上层决定怎么降级。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message
