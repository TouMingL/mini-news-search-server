# app/utils/exceptions.py
# 自定义异常类

class WeChatAPIError(Exception):
    """微信API调用错误"""
    def __init__(self, message, errcode=None, errmsg=None):
        self.message = message
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(self.message)
    
    def __str__(self):
        if self.errcode:
            return f'{self.message} (errcode: {self.errcode}, errmsg: {self.errmsg})'
        return self.message


class WeChatConfigError(Exception):
    """微信配置错误"""
    pass


class WeChatNetworkError(Exception):
    """微信API网络错误"""
    def __init__(self, message, original_error=None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


class TokenGenerationError(Exception):
    """Token生成错误"""
    pass


class DatabaseOperationError(Exception):
    """数据库操作错误"""
    pass
