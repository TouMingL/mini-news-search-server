# app/utils/response.py
# 统一API响应格式

from flask import jsonify


def success_response(data=None, message=None):
    """成功响应"""
    response = {
        'success': True,
        'data': data
    }
    if message:
        response['message'] = message
    return jsonify(response)


def error_response(message, status_code=400, data=None):
    """错误响应"""
    response = {
        'success': False,
        'message': message
    }
    if data:
        response['data'] = data
    return jsonify(response), status_code
