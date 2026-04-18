
## 【当前爬取模式：多板块爬取】

用户选择了「多板块爬取」模式，这意味着：
1. 需要遍历多个分类/板块来获取完整数据
2. 使用「增强分析结果」中提供的 `verified_category_mapping` 作为分类字典
3. 如果 verified_category_mapping 提供了 `menu_to_filters`：表示“同一个列表接口 + 不同 filters 参数”，应遍历这些 filters 抓取
4. 如果 verified_category_mapping 提供了 `menu_to_urls`：表示“不同板块对应不同列表页 URL（服务端渲染/跳转型菜单）”，应遍历这些 URL 逐个抓取
5. 如果没有提供 verified_category_mapping，按照捕获请求中的分类参数构建
