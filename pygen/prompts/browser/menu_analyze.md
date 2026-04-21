分析以下网页 HTML 片段，识别其中的导航菜单/目录树结构。
返回 JSON 格式：
```json
{{"success": true, "menu_items": [{{"name": "一级", "children": [{{"name": "二级", "children": []}}]}}]}}
```
如果没有找到，返回：{{"success": false, "menu_items": []}}

HTML 片段：
{html_content}
