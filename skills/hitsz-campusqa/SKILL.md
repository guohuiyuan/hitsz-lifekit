---
name: hitsz-campusqa
description: 当用户询问哈尔滨工业大学（深圳）校园事务、学习生活、校内办事流程时，通过 CampusQA JSON 接口检索官方问答并总结回答。
version: 0.1.0
---

# HITSZ CampusQA Skill

## 目标

当用户询问哈尔滨工业大学（深圳）学习、工作、生活相关问题时，优先通过 CampusQA 接口检索校内问答内容，并基于官方问答结果给出简洁、可靠、可追溯的回答。

该 skill 完全自包含：所有代码、接口契约和回答规范都在本目录内。运行时直接请求 CampusQA JSON 接口，不依赖任何外部项目和预先生成的数据文件。

## 适用场景

- **校园生活**: 宿舍、食堂、校园卡、网络、快递、校区设施等。
- **学习培养**: 选课、考试、成绩、培养方案、教务流程等。
- **学生事务**: 奖助贷、证明材料、请假、户籍、医保、就业等。
- **校内办事**: 办理地点、办理材料、流程、时间、负责部门等。

不适用于与哈工深无关的问题、明显需要实时人工确认的问题、涉及个人隐私或账号权限的问题。

## 数据源

- **站点**: `http://campusqa.hitsz.edu.cn`
- **列表页来源**: `http://campusqa.hitsz.edu.cn/all`
- **内置接口客户端**: `scripts/campusqa.py`，仅依赖 Python 3 标准库。
- **默认请求头**:

```http
Accept: application/json, text/html;q=0.9, */*;q=0.8
User-Agent: hitsz-lifekit-campusqa/0.1
```

## 内置脚本

`scripts/campusqa.py` 提供七个子命令，便于 Codex 在 skill 内直接调用 CampusQA，无需额外安装第三方包：

```bash
python scripts/campusqa.py categories
python scripts/campusqa.py questions --page 1 --per-page 100 --sort created_at
python scripts/campusqa.py detail <question_id>
python scripts/campusqa.py search "你的问题" --top 5
python scripts/campusqa.py local-search "你的问题" --top 5
python scripts/campusqa.py ai-chat "你的问题"
python scripts/campusqa.py answer "你的问题" --top 1
```

`search` 子命令默认调用 CampusQA 官方 `/api/search`，不会全量分页拉取问题列表，省请求和 token。`local-search` 才会按 `created_at` 顺序分页拉取问题列表并本地匹配，通常只作为回退排查使用。`answer` 子命令会基于官方搜索结果输出 Markdown 回答，包含结论、官方回答、依据和提醒。`ai-chat` 可读取官网 `/api/ai-chat` 的 SSE 官方生成回答。

### 快速自测

CampusQA 公开问答接口默认匿名可读，**无需账号密码**。Codex 可以用以下命令快速验证 skill 是否可用：

```bash
python scripts/campusqa.py categories
python scripts/campusqa.py search "校园卡" --top 3 --no-details
python scripts/campusqa.py detail 2
python scripts/campusqa.py answer "校园卡" --top 1
```

预期：

- `categories` 返回 8 个分类。
- `search "校园卡"` 通过 `/api/search` 返回 `total_questions` 和 `total_answers`，首条通常是「校园卡丢失怎么办」。
- `detail 2` 返回包含 `question` 和 `answers` 字段的 JSON。
- `answer "校园卡"` 返回「校园卡丢失怎么办」等 CampusQA 依据，并输出结构化 Markdown 回答。

如果遇到中文乱码，是终端编码问题，不是 skill 问题；脚本内部已强制 UTF-8 输出，可在终端执行 `chcp 65001` 或设置 `PYTHONIOENCODING=utf-8`。

## 可调用接口

### 分类列表

```http
GET http://campusqa.hitsz.edu.cn/api/categories
```

返回结构：

```json
{
  "categories": []
}
```

### 问题列表

```http
GET http://campusqa.hitsz.edu.cn/api/questions?page={page}&per_page={per_page}&sort=created_at
```

参数建议：

- **page**: 从 `1` 开始。
- **per_page**: 默认使用 `100`，减少分页请求次数。
- **sort**: 使用 `created_at`，这是已验证可用的列表排序参数。

返回结构：

```json
{
  "page": 1,
  "per_page": 100,
  "questions": [],
  "total": 0
}
```

### 问题详情

