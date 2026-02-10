# app/utils/wechat.py
# 微信API封装

import requests
import time
from flask import current_app
from app.utils.exceptions import WeChatAPIError, WeChatConfigError, WeChatNetworkError

# access_token 内存缓存，键为 (appid,) 便于多应用；值 { 'access_token', 'expires_at' }
_access_token_cache = {}


def _get_cached_access_token():
    appid = current_app.config.get('WECHAT_APPID', '')
    if not appid:
        return None
    key = appid
    if key in _access_token_cache:
        entry = _access_token_cache[key]
        if entry and entry.get('expires_at', 0) > time.time():
            return entry.get('access_token')
    return None


def _set_cached_access_token(token, expires_in_sec=7200):
    appid = current_app.config.get('WECHAT_APPID', '')
    if not appid:
        return
    # 提前 5 分钟视为过期，避免边界竞态
    _access_token_cache[appid] = {
        'access_token': token,
        'expires_at': time.time() + max(0, expires_in_sec - 300),
    }


def get_access_token():
    """
    获取小程序 access_token（用于订阅消息等服务端接口）。
    使用 client_credential 方式，带内存缓存，过期前 5 分钟刷新。
    """
    cached = _get_cached_access_token()
    if cached:
        return cached
    appid = current_app.config.get('WECHAT_APPID', '')
    secret = current_app.config.get('WECHAT_SECRET', '')
    if not appid or not secret:
        raise WeChatConfigError('微信小程序 AppID 或 Secret 未配置')
    url = 'https://api.weixin.qq.com/cgi-bin/token'
    params = {'grant_type': 'client_credential', 'appid': appid, 'secret': secret}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise WeChatNetworkError(f'获取 access_token 请求失败: {e}')
    errcode = data.get('errcode')
    if errcode:
        raise WeChatAPIError(data.get('errmsg', '未知错误'), errcode=errcode, errmsg=data.get('errmsg'))
    token = data.get('access_token')
    expires_in = int(data.get('expires_in', 7200))
    if not token:
        raise WeChatAPIError('微信返回数据异常，未包含 access_token')
    _set_cached_access_token(token, expires_in)
    return token


def send_subscribe_message(openid, template_id, data, page=None):
    """
    发送一次性订阅消息。
    :param openid: 用户 openid
    :param template_id: 订阅消息模板 ID（公众平台-订阅消息中配置）
    :param data: 模板变量，格式 {"key1": {"value": "v1"}, "key2": {"value": "v2"}}，键与模板占位符一致
    :param page: 可选，点击消息跳转的小程序页面路径
    :return: dict 微信返回（含 errcode、msgid 等）
    """
    token = get_access_token()
    url = f'https://api.weixin.qq.com/cgi-bin/message/subscribe/send?access_token={token}'
    body = {
        'touser': openid,
        'template_id': template_id,
        'data': data,
    }
    if page:
        body['page'] = page
    try:
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        out = resp.json()
    except requests.RequestException as e:
        raise WeChatNetworkError(f'发送订阅消息请求失败: {e}')
    if out.get('errcode') != 0:
        raise WeChatAPIError(
            out.get('errmsg', '发送失败'),
            errcode=out.get('errcode'),
            errmsg=out.get('errmsg'),
        )
    return out


def code2session(code, max_retries=2, retry_delay=1):
    """
    调用微信 code2Session 接口
    
    Args:
        code: 微信登录凭证code
        max_retries: 最大重试次数（仅对网络错误）
        retry_delay: 重试延迟（秒）
        
    Returns:
        dict: {
            'openid': str,
            'session_key': str,
            'unionid': str (可选)
        }
        
    Raises:
        WeChatConfigError: 配置错误
        WeChatAPIError: 微信API业务错误
        WeChatNetworkError: 网络错误
    """
    appid = current_app.config.get('WECHAT_APPID', '')
    secret = current_app.config.get('WECHAT_SECRET', '')
    
    if not appid or not secret:
        raise WeChatConfigError('微信小程序AppID或Secret未配置，请检查.env文件')
    
    url = 'https://api.weixin.qq.com/sns/jscode2session'
    params = {
        'appid': appid,
        'secret': secret,
        'js_code': code,
        'grant_type': 'authorization_code'
    }
    
    last_exception = None
    
    # 重试机制（仅对网络错误）
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # 检查微信接口返回的错误
            if 'errcode' in data:
                errcode = data.get('errcode')
                errmsg = data.get('errmsg', '未知错误')
                
                # 根据错误码提供更友好的错误信息
                error_messages = {
                    40029: 'code无效或已过期，请重新获取',
                    45011: 'API调用太频繁，请稍后再试',
                    40163: 'code已被使用，请重新获取',
                }
                
                user_message = error_messages.get(errcode, errmsg)
                current_app.logger.warning(
                    f'微信API错误: {errmsg} (errcode: {errcode}, code: {code[:10]}...)'
                )
                
                raise WeChatAPIError(user_message, errcode=errcode, errmsg=errmsg)
            
            # 验证返回数据
            openid = data.get('openid')
            if not openid:
                current_app.logger.error(f'微信API返回数据异常: {data}')
                raise WeChatAPIError('微信接口返回数据异常，未获取到openid')
            
            # 记录成功日志
            current_app.logger.info(
                f'微信API调用成功: openid={openid[:10]}..., has_unionid={bool(data.get("unionid"))}'
            )
            
            # 返回成功数据
            return {
                'openid': openid,
                'session_key': data.get('session_key'),
                'unionid': data.get('unionid')  # 如果小程序绑定到开放平台，会有unionid
            }
            
        except requests.RequestException as e:
            last_exception = e
            if attempt < max_retries:
                current_app.logger.warning(
                    f'微信API网络错误，{retry_delay}秒后重试 ({attempt + 1}/{max_retries}): {str(e)}'
                )
                time.sleep(retry_delay)
            else:
                current_app.logger.error(f'微信API网络错误，已达到最大重试次数: {str(e)}')
                raise WeChatNetworkError(
                    f'请求微信接口失败，请检查网络连接: {str(e)}',
                    original_error=e
                )
        except WeChatAPIError:
            # 业务错误不重试
            raise
        except Exception as e:
            current_app.logger.error(f'微信API调用未知错误: {str(e)}')
            raise WeChatAPIError(f'微信接口调用失败: {str(e)}')
    
    # 如果所有重试都失败
    if last_exception:
        raise WeChatNetworkError(
            f'请求微信接口失败，已重试{max_retries}次: {str(last_exception)}',
            original_error=last_exception
        )
