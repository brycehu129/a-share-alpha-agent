"""Provider access decisions, separate from transient network failures."""

# Run 35063869824-1 returned 18 explicit blacklist denials.
# Resume only after provider clearance; do not rotate runners or addresses.
BAOSTOCK_BLOCK_REASON = (
    'BaoStock已暂停：源返回“黑名单用户，请与管理员联系”；'
    '需服务方确认恢复访问后才能启用。已下载数据继续保留。'
)
