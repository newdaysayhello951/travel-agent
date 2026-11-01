"""仅内部识别额度/429，禁止输出原始异常正文或凭据。"""
def is_quota_error(exc):
    pending=[exc];seen=set()
    while pending:
        item=pending.pop()
        if item is None or id(item) in seen:continue
        seen.add(id(item))
        status=getattr(item,'status_code',None) or getattr(getattr(item,'response',None),'status_code',None)
        if status in (402,429):return True
        body=str(getattr(item,'body','')).lower()
        message=str(item).lower()
        if any(t in body or t in message for t in ('insufficient_quota','insufficient balance','quota exceeded','quota_exceeded','rate_limit_exceeded','too many requests','额度不足','余额不足')):return True
        pending.extend([getattr(item,'__cause__',None),getattr(item,'__context__',None)])
        pending.extend(getattr(item,'exceptions',()))
    return False
