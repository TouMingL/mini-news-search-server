# 重命名：last_turn_subject → last_turn_user_input 全量变更清单

将「上轮主题(subject)」统一改为「上轮用户输入(user_input)」语义的命名，涉及三处：

1. **模块级函数** `_get_last_turn_subject_from_history` → `_get_last_turn_user_input_from_history`
2. **query_rewriter 方法** `_format_last_turn_subject` → `_format_last_turn_user_input`
3. **模板占位与变量/参数** `last_turn_subject_line` → `last_turn_user_input_line`

---

## 1. `_get_last_turn_subject_from_history` → `_get_last_turn_user_input_from_history`

| 文件 | 行号 | 类型 | 说明 |
|------|------|------|------|
| pipeline.py | 314 | 定义 | 函数定义与 docstring 中的「上轮主题」可改为「上轮用户输入」 |
| pipeline.py | 1084 | 调用 | `last_turn_user_input = _get_last_turn_xxx_from_history(history)` |
| pipeline.py | 1332 | 调用 | 同上 |

---

## 2. `_format_last_turn_subject` → `_format_last_turn_user_input`

| 文件 | 行号 | 类型 | 说明 |
|------|------|------|------|
| query_rewriter.py | 175 | 定义 | 方法名及 docstring |
| query_rewriter.py | 232 | 调用 | `last_turn_xxx_line = self._format_last_turn_xxx(...)`（变量名在 3 中改） |
| pipeline.py | 1105 | 调用 | `self.query_rewriter._format_last_turn_xxx(...)` |
| pipeline.py | 1353 | 调用 | 同上 |

---

## 3. `last_turn_subject_line` → `last_turn_user_input_line`

| 文件 | 行号 | 类型 | 说明 |
|------|------|------|------|
| query_rewriter.py | 75 | 模板 | `REWRITE_PROMPT` 中的 `{last_turn_subject_line}` |
| query_rewriter.py | 232 | 变量 | 接收 `_format_last_turn_user_input` 返回值的变量名 |
| query_rewriter.py | 236 | 使用 | `if last_turn_xxx_line:` |
| query_rewriter.py | 237 | 使用 | `last_turn_xxx_line = last_turn_xxx_line + "\n"` |
| query_rewriter.py | 246 | 参数 | `.format(..., last_turn_xxx_line=last_turn_xxx_line, ...)` |
| pipeline.py | 1112 | 参数 | `.format(..., last_turn_xxx_line=last_turn_line, ...)` |
| pipeline.py | 1360 | 参数 | 同上 |

---

## 执行顺序建议

1. **query_rewriter.py**：先改模板占位(75)、方法名(175)、再改 rewrite 内变量与参数(232,236,237,246)。
2. **pipeline.py**：改函数名(314)、两处调用(1084,1332)、两处 query_rewriter 方法调用(1105,1353)、两处 format 参数(1112,1360)。

---

## 遗漏检查（修改后必做）

- 仓库内全文搜索：`last_turn_subject`、`_format_last_turn_subject`、`_get_last_turn_subject_from_history`。**代码内（app/）结果应为 0**；仅本文档中会保留上述旧名作为变更说明。
- 确认 `REWRITE_PROMPT.format(...)` 的键与模板内占位符一致，均为 `last_turn_user_input_line`。

---

## 变更结果（已完成）

- pipeline.py：函数定义 1 处、调用 2 处、query_rewriter 方法调用 2 处、format 参数 2 处，共 7 处已改。
- query_rewriter.py：模板 1 处、方法定义 1 处、变量与 format 参数 5 处，共 7 处已改。
- 复核：app/ 下无旧命名残留。