```http
GET http://campusqa.hitsz.edu.cn/api/questions/{id}
```

返回结构：

```json
{
  "question": {},
  "answers": []
}
```

### 官方搜索

```http
GET http://campusqa.hitsz.edu.cn/api/search?keyword={keyword}&page={page}&per_page={per_page}&answer_page={answer_page}
```

返回结构：

```json
{
  "answers": [],
  "questions": [],
  "page": 1,
  "per_page": 20,
  "total_answers": 0,
  "total_questions": 0
}
```

### 官方 AI 回答

```http
GET http://campusqa.hitsz.edu.cn/api/ai-chat?keyword={keyword}
```

返回 `text/event-stream`，每个 `event: answer` 的 `data` 中包含一个 JSON 片段：

```json
{
  "text": "回答片段"
}
```

### 登录态检查

```http
GET http://campusqa.hitsz.edu.cn/api/auth/profile
```

通常优先使用匿名请求。只有接口返回 `401` 或 `403`，并且运行环境已提供可用登录态时，才尝试使用登录态重试。不要在 skill 中硬编码用户名、密码、Cookie 或 MFA 验证码。

## 调用流程

1. **判断是否触发**
   - 用户问题包含哈工深、HITSZ、校园事务、教务、宿舍、校内流程等意图时触发。
   - 如果用户问题过于宽泛，先根据 CampusQA 分类或常见关键词检索，再回答。

2. **直接回答**
   - 优先调用 `python scripts/campusqa.py answer "<用户问题>" --top 1` 生成带依据的 Markdown 回答。
   - 该命令默认使用官方 `/api/search`，通常只需要 1 次 HTTP 请求。
   - 如果需要机器可读结果，可调用 `python scripts/campusqa.py answer "<用户问题>" --format json`。
   - 如果想复用官网 AI 生成结果，可调用 `python scripts/campusqa.py ai-chat "<用户问题>"`。
   - `answer` 默认过滤低分弱相关结果，避免把只匹配到“校园”等宽泛词的条目当作依据。

3. **检索和匹配**
   - 对用户问题、问题标题、问题正文、分类名、标签等文本做中文关键词匹配。
   - 优先选择标题或正文高度匹配的候选。
   - 如需手动排查，可调用 `search` 获取候选，再调用 `detail` 获取完整回答。

4. **生成回答**
   - 以 CampusQA 详情接口的 `answers` 为主要依据。
   - 如果存在多个回答，优先总结共同结论，再保留关键差异。
   - 回答要说明信息来自 CampusQA，并尽量给出问题 `id` 或接口来源，方便追溯。

5. **无结果处理**
   - 如果没有匹配结果，明确说明“CampusQA 暂未检索到直接对应答案”。
   - 可以给出下一步建议，例如咨询学院教务、学生事务中心、辅导员或相关职能部门。
   - 不要编造校内政策、日期、地点、电话或办理材料。

## 回答规范

- **语言**: 默认使用中文。
- **语气**: 面向学生，简洁、直接、可执行。
- **结构**: 先给结论，再列流程、材料、地点或注意事项。
- **可信度**: 对 CampusQA 中没有明确说明的信息标注“不确定”。
- **时效性**: 涉及政策、时间、费用、地点时提示用户以学校最新通知或办事部门为准。
- **隐私安全**: 不要求用户提供学号、密码、身份证号、验证码等敏感信息。

## 推荐输出格式

```markdown
根据 CampusQA 检索结果：

**结论**
...

**办理方式 / 注意事项**
- ...
- ...

**来源**
- CampusQA 问题 ID: ...
- 接口: /api/questions/{id}
```

## 接口失败处理

- **网络失败**: 提示 CampusQA 接口暂不可用，可稍后重试。
- **401 / 403**: 提示当前环境缺少访问权限或登录态，不要向用户索要密码。
- **5xx**: 提示服务端异常，不要把错误堆栈暴露给用户。
- **字段缺失**: 按已有字段回答，并说明来源数据不完整。

## 依赖边界

该 skill 自包含，运行时只依赖：

- Python 3 标准库（用于 `scripts/campusqa.py`）。
- 可访问 `http://campusqa.hitsz.edu.cn` 的网络环境。

不依赖任何外部项目、二进制、缓存或本地数据文件。Codex 使用该 skill 时，应在运行时直接调用 CampusQA 接口完成问答，而不是依赖预先生成的数据。
