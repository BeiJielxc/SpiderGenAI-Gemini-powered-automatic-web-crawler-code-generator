
## 【当前爬取模式：单一板块爬取】

⚠️ **重要**：用户选择了「单一板块爬取」模式，这意味着：
1. **只抓取当前页面默认显示的数据**，不要遍历多个分类/板块
2. **禁止**定义 CATEGORIES 字典来遍历多个分类
3. 如果 API 需要分类参数，使用页面当前的默认值或从捕获的请求中提取的值
4. 生成的脚本应该简单直接，只针对单一数据源

```python
# ❌ 错误：单一板块模式下不应该遍历多个分类
CATEGORIES = {"深市": "szse", "沪市": "sse", "北交所": "bj"}
for cat_name, col_val in CATEGORIES.items():
    fetch_data(cat_name, col_val)

# ✅ 正确：直接使用默认分类或当前页面的参数
def fetch_data():
    # 使用从捕获请求中提取的默认参数
    params = {"column": "szse", "pageNum": 1, "pageSize": 30}
    response = requests.post(API_URL, data=params)
```
