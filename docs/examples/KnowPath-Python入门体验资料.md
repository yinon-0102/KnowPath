# 1. 变量、赋值与基本类型

Python 中，赋值把名称绑定到对象。`count = 3` 把名称 count 绑定到一个整数对象；`count = "三"` 会重新绑定为字符串，不会把原来的整数对象变为字符串。常见基本类型包括 int、float、bool、str 和 NoneType。

`type(3)` 返回 int 类型，`type("3")` 返回 str 类型。`int("3")` 返回整数 3；`int("abc")` 会抛出 ValueError，转换前应考虑输入是否有效。

```python
count = 3
price = 2.5
enabled = True
message = "Python"
value = None
print(count + 2)  # 5
```

练习验收：解释整数 3 与字符串 "3" 的区别，说明赋值和类型转换的差异。这里列出的是学习目标，不代表使用者已经掌握。

# 2. 条件分支与布尔判断

`if`、`elif`、`else` 根据条件决定执行哪个分支，缩进定义代码块。同一组分支按顺序判断，最多执行其中一个。`==` 比较相等，`=` 用于赋值；`is` 比较对象身份，不应代替普通数值或字符串的相等比较。

```python
score = 75
if score >= 90:
    level = "优秀"
elif score >= 60:
    level = "合格"
else:
    level = "待加强"
print(level)  # 合格
```

空字符串、空列表、0 和 None 在布尔判断中为假。`and` 和 `or` 会短路求值，并返回相应操作数，不保证返回 bool。

练习验收：说明 score 为 95、75、40 时分别进入哪个分支，解释 `==` 与 `is` 的区别。

# 3. 循环、range 与列表

`for` 遍历可迭代对象；`while` 在条件为真时重复执行。`range(3)` 产生 0、1、2，结束值不包含在内。`break` 结束当前最内层循环，`continue` 跳过本轮剩余代码。

```python
numbers = [2, 4, 6]
total = 0
for number in numbers:
    total += number
print(total)  # 12
print(list(range(1, 4)))  # [1, 2, 3]
```

列表是可变的有序集合，索引从 0 开始。`numbers.append(8)` 修改原列表；`numbers[1:3]` 返回包含索引 1 和 2 的新列表。访问不存在的索引会抛出 IndexError。

练习验收：写出 range 的开始、结束、步长含义，解释 append、索引和切片的差异。

# 4. 函数、参数与返回值

`def` 定义函数。参数接收调用时传入的对象，`return` 返回结果并结束当前函数。函数执行到末尾而没有显式 return 时返回 None。`print` 把内容输出到标准输出，不等同于把结果返回给调用者。

```python
def add(a, b):
    return a + b

result = add(2, 3)
print(result)  # 5
```

默认参数在函数定义时求值。避免使用可变列表作为默认值，可使用 None 并在函数内创建新列表。

```python
def collect(value, items=None):
    if items is None:
        items = []
    items.append(value)
    return items
```

练习验收：解释 print 和 return 的差异，以及可变默认参数为什么可能导致多次调用共享状态。

# 5. 字典与异常处理

字典以键关联值，键必须可哈希。`record["name"]` 读取键，键不存在时抛出 KeyError；`record.get("name", "未知")` 在键不存在时返回默认值。`"name" in record` 检查键是否存在。

```python
record = {"name": "小林", "score": 80}
print(record["name"])  # 小林
print(record.get("age", 0))  # 0

try:
    number = int("abc")
except ValueError:
    print("输入不是有效整数")
```

异常处理用于处理预期可能发生的失败。应捕获具体异常类型，而不是无条件忽略所有异常。finally 中的代码通常用于清理，在正常执行或异常传播时都执行。

练习验收：解释字典索引与 get 的差异，并指出示例中需要捕获 ValueError 的原因。
